"""Deterministic sampling and alignment analysis for routing ground-truth audits."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ANSWER_TYPES = (
    "text_only",
    "image_only",
    "table_required",
    "image_plus_text_as_answer",
)
AUDIT_SAMPLE_METHOD = "answer_type_domain_round_robin_v1"
ALIGNMENT_OUTCOMES = (
    "BOTH_CORRECT",
    "TEXT_ONLY_BETTER",
    "VISION_NEEDED",
    "BOTH_WRONG",
)


def _group_seed(seed: int, answer_type: str, domain: str) -> int:
    value = f"{seed}\0{answer_type}\0{domain}".encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big")


def stratified_audit_sample(
    rows: Sequence[Mapping[str, Any]], *, per_answer_type: int = 40, seed: int = 42
) -> list[dict[str, Any]]:
    """Sample each answer type while round-robin balancing domains."""
    if per_answer_type <= 0:
        raise ValueError("per_answer_type must be greater than zero")
    selected: list[dict[str, Any]] = []
    for answer_type in ANSWER_TYPES:
        by_domain: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            if row.get("answer_type") == answer_type:
                by_domain[str(row.get("domain") or "<missing>")].append(row)
        if sum(map(len, by_domain.values())) < per_answer_type:
            raise ValueError(f"not enough rows for answer_type {answer_type!r}")
        for domain, group in by_domain.items():
            random.Random(_group_seed(seed, answer_type, domain)).shuffle(group)

        domain_order = sorted(by_domain)
        offsets = Counter()
        type_rows: list[Mapping[str, Any]] = []
        while len(type_rows) < per_answer_type:
            made_progress = False
            for domain in domain_order:
                offset = offsets[domain]
                if offset >= len(by_domain[domain]):
                    continue
                type_rows.append(by_domain[domain][offset])
                offsets[domain] += 1
                made_progress = True
                if len(type_rows) == per_answer_type:
                    break
            if not made_progress:
                raise ValueError(f"could not fill answer_type {answer_type!r}")
        selected.extend(
            {
                **row,
                "audit_sampling_seed": seed,
                "audit_sampling_method": AUDIT_SAMPLE_METHOD,
            }
            for row in type_rows
        )
    return selected


def write_audit_jsonl(rows: Sequence[Mapping[str, Any]], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def alignment_outcome(text_correct: bool, vision_correct: bool) -> str:
    if text_correct and vision_correct:
        return "BOTH_CORRECT"
    if text_correct:
        return "TEXT_ONLY_BETTER"
    if vision_correct:
        return "VISION_NEEDED"
    return "BOTH_WRONG"


def build_alignment(
    dataset_rows: Sequence[Mapping[str, Any]],
    text_results: Sequence[Mapping[str, Any]],
    vision_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    dataset = {str(row["id"]): row for row in dataset_rows}
    text = {str(row["query_id"]): row for row in text_results}
    vision = {str(row["query_id"]): row for row in vision_results}
    if set(text) != set(dataset) or set(vision) != set(dataset):
        raise ValueError("dataset and judged strategy query IDs must match exactly")
    failures = [
        f"{row['query_id']}/{row['strategy']}"
        for row in [*text_results, *vision_results]
        if row.get("error")
    ]
    if failures:
        raise ValueError(f"cannot align judge failures: {', '.join(failures[:10])}")
    aligned: list[dict[str, Any]] = []
    for query_id, source in dataset.items():
        text_correct = text[query_id].get("judge_correct") is True
        vision_correct = vision[query_id].get("judge_correct") is True
        aligned.append(
            {
                "query_id": query_id,
                "question": source["question"],
                "gold_answer": source["gold_answer"],
                "answer_type": source["answer_type"],
                "domain": source.get("domain", ""),
                "text_correct": text_correct,
                "vision_correct": vision_correct,
                "outcome": alignment_outcome(text_correct, vision_correct),
                "text_correctness_score": text[query_id].get("judge_correctness_score", ""),
                "vision_correctness_score": vision[query_id].get(
                    "judge_correctness_score", ""
                ),
                "text_e2e_latency_ms": text[query_id].get("total_latency_ms", ""),
                "vision_e2e_latency_ms": vision[query_id].get("total_latency_ms", ""),
            }
        )
    return aligned


def summarize_alignment(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups = [("overall", list(rows))]
    groups.extend(
        (answer_type, [row for row in rows if row["answer_type"] == answer_type])
        for answer_type in ANSWER_TYPES
    )
    summaries: list[dict[str, Any]] = []
    for answer_type, group in groups:
        count = len(group)
        text_correct = sum(row["text_correct"] is True for row in group)
        vision_correct = sum(row["vision_correct"] is True for row in group)
        outcome_counts = Counter(str(row["outcome"]) for row in group)
        summary: dict[str, Any] = {
            "answer_type": answer_type,
            "count": count,
            "forced_text_correctness": text_correct / count if count else 0.0,
            "forced_vision_correctness": vision_correct / count if count else 0.0,
            "vision_gain": (vision_correct - text_correct) / count if count else 0.0,
            "forced_text_avg_e2e_ms": _average(group, "text_e2e_latency_ms"),
            "forced_vision_avg_e2e_ms": _average(group, "vision_e2e_latency_ms"),
        }
        for outcome in ALIGNMENT_OUTCOMES:
            summary[f"{outcome.lower()}_count"] = outcome_counts[outcome]
            summary[f"{outcome.lower()}_rate"] = (
                outcome_counts[outcome] / count if count else 0.0
            )
        summaries.append(summary)
    return summaries


def _average(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    values = [float(row[field]) for row in rows if str(row.get(field, "")).strip()]
    return sum(values) / len(values) if values else 0.0


def write_csv(rows: Sequence[Mapping[str, Any]], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
