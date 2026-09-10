"""Blind LLM-as-judge evaluation for saved E2E benchmark answers."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.llm import FuriosaLlm

STRATEGIES = ("always_vision", "llm", "adaptive")
CANDIDATE_FIELDS = {"id", "question", "category", "answer"}
OUTPUT_FIELDS = [
    "id", "question", "category", "strategy", "correctness", "completeness",
    "grounding", "task_satisfaction", "quality_score", "judge_reason",
    "judge_raw_response", "judge_parser_mode", "judge_parser_policy_version",
    "judge_attempt_count", "judge_latency_ms", "error",
]

JUDGE_PARSER_POLICY_VERSION = "judge-json-v2"

JUDGE_PROMPT = """You are a strict answer-quality judge for questions about the
Transformer paper "Attention Is All You Need".

Evaluate the candidate answer using this rubric:
Correctness (0-4): 4 fully correct with no meaningful factual errors; 3 mostly
correct with a minor error or imprecision; 2 partially correct with important
information missing or inaccurate; 1 mostly incorrect with a small correct
element; 0 incorrect or does not answer.
Completeness (0-2): 2 covers all essential points; 1 covers the main idea but
misses an important part; 0 misses major parts.
Grounding (0-2): 2 consistent with the reference and Transformer paper; 1 mostly
grounded but includes an unsupported or unnecessary claim; 0 substantially
contradicts the reference or is unsupported.
Task Satisfaction (0-2): 2 directly answers the exact requested relationship,
comparison, flow, or explanation; 1 generally answers but is indirect or
incomplete; 0 fails the requested task.

Rules:
- You are not given strategy, route, vision usage, or latency information.
- Do not raise or lower a score based on whether Vision was used.
- Do not reward an answer merely for mentioning a figure, table, or image.
- Judge only how accurately the final answer answers the question.
- Accept semantically correct wording that differs from the reference.
- Return strict valid JSON only, without markdown fences or extra text.
- Escape every backslash inside JSON string values according to JSON syntax.
- Avoid LaTeX commands in the reason field; explain formulas in plain language.

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


class JudgeBackend(Protocol):
    def generate(self, prompt: str, *, max_tokens: int = 512) -> str: ...


class JudgeOutputError(ValueError):
    """Raised when judge output cannot be parsed or validated."""

    raw_response: str = ""
    parser_mode: str = ""
    parser_policy_version: str = ""
    attempt_count: int = 0


SCORE_LIMITS = {
    "correctness": (0, 4),
    "completeness": (0, 2),
    "grounding": (0, 2),
    "task_satisfaction": (0, 2),
}


@dataclass(frozen=True, slots=True)
class JudgeScore:
    correctness: int
    completeness: int
    grounding: int
    task_satisfaction: int
    total: int
    reason: str
    raw_response: str = ""
    parser_mode: str = ""
    parser_policy_version: str = JUDGE_PARSER_POLICY_VERSION
    attempt_count: int = 1


class _StrictJsonSyntaxError(JudgeOutputError):
    """Strict JSON decoding failed in a way eligible for the narrow fallback."""


