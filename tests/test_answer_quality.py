from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from furiosa_rag.cli.evaluate_answer_quality import (
    JudgeOutputError,
    evaluate_quality,
    judge_answer,
    load_candidates,
    load_references,
    parse_judge_output,
    reevaluate_one,
    repair_invalid_json_escapes,
    shuffled_candidates,
    summarize_quality,
)


def test_load_references(tmp_path: Path) -> None:
    path = tmp_path / "references.jsonl"
    path.write_text(
        json.dumps({"id": "E_T01", "reference": "reference"}) + "\n",
        encoding="utf-8",
    )
    assert load_references(path) == {"E_T01": "reference"}


def test_load_candidate_csv(tmp_path: Path) -> None:
    path = tmp_path / "answers.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=("id", "question", "category", "answer", "route"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "id": "E_T01",
                "question": "question",
                "category": "text",
                "answer": "answer",
                "route": "TEXT_ONLY",
            }
        )
    assert load_candidates(path, "llm") == [
        {
            "id": "E_T01",
            "question": "question",
            "category": "text",
            "answer": "answer",
            "strategy": "llm",
        }
    ]


def _candidate_sets() -> dict[str, list[dict[str, str]]]:
    return {
        strategy: [
            {
                "id": candidate_id,
                "question": f"question {candidate_id}",
                "category": "text",
                "answer": f"answer {strategy}",
                "strategy": strategy,
            }
            for candidate_id in ("E_T01", "E_A02")
        ]
        for strategy in ("always_vision", "llm", "adaptive")
    }


def test_shuffle_is_deterministic_and_preserves_question_groups() -> None:
    references = {"E_T01": "ref 1", "E_A02": "ref 2"}
    first = shuffled_candidates(references, _candidate_sets(), seed=42)
    second = shuffled_candidates(references, _candidate_sets(), seed=42)

    assert [row["strategy"] for row in first] == [row["strategy"] for row in second]
    assert [row["id"] for row in first[:3]] == ["E_T01"] * 3
    assert [row["id"] for row in first[3:]] == ["E_A02"] * 3


def test_parse_judge_json_removes_fence_and_validates_total() -> None:
    raw = """```json
{"correctness":4,"completeness":2,"grounding":1,
 "task_satisfaction":2,"total":9,"reason":"Mostly grounded."}
```"""
    result = parse_judge_output(raw)
    assert result.correctness == 4
    assert result.total == 9
    assert result.reason == "Mostly grounded."
    assert result.parser_mode == "strict"
    assert result.parser_policy_version == "judge-json-v2"
    assert result.raw_response == raw


@pytest.mark.parametrize(
    "wrapper",
    (
        "{}",
        "```json\n{}\n```",
        "```\n{}\n```",
        "  \n```json\n{}\n```\n  ",
        "Judge result follows:\n{}\nEnd of result.",
    ),
    ids=("raw", "json-fence", "plain-fence", "fence-whitespace", "surrounding-text"),
)
def test_parse_judge_output_common_hosted_formats(wrapper: str) -> None:
    payload = json.dumps(
        {
            "correctness": 4,
            "completeness": 2,
            "grounding": 2,
            "task_satisfaction": 2,
            "total": 10,
            "reason": "Handles braces such as {example} inside a string.",
        }
    )
    result = parse_judge_output(wrapper.format(payload))
    assert result.total == 10
    assert "{example}" in result.reason


def test_malformed_latex_escape_reproduces_invalid_escape_and_is_repaired() -> None:
    response = r'''```json
{
  "correctness": 4,
  "completeness": 2,
  "grounding": 2,
  "task_satisfaction": 2,
  "total": 10,
  "reason": "Scaling by $1/\sqrt{d_k}$ prevents overly large dot products."
}
```'''
    unfenced = response.removeprefix("```json\n").removesuffix("\n```")
    with pytest.raises(json.JSONDecodeError) as raised:
        json.loads(unfenced)
    assert raised.value.msg == "Invalid \\escape"
    assert raised.value.pos > 0
    assert raised.value.lineno == 7
    assert raised.value.colno > 1

    result = parse_judge_output(response)
    assert result.correctness == 4
    assert result.completeness == 2
    assert result.grounding == 2
    assert result.task_satisfaction == 2
    assert result.total == 10


