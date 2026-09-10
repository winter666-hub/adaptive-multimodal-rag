"""Judge forced UniDoc runs and derive answer-type routing alignment outcomes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from furiosa_rag.benchmark_checkpoint import (
    BenchmarkFingerprint,
    ExecutionKey,
    append_checkpoint,
    latest_checkpoint_records,
    load_checkpoint,
    validate_checkpoint_fingerprint,
)
from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.cli.evaluate_answer_quality import (
    JUDGE_PARSER_POLICY_VERSION,
    JudgeOutputError,
    judge_answer,
)
from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.llm import FuriosaLlm
from furiosa_rag.route_gt_audit import build_alignment, summarize_alignment, write_csv

AUDIT_JUDGE_PROMPT_VERSION = "unidoc-route-gt-audit-v1"
AUDIT_JUDGE_MAX_TOKENS = 512
AUDIT_CORRECTNESS_THRESHOLD = 3
AUDIT_JUDGE_PROMPT = """You are a strict answer-quality judge for document questions.

Evaluate the candidate answer only against the question and reference answer.
Correctness (0-4): 4 fully correct with no meaningful factual errors; 3 mostly
correct with only a minor error or imprecision; 2 partially correct with important
information missing or inaccurate; 1 mostly incorrect with a small correct element;
0 incorrect or does not answer.
Completeness (0-2): 2 covers all essential points; 1 covers the main idea but misses
an important part; 0 misses major parts.
Grounding (0-2): 2 is consistent with the reference; 1 includes a minor unsupported
claim; 0 substantially contradicts the reference or is unsupported.
Task Satisfaction (0-2): 2 directly answers the requested task; 1 is indirect or
incomplete; 0 fails the requested task.

Rules:
- You are not given strategy, route, vision usage, or latency information.
- Do not infer or reward whether Vision was used.
- Accept semantically correct wording that differs from the reference.
- Return strict valid JSON only, without markdown fences or extra text.

Return this object:
{{"correctness": 0, "completeness": 0, "grounding": 0,
 "task_satisfaction": 0, "total": 0, "reason": "concise explanation"}}

Question:
{question}

Reference answer:
{reference}

Candidate answer:
{candidate}
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(
    dataset: Path,
    forced_text: Path,
    forced_vision: Path,
    judge_model: str,
    retrieval_aware: Path | None = None,
) -> BenchmarkFingerprint:
    payload = {
        "audit": "unidoc_route_ground_truth",
        "inputs": {
            "dataset_sha256": _sha256_file(dataset),
            "forced_text_sha256": _sha256_file(forced_text),
            "forced_vision_sha256": _sha256_file(forced_vision),
        },
        "judge": {
            "model": judge_model,
            "temperature": 0,
            "max_tokens": AUDIT_JUDGE_MAX_TOKENS,
            "thinking_enabled": False,
            "prompt_version": AUDIT_JUDGE_PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(AUDIT_JUDGE_PROMPT.encode()).hexdigest(),
            "correctness_threshold": AUDIT_CORRECTNESS_THRESHOLD,
        },
    }
    if retrieval_aware is not None:
        payload["inputs"]["retrieval_aware_sha256"] = _sha256_file(retrieval_aware)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return BenchmarkFingerprint(hashlib.sha256(canonical.encode()).hexdigest(), payload)


def load_forced_results(path: str | Path, expected_strategy: str) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise ValueError(f"forced result is empty: {path}")
    ids: set[str] = set()
    for row in rows:
        query_id = row.get("query_id") or row.get("id")
        if not query_id or query_id in ids:
            raise ValueError(f"missing or duplicate query_id in {path}: {query_id!r}")
        if row.get("strategy") != expected_strategy:
            raise ValueError(f"unexpected strategy in {path}: {row.get('strategy')!r}")
        ids.add(query_id)
        row["query_id"] = query_id
    return rows


