from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from furiosa_rag.cli.benchmark_e2e import _benchmark_inputs, _clients
from furiosa_rag.config import Settings
from furiosa_rag.e2e_benchmark import (
    IncompletePrewarmError,
    export_e2e_csv,
    load_e2e_jsonl,
    prewarm_documents,
    require_complete_prewarm,
    resolve_pdf_path,
    run_e2e,
    summarize_e2e,
)
from furiosa_rag.models import (
    Chunk,
    MultimodalRagAnswer,
    RagAnswer,
    RetrievalAwareRagAnswer,
    RetrievedChunk,
    VisionUsage,
)
from furiosa_rag.pipeline import DocumentPreparation
from furiosa_rag.router import QueryRoute, RoutingDecision


def _source() -> RetrievedChunk:
    return RetrievedChunk(Chunk("page-3-chunk-1", 3, "evidence"), 0.8, 0.9)


def _source_on(page: int, chunk: int = 1) -> RetrievedChunk:
    return RetrievedChunk(Chunk(f"page-{page}-chunk-{chunk}", page, "evidence"), 0.8, 0.9)


def _text_result() -> RagAnswer:
    return RagAnswer(
        "text answer",
        (_source(),),
        {
            "cache_hit": True,
            "query_embedding": 2.0,
            "reranking": 3.0,
            "answer_generation": 5.0,
            "total": 10.0,
        },
    )


def _visual_result() -> MultimodalRagAnswer:
    return MultimodalRagAnswer(
        "visual answer",
        (_source(),),
        VisionUsage(3, True, "fake-vision"),
        {
            "cache_hit": False,
            "query_embedding": 2.0,
            "reranking": 3.0,
            "page_rendering": 4.0,
            "vision_analysis": 8.0,
            "answer_generation": 5.0,
            "total": 22.0,
        },
    )


def _rows() -> list[dict[str, str]]:
    return [
        {"id": "Q1", "question": "text q", "category": "text"},
        {"id": "Q2", "question": "visual q", "category": "implicit_visual"},
    ]


def test_always_vision_uses_multimodal_for_every_question() -> None:
    text_pipeline = Mock()
    multimodal_pipeline = Mock()
    multimodal_pipeline.answer_multimodal.return_value = _visual_result()

    results = run_e2e(
        _rows(), "paper.pdf", strategy="always_vision",
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )

    assert multimodal_pipeline.answer_multimodal.call_count == 2
    text_pipeline.answer.assert_not_called()
    assert all(row["vision_used"] for row in results)


def test_forced_text_never_calls_vision() -> None:
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()
    multimodal_pipeline = Mock()

    results = run_e2e(
        _rows(), "paper.pdf", strategy="forced_text",
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )

    assert text_pipeline.answer.call_count == 2
    multimodal_pipeline.answer_multimodal.assert_not_called()
    assert all(row["route"] == "TEXT_ONLY" for row in results)


def test_forced_vision_calls_current_multimodal_branch() -> None:
    text_pipeline = Mock()
    multimodal_pipeline = Mock()
    multimodal_pipeline.answer_multimodal.return_value = _visual_result()

    results = run_e2e(
        _rows(), "paper.pdf", strategy="forced_vision",
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )

    assert multimodal_pipeline.answer_multimodal.call_count == 2
    text_pipeline.answer.assert_not_called()
    assert all(row["selected_page"] == 3 for row in results)


