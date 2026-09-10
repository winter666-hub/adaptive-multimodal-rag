from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from furiosa_rag.benchmark_checkpoint import (
    BenchmarkFingerprint,
    append_checkpoint,
    load_checkpoint,
)
from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.cli.audit_route_ground_truth import judge_candidates, main
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
        self.calls: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        question = prompt.split("Question:\n", 1)[1].split("\n\nReference answer:", 1)[0]
        self.calls.append(question)
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


def _fingerprint() -> BenchmarkFingerprint:
    return BenchmarkFingerprint(
        "fingerprint", {"judge": {"model": "judge", "prompt_sha256": "abc"}}
    )


def _source(query_id: str) -> dict[str, object]:
    return {
        "id": query_id,
        "question": query_id,
        "gold_answer": "gold",
        "answer_type": "text_only",
        "domain": "domain",
    }


def _candidate(query_id: str, strategy: str = "forced_text") -> dict[str, object]:
    return {
        "id": query_id,
        "query_id": query_id,
        "question": query_id,
        "strategy": strategy,
        "answer": "candidate",
        "error": "",
        "total_latency_ms": "1.5",
    }


def _seed_checkpoint(
    checkpoint: Path,
    fingerprint: BenchmarkFingerprint,
    query_id: str,
    *,
    strategy: str = "forced_text",
    error: str = "",
) -> None:
    append_checkpoint(
        checkpoint,
        {
            "query_id": query_id,
            "strategy": strategy,
            "judge_correctness_score": "" if error else 4,
            "error": error,
        },
        fingerprint,
    )


def _appended_keys(checkpoint: Path, initial_count: int) -> list[tuple[str, str]]:
    records = load_checkpoint(checkpoint)
    return [(row["query_id"], row["strategy"]) for row in records[initial_count:]]


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
    fingerprint = _fingerprint()
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

    assert len(judge.calls) == 1
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
    fingerprint = _fingerprint()
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

    assert len(judge.calls) == 3
    assert len(rows) == 2
    assert "targeted judge retries (1)" in capsys.readouterr().out
    records = [json.loads(line) for line in checkpoint.read_text().splitlines()]
    assert sum((row["query_id"], row["strategy"]) == target for row in records) == 2
    assert sum(row["judge_parser_policy_version"] == "judge-json-v2" for row in records) == 3


def test_targeted_retry_skips_unseen_non_target_candidate(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1")
    judge = FakeJudge()

    judge_candidates(
        [_source("q1"), _source("q2")],
        {"forced_text": [_candidate("q1"), _candidate("q2")]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
        retry_execution_keys={("q1", "forced_text")},
    )

    assert judge.calls == ["q1"]
    assert _appended_keys(checkpoint, 1) == [("q1", "forced_text")]


def test_targeted_retry_errors_skips_non_target_error(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1")
    _seed_checkpoint(checkpoint, fingerprint, "q2", error="previous failure")
    judge = FakeJudge()

    rows = judge_candidates(
        [_source("q1"), _source("q2")],
        {"forced_text": [_candidate("q1"), _candidate("q2")]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=True,
        seed=42,
        retry_execution_keys={("q1", "forced_text")},
    )

    assert judge.calls == ["q1"]
    assert _appended_keys(checkpoint, 2) == [("q1", "forced_text")]
    assert next(row for row in rows if row["query_id"] == "q2")["error"] == "previous failure"


def test_targeted_retry_reexecutes_named_error(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1", error="previous failure")
    judge = FakeJudge()

    rows = judge_candidates(
        [_source("q1")],
        {"forced_text": [_candidate("q1")]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
        retry_execution_keys={("q1", "forced_text")},
    )

    assert judge.calls == ["q1"]
    assert _appended_keys(checkpoint, 1) == [("q1", "forced_text")]
    assert rows[0]["error"] == ""


def test_multiple_targeted_retries_execute_exact_key_set(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    for query_id, strategy in (
        ("q1", "forced_text"),
        ("q2", "forced_vision"),
        ("q3", "forced_text"),
    ):
        _seed_checkpoint(checkpoint, fingerprint, query_id, strategy=strategy)
    judge = FakeJudge()

    judge_candidates(
        [_source("q1"), _source("q2"), _source("q3")],
        {
            "forced_text": [_candidate("q1"), _candidate("q3")],
            "forced_vision": [_candidate("q2", "forced_vision")],
        },
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
        retry_execution_keys={("q1", "forced_text"), ("q2", "forced_vision")},
    )

    assert Counter(judge.calls) == Counter(("q1", "q2"))
    assert Counter(_appended_keys(checkpoint, 3)) == Counter(
        (("q1", "forced_text"), ("q2", "forced_vision"))
    )


@pytest.mark.parametrize(
    "target",
    (("missing", "forced_text"), ("q1", "invalid_strategy")),
    ids=("invalid-query-id", "invalid-strategy"),
)
def test_invalid_targeted_retry_is_rejected_without_judge_call(
    tmp_path: Path, target: tuple[str, str]
) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1")
    judge = FakeJudge()

    with pytest.raises(ValueError, match="targeted retry candidates do not exist"):
        judge_candidates(
            [_source("q1")],
            {"forced_text": [_candidate("q1")]},
            judge,  # type: ignore[arg-type]
            fingerprint=fingerprint,
            checkpoint=checkpoint,
            resume=True,
            retry_errors=False,
            seed=42,
            retry_execution_keys={target},
        )

    assert judge.calls == []
    assert len(load_checkpoint(checkpoint)) == 1


def test_duplicate_cli_target_is_rejected_before_loading_inputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit-route-ground-truth",
            "--dataset",
            "unused.jsonl",
            "--forced-text",
            "unused-text.csv",
            "--forced-vision",
            "unused-vision.csv",
            "--resume",
            "--retry-execution",
            "q1/forced_text",
            "--retry-execution",
            "q1/forced_text",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 2
    assert "duplicate --retry-execution key" in capsys.readouterr().err


def test_non_targeted_resume_runs_only_unseen_candidate(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1")
    _seed_checkpoint(checkpoint, fingerprint, "q2", error="previous failure")
    judge = FakeJudge()

    judge_candidates(
        [_source("q1"), _source("q2"), _source("q3")],
        {"forced_text": [_candidate("q1"), _candidate("q2"), _candidate("q3")]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=False,
        seed=42,
    )

    assert judge.calls == ["q3"]
    assert _appended_keys(checkpoint, 2) == [("q3", "forced_text")]


def test_non_targeted_retry_errors_runs_only_error_candidate(tmp_path: Path) -> None:
    checkpoint = tmp_path / "judge.jsonl"
    fingerprint = _fingerprint()
    _seed_checkpoint(checkpoint, fingerprint, "q1")
    _seed_checkpoint(checkpoint, fingerprint, "q2", error="previous failure")
    judge = FakeJudge()

    judge_candidates(
        [_source("q1"), _source("q2")],
        {"forced_text": [_candidate("q1"), _candidate("q2")]},
        judge,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        checkpoint=checkpoint,
        resume=True,
        retry_errors=True,
        seed=42,
    )

    assert judge.calls == ["q2"]
    assert _appended_keys(checkpoint, 2) == [("q2", "forced_text")]
