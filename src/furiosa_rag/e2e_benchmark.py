"""Strategy-level end-to-end RAG benchmark orchestration."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.models import MultimodalRagAnswer, RagAnswer, RetrievalAwareRagAnswer
from furiosa_rag.pipeline import DocumentPreparation
from furiosa_rag.router import QueryRoute, QueryRouter
from furiosa_rag.routing_metrics import compute_routing_metrics

STRATEGIES = (
    "always_vision",
    "llm",
    "adaptive",
    "forced_text",
    "forced_vision",
    "retrieval_aware_adaptive",
)
OPTIONAL_METADATA_FIELDS = (
    "category",
    "paper",
    "expected_route",
    "expected_page",
    "expected_pages",
    "expected_visual_evidence",
    "document_id",
    "source_pdf",
    "domain",
    "question_type",
    "answer_type",
    "gold_answer",
    "audit_sampling_seed",
    "audit_sampling_method",
)
CSV_FIELDS = [
    "id", "query_id", "question", *OPTIONAL_METADATA_FIELDS,
    "strategy", "route", "predicted_route", "correct", "route_correct",
    "routing_reason", "routing_latency_ms", "used_llm_router",
    "vision_used", "selected_page", "page_hit_1",
    "retrieval_page_hit_3", "retrieval_page_hit_5",
    "query_embedding_latency_ms", "reranking_latency_ms",
    "page_rendering_latency_ms", "vision_analysis_latency_ms",
    "answer_generation_latency_ms", "total_latency_ms", "cache_state", "cache_hit",
    "answer", "sources", "error",
    "router_evidence_chunk_count", "router_evidence_pages",
    "router_prompt_version", "router_prompt_sha256",
]


class TextPipeline(Protocol):
    def answer(self, pdf_path: str | Path, question: str) -> RagAnswer: ...


class MultimodalPipeline(Protocol):
    def answer_multimodal(
        self, pdf_path: str | Path, question: str
    ) -> MultimodalRagAnswer: ...


class DocumentPreparer(Protocol):
    def prepare_document(
        self, pdf_path: str | Path, *, rebuild_cache: bool = False
    ) -> DocumentPreparation: ...


class RetrievalAwarePipeline(Protocol):
    config: object

    def answer_retrieval_aware(
        self, pdf_path: str | Path, question: str
    ) -> RetrievalAwareRagAnswer: ...


@dataclass(frozen=True, slots=True)
class PrewarmFailure:
    pdf_path: str
    error: str


@dataclass(frozen=True, slots=True)
class PrewarmReport:
    total_unique_pdfs: int
    preparations: tuple[DocumentPreparation, ...]
    failures: tuple[PrewarmFailure, ...]
    total_latency_ms: float

    @property
    def success_count(self) -> int:
        return len(self.preparations)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def cache_hit_count(self) -> int:
        return sum(preparation.cache_hit for preparation in self.preparations)

    @property
    def cache_miss_count(self) -> int:
        return self.success_count - self.cache_hit_count


class IncompletePrewarmError(RuntimeError):
    """Raised when measured execution would follow an incomplete prewarm."""


def require_complete_prewarm(report: PrewarmReport) -> None:
    if report.failure_count:
        raise IncompletePrewarmError(
            f"prewarm failed for {report.failure_count} of {report.total_unique_pdfs} PDFs"
        )


def load_e2e_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return load_benchmark_jsonl(path)


def _sources_json(result: RagAnswer | MultimodalRagAnswer | RetrievalAwareRagAnswer) -> str:
    return json.dumps(
        [
            {
                "page": source.chunk.page_number,
                "chunk": source.chunk.chunk_id,
                "retrieval_score": source.retrieval_score,
                "rerank_score": source.rerank_score,
            }
            for source in result.sources
        ],
        ensure_ascii=False,
    )


def _latency(
    result: RagAnswer | MultimodalRagAnswer | RetrievalAwareRagAnswer, key: str
) -> float:
    value = result.latency_ms.get(key, 0.0)
    return 0.0 if isinstance(value, bool) else float(value)


def resolve_pdf_path(
    item: dict[str, Any],
    pdf_path: str | Path | None,
    pdf_root: str | Path | None,
) -> Path:
    source_pdf = item.get("source_pdf")
    if source_pdf:
        if pdf_root is None:
            raise ValueError("pdf_root is required when a row contains source_pdf")
        root = Path(pdf_root).resolve()
        resolved = (root / str(source_pdf)).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"source_pdf escapes pdf_root: {source_pdf}")
        if resolved.name.startswith("._"):
            raise ValueError(f"AppleDouble metadata is not a valid PDF: {resolved}")
        if not resolved.exists():
            raise FileNotFoundError(f"source PDF does not exist: {resolved}")
        if not resolved.is_file():
            raise ValueError(f"source PDF is not a file: {resolved}")
        return resolved
    if pdf_path is None:
        raise ValueError("a global pdf_path or per-row source_pdf with pdf_root is required")
    return Path(pdf_path)


def prewarm_documents(
    rows: list[dict[str, Any]],
    pdf_path: str | Path | None,
    *,
    pdf_root: str | Path | None,
    pipeline: DocumentPreparer,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> PrewarmReport:
    started = time.perf_counter()
    unique_paths: list[Path] = []
    seen_paths: set[Path] = set()
    failures: list[PrewarmFailure] = []
    failed_references: set[str] = set()
    for item in rows:
        reference = str(item.get("source_pdf") or pdf_path or "<missing PDF>")
        try:
            resolved = resolve_pdf_path(item, pdf_path, pdf_root).resolve()
        except (FileNotFoundError, OSError, ValueError) as exc:
            if reference not in failed_references:
                failed_references.add(reference)
                failures.append(
                    PrewarmFailure(reference, f"{type(exc).__name__}: {exc}")
                )
            continue
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            unique_paths.append(resolved)

    total = len(unique_paths) + len(failures)
    preparations: list[DocumentPreparation] = []
    completed = len(failures)
    for path in unique_paths:
        try:
            preparations.append(pipeline.prepare_document(path))
        except Exception as exc:  # noqa: BLE001 - aggregate every per-document failure
            failures.append(PrewarmFailure(str(path), f"{type(exc).__name__}: {exc}"))
        completed += 1
        if on_progress is not None:
            on_progress(completed, total, str(path))
    return PrewarmReport(
        total_unique_pdfs=total,
        preparations=tuple(preparations),
        failures=tuple(failures),
        total_latency_ms=(time.perf_counter() - started) * 1000,
    )


def _expected_pages(item: dict[str, Any]) -> list[int]:
    pages = item.get("expected_pages")
    if isinstance(pages, list):
        return [page for page in pages if isinstance(page, int) and not isinstance(page, bool)]
    page = item.get("expected_page")
    return [page] if isinstance(page, int) and not isinstance(page, bool) else []


def _unique_source_pages(
    result: RagAnswer | MultimodalRagAnswer | RetrievalAwareRagAnswer,
) -> list[int]:
    return list(dict.fromkeys(source.chunk.page_number for source in result.sources))


def _configured_top_n(
    pipeline: object, result: RagAnswer | MultimodalRagAnswer | RetrievalAwareRagAnswer
) -> int:
    configured = getattr(getattr(pipeline, "config", None), "top_n", None)
    return configured if isinstance(configured, int) else len(result.sources)


def _set_page_metrics(
    row: dict[str, Any],
    item: dict[str, Any],
    result: RagAnswer | MultimodalRagAnswer | RetrievalAwareRagAnswer,
    pipeline: object,
) -> None:
    expected_pages = _expected_pages(item)
    expected_page_set = set(expected_pages)
    if item.get("expected_route") == QueryRoute.VISUAL_REQUIRED.value:
        row["page_hit_1"] = row["selected_page"] in expected_page_set

    if not expected_pages:
        return
    unique_pages = _unique_source_pages(result)
    row["retrieval_page_hit_3"] = bool(expected_page_set.intersection(unique_pages[:3]))
    if _configured_top_n(pipeline, result) >= 5 and len(unique_pages) >= 5:
        row["retrieval_page_hit_5"] = bool(expected_page_set.intersection(unique_pages[:5]))


def run_e2e(
    rows: list[dict[str, Any]],
    pdf_path: str | Path | None,
    *,
    strategy: str,
    text_pipeline: TextPipeline,
    multimodal_pipeline: MultimodalPipeline,
    retrieval_aware_pipeline: RetrievalAwarePipeline | None = None,
    router: QueryRouter | None = None,
    pdf_root: str | Path | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    require_warm_cache: bool = False,
) -> list[dict[str, Any]]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unsupported strategy: {strategy}")
    if strategy in {"llm", "adaptive"} and router is None:
        raise ValueError(f"strategy {strategy} requires a router")
    if strategy == "retrieval_aware_adaptive" and retrieval_aware_pipeline is None:
        raise ValueError("retrieval_aware_adaptive requires a retrieval-aware pipeline")

    results: list[dict[str, Any]] = []
    for item in rows:
        total_started = time.perf_counter_ns()
        row: dict[str, Any] = {
            **item,
            **{
                field: item.get(field, "")
                for field in OPTIONAL_METADATA_FIELDS
            },
            "expected_pages": _expected_pages(item),
            "strategy": strategy,
            "query_id": item["id"],
            "route": "",
            "predicted_route": "",
            "correct": None,
            "route_correct": None,
            "routing_reason": "",
            "routing_latency_ms": 0.0,
            "used_llm_router": False,
            "vision_used": False,
            "selected_page": None,
            "page_hit_1": (
                False
                if item.get("expected_route") == QueryRoute.VISUAL_REQUIRED.value
                else None
            ),
            "retrieval_page_hit_3": False if _expected_pages(item) else None,
            "retrieval_page_hit_5": None,
            "query_embedding_latency_ms": 0.0,
            "reranking_latency_ms": 0.0,
            "page_rendering_latency_ms": 0.0,
            "vision_analysis_latency_ms": 0.0,
            "answer_generation_latency_ms": 0.0,
            "cache_state": "unknown",
            "cache_hit": None,
            "answer": "",
            "sources": "[]",
            "error": "",
            "router_evidence_chunk_count": 0,
            "router_evidence_pages": [],
            "router_prompt_version": "",
            "router_prompt_sha256": "",
        }
        try:
            active_pdf_path = resolve_pdf_path(item, pdf_path, pdf_root)
            if strategy == "retrieval_aware_adaptive":
                active_pipeline = retrieval_aware_pipeline
                result = retrieval_aware_pipeline.answer_retrieval_aware(  # type: ignore[union-attr]
                    active_pdf_path, item["question"]
                )
                route = QueryRoute(result.route)
                row["routing_reason"] = result.routing_reason
                row["routing_latency_ms"] = result.routing_latency_ms
                row["used_llm_router"] = result.used_llm_router
                row["vision_used"] = result.vision.used
                row["selected_page"] = result.vision.selected_page
                row["router_evidence_chunk_count"] = result.router_evidence_chunk_count
                row["router_evidence_pages"] = list(result.router_evidence_pages)
                row["router_prompt_version"] = result.router_prompt_version
                row["router_prompt_sha256"] = result.router_prompt_sha256
                if result.vision.error:
                    row["error"] = result.vision.error
            elif strategy in {"always_vision", "forced_vision"}:
                route = QueryRoute.VISUAL_REQUIRED
                row["routing_reason"] = f"{strategy} strategy"
            elif strategy == "forced_text":
                route = QueryRoute.TEXT_ONLY
                row["routing_reason"] = "forced_text strategy"
            else:
                routing_started = time.perf_counter_ns()
                decision = router.route(item["question"])  # type: ignore[union-attr]
                row["routing_latency_ms"] = (
                    time.perf_counter_ns() - routing_started
                ) / 1_000_000
                route = decision.route
                row["routing_reason"] = decision.reason
                row["used_llm_router"] = decision.used_llm_router
            row["route"] = route.value
            row["predicted_route"] = route.value
            expected_route = item.get("expected_route")
            if expected_route is not None:
                row["route_correct"] = route.value == expected_route
                row["correct"] = row["route_correct"]

            if strategy == "retrieval_aware_adaptive":
                pass
            elif route is QueryRoute.VISUAL_REQUIRED:
                active_pipeline: object = multimodal_pipeline
                result = multimodal_pipeline.answer_multimodal(
                    active_pdf_path, item["question"]
                )
                row["vision_used"] = result.vision.used
                row["selected_page"] = result.vision.selected_page
                if result.vision.error:
                    row["error"] = result.vision.error
            else:
                active_pipeline = text_pipeline
                result = text_pipeline.answer(active_pdf_path, item["question"])

            row["query_embedding_latency_ms"] = _latency(result, "query_embedding")
            row["reranking_latency_ms"] = _latency(result, "reranking")
            row["page_rendering_latency_ms"] = _latency(result, "page_rendering")
            row["vision_analysis_latency_ms"] = _latency(result, "vision_analysis")
            row["answer_generation_latency_ms"] = _latency(result, "answer_generation")
            cache_hit = result.latency_ms.get("cache_hit")
            row["cache_hit"] = cache_hit if isinstance(cache_hit, bool) else None
            row["cache_state"] = (
                "warm" if cache_hit is True else "cold" if cache_hit is False else "unknown"
            )
            if require_warm_cache and cache_hit is not True:
                raise RuntimeError(
                    f"warm cache required, but query used {row['cache_state']} document index"
                )
            row["answer"] = result.answer
            row["sources"] = _sources_json(result)
            _set_page_metrics(row, item, result, active_pipeline)
        except Exception as exc:  # noqa: BLE001 - preserve per-question benchmark progress
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["total_latency_ms"] = (time.perf_counter_ns() - total_started) / 1_000_000
        results.append(row)
        if on_result is not None:
            on_result(row)
    return results


def summarize_e2e(results: list[dict[str, Any]]) -> dict[str, int | float]:
    total = len(results)
    failures = sum(bool(row["error"]) for row in results)
    vision_calls = sum(bool(row["vision_used"]) for row in results)
    successful_results = [row for row in results if not row["error"]]
    category_totals: Counter[str] = Counter(
        str(row["category"]) for row in results if row.get("category")
    )

    def average(field: str, subset: list[dict[str, Any]] = results) -> float:
        return sum(float(row[field]) for row in subset) / len(subset) if subset else 0.0

    routing_metrics = compute_routing_metrics(results)
    visual_page_rows = [
        row
        for row in results
        if row.get("expected_route") == QueryRoute.VISUAL_REQUIRED.value
    ]
    retrieval_page_rows = [row for row in results if _expected_pages(row)]
    retrieval_page_5_rows = [
        row for row in retrieval_page_rows if row.get("retrieval_page_hit_5") is not None
    ]

    summary: dict[str, int | float] = {
        **routing_metrics,
        "total_questions": total,
        "success_count": total - failures,
        "failure_count": failures,
        "vision_calls": vision_calls,
        "vision_call_rate": vision_calls / total if total else 0.0,
        "average_routing_latency_ms": average("routing_latency_ms"),
        "average_vision_latency_ms": average(
            "vision_analysis_latency_ms",
            [
                row
                for row in successful_results
                if bool(row["vision_used"])
            ],
        ),
        "average_total_latency_ms": average("total_latency_ms", successful_results),
        "visual_page_hit_1_count": sum(row.get("page_hit_1") is True for row in visual_page_rows),
        "visual_page_hit_1_denominator": len(visual_page_rows),
        "visual_page_hit_1_rate": (
            sum(row.get("page_hit_1") is True for row in visual_page_rows)
            / len(visual_page_rows)
            if visual_page_rows
            else 0.0
        ),
        "retrieval_page_hit_3_count": sum(
            row.get("retrieval_page_hit_3") is True for row in retrieval_page_rows
        ),
        "retrieval_page_hit_3_denominator": len(retrieval_page_rows),
        "retrieval_page_hit_3_rate": (
            sum(row.get("retrieval_page_hit_3") is True for row in retrieval_page_rows)
            / len(retrieval_page_rows)
            if retrieval_page_rows
            else 0.0
        ),
        "retrieval_page_hit_5_count": sum(
            row.get("retrieval_page_hit_5") is True for row in retrieval_page_5_rows
        ),
        "retrieval_page_hit_5_denominator": len(retrieval_page_5_rows),
        "retrieval_page_hit_5_rate": (
            sum(row.get("retrieval_page_hit_5") is True for row in retrieval_page_5_rows)
            / len(retrieval_page_5_rows)
            if retrieval_page_5_rows
            else 0.0
        ),
        "retrieval_page_hit_5_unavailable_count": (
            len(retrieval_page_rows) - len(retrieval_page_5_rows)
        ),
    }
    for category in ("text", "explicit_visual", "implicit_visual"):
        subset = [row for row in results if row.get("category") == category]
        summary[f"average_total_latency_{category}_ms"] = (
            average("total_latency_ms", subset) if category_totals[category] else 0.0
        )
    return summary


def export_e2e_csv(results: list[dict[str, Any]], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for result in results:
            exported = {field: result.get(field, "") for field in CSV_FIELDS}
            exported["expected_pages"] = json.dumps(
                result.get("expected_pages", []), ensure_ascii=False
            )
            exported["router_evidence_pages"] = json.dumps(
                result.get("router_evidence_pages", []), ensure_ascii=False
            )
            if not isinstance(exported["sources"], str):
                exported["sources"] = json.dumps(exported["sources"], ensure_ascii=False)
            writer.writerow(exported)