def test_unrecoverable_json_error_rejects_missing_score_fields() -> None:
    raw = r'{"correctness": 4, "reason": "bad \sqrt{x}" trailing}'
    with pytest.raises(JudgeOutputError) as raised:
        parse_judge_output(raw)
    assert "missing required field: completeness" in str(raised.value)


def test_unescaped_quote_in_reason_recovers_scores() -> None:
    raw = '''{"correctness":3,"completeness":1,"grounding":2,
    "task_satisfaction":2,"total":8,
    "reason":"It accurately describes the "handling" scenario."}'''
    result = parse_judge_output(raw)
    assert (result.correctness, result.completeness, result.grounding) == (3, 1, 2)
    assert result.task_satisfaction == 2
    assert result.total == 8
    assert "handling" in result.reason
    assert result.reason.startswith("[fallback: malformed judge JSON]")
    assert result.parser_mode == "fallback"


def test_multiple_unescaped_quotes_in_reason_recovers_scores() -> None:
    raw = '''{"correctness":1,"completeness":0,"grounding":0,
    "task_satisfaction":1,"total":2,
    "reason":"Unsupported "death of distance" and "handling" claims."}'''
    result = parse_judge_output(raw)
    assert (result.correctness, result.completeness, result.grounding) == (1, 0, 0)
    assert result.task_satisfaction == 1
    assert result.total == 2


@pytest.mark.parametrize("field", ("correctness", "total"))
def test_strict_duplicate_required_key_is_rejected(field: str) -> None:
    raw = (
        '{"correctness":3,"completeness":1,"grounding":2,'
        '"task_satisfaction":2,"total":8,"reason":"ok",'
        f'"{field}":3}}'
    )
    with pytest.raises(JudgeOutputError, match="duplicate JSON key"):
        parse_judge_output(raw)


@pytest.mark.parametrize(
    "total",
    (None, 8.0, 11, 7),
    ids=("missing", "non-integer", "out-of-range", "inconsistent"),
)
def test_strict_total_is_required_integer_in_range_and_consistent(total: object) -> None:
    payload = {
        "correctness": 3,
        "completeness": 1,
        "grounding": 2,
        "task_satisfaction": 2,
        "reason": "ok",
    }
    if total is not None:
        payload["total"] = total
    with pytest.raises(JudgeOutputError):
        parse_judge_output(json.dumps(payload))


def test_fallback_ignores_score_like_text_in_reason() -> None:
    raw = (
        '{"correctness":3,"completeness":1,"grounding":2,'
        '"task_satisfaction":2,"total":8,'
        '"reason":"It says "correctness": 4, but that is reason text."}'
    )
    result = parse_judge_output(raw)
    assert result.correctness == 3
    assert result.parser_mode == "fallback"


def test_fallback_rejects_correctness_only_present_in_reason() -> None:
    raw = (
        '{"completeness":1,"grounding":2,"task_satisfaction":2,"total":8,'
        '"reason":"It says "correctness": 3, but the top-level field is absent."}'
    )
    with pytest.raises(JudgeOutputError, match="missing required field: correctness"):
        parse_judge_output(raw)


def test_fallback_rejects_duplicate_score_in_prefix() -> None:
    raw = (
        '{"correctness":3,"correctness":2,"completeness":1,"grounding":2,'
        '"task_satisfaction":2,"total":8,"reason":"bad "quote""}'
    )
    with pytest.raises(JudgeOutputError, match="duplicate JSON key"):
        parse_judge_output(raw)