def test_retrieval_aware_strategy_records_route_vision_and_metadata() -> None:
    retrieval_pipeline = Mock()
    retrieval_pipeline.answer_retrieval_aware.return_value = RetrievalAwareRagAnswer(
        answer="retrieval-aware answer",
        sources=(_source(),),
        vision=VisionUsage(3, True, "fake-vision"),
        latency_ms={
            "cache_hit": True,
            "query_embedding": 2.0,
            "reranking": 3.0,
            "vision_analysis": 8.0,
            "answer_generation": 5.0,
        },
        route="VISUAL_REQUIRED",
        routing_reason="evidence insufficient",
        used_llm_router=True,
        routing_latency_ms=4.5,
        router_evidence_chunk_count=1,
        router_evidence_pages=(3,),
        router_prompt_version="retrieval-aware-router-v1",
        router_prompt_sha256="abc",
    )
    item = {
        "id": "Q1",
        "question": "question",
        "gold_answer": "gold",
        "answer_type": "image_only",
    }

    result = run_e2e(
        [item],
        "paper.pdf",
        strategy="retrieval_aware_adaptive",
        text_pipeline=Mock(),
        multimodal_pipeline=Mock(),
        retrieval_aware_pipeline=retrieval_pipeline,
    )[0]

    assert result["predicted_route"] == "VISUAL_REQUIRED"
    assert result["routing_latency_ms"] == 4.5
    assert result["vision_used"] is True
    assert result["router_evidence_chunk_count"] == 1
    assert result["router_evidence_pages"] == [3]
    assert result["router_prompt_version"] == "retrieval-aware-router-v1"
    assert result["gold_answer"] == "gold"


@pytest.mark.parametrize("strategy", ("llm", "adaptive"))
def test_routed_strategy_uses_text_pipeline_for_text_route(strategy: str) -> None:
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "router text")
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()
    multimodal_pipeline = Mock()

    result = run_e2e(
        _rows()[:1], "paper.pdf", strategy=strategy, router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )[0]

    text_pipeline.answer.assert_called_once()
    multimodal_pipeline.answer_multimodal.assert_not_called()
    assert result["route"] == "TEXT_ONLY"
    assert result["vision_used"] is False


@pytest.mark.parametrize("strategy", ("llm", "adaptive"))
def test_routed_strategy_uses_multimodal_for_visual_route(strategy: str) -> None:
    router = Mock()
    router.route.return_value = RoutingDecision(
        QueryRoute.VISUAL_REQUIRED, "router visual"
    )
    text_pipeline = Mock()
    multimodal_pipeline = Mock()
    multimodal_pipeline.answer_multimodal.return_value = _visual_result()

    result = run_e2e(
        _rows()[1:], "paper.pdf", strategy=strategy, router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )[0]

    multimodal_pipeline.answer_multimodal.assert_called_once()
    text_pipeline.answer.assert_not_called()
    assert result["route"] == "VISUAL_REQUIRED"
    assert result["selected_page"] == 3


def test_summary_calculates_vision_calls_and_latencies() -> None:
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()
    multimodal_pipeline = Mock()
    multimodal_pipeline.answer_multimodal.return_value = _visual_result()
    router = Mock()
    router.route.side_effect = [
        RoutingDecision(QueryRoute.TEXT_ONLY, "text"),
        RoutingDecision(QueryRoute.VISUAL_REQUIRED, "visual"),
    ]
    results = run_e2e(
        _rows(), "paper.pdf", strategy="llm", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=multimodal_pipeline,
    )

    summary = summarize_e2e(results)

    assert summary["total_questions"] == 2
    assert summary["vision_calls"] == 1
    assert summary["vision_call_rate"] == 0.5
    assert summary["average_vision_latency_ms"] == 8.0
    assert float(summary["average_total_latency_ms"]) >= 0


def test_csv_export_contains_required_fields(tmp_path: Path) -> None:
    pipeline = Mock()
    pipeline.answer_multimodal.return_value = _visual_result()
    results = run_e2e(
        _rows()[:1], "paper.pdf", strategy="always_vision",
        text_pipeline=Mock(), multimodal_pipeline=pipeline,
    )
    output = tmp_path / "nested" / "e2e.csv"

    export_e2e_csv(results, output)

    with output.open(encoding="utf-8", newline="") as source:
        row = next(csv.DictReader(source))
    assert row["strategy"] == "always_vision"
    assert row["vision_used"] == "True"
    assert row["answer"] == "visual answer"
    assert row["error"] == ""
    assert row["paper"] == ""
    assert row["expected_route"] == ""
    assert row["expected_page"] == ""
    assert row["expected_visual_evidence"] == ""