def judge_candidates(
    dataset_rows: list[dict[str, Any]],
    candidates_by_strategy: Mapping[str, list[dict[str, Any]]],
    backend: FuriosaLlm,
    *,
    fingerprint: BenchmarkFingerprint,
    checkpoint: Path,
    resume: bool,
    retry_errors: bool,
    seed: int,
    retry_execution_keys: set[ExecutionKey] | None = None,
) -> list[dict[str, Any]]:
    source_by_id = {row["id"]: row for row in dataset_rows}
    records = (
        load_checkpoint(checkpoint, repair_truncated_final_line=True) if resume else []
    )
    validate_checkpoint_fingerprint(records, fingerprint)
    latest = latest_checkpoint_records(records)
    candidates = [row for rows in candidates_by_strategy.values() for row in rows]
    candidate_keys = {(row["query_id"], row["strategy"]) for row in candidates}
    targeted = retry_execution_keys or set()
    missing_candidates = sorted(targeted - candidate_keys)
    missing_checkpoint = sorted(targeted - latest.keys())
    if missing_candidates:
        raise ValueError(f"targeted retry candidates do not exist: {missing_candidates}")
    if missing_checkpoint:
        raise ValueError(f"targeted retry checkpoint records do not exist: {missing_checkpoint}")
    if targeted:
        print(f"targeted judge retries ({len(targeted)}):")
        for query_id, strategy in sorted(targeted):
            previous = latest[(query_id, strategy)]
            print(
                f"  {query_id} / {strategy} "
                f"previous_score={previous.get('judge_correctness_score', '')} "
                f"previous_error={previous.get('error', '')!r}"
            )
    random.Random(seed).shuffle(candidates)
    for candidate in candidates:
        key = (candidate["query_id"], candidate["strategy"])
        previous = latest.get(key)
        if targeted:
            if key not in targeted:
                continue
        elif previous is not None and (not previous.get("error") or not retry_errors):
            continue
        source = source_by_id.get(candidate["query_id"])
        if source is None:
            raise ValueError(f"candidate not found in audit dataset: {candidate['query_id']}")
        started = time.perf_counter_ns()
        result: dict[str, Any] = {
            **candidate,
            "gold_answer": source["gold_answer"],
            "answer_type": source["answer_type"],
            "domain": source.get("domain", ""),
            "judge_correct": False,
            "judge_correctness_score": "",
            "judge_reason": "",
            "judge_raw_response": "",
            "judge_parser_mode": "",
            "judge_parser_policy_version": JUDGE_PARSER_POLICY_VERSION,
            "judge_attempt_count": "",
            "judge_model_id": fingerprint.payload["judge"]["model"],
            "judge_temperature": 0,
            "judge_max_tokens": AUDIT_JUDGE_MAX_TOKENS,
            "judge_thinking_enabled": False,
            "judge_prompt_version": AUDIT_JUDGE_PROMPT_VERSION,
            "judge_prompt_sha256": fingerprint.payload["judge"]["prompt_sha256"],
            "judge_execution_timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "",
        }
        if candidate.get("error"):
            result["error"] = f"candidate error: {candidate['error']}"
        else:
            try:
                score = judge_answer(
                    backend,
                    question=source["question"],
                    reference=source["gold_answer"],
                    candidate=candidate["answer"],
                    prompt_template=AUDIT_JUDGE_PROMPT,
                )
                result["judge_correctness_score"] = score.correctness
                result["judge_correct"] = score.correctness >= AUDIT_CORRECTNESS_THRESHOLD
                result["judge_reason"] = score.reason
                result["judge_raw_response"] = score.raw_response
                result["judge_parser_mode"] = score.parser_mode
                result["judge_parser_policy_version"] = score.parser_policy_version
                result["judge_attempt_count"] = score.attempt_count
            except Exception as exc:  # noqa: BLE001 - checkpoint individual judge failures
                if isinstance(exc, JudgeOutputError):
                    result["judge_raw_response"] = exc.raw_response
                    result["judge_parser_mode"] = exc.parser_mode
                    result["judge_parser_policy_version"] = exc.parser_policy_version
                    result["judge_attempt_count"] = exc.attempt_count
                result["error"] = f"{type(exc).__name__}: {exc}"
        result["judge_latency_ms"] = (
            time.perf_counter_ns() - started
        ) / 1_000_000
        append_checkpoint(checkpoint, result, fingerprint)
        latest[key] = result
    return list(latest_checkpoint_records(load_checkpoint(checkpoint)).values())


def _print_summary(rows: list[dict[str, Any]]) -> None:
    for summary in summarize_alignment(rows):
        print(f"\nanswer_type={summary['answer_type']} count={summary['count']}")
        for key, value in summary.items():
            if key not in {"answer_type", "count"}:
                print(f"{key}={value:.6f}" if isinstance(value, float) else f"{key}={value}")


def _is_true(value: Any) -> bool:
    return value is True or str(value).casefold() == "true"


def _retry_execution(value: str) -> ExecutionKey:
    query_id, separator, strategy = value.rpartition("/")
    if not separator or not query_id or not strategy:
        raise argparse.ArgumentTypeError("expected QUERY_ID/STRATEGY")
    return query_id, strategy


