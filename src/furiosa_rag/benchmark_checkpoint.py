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

FINGERPRINT_SCHEMA_VERSION = 2
SEMANTIC_CHECKPOINT_FIELDS = (
    "id",
    "question",
    "gold_answer",
    "domain",
    "question_type",
    "answer_type",
    "expected_route",
    "document_id",
    "source_pdf",
    "expected_pages",
)


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


def _canonical_dataset_sha256(path: str | Path) -> tuple[str, int]:
    """Hash ordered JSONL values independently of their byte serialization."""
    digest = hashlib.sha256()
    seen_ids: set[str] = set()
    row_count = 0
    with Path(path).open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid dataset JSON on line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise TypeError(f"invalid dataset row on line {line_number}: expected an object")
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                raise ValueError(f"invalid dataset id on line {line_number}")
            if row_id in seen_ids:
                raise ValueError(f"duplicate dataset id: {row_id}")
            seen_ids.add(row_id)
            canonical_row = json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            digest.update(canonical_row.encode("utf-8"))
            digest.update(b"\n")
            row_count += 1
    return digest.hexdigest(), row_count


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
    dataset_sha256, dataset_row_count = _canonical_dataset_sha256(dataset)
    payload: dict[str, Any] = {
        "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
        "dataset": {
            "path": str(dataset),
            "sha256": _file_sha256(dataset),
            "canonical_sha256": dataset_sha256,
            "row_count": dataset_row_count,
        },
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
    identity = _portable_identity(payload)
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
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


def _portable_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return experiment semantics, excluding machine-local diagnostic metadata."""
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"dataset", "cache", "pdf_root", "pdf"}
    }
    dataset = payload.get("dataset")
    if isinstance(dataset, Mapping):
        identity["dataset"] = {
            "canonical_sha256": dataset.get("canonical_sha256"),
            "row_count": dataset.get("row_count"),
        }
    cache = payload.get("cache")
    if isinstance(cache, Mapping):
        identity["cache"] = {"require_warm": cache.get("require_warm")}
    pdf = payload.get("pdf")
    if isinstance(pdf, Mapping):
        identity["pdf"] = {"sha256": pdf.get("sha256")}
    return identity


def _legacy_config_changes(
    old_payload: Mapping[str, Any], new_payload: Mapping[str, Any]
) -> list[str]:
    ignored = {
        "dataset.path",
        "dataset.sha256",
        "cache.directory",
        "pdf_root",
        "pdf.path",
    }
    old_flat = _flatten(old_payload)
    new_flat = _flatten(new_payload)
    # Fields introduced by the portable schema have no legacy counterpart.
    new_flat = {
        key: value
        for key, value in new_flat.items()
        if key not in {"fingerprint_schema_version", "dataset.canonical_sha256", "dataset.row_count"}
    }
    return sorted(
        key
        for key in old_flat.keys() | new_flat.keys()
        if key not in ignored and old_flat.get(key) != new_flat.get(key)
    )


def _validate_current_dataset_ids(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"invalid current dataset id at row {index}")
        if row_id in by_id:
            raise ValueError(f"duplicate current dataset id: {row_id}")
        by_id[row_id] = row
    return by_id


def _validate_legacy_checkpoint_rows(
    records: Sequence[Mapping[str, Any]], current_rows: Sequence[Mapping[str, Any]]
) -> None:
    current_by_id = _validate_current_dataset_ids(current_rows)
    for record in records:
        row_id = record.get("query_id")
        current = current_by_id.get(str(row_id))
        if current is None:
            raise ValueError(f"checkpoint id is missing from current dataset: {row_id}")
        for field in SEMANTIC_CHECKPOINT_FIELDS:
            checkpoint_value = record.get("query_id") if field == "id" else record.get(field)
            current_value = current.get(field)
            if checkpoint_value != current_value:
                raise ValueError(
                    "legacy checkpoint row does not match current dataset: "
                    f"id={row_id}, field={field}"
                )


def validate_checkpoint_fingerprint(
    records: Sequence[Mapping[str, Any]],
    fingerprint: BenchmarkFingerprint,
    *,
    current_rows: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    mismatched = [
        record
        for record in records
        if record.get("benchmark_fingerprint") != fingerprint.value
    ]
    if not mismatched:
        return
    legacy_records: list[Mapping[str, Any]] = []
    for record in mismatched:
        old_payload = record.get("fingerprint_config")
        if not isinstance(old_payload, Mapping):
            raise TypeError("checkpoint fingerprint config must be an object")
        if old_payload.get("fingerprint_schema_version") == FINGERPRINT_SCHEMA_VERSION:
            old_identity = _flatten(_portable_identity(old_payload))
            new_identity = _flatten(_portable_identity(fingerprint.payload))
            changed = sorted(
                key
                for key in old_identity.keys() | new_identity.keys()
                if old_identity.get(key) != new_identity.get(key)
            )
        else:
            changed = _legacy_config_changes(old_payload, fingerprint.payload)
            legacy_records.append(record)
        if changed:
            raise ValueError(
                "checkpoint fingerprint does not match current benchmark; "
                f"changed config: {', '.join(changed)}"
            )
    if legacy_records:
        if current_rows is None:
            raise ValueError(
                "current dataset rows are required to validate a legacy checkpoint"
            )
        _validate_legacy_checkpoint_rows(legacy_records, current_rows)


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
    current_rows: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records = load_checkpoint(checkpoint_path)
    if fingerprint is not None:
        validate_checkpoint_fingerprint(records, fingerprint, current_rows=current_rows)
    latest = list(latest_checkpoint_records(records).values())
    from furiosa_rag.e2e_benchmark import export_e2e_csv

    export_e2e_csv(latest, output_path)
    return latest