@pytest.mark.parametrize(
    "raw",
    (
        # Missing total.
        (
            '{"correctness":3,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"reason":"bad "quote""}'
        ),
        # Duplicate correctness.
        (
            '{"correctness":3,"correctness":2,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":8,"reason":"bad "quote""}'
        ),
        # Non-integer component.
        (
            '{"correctness":3.0,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":8,"reason":"bad "quote""}'
        ),
        # Out-of-range component.
        (
            '{"correctness":5,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":10,"reason":"bad "quote""}'
        ),
        # Out-of-range total; strict behavior otherwise recomputes total from components.
        (
            '{"correctness":3,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":11,"reason":"bad "quote""}'
        ),
        # In-range total inconsistent with component sum.
        (
            '{"correctness":3,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":7,"reason":"bad "quote""}'
        ),
        # Malformed integer token.
        (
            '{"correctness":3x,"completeness":1,"grounding":2,'
            '"task_satisfaction":2,"total":8,"reason":"bad "quote""}'
        ),
    ),
    ids=(
        "missing-score",
        "duplicate-score",
        "non-integer-score",
        "out-of-range-score",
        "out-of-range-total",
        "inconsistent-total",
        "malformed-score",
    ),
)
def test_malformed_json_fallback_rejects_ambiguous_scores(raw: str) -> None:
    with pytest.raises(JudgeOutputError):
        parse_judge_output(raw)


def test_escape_repair_preserves_every_valid_json_escape() -> None:
    valid = (
        r'{"correctness":4,"completeness":2,"grounding":2,'
        r'"task_satisfaction":2,"total":10,"reason":"line1\nline2\tvalue\r'
        r' quote=\" slash=\/ backslash=\\ unicode=\u1234"}'
    )
    assert repair_invalid_json_escapes(valid) == valid
    result = parse_judge_output(valid)
    assert result.total == 10
    assert "line1\nline2\tvalue" in result.reason
    assert 'quote="' in result.reason
    assert "backslash=\\" in result.reason
    assert "unicode=ሴ" in result.reason


@pytest.mark.parametrize("command", (r"\sqrt", r"\frac", r"\alpha", r"\beta", r"\text", r"\mathrm"))
def test_escape_repair_handles_common_latex_commands(command: str) -> None:
    raw = (
        '{"correctness":4,"completeness":2,"grounding":2,'
        f'"task_satisfaction":2,"total":10,"reason":"formula {command}{{x}}"}}'
    )
    assert command in parse_judge_output(raw).reason


@pytest.mark.parametrize(
    "payload, message",
    (
        (
            {
                "correctness": 5,
                "completeness": 2,
                "grounding": 2,
                "task_satisfaction": 2,
                "reason": "bad range",
            },
            "correctness",
        ),
        (
            {
                "correctness": 4,
                "completeness": 1.5,
                "grounding": 2,
                "task_satisfaction": 2,
                "reason": "bad type",
            },
            "completeness",
        ),
    ),
)
def test_score_validation(payload: dict[str, object], message: str) -> None:
    with pytest.raises(JudgeOutputError, match=message):
        parse_judge_output(json.dumps(payload))


def test_failed_judge_attempt_preserves_raw_parser_provenance() -> None:
    class InvalidJudge:
        def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
            return '{"correctness":4,"reason":"incomplete"}'

    with pytest.raises(JudgeOutputError) as raised:
        judge_answer(
            InvalidJudge(),
            question="question",
            reference="reference",
            candidate="candidate",
        )
    assert raised.value.raw_response == '{"correctness":4,"reason":"incomplete"}'
    assert raised.value.parser_mode == "strict"
    assert raised.value.parser_policy_version == "judge-json-v2"
    assert raised.value.attempt_count == 2


class FakeJudge:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "correctness": 4,
                "completeness": 2,
                "grounding": 2,
                "task_satisfaction": 1,
                "total": 9,
                "reason": "good",
            }
        )


def test_summary_aggregation_and_e_a02_output() -> None:
    references = {"E_T01": "ref 1", "E_A02": "ref 2"}
    judge = FakeJudge()
    results = evaluate_quality(references, _candidate_sets(), judge, seed=42)
    summary = summarize_quality(results)

    assert len(results) == 6
    assert summary["strategies"]["llm"]["evaluated_count"] == 2
    assert summary["strategies"]["llm"]["average_correctness"] == 4.0
    assert summary["strategies"]["adaptive"]["average_quality_score"] == 9.0
    assert len(summary["e_a02"]) == 3
    assert all("strategy=" not in prompt for prompt in judge.prompts)


