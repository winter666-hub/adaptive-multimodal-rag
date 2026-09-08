import csv
import json
from pathlib import Path

import pytest

from furiosa_rag.benchmark_checkpoint import (
    BenchmarkFingerprint,
    append_checkpoint,
    build_benchmark_fingerprint,
    latest_checkpoint_records,
    load_checkpoint,
    materialize_checkpoint_csv,
    plan_resume,
    validate_checkpoint_fingerprint,
)


def fingerprint(value: str = "fingerprint") -> BenchmarkFingerprint:
    return BenchmarkFingerprint(value, {"strategy": "adaptive", "rag": {"top_n": 3}})


def result(query_id: str, *, error: str = "", answer: str = "answer") -> dict[str, object]:
    return {
        "id": query_id,
        "query_id": query_id,
        "strategy": "adaptive",
        "answer": answer,
        "error": error,
        "expected_pages": [3],
        "sources": "[]",
    }


def test_fresh_append_and_reload(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    append_checkpoint(checkpoint, result("Q1"), fingerprint())
    records = load_checkpoint(checkpoint)
    assert len(records) == 1
    assert records[0]["query_id"] == "Q1"
    assert records[0]["benchmark_fingerprint"] == "fingerprint"


def test_resume_skips_success_and_error_by_default() -> None:
    latest = {
        ("Q1", "adaptive"): result("Q1"),
        ("Q2", "adaptive"): result("Q2", error="timeout"),
    }
    rows = [{"id": "Q1"}, {"id": "Q2"}, {"id": "Q3"}]
    plan = plan_resume(rows, strategy="adaptive", latest=latest, retry_errors=False)
    assert [row["id"] for row in plan.pending_rows] == ["Q3"]
    assert plan.skipped_successes == 1
    assert plan.skipped_errors == 1
    assert plan.retrying_errors == 0


def test_retry_errors_reruns_only_error_records() -> None:
    latest = {
        ("Q1", "adaptive"): result("Q1"),
        ("Q2", "adaptive"): result("Q2", error="timeout"),
    }
    rows = [{"id": "Q1"}, {"id": "Q2"}]
    plan = plan_resume(rows, strategy="adaptive", latest=latest, retry_errors=True)
    assert [row["id"] for row in plan.pending_rows] == ["Q2"]
    assert plan.skipped_successes == 1
    assert plan.retrying_errors == 1


@pytest.mark.parametrize(
    ("history", "expected_error"),
    (
        ([result("Q1", error="failed"), result("Q1", answer="recovered")], ""),
        ([result("Q1"), result("Q1", error="new failure")], "new failure"),
    ),
)
def test_latest_duplicate_record_is_authoritative(
    history: list[dict[str, object]], expected_error: str
) -> None:
    latest = latest_checkpoint_records(history)
    assert latest[("Q1", "adaptive")]["error"] == expected_error


def test_truncated_final_line_is_ignored_and_can_be_repaired(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    append_checkpoint(checkpoint, result("Q1"), fingerprint())
    with checkpoint.open("a", encoding="utf-8") as output:
        output.write('{"query_id":"Q2"')

    with pytest.warns(UserWarning, match="malformed final checkpoint line"):
        records = load_checkpoint(checkpoint, repair_truncated_final_line=True)
    assert [record["query_id"] for record in records] == ["Q1"]

    append_checkpoint(checkpoint, result("Q2"), fingerprint())
    assert [record["query_id"] for record in load_checkpoint(checkpoint)] == ["Q1", "Q2"]


def test_invalid_utf8_in_final_line_does_not_hide_previous_records(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    append_checkpoint(checkpoint, result("Q1"), fingerprint())
    with checkpoint.open("ab") as output:
        output.write(b'{"query_id":"Q2","answer":"\xed\x95')

    with pytest.warns(UserWarning, match="malformed final checkpoint line"):
        records = load_checkpoint(checkpoint)
    assert [record["query_id"] for record in records] == ["Q1"]


def test_malformed_middle_line_is_rejected(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    valid = {
        **result("Q1"),
        "benchmark_fingerprint": "fingerprint",
        "fingerprint_config": {},
    }
    checkpoint.write_text(
        json.dumps(valid) + "\nnot-json\n" + json.dumps({**valid, "query_id": "Q2"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed checkpoint line 2"):
        load_checkpoint(checkpoint)


def fingerprint_args(dataset: Path, pdf: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "dataset_path": dataset,
        "strategy": "adaptive",
        "embedding_model": "embedding-v1",
        "reranker_model": "reranker-v1",
        "router_llm_model": "router-v1",
        "final_llm_model": "llm-v1",
        "vision_model": "vision-v1",
        "chunk_size": 700,
        "chunk_overlap": 100,
        "top_k": 5,
        "top_n": 3,
        "vision_dpi": 144.0,
        "pdf_path": pdf,
    }
    values.update(overrides)
    return values


def test_fingerprint_is_stable_and_contains_no_secret(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("dataset", encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    first = build_benchmark_fingerprint(**fingerprint_args(dataset, pdf))  # type: ignore[arg-type]
    second = build_benchmark_fingerprint(**fingerprint_args(dataset, pdf))  # type: ignore[arg-type]
    assert first == second
    serialized = json.dumps(first.payload).casefold()
    assert "api_key" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    "override",
    (
        {"strategy": "llm"},
        {"top_n": 5},
        {"embedding_model": "embedding-v2"},
        {"cache_dir": "different-cache"},
        {"require_warm_cache": True},
    ),
)
def test_fingerprint_changes_with_config(tmp_path: Path, override: dict[str, object]) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("dataset", encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    baseline = build_benchmark_fingerprint(**fingerprint_args(dataset, pdf))  # type: ignore[arg-type]
    changed = build_benchmark_fingerprint(  # type: ignore[arg-type]
        **fingerprint_args(dataset, pdf, **override)
    )
    assert changed.value != baseline.value


def test_fingerprint_changes_with_dataset_content(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("first", encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    first = build_benchmark_fingerprint(**fingerprint_args(dataset, pdf))  # type: ignore[arg-type]
    dataset.write_text("second", encoding="utf-8")
    second = build_benchmark_fingerprint(**fingerprint_args(dataset, pdf))  # type: ignore[arg-type]
    assert first.value != second.value


def test_mismatched_fingerprint_is_rejected_with_changed_category() -> None:
    record = {
        **result("Q1"),
        "benchmark_fingerprint": "old",
        "fingerprint_config": {"strategy": "llm", "rag": {"top_n": 3}},
    }
    with pytest.raises(ValueError, match="strategy"):
        validate_checkpoint_fingerprint([record], fingerprint("new"))


def test_materialization_keeps_latest_retry_and_existing_columns(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    append_checkpoint(checkpoint, result("Q1", error="failed", answer=""), fingerprint())
    append_checkpoint(checkpoint, result("Q1", answer="recovered"), fingerprint())
    output = tmp_path / "results.csv"

    rows = materialize_checkpoint_csv(checkpoint, output, fingerprint=fingerprint())

    assert len(rows) == 1
    with output.open(encoding="utf-8", newline="") as source:
        exported = list(csv.DictReader(source))
    assert len(exported) == 1
    assert exported[0]["id"] == "Q1"
    assert exported[0]["answer"] == "recovered"
    assert json.loads(exported[0]["expected_pages"]) == [3]
    assert "route" in exported[0]
