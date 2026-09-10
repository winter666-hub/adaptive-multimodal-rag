from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from furiosa_rag.benchmark_checkpoint import BenchmarkFingerprint
from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.cli.audit_route_ground_truth import judge_candidates
from furiosa_rag.route_gt_audit import (
    ANSWER_TYPES,
    alignment_outcome,
    build_alignment,
    stratified_audit_sample,
)


def _dataset() -> list[dict[str, object]]:
    return [
        {
            "id": f"{answer_type}-{domain}-{index}",
            "question": "question",
            "gold_answer": "gold",
            "answer_type": answer_type,
            "domain": domain,
            "source_pdf": f"{domain}/{domain}/{index}.pdf",
        }
        for answer_type in ANSWER_TYPES
        for domain in ("a", "b", "c", "d")
        for index in range(12)
    ]


def test_stratified_sample_is_deterministic_and_balanced() -> None:
    first = stratified_audit_sample(_dataset(), per_answer_type=40, seed=7)
    second = stratified_audit_sample(_dataset(), per_answer_type=40, seed=7)

    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(first) == 160
    assert Counter(row["answer_type"] for row in first) == {
        answer_type: 40 for answer_type in ANSWER_TYPES
    }
    assert set(Counter((row["answer_type"], row["domain"]) for row in first).values()) == {
        10
    }
    assert all(row["audit_sampling_seed"] == 7 for row in first)


def test_committed_audit_160_is_loadable_and_balanced() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "unidoc_route_gt_audit_160.jsonl"
    rows = load_benchmark_jsonl(path)

    assert len(rows) == 160
    assert Counter(row["answer_type"] for row in rows) == {
        answer_type: 40 for answer_type in ANSWER_TYPES
    }
    assert all(row["gold_answer"] for row in rows)


def test_all_four_alignment_outcomes() -> None:
    assert alignment_outcome(True, True) == "BOTH_CORRECT"
    assert alignment_outcome(True, False) == "TEXT_ONLY_BETTER"
    assert alignment_outcome(False, True) == "VISION_NEEDED"
    assert alignment_outcome(False, False) == "BOTH_WRONG"


def test_alignment_preserves_gold_answer_metadata() -> None:
    dataset = [_dataset()[0]]
    query_id = str(dataset[0]["id"])
    text = [{"query_id": query_id, "strategy": "forced_text", "judge_correct": True}]
    vision = [
        {"query_id": query_id, "strategy": "forced_vision", "judge_correct": False}
    ]

    result = build_alignment(dataset, text, vision)[0]

    assert result["gold_answer"] == "gold"
    assert result["outcome"] == "TEXT_ONLY_BETTER"


class FakeJudge:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        self.calls += 1
        return json.dumps(
            {
                "correctness": 4,
                "completeness": 2,
                "grounding": 2,
                "task_satisfaction": 2,
                "total": 10,
                "reason": "correct",
            }
        )


def test_judge_checkpoint_resume_preserves_gold_metadata(tmp_path: Path) -> None:
    source = _dataset()[0]
    candidate = {
        "id": source["id"],
        "query_id": source["id"],
        "question": source["question"],
        "strategy": "forced_text",
        "answer": "candidate",
        "error": "",
        "total_latency_ms": "1.5",
    }
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = BenchmarkFingerprint("fingerprint", {"judge": {"model": "judge", "prompt_sha256": "abc"}})
    judge = FakeJudge()

    first = judge_candidates(
        [source],
        {"forced_text": [candidate]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=False,
        retry_errors=False,
        seed=42,
    )
    second = judge_candidates(
        [source],
        {"forced_text": [candidate]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
    )

    assert judge.calls == 1
    assert first[0]["gold_answer"] == second[0]["gold_answer"] == "gold"
    assert second[0]["judge_correct"] is True


def test_targeted_retry_reexecutes_only_named_success(tmp_path: Path, capsys) -> None:
    sources = _dataset()[:2]
    candidates = [
        {
            "id": source["id"],
            "query_id": source["id"],
            "question": source["question"],
            "strategy": "forced_text",
            "answer": "candidate",
            "error": "",
            "total_latency_ms": "1.5",
        }
        for source in sources
    ]
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = BenchmarkFingerprint(
        "fingerprint", {"judge": {"model": "judge", "prompt_sha256": "abc"}}
    )
    judge = FakeJudge()
    judge_candidates(
        sources,
        {"forced_text": candidates},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=False,
        retry_errors=False,
        seed=42,
    )
    target = (str(sources[0]["id"]), "forced_text")
    rows = judge_candidates(
        sources,
        {"forced_text": candidates},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
        retry_execution_keys={target},
    )

    assert judge.calls == 3
    assert len(rows) == 2
    assert "targeted judge retries (1)" in capsys.readouterr().out
    records = [json.loads(line) for line in checkpoint.read_text().splitlines()]
    assert sum((row["query_id"], row["strategy"]) == target for row in records) == 2
    assert sum(row["judge_parser_policy_version"] == "judge-json-v2" for row in records) == 3