def test_csv_export_contains_optional_metadata(tmp_path: Path) -> None:
    pipeline = Mock()
    pipeline.answer_multimodal.return_value = _visual_result()
    item = {
        "id": "Q1",
        "question": "visual q",
        "category": "implicit_visual",
        "paper": "bert",
        "expected_route": "VISUAL_REQUIRED",
        "expected_page": 5,
        "expected_visual_evidence": "BERT Figure 2",
    }
    results = run_e2e(
        [item], "paper.pdf", strategy="always_vision",
        text_pipeline=Mock(), multimodal_pipeline=pipeline,
    )
    output = tmp_path / "e2e.csv"

    export_e2e_csv(results, output)

    with output.open(encoding="utf-8", newline="") as source:
        row = next(csv.DictReader(source))
    assert row["paper"] == "bert"
    assert row["expected_route"] == "VISUAL_REQUIRED"
    assert row["expected_page"] == "5"
    assert row["expected_visual_evidence"] == "BERT Figure 2"
    assert row["selected_page"] == "3"


def test_pipeline_error_is_recorded_and_next_question_continues() -> None:
    text_pipeline = Mock()
    text_pipeline.answer.side_effect = [RuntimeError("pipeline failed"), _text_result()]
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")

    results = run_e2e(
        _rows(), "paper.pdf", strategy="llm", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
    )

    assert results[0]["error"] == "RuntimeError: pipeline failed"
    assert results[1]["answer"] == "text answer"
    assert summarize_e2e(results)["failure_count"] == 1


def test_run_e2e_calls_checkpoint_hook_after_each_result() -> None:
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")
    completed: list[dict[str, object]] = []

    results = run_e2e(
        _rows(), "paper.pdf", strategy="adaptive", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
        on_result=completed.append,
    )

    assert completed == results


def test_small_e2e_dataset_has_five_questions_per_category() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "e2e_eval_small.jsonl"
    rows = load_e2e_jsonl(path)
    assert len(rows) == 15
    assert len({row["id"] for row in rows}) == 15
    assert {
        category: sum(row["category"] == category for row in rows)
        for category in ("text", "explicit_visual", "implicit_visual")
    } == {"text": 5, "explicit_visual": 5, "implicit_visual": 5}


def test_load_e2e_jsonl_keeps_legacy_row_unchanged(tmp_path: Path) -> None:
    item = {"id": "Q1", "question": "text q", "category": "text"}
    dataset = tmp_path / "legacy.jsonl"
    dataset.write_text(json.dumps(item) + "\n", encoding="utf-8")

    assert load_e2e_jsonl(dataset) == [{**item, "expected_pages": []}]


def test_load_e2e_jsonl_preserves_optional_metadata(tmp_path: Path) -> None:
    item = {
        "id": "Q1",
        "question": "visual q",
        "category": "implicit_visual",
        "paper": "bert",
        "expected_route": "VISUAL_REQUIRED",
        "expected_page": 5,
        "expected_visual_evidence": "BERT Figure 2",
    }
    dataset = tmp_path / "ack.jsonl"
    dataset.write_text(json.dumps(item) + "\n", encoding="utf-8")

    assert load_e2e_jsonl(dataset) == [{**item, "expected_pages": [5]}]


def test_load_e2e_jsonl_accepts_unidoc_metadata_without_category(tmp_path: Path) -> None:
    item = {
        "id": "unidoc_finance_0001",
        "question": "What is shown?",
        "gold_answer": "A chart.",
        "domain": "finance",
        "question_type": "factual_retrieval",
        "answer_type": "image_only",
        "expected_route": "VISUAL_REQUIRED",
        "document_id": "0002128",
        "source_pdf": "finance/finance/0002128.pdf",
        "expected_pages": [4],
    }
    dataset = tmp_path / "unidoc.jsonl"
    dataset.write_text(json.dumps(item) + "\n", encoding="utf-8")

    assert load_e2e_jsonl(dataset) == [item]


def test_e2e_clients_use_separate_general_and_vision_timeouts() -> None:
    settings = Settings(
        api_key="test-key",
        request_timeout=10,
        vision_request_timeout=60,
        vision_max_tokens=256,
        endpoints=(),
    )

    client, vision_client = _clients(settings)

    assert client.timeout == 10
    assert vision_client.timeout == 60


def test_resolves_per_row_pdf_under_pdf_root(tmp_path: Path) -> None:
    pdf = tmp_path / "finance" / "finance" / "0002128.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")

    resolved = resolve_pdf_path(
        {"source_pdf": "finance/finance/0002128.pdf"}, None, tmp_path
    )

    assert resolved == pdf.resolve()