class _DuplicateJsonKeyError(JudgeOutputError):
    """A JSON object contained a duplicate key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def load_references(path: str | Path) -> dict[str, str]:
    references: dict[str, str] = {}
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError(  # noqa: TRY004 - malformed file content, not API misuse
                    f"invalid reference id on line {line_number}"
                )
            if not isinstance(item.get("reference"), str) or not item["reference"].strip():
                raise ValueError(f"invalid reference text on line {line_number}")
            if item["id"] in references:
                raise ValueError(f"duplicate reference id: {item['id']}")
            references[item["id"]] = item["reference"]
    if not references:
        raise ValueError("reference dataset is empty")
    return references


def load_candidates(path: str | Path, strategy: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with Path(path).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        missing = CANDIDATE_FIELDS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"candidate CSV is missing fields: {', '.join(sorted(missing))}")
        for item in reader:
            candidate_id = item["id"]
            if candidate_id in seen:
                raise ValueError(f"duplicate candidate id for {strategy}: {candidate_id}")
            seen.add(candidate_id)
            rows.append(
                {
                    "id": candidate_id,
                    "question": item["question"],
                    "category": item["category"],
                    "answer": item["answer"],
                    "strategy": strategy,
                }
            )
    return rows


def shuffled_candidates(
    references: dict[str, str],
    candidates: dict[str, list[dict[str, str]]],
    *,
    seed: int,
) -> list[dict[str, str]]:
    by_strategy = {
        strategy: {row["id"]: row for row in rows}
        for strategy, rows in candidates.items()
    }
    expected_ids = set(references)
    for strategy in STRATEGIES:
        actual_ids = set(by_strategy.get(strategy, {}))
        if actual_ids != expected_ids:
            missing = sorted(expected_ids - actual_ids)
            extra = sorted(actual_ids - expected_ids)
            raise ValueError(f"{strategy} candidate IDs differ: missing={missing}, extra={extra}")

    rng = random.Random(seed)
    ordered: list[dict[str, str]] = []
    for candidate_id in references:
        group = [by_strategy[strategy][candidate_id] for strategy in STRATEGIES]
        rng.shuffle(group)
        ordered.extend(group)
    return ordered


def repair_invalid_json_escapes(text: str) -> str:
    """Escape only invalid backslashes occurring inside JSON string values."""
    repaired: list[str] = []
    in_string = False
    index = 0
    valid_simple_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t"}
    latex_commands = ("\\sqrt", "\\frac", "\\alpha", "\\beta", "\\text", "\\mathrm")
    hex_digits = set("0123456789abcdefABCDEF")
    while index < len(text):
        char = text[index]
        if char == '"':
            in_string = not in_string
            repaired.append(char)
            index += 1
            continue
        if in_string and char == "\\":
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if any(text.startswith(command, index) for command in latex_commands):
                repaired.append("\\\\")
                index += 1
                continue
            if next_char in valid_simple_escapes:
                repaired.extend((char, next_char))
                index += 2
                continue
            if (
                next_char == "u"
                and index + 5 < len(text)
                and all(value in hex_digits for value in text[index + 2 : index + 6])
            ):
                repaired.append(text[index : index + 6])
                index += 6
                continue
            repaired.append("\\\\")
            index += 1
            continue
        repaired.append(char)
        index += 1
    return "".join(repaired)


def _json_object_candidates(text: str) -> list[str]:
    """Extract balanced JSON object candidates without regex-greedy brace matching."""
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"' and start is not None:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : index + 1])
                start = None
        index += 1
    return candidates


def _json_error_detail(exc: json.JSONDecodeError) -> str:
    return (
        f"{exc.msg} at pos={exc.pos}, line={exc.lineno}, column={exc.colno}"
    )


def _load_judge_json(text: str, raw: str) -> dict[str, Any]:
    errors: list[json.JSONDecodeError] = []
    parse_targets = [text, *_json_object_candidates(text)]
    for candidate in dict.fromkeys(parse_targets):
        try:
            payload = json.loads(candidate, object_pairs_hook=_reject_duplicate_keys)
        except _DuplicateJsonKeyError:
            raise
        except json.JSONDecodeError as exc:
            errors.append(exc)
            continue
        if isinstance(payload, dict):
            repaired = repair_invalid_json_escapes(candidate)
            if repaired != candidate:
                repaired_payload = json.loads(
                    repaired, object_pairs_hook=_reject_duplicate_keys
                )
                if isinstance(repaired_payload, dict):
                    return repaired_payload
            return payload

    detail = _json_error_detail(errors[-1]) if errors else "no JSON object found"
    raise _StrictJsonSyntaxError(f"invalid judge JSON ({detail}): {raw!r}")


def _top_level_reason_bounds(text: str) -> tuple[int, int]:
    """Return the first top-level reason key start and value start."""
    object_start = text.find("{")
    if object_start < 0:
        raise JudgeOutputError("fallback requires a top-level JSON object")
    depth = 0
    index = object_start
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth -= 1
            index += 1
            continue
        if char != '"':
            index += 1
            continue

        key_start = index
        index += 1
        escaped = False
        while index < len(text):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                break
            index += 1
        if index >= len(text):
            break
        token = text[key_start : index + 1]
        after = index + 1
        while after < len(text) and text[after].isspace():
            after += 1
        if depth == 1 and after < len(text) and text[after] == ":":
            try:
                key = json.loads(token)
            except json.JSONDecodeError:
                key = None
            if key == "reason":
                return key_start, after + 1
        index += 1
    raise JudgeOutputError("fallback requires one top-level reason field")


def _fallback_judge_json(text: str) -> dict[str, Any]:
    """Recover a valid score prefix while treating malformed reason text as opaque."""
    repaired = repair_invalid_json_escapes(text)
    if repaired != text:
        try:
            repaired_payload = json.loads(
                repaired, object_pairs_hook=_reject_duplicate_keys
            )
        except _DuplicateJsonKeyError:
            raise
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(repaired_payload, dict):
                return repaired_payload

    reason_start, reason_value_start = _top_level_reason_bounds(text)
    object_start = text.find("{")
    score_prefix = text[object_start:reason_start]
    synthetic = score_prefix + '"reason":"fallback reason"}'
    try:
        payload = json.loads(synthetic, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKeyError:
        raise
    except json.JSONDecodeError as exc:
        raise JudgeOutputError(
            f"fallback score prefix is not valid JSON ({_json_error_detail(exc)})"
        ) from exc
    if not isinstance(payload, dict):
        raise JudgeOutputError("fallback score prefix must be a JSON object")

    reason = text[reason_value_start:].strip()
    reason = re.sub(r"\s*}\s*$", "", reason).strip()
    reason = reason.removeprefix('"').removesuffix('"').strip()
    payload["reason"] = (
        f"[fallback: malformed judge JSON] {reason}"
        if reason
        else "[fallback: malformed judge JSON; reason unavailable]"
    )
    return payload


def _validate_judge_payload(
    payload: dict[str, Any], *, raw: str, parser_mode: str
) -> JudgeScore:
    limits = {
        **SCORE_LIMITS,
        "total": (0, sum(limit[1] for limit in SCORE_LIMITS.values())),
    }
    scores: dict[str, int] = {}
    for field, (minimum, maximum) in limits.items():
        if field not in payload:
            raise JudgeOutputError(f"missing required field: {field}")
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise JudgeOutputError(f"{field} must be an integer")
        if not minimum <= value <= maximum:
            raise JudgeOutputError(f"{field} must be between {minimum} and {maximum}")
        scores[field] = value
    component_total = sum(scores[field] for field in SCORE_LIMITS)
    if scores["total"] != component_total:
        raise JudgeOutputError("total does not match component scores")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JudgeOutputError("reason must be a non-empty string")
    return JudgeScore(
        correctness=scores["correctness"],
        completeness=scores["completeness"],
        grounding=scores["grounding"],
        task_satisfaction=scores["task_satisfaction"],
        total=scores["total"],
        reason=reason,
        raw_response=raw,
        parser_mode=parser_mode,
    )


def parse_judge_output(raw: str) -> JudgeScore:
    stripped = raw.strip()
    fenced = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = _load_judge_json(stripped, raw)
    except _DuplicateJsonKeyError as strict_error:
        strict_error.raw_response = raw
        strict_error.parser_mode = "strict"
        strict_error.parser_policy_version = JUDGE_PARSER_POLICY_VERSION
        raise
    except _StrictJsonSyntaxError as strict_error:
        try:
            payload = _fallback_judge_json(stripped)
        except JudgeOutputError as fallback_error:
            fallback_error.raw_response = raw
            fallback_error.parser_mode = "fallback"
            fallback_error.parser_policy_version = JUDGE_PARSER_POLICY_VERSION
            raise fallback_error from strict_error
        try:
            return _validate_judge_payload(payload, raw=raw, parser_mode="fallback")
        except JudgeOutputError as fallback_error:
            fallback_error.raw_response = raw
            fallback_error.parser_mode = "fallback"
            fallback_error.parser_policy_version = JUDGE_PARSER_POLICY_VERSION
            raise
    try:
        return _validate_judge_payload(payload, raw=raw, parser_mode="strict")
    except JudgeOutputError as strict_error:
        strict_error.raw_response = raw
        strict_error.parser_mode = "strict"
        strict_error.parser_policy_version = JUDGE_PARSER_POLICY_VERSION
        raise


def judge_answer(
    backend: JudgeBackend,
    *,
    question: str,
    reference: str,
    candidate: str,
    prompt_template: str = JUDGE_PROMPT,
) -> JudgeScore:
    prompt = prompt_template.format(
        question=question,
        reference=reference,
        candidate=candidate,
    )
    last_error: JudgeOutputError | None = None
    for attempt in range(1, 3):
        raw = backend.generate(prompt, max_tokens=512)
        try:
            return replace(parse_judge_output(raw), attempt_count=attempt)
        except JudgeOutputError as exc:
            exc.attempt_count = attempt
            last_error = exc
    assert last_error is not None
    raise last_error


def evaluate_quality(
    references: dict[str, str],
    candidates: dict[str, list[dict[str, str]]],
    backend: JudgeBackend,
    *,
    seed: int = 42,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate in shuffled_candidates(references, candidates, seed=seed):
        started = time.perf_counter_ns()
        result: dict[str, Any] = {
            **candidate,
            "correctness": "",
            "completeness": "",
            "grounding": "",
            "task_satisfaction": "",
            "quality_score": "",
            "judge_reason": "",
            "judge_raw_response": "",
            "judge_parser_mode": "",
            "judge_parser_policy_version": JUDGE_PARSER_POLICY_VERSION,
            "judge_attempt_count": "",
            "error": "",
        }
        result.pop("answer")
        try:
            score = judge_answer(
                backend,
                question=candidate["question"],
                reference=references[candidate["id"]],
                candidate=candidate["answer"],
            )
            result.update(
                correctness=score.correctness,
                completeness=score.completeness,
                grounding=score.grounding,
                task_satisfaction=score.task_satisfaction,
                quality_score=score.total,
                judge_reason=score.reason,
                judge_raw_response=score.raw_response,
                judge_parser_mode=score.parser_mode,
                judge_parser_policy_version=score.parser_policy_version,
                judge_attempt_count=score.attempt_count,
            )
        except Exception as exc:  # noqa: BLE001 - preserve individual judge failures
            if isinstance(exc, JudgeOutputError):
                result.update(
                    judge_raw_response=exc.raw_response,
                    judge_parser_mode=exc.parser_mode,
                    judge_parser_policy_version=exc.parser_policy_version,
                    judge_attempt_count=exc.attempt_count,
                )
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["judge_latency_ms"] = (time.perf_counter_ns() - started) / 1_000_000
        results.append(result)
    return results


def summarize_quality(results: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "correctness", "completeness", "grounding", "task_satisfaction", "quality_score"
    )

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, int | float]:
        evaluated = [row for row in rows if not row["error"]]
        summary: dict[str, int | float] = {"evaluated_count": len(evaluated)}
        for field in fields:
            summary[f"average_{field}"] = (
                sum(float(row[field]) for row in evaluated) / len(evaluated)
                if evaluated
                else 0.0
            )
        return summary

    strategy_summary: dict[str, Any] = {}
    for strategy in STRATEGIES:
        rows = [row for row in results if row["strategy"] == strategy]
        strategy_summary[strategy] = aggregate(rows)
        strategy_summary[strategy]["categories"] = {
            category: aggregate([row for row in rows if row["category"] == category])[
                "average_quality_score"
            ]
            for category in ("text", "explicit_visual", "implicit_visual")
        }
    return {
        "strategies": strategy_summary,
        "e_a02": [row for row in results if row["id"] == "E_A02"],
    }


def export_results(results: list[dict[str, Any]], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(results)


def load_existing_results(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        provenance_fields = {
            "judge_raw_response",
            "judge_parser_mode",
            "judge_parser_policy_version",
            "judge_attempt_count",
        }
        missing = set(OUTPUT_FIELDS) - provenance_fields - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"existing output is missing fields: {', '.join(sorted(missing))}")
        return [{field: row.get(field, "") for field in OUTPUT_FIELDS} for row in reader]


def reevaluate_one(
    existing_results: list[dict[str, Any]],
    references: dict[str, str],
    candidates: dict[str, list[dict[str, str]]],
    backend: JudgeBackend,
    *,
    candidate_id: str,
    strategy: str,
) -> list[dict[str, Any]]:
    matches = [
        (index, row)
        for index, row in enumerate(existing_results)
        if row["id"] == candidate_id and row["strategy"] == strategy
    ]
    if len(matches) != 1:
        raise ValueError(
            f"existing output must contain exactly one row for {candidate_id}/{strategy}"
        )
    candidate_matches = [
        row for row in candidates[strategy] if row["id"] == candidate_id
    ]
    if len(candidate_matches) != 1 or candidate_id not in references:
        raise ValueError(f"candidate/reference not found for {candidate_id}/{strategy}")

    candidate = candidate_matches[0]
    started = time.perf_counter_ns()
    replacement_row: dict[str, Any] = {
        "id": candidate["id"],
        "question": candidate["question"],
        "category": candidate["category"],
        "strategy": strategy,
        "correctness": "",
        "completeness": "",
        "grounding": "",
        "task_satisfaction": "",
        "quality_score": "",
        "judge_reason": "",
        "judge_raw_response": "",
        "judge_parser_mode": "",
        "judge_parser_policy_version": JUDGE_PARSER_POLICY_VERSION,
        "judge_attempt_count": "",
        "error": "",
    }
    try:
        score = judge_answer(
            backend,
            question=candidate["question"],
            reference=references[candidate_id],
            candidate=candidate["answer"],
        )
        replacement_row.update(
            correctness=score.correctness,
            completeness=score.completeness,
            grounding=score.grounding,
            task_satisfaction=score.task_satisfaction,
            quality_score=score.total,
            judge_reason=score.reason,
            judge_raw_response=score.raw_response,
            judge_parser_mode=score.parser_mode,
            judge_parser_policy_version=score.parser_policy_version,
            judge_attempt_count=score.attempt_count,
        )
    except Exception as exc:  # noqa: BLE001 - preserve individual judge failures
        if isinstance(exc, JudgeOutputError):
            replacement_row.update(
                judge_raw_response=exc.raw_response,
                judge_parser_mode=exc.parser_mode,
                judge_parser_policy_version=exc.parser_policy_version,
                judge_attempt_count=exc.attempt_count,
            )
        replacement_row["error"] = f"{type(exc).__name__}: {exc}"
    replacement_row["judge_latency_ms"] = (
        time.perf_counter_ns() - started
    ) / 1_000_000
    updated = list(existing_results)
    updated[matches[0][0]] = replacement_row
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Blind answer-quality evaluation")
    parser.add_argument("--references", required=True)
    parser.add_argument("--always-vision", required=True)
    parser.add_argument("--llm", required=True)
    parser.add_argument("--adaptive", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--only-id")
    parser.add_argument("--only-strategy", choices=STRATEGIES)
    args = parser.parse_args()

    if bool(args.only_id) != bool(args.only_strategy):
        parser.error("--only-id and --only-strategy must be provided together")

    references = load_references(args.references)
    candidates = {
        "always_vision": load_candidates(args.always_vision, "always_vision"),
        "llm": load_candidates(args.llm, "llm"),
        "adaptive": load_candidates(args.adaptive, "adaptive"),
    }
    settings = Settings.from_env()
    endpoint = next(item for item in settings.endpoints if item.name == "llm")
    backend = FuriosaLlm(
        endpoint,
        FuriosaClient(settings.api_key, settings.request_timeout),
    )
    if args.only_id:
        results = reevaluate_one(
            load_existing_results(args.output),
            references,
            candidates,
            backend,
            candidate_id=args.only_id,
            strategy=args.only_strategy,
        )
    else:
        results = evaluate_quality(references, candidates, backend, seed=args.seed)
    export_results(results, args.output)

    summary = summarize_quality(results)
    for strategy, values in summary["strategies"].items():
        print(f"\nstrategy={strategy}")
        for key, value in values.items():
            if key != "categories":
                print(f"{key}={value}")
        for category, average in values["categories"].items():
            print(f"average_quality_score_{category}={average}")
    print("\nE_A02")
    for row in summary["e_a02"]:
        print(
            f"strategy={row['strategy']} quality_score={row['quality_score']} "
            f"error={row['error']}"
        )
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