def _print_retrieval_aware_summary(rows: list[dict[str, Any]]) -> None:
    for answer_type in ("overall", "text_only", "image_only", "table_required", "image_plus_text_as_answer"):
        group = rows if answer_type == "overall" else [
            row for row in rows if row.get("answer_type") == answer_type
        ]
        successful = [row for row in group if not row.get("error")]
        count = len(group)
        correctness = sum(row.get("judge_correct") is True for row in successful)
        vision_calls = sum(_is_true(row.get("vision_used")) for row in successful)
        average_latency = (
            sum(float(row["total_latency_ms"]) for row in successful) / len(successful)
            if successful
            else 0.0
        )
        average_routing = (
            sum(float(row["routing_latency_ms"]) for row in successful) / len(successful)
            if successful
            else 0.0
        )
        print(f"\nretrieval_aware answer_type={answer_type} count={count}")
        print(f"correctness={correctness / count if count else 0.0:.6f}")
        print(f"vision_call_rate={vision_calls / count if count else 0.0:.6f}")
        print(f"average_total_latency_ms={average_latency:.6f}")
        print(f"average_routing_latency_ms={average_routing:.6f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--forced-text", type=Path, required=True)
    parser.add_argument("--forced-vision", type=Path, required=True)
    parser.add_argument("--retrieval-aware", type=Path)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path("results/unidoc_gt_audit_judge.checkpoint.jsonl"),
    )
    parser.add_argument(
        "--alignment-output",
        type=Path,
        default=Path("results/unidoc_gt_audit_alignment.csv"),
    )
    parser.add_argument(
        "--judged-text-output",
        type=Path,
        default=Path("results/unidoc_gt_audit_forced_text_judged.csv"),
    )
    parser.add_argument(
        "--judged-vision-output",
        type=Path,
        default=Path("results/unidoc_gt_audit_forced_vision_judged.csv"),
    )
    parser.add_argument(
        "--judged-retrieval-aware-output",
        type=Path,
        default=Path("results/unidoc_gt_audit_retrieval_aware_judged.csv"),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--retry-execution",
        action="append",
        default=[],
        type=_retry_execution,
        metavar="QUERY_ID/STRATEGY",
        help="explicitly append a new judge execution for one existing checkpoint key",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.retry_errors and not args.resume:
        parser.error("--retry-errors requires --resume")
    if args.retry_execution and not args.resume:
        parser.error("--retry-execution requires --resume")
    if len(set(args.retry_execution)) != len(args.retry_execution):
        parser.error("duplicate --retry-execution key")
    if args.checkpoint.exists() and args.checkpoint.stat().st_size and not args.resume:
        parser.error("checkpoint already exists; use --resume or choose a new path")

    dataset_rows = load_benchmark_jsonl(args.dataset)
    candidates = {
        "forced_text": load_forced_results(args.forced_text, "forced_text"),
        "forced_vision": load_forced_results(args.forced_vision, "forced_vision"),
    }
    if args.retrieval_aware is not None:
        candidates["retrieval_aware_adaptive"] = load_forced_results(
            args.retrieval_aware, "retrieval_aware_adaptive"
        )
    settings = Settings.from_env()
    endpoint = next(item for item in settings.endpoints if item.name == "llm")
    fingerprint = _fingerprint(
        args.dataset,
        args.forced_text,
        args.forced_vision,
        endpoint.model,
        args.retrieval_aware,
    )
    judged = judge_candidates(
        dataset_rows,
        candidates,
        FuriosaLlm(endpoint, FuriosaClient(settings.api_key, settings.request_timeout)),
        fingerprint=fingerprint,
        checkpoint=args.checkpoint,
        resume=args.resume,
        retry_errors=args.retry_errors,
        seed=args.seed,
        retry_execution_keys=set(args.retry_execution),
    )
    by_strategy = {
        strategy: [row for row in judged if row["strategy"] == strategy]
        for strategy in candidates
    }
    write_csv(by_strategy["forced_text"], args.judged_text_output)
    write_csv(by_strategy["forced_vision"], args.judged_vision_output)
    if "retrieval_aware_adaptive" in by_strategy:
        write_csv(
            by_strategy["retrieval_aware_adaptive"],
            args.judged_retrieval_aware_output,
        )
    alignment = build_alignment(
        dataset_rows, by_strategy["forced_text"], by_strategy["forced_vision"]
    )
    write_csv(alignment, args.alignment_output)
    _print_summary(alignment)
    if "retrieval_aware_adaptive" in by_strategy:
        _print_retrieval_aware_summary(by_strategy["retrieval_aware_adaptive"])
    print(f"alignment_output={args.alignment_output}")
    print(f"judged_text_output={args.judged_text_output}")
    print(f"judged_vision_output={args.judged_vision_output}")
    if "retrieval_aware_adaptive" in by_strategy:
        print(f"judged_retrieval_aware_output={args.judged_retrieval_aware_output}")
    print(f"judge_checkpoint={args.checkpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