def test_resolver_keeps_legacy_global_pdf() -> None:
    assert resolve_pdf_path({}, "paper.pdf", None) == Path("paper.pdf")


def test_resolver_requires_pdf_root_for_source_pdf() -> None:
    with pytest.raises(ValueError, match="pdf_root is required"):
        resolve_pdf_path({"source_pdf": "finance/finance/0002128.pdf"}, None, None)


def test_resolver_rejects_nonexistent_pdf(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_pdf_path({"source_pdf": "finance/finance/missing.pdf"}, None, tmp_path)


def test_resolver_rejects_directory(tmp_path: Path) -> None:
    directory = tmp_path / "finance" / "finance"
    directory.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a file"):
        resolve_pdf_path({"source_pdf": "finance/finance"}, None, tmp_path)


def test_resolver_rejects_containment_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF")
    with pytest.raises(ValueError, match="escapes pdf_root"):
        resolve_pdf_path({"source_pdf": "../outside.pdf"}, None, tmp_path)


def test_resolver_rejects_appledouble_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "finance" / "finance" / "._0002128.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"metadata")
    with pytest.raises(ValueError, match="AppleDouble"):
        resolve_pdf_path(
            {"source_pdf": "finance/finance/._0002128.pdf"}, None, tmp_path
        )


def test_unidoc_result_schema_routing_and_selected_page_hit(tmp_path: Path) -> None:
    pdf = tmp_path / "finance" / "finance" / "0002128.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    item = {
        "id": "unidoc_finance_0001",
        "question": "What is shown?",
        "gold_answer": "A chart.",
        "domain": "finance",
        "question_type": "factual_retrieval",
        "answer_type": "image_only",
        "expected_route": "VISUAL_REQUIRED",
        "document_id": "0002128",
        "source_pdf": "finance/finance/0002128.pdf",
        "expected_pages": [2, 3],
    }
    router = Mock()
    router.route.return_value = RoutingDecision(
        QueryRoute.VISUAL_REQUIRED, "LLM", used_llm_router=True
    )
    pipeline = Mock()
    pipeline.answer_multimodal.return_value = _visual_result()

    result = run_e2e(
        [item], None, strategy="adaptive", router=router,
        text_pipeline=Mock(), multimodal_pipeline=pipeline, pdf_root=tmp_path,
    )[0]

    pipeline.answer_multimodal.assert_called_once_with(pdf.resolve(), item["question"])
    assert result["query_id"] == item["id"]
    assert result["route"] == result["predicted_route"] == "VISUAL_REQUIRED"
    assert result["correct"] is result["route_correct"] is True
    assert result["used_llm_router"] is True
    assert result["page_hit_1"] is True
    assert result["gold_answer"] == "A chart."


def test_text_route_page_hit_is_null_but_retrieval_hit_is_measured() -> None:
    item = {
        "id": "Q1",
        "question": "text q",
        "expected_route": "TEXT_ONLY",
        "expected_pages": [3],
    }
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()

    result = run_e2e(
        [item], "paper.pdf", strategy="adaptive", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
    )[0]

    assert result["page_hit_1"] is None
    assert result["retrieval_page_hit_3"] is True
    assert result["retrieval_page_hit_5"] is None


def test_visual_route_without_selected_page_is_page_miss() -> None:
    pipeline = Mock()
    pipeline.answer_multimodal.return_value = MultimodalRagAnswer(
        "answer", (), VisionUsage(None, False, "vision"), {"cache_hit": True}
    )
    item = {
        "id": "Q1",
        "question": "visual q",
        "expected_route": "VISUAL_REQUIRED",
        "expected_pages": [3],
    }

    result = run_e2e(
        [item], "paper.pdf", strategy="always_vision",
        text_pipeline=Mock(), multimodal_pipeline=pipeline,
    )[0]

    assert result["page_hit_1"] is False


