"""Benchmark routing strategies through the real text and multimodal pipelines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from furiosa_rag.benchmark_checkpoint import (
    append_checkpoint,
    build_benchmark_fingerprint,
    latest_checkpoint_records,
    load_checkpoint,
    materialize_checkpoint_csv,
    plan_resume,
    validate_checkpoint_fingerprint,
)
from furiosa_rag.cache import DocumentEmbeddingCache
from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import ModelEndpoint, Settings
from furiosa_rag.e2e_benchmark import (
    STRATEGIES,
    export_e2e_csv,
    load_e2e_jsonl,
    prewarm_documents,
    require_complete_prewarm,
    run_e2e,
    summarize_e2e,
)
from furiosa_rag.embedding import FuriosaEmbedding
from furiosa_rag.llm import FuriosaLlm
from furiosa_rag.pipeline import (
    MultimodalRagPipeline,
    RagConfig,
    RetrievalAwareRagPipeline,
    TextRagPipeline,
)
from furiosa_rag.reranker import FuriosaReranker
from furiosa_rag.router import (
    AdaptiveQueryRouter,
    LLMQueryRouter,
    QueryRouter,
    RetrievalAwareAdaptiveRouter,
)
from furiosa_rag.vision import FuriosaVision


def _endpoint(settings: Settings, name: str) -> ModelEndpoint:
    return next(endpoint for endpoint in settings.endpoints if endpoint.name == name)


def _clients(settings: Settings) -> tuple[FuriosaClient, FuriosaClient]:
    return (
        FuriosaClient(settings.api_key, settings.request_timeout),
        FuriosaClient(settings.api_key, settings.vision_request_timeout),
    )


def _benchmark_inputs(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[str | None, str, str | None]:
    flagged_mode = args.dataset_option is not None or args.pdf_root is not None
    if flagged_mode:
        if args.dataset_option is None or args.pdf_root is None:
            parser.error("UniDoc mode requires both --dataset and --pdf-root")
        if args.pdf is not None or args.legacy_dataset is not None:
            parser.error("do not mix positional PDF/dataset arguments with --dataset/--pdf-root")
        return None, args.dataset_option, args.pdf_root
    if args.pdf is None or args.legacy_dataset is None:
        parser.error("legacy mode requires positional PDF and dataset arguments")
    return args.pdf, args.legacy_dataset, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark E2E RAG routing strategies")
    parser.add_argument("pdf", nargs="?", help="legacy single PDF path")
    parser.add_argument("legacy_dataset", nargs="?", help="legacy JSONL dataset path")
    parser.add_argument("--dataset", dest="dataset_option", help="per-row PDF JSONL dataset")
    parser.add_argument("--pdf-root", help="root for per-row source_pdf paths")
    parser.add_argument("--checkpoint", type=Path, help="append-only JSONL checkpoint path")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="prepare every unique document index before measured queries",
    )
    parser.add_argument(
        "--require-warm-cache",
        action="store_true",
        help="record an error if a measured query performs cold document indexing",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="shared document-index cache directory (independent of strategy)",
    )
    parser.add_argument("--strategy", choices=STRATEGIES, required=True)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=700)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    parser.add_argument("--vision-dpi", type=float, default=144.0)
    parser.add_argument("--output", default="data/benchmarks/e2e_results.csv")
    args = parser.parse_args()
    pdf_path, dataset_path, pdf_root = _benchmark_inputs(parser, args)
    if args.resume and args.checkpoint is None:
        parser.error("--resume requires --checkpoint")
    if args.retry_errors and not args.resume:
        parser.error("--retry-errors requires --resume")
    if (
        args.checkpoint is not None
        and args.checkpoint.exists()
        and args.checkpoint.stat().st_size > 0
        and not args.resume
    ):
        parser.error("checkpoint already exists; use --resume or choose a new path")

    settings = Settings.from_env()
    dataset_rows = load_e2e_jsonl(dataset_path)
    llm_endpoint = _endpoint(settings, "llm")
    vision_endpoint = _endpoint(settings, "vision")
    embedding_endpoint = _endpoint(settings, "embedding")
    reranker_endpoint = _endpoint(settings, "reranker")
    client, vision_client = _clients(settings)
    config = RagConfig(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        top_k=args.top_k,
        top_n=args.top_n,
        vision_max_tokens=settings.vision_max_tokens,
        vision_dpi=args.vision_dpi,
    )
    embedding = FuriosaEmbedding(embedding_endpoint, client)
    reranker = FuriosaReranker(reranker_endpoint, client)
    llm = FuriosaLlm(llm_endpoint, client)
    cache = (
        DocumentEmbeddingCache(args.cache_dir)
        if args.cache_dir is not None
        else DocumentEmbeddingCache()
    )
    text_pipeline = TextRagPipeline(
        embedding, reranker, llm, config=config, cache=cache
    )
    multimodal_pipeline = MultimodalRagPipeline(
        embedding,
        reranker,
        llm,
        vision=FuriosaVision(vision_endpoint, vision_client),
        config=config,
        cache=cache,
    )
    retrieval_aware_pipeline = None
    retrieval_aware_router = None
    if args.strategy == "retrieval_aware_adaptive":
        retrieval_aware_router = RetrievalAwareAdaptiveRouter(llm_endpoint, client)
        retrieval_aware_pipeline = RetrievalAwareRagPipeline(
            embedding,
            reranker,
            llm,
            vision=FuriosaVision(vision_endpoint, vision_client),
            router=retrieval_aware_router,
            config=config,
            cache=cache,
        )

    router: QueryRouter | None = None
    if args.strategy in {"llm", "adaptive"}:
        llm_router = LLMQueryRouter(llm_endpoint, client)
        router = llm_router if args.strategy == "llm" else AdaptiveQueryRouter(llm_router)

    fingerprint = None
    rows_to_run = dataset_rows
    on_result = None
    if args.checkpoint is not None:
        fingerprint = build_benchmark_fingerprint(
            dataset_path=dataset_path,
            strategy=args.strategy,
            embedding_model=embedding_endpoint.model,
            reranker_model=reranker_endpoint.model,
            router_llm_model=llm_endpoint.model,
            final_llm_model=llm_endpoint.model,
            vision_model=vision_endpoint.model,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            top_k=args.top_k,
            top_n=args.top_n,
            vision_dpi=args.vision_dpi,
            pdf_root=pdf_root,
            pdf_path=pdf_path,
            cache_dir=cache.cache_dir,
            require_warm_cache=args.require_warm_cache,
            router_prompt_sha256=(
                str(
                    retrieval_aware_router.reproducibility_metadata()[
                        "router_prompt_sha256"
                    ]
                )
                if retrieval_aware_router is not None
                else None
            ),
        )
        checkpoint_records = (
            load_checkpoint(args.checkpoint, repair_truncated_final_line=True)
            if args.resume
            else []
        )
        validate_checkpoint_fingerprint(
            checkpoint_records, fingerprint, current_rows=dataset_rows
        )
        resume_plan = plan_resume(
            dataset_rows,
            strategy=args.strategy,
            latest=latest_checkpoint_records(checkpoint_records),
            retry_errors=args.retry_errors,
        )
        rows_to_run = resume_plan.pending_rows
        print(f"total candidate executions: {len(dataset_rows)}")
        print(f"already completed: {resume_plan.already_completed}")
        print(f"skipped successes: {resume_plan.skipped_successes}")
        print(f"skipped errors: {resume_plan.skipped_errors}")
        print(f"retrying errors: {resume_plan.retrying_errors}")
        print(f"remaining: {len(rows_to_run)}")
        progress = {"completed": 0, "errors": 0}

        def checkpoint_result(result: dict[str, object]) -> None:
            append_checkpoint(args.checkpoint, result, fingerprint)  # type: ignore[arg-type]
            progress["completed"] += 1
            progress["errors"] += bool(result.get("error"))
            if progress["completed"] % 10 == 0 or progress["completed"] == len(rows_to_run):
                print(
                    f"progress: {progress['completed']}/{len(rows_to_run)} "
                    f"current errors: {progress['errors']}"
                )

        on_result = checkpoint_result

    if args.prewarm:
        print("\nPrewarming unique document indexes")

        def prewarm_progress(completed: int, total: int, path: str) -> None:
            if completed % 25 == 0 or completed == total:
                print(f"prewarm progress: {completed}/{total} ({path})")

        prewarm_report = prewarm_documents(
            dataset_rows,
            pdf_path,
            pdf_root=pdf_root,
            pipeline=text_pipeline,
            on_progress=prewarm_progress,
        )
        print(f"prewarm unique PDFs: {prewarm_report.total_unique_pdfs}")
        print(f"prewarm successes: {prewarm_report.success_count}")
        print(f"prewarm failures: {prewarm_report.failure_count}")
        print(f"prewarm cache hits: {prewarm_report.cache_hit_count}")
        print(f"prewarm cache misses: {prewarm_report.cache_miss_count}")
        print(f"prewarm total latency ms: {prewarm_report.total_latency_ms:.1f}")
        for failure in prewarm_report.failures:
            print(f"prewarm failure: {failure.pdf_path}: {failure.error}")
        try:
            require_complete_prewarm(prewarm_report)
        except RuntimeError:
            print("Measured E2E execution blocked because prewarm was incomplete.")
            return 1

    try:
        results = run_e2e(
            rows_to_run,
            pdf_path,
            strategy=args.strategy,
            text_pipeline=text_pipeline,
            multimodal_pipeline=multimodal_pipeline,
            retrieval_aware_pipeline=retrieval_aware_pipeline,
            router=router,
            pdf_root=pdf_root,
            on_result=on_result,
            require_warm_cache=args.require_warm_cache,
        )
    except KeyboardInterrupt:
        if args.checkpoint is not None and fingerprint is not None:
            materialize_checkpoint_csv(
                args.checkpoint,
                args.output,
                fingerprint=fingerprint,
                current_rows=dataset_rows,
            )
            print(f"\nInterrupted; materialized completed checkpoint rows to {args.output}")
        raise

    if args.checkpoint is not None and fingerprint is not None:
        results = materialize_checkpoint_csv(
            args.checkpoint,
            args.output,
            fingerprint=fingerprint,
            current_rows=dataset_rows,
        )
        current_errors = sum(bool(result.get("error")) for result in results)
        print(f"completed in current invocation: {len(rows_to_run)}")
        print(f"current error count: {current_errors}")
    else:
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
    summary = summarize_e2e(results)
    if summary["retrieval_page_hit_5_unavailable_count"]:
        print(
            "WARNING: Retrieval Page Hit@5 is unavailable for "
            f"{summary['retrieval_page_hit_5_unavailable_count']} row(s); "
            "use --top-n 5 or greater and ensure five unique reranked pages are returned."
        )
    print("\nSummary")
    for key, value in summary.items():
        if key.endswith(("rate", "accuracy", "precision", "recall", "f1")):
            print(f"{key}: {float(value):.2%}")
        else:
            print(f"{key}: {value}")
    if args.checkpoint is None:
        export_e2e_csv(results, args.output)
    print(f"output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
