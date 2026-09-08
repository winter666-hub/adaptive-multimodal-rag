"""Durable append-only checkpoints for long-running E2E benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ExecutionKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class BenchmarkFingerprint:
    value: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResumePlan:
    pending_rows: list[dict[str, Any]]
    already_completed: int
    skipped_successes: int
    skipped_errors: int
    retrying_errors: int


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_benchmark_fingerprint(
    *,
    dataset_path: str | Path,
    strategy: str,
    embedding_model: str,
    reranker_model: str,
    router_llm_model: str,
    final_llm_model: str,
    vision_model: str,
    chunk_size: int,
    chunk_overlap: int,
    top_k: int,
    top_n: int,
    vision_dpi: float,
    pdf_root: str | Path | None = None,
    pdf_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    require_warm_cache: bool = False,
    router_prompt_sha256: str | None = None,
) -> BenchmarkFingerprint:
    """Build a deterministic fingerprint without accepting credentials or secrets."""
    dataset = Path(dataset_path).resolve()
    if (pdf_root is None) == (pdf_path is None):
        raise ValueError("fingerprint requires exactly one of pdf_root or pdf_path")
    payload: dict[str, Any] = {
        "dataset": {"path": str(dataset), "sha256": _file_sha256(dataset)},
        "strategy": strategy,
        "models": {
            "embedding": embedding_model,
            "reranker": reranker_model,
            "router_llm": router_llm_model,
            "final_llm": final_llm_model,
            "vision": vision_model,
        },
        "rag": {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "top_k": top_k,
            "top_n": top_n,
            "vision_dpi": vision_dpi,
        },
        "cache": {
            "directory": str(Path(cache_dir).resolve()) if cache_dir is not None else None,
            "require_warm": require_warm_cache,
        },
        "router_prompt_sha256": router_prompt_sha256,
    }
    if pdf_root is not None:
        payload["benchmark_mode"] = "per_row_pdf"
        payload["pdf_root"] = str(Path(pdf_root).resolve())
    else:
        pdf = Path(pdf_path).resolve()  # type: ignore[arg-type]
        payload["benchmark_mode"] = "single_pdf"
        payload["pdf"] = {"path": str(pdf), "sha256": _file_sha256(pdf)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return BenchmarkFingerprint(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), payload)


def append_checkpoint(
    path: str | Path,
    result: Mapping[str, Any],
    fingerprint: BenchmarkFingerprint,
) -> None:
    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **result,
        "benchmark_fingerprint": fingerprint.value,
        "fingerprint_config": fingerprint.payload,
    }
    with checkpoint_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _validate_record(record: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError(f"invalid checkpoint record on line {line_number}: expected an object")
    for field in ("query_id", "strategy", "benchmark_fingerprint"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"invalid checkpoint {field} on line {line_number}")
    if not isinstance(record.get("error", ""), str):
        raise TypeError(f"invalid checkpoint error on line {line_number}")
    return record


def load_checkpoint(
    path: str | Path, *, repair_truncated_final_line: bool = False
) -> list[dict[str, Any]]:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return []
    lines = checkpoint_path.read_bytes().splitlines()
    nonempty_indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty_indexes:
        return []
    last_nonempty = nonempty_indexes[-1]
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        line_number = index + 1
        try:
            raw_record = json.loads(line.decode("utf-8"))
            records.append(_validate_record(raw_record, line_number))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            if index == last_nonempty:
                warnings.warn(
                    f"ignoring malformed final checkpoint line {line_number}: {exc}",
                    stacklevel=2,
                )
                if repair_truncated_final_line:
                    retained = b"\n".join(lines[:index])
                    if retained:
                        retained += b"\n"
                    with checkpoint_path.open("wb") as output:
                        output.write(retained)
                        output.flush()
                        os.fsync(output.fileno())
                continue
            raise ValueError(f"malformed checkpoint line {line_number}: {exc}") from exc
    return records


def execution_key(record: Mapping[str, Any]) -> ExecutionKey:
    return str(record["query_id"]), str(record["strategy"])


def latest_checkpoint_records(
    records: Sequence[Mapping[str, Any]],
) -> dict[ExecutionKey, dict[str, Any]]:
    latest: dict[ExecutionKey, dict[str, Any]] = {}
    for record in records:
        latest[execution_key(record)] = dict(record)
    return latest


def _flatten(payload: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            flattened.update(_flatten(value, name))
        else:
            flattened[name] = value
    return flattened


def validate_checkpoint_fingerprint(
    records: Sequence[Mapping[str, Any]],
    fingerprint: BenchmarkFingerprint,
) -> None:
    mismatched = [
        record
        for record in records
        if record.get("benchmark_fingerprint") != fingerprint.value
    ]
    if not mismatched:
        return
    old_payload = mismatched[-1].get("fingerprint_config")
    details = ""
    if isinstance(old_payload, Mapping):
        old_flat = _flatten(old_payload)
        new_flat = _flatten(fingerprint.payload)
        changed = sorted(
            key
            for key in old_flat.keys() | new_flat.keys()
            if old_flat.get(key) != new_flat.get(key)
        )
        if changed:
            details = f"; changed config: {', '.join(changed)}"
    raise ValueError(f"checkpoint fingerprint does not match current benchmark{details}")


def plan_resume(
    rows: Sequence[Mapping[str, Any]],
    *,
    strategy: str,
    latest: Mapping[ExecutionKey, Mapping[str, Any]],
    retry_errors: bool,
) -> ResumePlan:
    pending: list[dict[str, Any]] = []
    skipped_successes = skipped_errors = retrying_errors = 0
    for row in rows:
        previous = latest.get((str(row["id"]), strategy))
        if previous is None:
            pending.append(dict(row))
        elif previous.get("error"):
            if retry_errors:
                pending.append(dict(row))
                retrying_errors += 1
            else:
                skipped_errors += 1
        else:
            skipped_successes += 1
    return ResumePlan(
        pending_rows=pending,
        already_completed=skipped_successes + skipped_errors,
        skipped_successes=skipped_successes,
        skipped_errors=skipped_errors,
        retrying_errors=retrying_errors,
    )


def materialize_checkpoint_csv(
    checkpoint_path: str | Path,
    output_path: str | Path,
    *,
    fingerprint: BenchmarkFingerprint | None = None,
) -> list[dict[str, Any]]:
    records = load_checkpoint(checkpoint_path)
    if fingerprint is not None:
        validate_checkpoint_fingerprint(records, fingerprint)
    latest = list(latest_checkpoint_records(records).values())
    from furiosa_rag.e2e_benchmark import export_e2e_csv

    export_e2e_csv(latest, output_path)
    return latest