def test_retrieval_page_hits_deduplicate_pages_in_rerank_order() -> None:
    sources = tuple(
        _source_on(page, chunk)
        for page, chunk in ((1, 1), (1, 2), (2, 1), (3, 1), (4, 1), (5, 1))
    )
    text_pipeline = Mock()
    text_pipeline.answer.return_value = RagAnswer(
        "answer", sources, {"cache_hit": True}
    )
    item = {
        "id": "Q1",
        "question": "text q",
        "expected_route": "TEXT_ONLY",
        "expected_pages": [5],
    }
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")

    result = run_e2e(
        [item], "paper.pdf", strategy="adaptive", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
    )[0]

    assert result["retrieval_page_hit_3"] is False
    assert result["retrieval_page_hit_5"] is True


def test_retrieval_hit_5_is_null_when_fewer_than_five_candidates() -> None:
    item = {
        "id": "Q1",
        "question": "text q",
        "expected_route": "TEXT_ONLY",
        "expected_pages": [3],
    }
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()

    result = run_e2e(
        [item], "paper.pdf", strategy="adaptive", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
    )[0]

    assert result["retrieval_page_hit_5"] is None


def test_retrieval_hit_5_is_null_when_top_n_is_below_five() -> None:
    sources = tuple(_source_on(page) for page in range(1, 6))
    text_pipeline = Mock()
    text_pipeline.config = SimpleNamespace(top_n=3)
    text_pipeline.answer.return_value = RagAnswer(
        "answer", sources, {"cache_hit": True}
    )
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")
    item = {
        "id": "Q1",
        "question": "text q",
        "expected_route": "TEXT_ONLY",
        "expected_pages": [5],
    }

    result = run_e2e(
        [item], "paper.pdf", strategy="adaptive", router=router,
        text_pipeline=text_pipeline, multimodal_pipeline=Mock(),
    )[0]

    assert result["retrieval_page_hit_5"] is None


def test_page_and_routing_summary_exposes_denominators() -> None:
    results = [
        {
            "error": "",
            "vision_used": True,
            "expected_route": "VISUAL_REQUIRED",
            "predicted_route": "VISUAL_REQUIRED",
            "page_hit_1": True,
            "expected_pages": [3],
            "retrieval_page_hit_3": True,
            "retrieval_page_hit_5": None,
            "routing_latency_ms": 1.0,
            "vision_analysis_latency_ms": 2.0,
            "total_latency_ms": 3.0,
        }
    ]
    summary = summarize_e2e(results)
    assert summary["tp"] == 1
    assert summary["balanced_accuracy"] == 0.5
    assert summary["visual_page_hit_1_denominator"] == 1
    assert summary["retrieval_page_hit_3_denominator"] == 1
    assert summary["retrieval_page_hit_5_denominator"] == 0
    assert summary["retrieval_page_hit_5_unavailable_count"] == 1


def test_csv_serializes_expected_pages_and_sources_as_json(tmp_path: Path) -> None:
    result = run_e2e(
        [{"id": "Q1", "question": "q", "expected_pages": [3]}],
        "paper.pdf", strategy="always_vision", text_pipeline=Mock(),
        multimodal_pipeline=Mock(answer_multimodal=Mock(return_value=_visual_result())),
    )[0]
    output = tmp_path / "result.csv"
    export_e2e_csv([result], output)
    with output.open(encoding="utf-8", newline="") as source:
        exported = next(csv.DictReader(source))
    assert json.loads(exported["expected_pages"]) == [3]
    assert isinstance(json.loads(exported["sources"]), list)


def test_cli_input_modes() -> None:
    parser = argparse.ArgumentParser()
    legacy = argparse.Namespace(
        pdf="paper.pdf", legacy_dataset="ack.jsonl", dataset_option=None, pdf_root=None
    )
    unidoc = argparse.Namespace(
        pdf=None,
        legacy_dataset=None,
        dataset_option="benchmarks/unidoc.jsonl",
        pdf_root="datasets/unidoc",
    )
    assert _benchmark_inputs(parser, legacy) == ("paper.pdf", "ack.jsonl", None)
    assert _benchmark_inputs(parser, unidoc) == (
        None,
        "benchmarks/unidoc.jsonl",
        "datasets/unidoc",
    )