def test_reevaluate_one_preserves_other_rows() -> None:
    references = {"E_T01": "ref 1", "E_A02": "ref 2"}
    existing = [
        {
            "id": candidate_id,
            "question": f"question {candidate_id}",
            "category": "text",
            "strategy": strategy,
            "correctness": "",
            "completeness": "",
            "grounding": "",
            "task_satisfaction": "",
            "quality_score": "",
            "judge_reason": "",
            "judge_latency_ms": "1.0",
            "error": "old error",
        }
        for candidate_id in references
        for strategy in ("always_vision", "llm", "adaptive")
    ]

    judge = FakeJudge()
    updated = reevaluate_one(
        existing,
        references,
        _candidate_sets(),
        judge,
        candidate_id="E_T01",
        strategy="always_vision",
    )

    changed = [row for row in updated if row != existing[updated.index(row)]]
    assert len(changed) == 1
    assert changed[0]["id"] == "E_T01"
    assert changed[0]["strategy"] == "always_vision"
    assert changed[0]["quality_score"] == 9
    assert len(judge.prompts) == 1


class FencedJudge:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        self.calls += 1
        return r'''```json
{
  "correctness": 4,
  "completeness": 2,
  "grounding": 2,
  "task_satisfaction": 2,
  "total": 10,
  "reason": "Scaling by $1/\sqrt{d_k}$ prevents saturation and {keeps gradients useful}."
}
```'''


def test_fenced_hosted_response_flows_through_single_row_reevaluation() -> None:
    references = {"E_T02": "reference"}
    candidates = {
        strategy: [
            {
                "id": "E_T02",
                "question": "왜 sqrt(d_k)로 나누는가?",
                "category": "text",
                "answer": f"candidate {strategy}",
                "strategy": strategy,
            }
        ]
        for strategy in ("always_vision", "llm", "adaptive")
    }
    existing = [
        {
            "id": "E_T02",
            "question": "왜 sqrt(d_k)로 나누는가?",
            "category": "text",
            "strategy": strategy,
            "correctness": "",
            "completeness": "",
            "grounding": "",
            "task_satisfaction": "",
            "quality_score": "",
            "judge_reason": "",
            "judge_latency_ms": "1.0",
            "error": "old parsing error",
        }
        for strategy in ("always_vision", "llm", "adaptive")
    ]
    judge = FencedJudge()

    updated = reevaluate_one(
        existing,
        references,
        candidates,
        judge,
        candidate_id="E_T02",
        strategy="always_vision",
    )

    target = next(row for row in updated if row["strategy"] == "always_vision")
    untouched = [row for row in updated if row["strategy"] != "always_vision"]
    assert target["quality_score"] == 10
    assert target["judge_reason"] == (
        "Scaling by $1/\\sqrt{d_k}$ prevents saturation and "
        "{keeps gradients useful}."
    )
    assert target["error"] == ""
    assert untouched == existing[1:]
    assert judge.calls == 1


def test_partial_reevaluation_preserves_44_rows_and_recomputes_summary() -> None:
    ids = [f"E_T{index:02d}" for index in range(1, 16)]
    references = {candidate_id: "reference" for candidate_id in ids}
    candidates = {
        strategy: [
            {
                "id": candidate_id,
                "question": f"question {candidate_id}",
                "category": "text",
                "answer": f"answer {strategy}",
                "strategy": strategy,
            }
            for candidate_id in ids
        ]
        for strategy in ("always_vision", "llm", "adaptive")
    }
    existing = [
        {
            "id": candidate_id,
            "question": f"question {candidate_id}",
            "category": "text",
            "strategy": strategy,
            "correctness": "4",
            "completeness": "2",
            "grounding": "2",
            "task_satisfaction": "2",
            "quality_score": "10",
            "judge_reason": "existing",
            "judge_latency_ms": "1.0",
            "error": "old parsing error" if (
                candidate_id == "E_T02" and strategy == "always_vision"
            ) else "",
        }
        for candidate_id in ids
        for strategy in ("always_vision", "llm", "adaptive")
    ]
    judge = FencedJudge()

    updated = reevaluate_one(
        existing,
        references,
        candidates,
        judge,
        candidate_id="E_T02",
        strategy="always_vision",
    )

    changed_indices = [
        index for index, (before, after) in enumerate(zip(existing, updated, strict=True))
        if before != after
    ]
    assert changed_indices == [3]
    assert updated[3]["error"] == ""
    assert judge.calls == 1
    assert summarize_quality(updated)["strategies"]["always_vision"][
        "evaluated_count"
    ] == 15