@pytest.mark.parametrize(
    "args",
    (
        argparse.Namespace(
            pdf=None,
            legacy_dataset=None,
            dataset_option="benchmarks/unidoc.jsonl",
            pdf_root=None,
        ),
        argparse.Namespace(
            pdf="paper.pdf",
            legacy_dataset="ack.jsonl",
            dataset_option="benchmarks/unidoc.jsonl",
            pdf_root="datasets/unidoc",
        ),
    ),
)
def test_cli_rejects_incomplete_or_mixed_modes(args: argparse.Namespace) -> None:
    with pytest.raises(SystemExit):
        _benchmark_inputs(argparse.ArgumentParser(), args)


class FakePreparer:
    def __init__(self, *, failing_name: str | None = None) -> None:
        self.calls: list[Path] = []
        self.failing_name = failing_name

    def prepare_document(
        self, pdf_path: str | Path, *, rebuild_cache: bool = False
    ) -> DocumentPreparation:
        path = Path(pdf_path)
        self.calls.append(path)
        if path.name == self.failing_name:
            raise RuntimeError("corrupt document")
        return DocumentPreparation(
            pdf_path=str(path),
            cache_key=path.stem,
            cache_path=f"{path}.npz",
            cache_hit=path.stem == "warm",
            chunk_count=1,
            preparation_latency_ms=1.0,
        )


def _prewarm_pdf(root: Path, name: str) -> Path:
    path = root / "finance" / "finance" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF")
    return path


def test_prewarm_deduplicates_shared_pdf_and_prepares_each_unique_once(
    tmp_path: Path,
) -> None:
    _prewarm_pdf(tmp_path, "warm.pdf")
    _prewarm_pdf(tmp_path, "cold.pdf")
    rows = [
        {"source_pdf": "finance/finance/warm.pdf"},
        {"source_pdf": "finance/finance/warm.pdf"},
        {"source_pdf": "finance/finance/cold.pdf"},
    ]
    preparer = FakePreparer()

    report = prewarm_documents(
        rows, None, pdf_root=tmp_path, pipeline=preparer
    )

    assert [path.name for path in preparer.calls] == ["warm.pdf", "cold.pdf"]
    assert report.total_unique_pdfs == 2
    assert report.success_count == 2
    assert report.failure_count == 0
    assert report.cache_hit_count == 1
    assert report.cache_miss_count == 1
    require_complete_prewarm(report)


def test_prewarm_collects_missing_and_preparation_failures(tmp_path: Path) -> None:
    _prewarm_pdf(tmp_path, "corrupt.pdf")
    rows = [
        {"source_pdf": "finance/finance/missing.pdf"},
        {"source_pdf": "finance/finance/corrupt.pdf"},
    ]
    report = prewarm_documents(
        rows,
        None,
        pdf_root=tmp_path,
        pipeline=FakePreparer(failing_name="corrupt.pdf"),
    )

    assert report.total_unique_pdfs == 2
    assert report.success_count == 0
    assert report.failure_count == 2
    assert any("does not exist" in failure.error for failure in report.failures)
    assert any("corrupt document" in failure.error for failure in report.failures)
    with pytest.raises(IncompletePrewarmError, match="2 of 2"):
        require_complete_prewarm(report)


def test_require_warm_cache_accepts_warm_query() -> None:
    router = Mock()
    router.route.return_value = RoutingDecision(QueryRoute.TEXT_ONLY, "text")
    text_pipeline = Mock()
    text_pipeline.answer.return_value = _text_result()

    result = run_e2e(
        [{"id": "Q1", "question": "q"}],
        "paper.pdf",
        strategy="adaptive",
        router=router,
        text_pipeline=text_pipeline,
        multimodal_pipeline=Mock(),
        require_warm_cache=True,
    )[0]

    assert result["cache_hit"] is True
    assert result["cache_state"] == "warm"
    assert result["error"] == ""


def test_require_warm_cache_flags_cold_query() -> None:
    result = run_e2e(
        [{"id": "Q1", "question": "q"}],
        "paper.pdf",
        strategy="always_vision",
        text_pipeline=Mock(),
        multimodal_pipeline=Mock(
            answer_multimodal=Mock(return_value=_visual_result())
        ),
        require_warm_cache=True,
    )[0]

    assert result["cache_hit"] is False
    assert result["cache_state"] == "cold"
    assert "warm cache required" in result["error"]
