"""Expanded retrieval oracle with immutable saved ACK initial evidence.

Only the existing embedding, cosine retrieval and reranker are called. Completed
queries are checkpointed for resume; final artifacts refuse overwriting.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from furiosa_rag.cache import DocumentEmbeddingCache
from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.embedding import FuriosaEmbedding
from furiosa_rag.reranker import FuriosaReranker
from furiosa_rag.retrieval import CosineRetriever

OUT = ROOT / "results/oracle_headroom"
FINAL_NAMES = ("expanded_reranked_candidates.csv", "retrieval_depth_cohorts.csv",
               "retrieval_expansion_costs.csv", "human_review_expansion_join.csv",
               "phase1b_summary.md", "phase1b_metrics.json")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def original_sources(row):
    return [{"chunk_id": row[f"reranked_chunk_id_{i}"],
             "source_page": int(row[f"reranked_page_{i}"]),
             "chunk_text": row[f"reranked_chunk_text_{i}"]}
            for i in (1, 2, 3) if row[f"reranked_chunk_id_{i}"]]


def cumulative_states(original, expanded, gt):
    """Remove old chunk IDs, keep duplicate pages, append in stable score order."""
    old_ids = {r["chunk_id"] for r in original}
    if len(old_ids) != len(original):
        raise ValueError("Duplicate original chunk IDs")
    if len({r["chunk_id"] for r in expanded}) != len(expanded):
        raise ValueError("Duplicate expanded chunk IDs")
    new = [r for r in sorted(expanded, key=lambda r: r["rerank_score"], reverse=True)
           if r["chunk_id"] not in old_ids]
    states = (original, original + new[:3], original + new[:6])
    ids = [{r["chunk_id"] for r in state} for state in states]
    assert ids[0] <= ids[1] <= ids[2]
    assert states[1][:len(original)] == states[2][:len(original)] == original
    first = next((i for i, r in enumerate(new, 1) if r["source_page"] in gt), None)
    hit = [any(r["source_page"] in gt for r in state) for state in states]
    cohort = ("HIT_AT_3" if hit[0] else "MISS_AT_3_HIT_AT_6" if hit[1]
              else "MISS_AT_6_HIT_AT_9" if hit[2] else "MISS_AT_9")
    return states, new, first, cohort


def write_csv(name, rows):
    with (OUT / name).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def average(values):
    present = [v for v in values if isinstance(v, (int, float))]
    return sum(present) / len(present) if present else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4:
        parser.error("workers must be 1..4")
    OUT.mkdir(parents=True, exist_ok=True)
    if any((OUT / n).exists() for n in FINAL_NAMES):
        raise SystemExit("Phase 1B final artifacts already exist; refusing overwrite.")
    checkpoint = OUT / "phase1b_expansion.checkpoint.jsonl"
    manifest_path = OUT / "phase1b_run_manifest.json"
    if (checkpoint.exists() or manifest_path.exists()) and not args.resume:
        raise SystemExit("Existing Phase 1B run; use --resume.")
    protected = [ROOT / "benchmarks/unidoc.jsonl", ROOT / "docs/RESEARCH_NOTES.md",
                 *sorted((ROOT / "results").glob("unidoc_full*.*")),
                 *sorted((ROOT / "results/error_analysis").glob("*.*"))]
    before = {str(p.relative_to(ROOT)): sha(p) for p in protected if p.is_file()}
    master = read_csv(ROOT / "results/error_analysis/analysis_master.csv")
    assert len(master) == 1600 and len({r["query_id"] for r in master}) == 1600
    target = [r for r in master if r["gt_hit_at_3"] == "False"]
    assert len(target) == 224
    for r in master:
        expected = set(json.loads(r["expected_pages"]))
        assert any(s["source_page"] in expected for s in original_sources(r)) == (r["gt_hit_at_3"] == "True")
    settings = Settings.from_env(ROOT / ".env")
    endpoints = {e.name: e for e in settings.endpoints}
    models = {n: endpoints[n].model for n in ("embedding", "reranker")}
    assert models == {"embedding": "furiosa-ai/Qwen3-Embedding-8B",
                      "reranker": "furiosa-ai/Qwen3-Reranker-8B"}
    client = FuriosaClient(settings.api_key, max(settings.request_timeout, 120))
    embedding = FuriosaEmbedding(endpoints["embedding"], client)
    reranker = FuriosaReranker(endpoints["reranker"], client)
    retriever = CosineRetriever()
    cache = DocumentEmbeddingCache(ROOT / "results/unidoc_full_cache")
    config = {"top_k": 30, "top_n": 30, "models": models,
              "workers": args.workers, "target_query_ids": [r["query_id"] for r in target],
              "input_sha256": before, "short_document_policy": "min(30, available chunks)"}
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == config
    else:
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    completed = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["query_id"] not in completed
            assert record["query_id"] in config["target_query_ids"]
            completed[record["query_id"]] = record
    # Check actual PDF bytes against the original cache identity. Never rebuild cache.
    indices = {}
    verified_pdfs = set()
    for row in target:
        pdf = ROOT / "datasets/unidoc" / row["source_pdf"]
        path = ROOT / row["evidence_cache_path"]
        if pdf not in verified_pdfs:
            key = cache.cache_key(pdf, chunk_size=700, chunk_overlap=100,
                                  embedding_model=models["embedding"])
            assert path.stem == key, f"PDF/cache provenance mismatch: {row['source_pdf']}"
            verified_pdfs.add(pdf)
        if path not in indices:
            indexed = cache.load(path.stem)
            assert indexed is not None
            indices[path] = indexed
        text_by_id = {c.chunk_id: c.text for c in indices[path].chunks}
        assert all(text_by_id[s["chunk_id"]] == s["chunk_text"] for s in original_sources(row))
    print(f"Preflight passed: {len(target)} targets / {len(indices)} verified PDF-cache pairs.", flush=True)

    # Same embedding API and unchanged query strings; batches avoid per-call latency.
    # Batch duration is not divided into fictitious per-query inference latency.
    vector_checkpoint = OUT / "phase1b_query_embeddings.checkpoint.jsonl"
    vectors = {}
    embedding_batches = []
    if vector_checkpoint.exists():
        for line in vector_checkpoint.read_text(encoding="utf-8").splitlines():
            batch = json.loads(line)
            embedding_batches.append(batch)
            for qid, vector in zip(batch["query_ids"], batch["vectors"], strict=True):
                assert qid not in vectors and qid in config["target_query_ids"]
                vectors[qid] = (vector, batch["batch_size"], batch["latency_ms"])
    missing_vectors = [r for r in target if r["query_id"] not in completed and r["query_id"] not in vectors]
    for offset in range(0, len(missing_vectors), 32):
        rows = missing_vectors[offset:offset + 32]
        started = time.perf_counter()
        batch_vectors = embedding.embed([r["question"] for r in rows])
        elapsed = (time.perf_counter() - started) * 1000
        batch = {"query_ids": [r["query_id"] for r in rows], "vectors": batch_vectors,
                 "batch_size": len(rows), "latency_ms": elapsed}
        with vector_checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(batch) + "\n")
        embedding_batches.append(batch)
        for row, vector in zip(rows, batch_vectors, strict=True):
            vectors[row["query_id"]] = (vector, len(rows), elapsed)
        print(f"Embedded remaining queries: {min(offset + 32, len(missing_vectors))}/{len(missing_vectors)} (batch size {len(rows)})", flush=True)

    def run(row):
        indexed = indices[ROOT / row["evidence_cache_path"]]
        vector, embedding_batch_size, embedding_batch_ms = vectors[row["query_id"]]
        start = time.perf_counter()
        retrieved = retriever.search(vector, indexed.chunks, indexed.embeddings, top_k=30)
        retrieval_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        ranked = reranker.rerank(row["question"], [r.chunk.text for r in retrieved], top_n=30)
        reranking_ms = (time.perf_counter() - start) * 1000
        assert len(ranked) == len(retrieved) == min(30, len(indexed.chunks))
        sources = [{"chunk_id": retrieved[r.index].chunk.chunk_id,
                    "source_page": retrieved[r.index].chunk.page_number,
                    "retrieval_score": retrieved[r.index].retrieval_score,
                    "rerank_score": r.score, "chunk_text": retrieved[r.index].chunk.text}
                   for r in ranked]
        sources.sort(key=lambda r: r["rerank_score"], reverse=True)
        return {"query_id": row["query_id"], "sources": sources,
                "document_chunk_count": len(indexed.chunks),
                "query_embedding_latency_ms": "",
                "query_embedding_batch_size": embedding_batch_size,
                "query_embedding_batch_latency_ms": embedding_batch_ms,
                "retrieval_latency_ms": retrieval_ms, "reranking_latency_ms": reranking_ms,
                "expanded_candidate_count": len(retrieved), "reranker_pair_count": len(retrieved)}

    pending = [r for r in target if r["query_id"] not in completed]
    errors = []
    # Validate the largest actual 30-candidate request before the full cohort.
    if pending:
        pilot = max(pending, key=lambda r: len(indices[ROOT / r["evidence_cache_path"]].chunks))
        record = run(pilot)
        with checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        completed[record["query_id"]] = record
        pending.remove(pilot)
        print(f"30-candidate pilot passed; completed {len(completed)}/224.", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, row): row["query_id"] for row in pending}
        for future in as_completed(futures):
            try:
                record = future.result()
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                completed[record["query_id"]] = record
                if len(completed) % 10 == 0 or len(completed) == 224:
                    print(f"Completed {len(completed)}/224", flush=True)
            except Exception as exc:
                errors.append({"query_id": futures[future], "error": str(exc)})
                print(f"Failed query: {futures[future]} ({type(exc).__name__})", flush=True)
    if errors:
        print(json.dumps(errors, ensure_ascii=True), flush=True)
        raise SystemExit("Incomplete run; checkpoints preserved, no full-cohort statistics written. Resume to retry missing queries.")
    assert len(completed) == 224

    expanded_rows, cohorts, cost_rows = [], [], []
    for row in master:
        qid = row["query_id"]
        original = original_sources(row)
        gt = set(json.loads(row["expected_pages"]))
        record = completed.get(qid)
        if record:
            states, new, first, cohort = cumulative_states(original, record["sources"], gt)
            for rank, source in enumerate(record["sources"], 1):
                expanded_rows.append({"query_id": qid, "expanded_rank": rank, **source,
                                      "chunk_character_count": len(source["chunk_text"])})
            char_sizes = [sum(len(s["chunk_text"]) for s in state) for state in states]
            unique_sizes = [len({s["source_page"] for s in state}) for state in states]
            cost_rows.append({"query_id": qid, "requested_top_k": 30,
                              **{k: record[k] for k in ("document_chunk_count", "query_embedding_latency_ms",
                                                       "retrieval_latency_ms", "reranking_latency_ms",
                                                       "expanded_candidate_count", "reranker_pair_count")},
                              "query_embedding_batch_size": record.get("query_embedding_batch_size", 1),
                              "query_embedding_batch_latency_ms": record.get("query_embedding_batch_latency_ms", ""),
                              "new_candidate_count": len(new),
                              "E3_chunk_count": len(states[0]), "E6_chunk_count": len(states[1]),
                              "E9_chunk_count": len(states[2]),
                              "E3_text_chars": char_sizes[0], "E6_text_chars": char_sizes[1],
                              "E9_text_chars": char_sizes[2],
                              "delta_chars_3_to_6": char_sizes[1] - char_sizes[0],
                              "delta_chars_6_to_9": char_sizes[2] - char_sizes[1],
                              "E3_text_tokens": "", "E6_text_tokens": "", "E9_text_tokens": "",
                              "delta_tokens_3_to_6": "", "delta_tokens_6_to_9": "",
                              "original_top3_unique_pages": unique_sizes[0],
                              "E6_unique_pages": unique_sizes[1], "E9_unique_pages": unique_sizes[2],
                              "strict_E3_subset_E6_subset_E9": len(new) > 3,
                              "E6_complete": len(new) >= 3, "E9_complete": len(new) >= 6,
                              "original_chunks_in_expanded_pool": len({s["chunk_id"] for s in original} &
                                                                      {s["chunk_id"] for s in record["sources"]})})
        else:
            states = (original, None, None)
            new, first, cohort = [], None, "HIT_AT_3"
        cohorts.append({"query_id": qid, "gt_pages": row["expected_pages"], "cohort": cohort,
                        "expanded_run_performed": record is not None,
                        "first_recovery_new_rank": first if first is not None else "",
                        "first_recovery_bucket": (f"NEW{first}" if first and first <= 6
                                                  else "BEYOND_NEW6" if first else
                                                  "NOT_RECOVERED_IN_EXPANDED_POOL" if record else "INITIAL_HIT"),
                        "new_candidate_count": len(new) if record else "",
                        "E3_ranked_chunks": json.dumps([s["chunk_id"] for s in original]),
                        "E3_ranked_pages": json.dumps([s["source_page"] for s in original]),
                        "E6_ranked_chunks": json.dumps([s["chunk_id"] for s in states[1]]) if record else "",
                        "E6_ranked_pages": json.dumps([s["source_page"] for s in states[1]]) if record else "",
                        "E9_ranked_chunks": json.dumps([s["chunk_id"] for s in states[2]]) if record else "",
                        "E9_ranked_pages": json.dumps([s["source_page"] for s in states[2]]) if record else "",
                        "state_status": ("FULL_E9" if len(new) >= 6 else "EXHAUSTED_SHORT_DOCUMENT") if record else "NOT_EXPANDED_INITIAL_HIT"})
    by_id = {r["query_id"]: r for r in cohorts}
    human = read_csv(ROOT / "results/error_analysis/human_review_progress.csv")
    joined = [{**r, **{k: v for k, v in by_id[r["query_id"]].items() if k != "query_id"},
               "valid_retrieve_more": r["human_judge_valid"] == "YES" and r["human_best_next_action"] == "RETRIEVE_MORE"}
              for r in human]
    human_retrieve = [r for r in joined if r["valid_retrieve_more"]]
    counts = Counter(r["cohort"] for r in cohorts)
    h6 = 1376 + counts["MISS_AT_3_HIT_AT_6"]
    h9 = h6 + counts["MISS_AT_6_HIT_AT_9"]
    assert counts["HIT_AT_3"] == 1376 and sum(counts.values()) == 1600
    assert len(human_retrieve) == 22
    cost_summary = {k: average([r[k] for r in cost_rows]) for k in
                    ("query_embedding_latency_ms", "retrieval_latency_ms", "reranking_latency_ms",
                     "expanded_candidate_count", "reranker_pair_count", "E3_text_chars", "E6_text_chars",
                     "E9_text_chars", "delta_chars_3_to_6", "delta_chars_6_to_9",
                     "original_top3_unique_pages", "E6_unique_pages", "E9_unique_pages")}
    unique_no_gain = {"E3_to_E6": sum(r["original_top3_unique_pages"] == r["E6_unique_pages"] for r in cost_rows),
                      "E6_to_E9": sum(r["E6_unique_pages"] == r["E9_unique_pages"] for r in cost_rows)}
    same_page_only = {
        "E3_to_E6": sum(r["E6_chunk_count"] > r["E3_chunk_count"] and
                         r["original_top3_unique_pages"] == r["E6_unique_pages"] for r in cost_rows),
        "E6_to_E9": sum(r["E9_chunk_count"] > r["E6_chunk_count"] and
                         r["E6_unique_pages"] == r["E9_unique_pages"] for r in cost_rows)}
    depth = Counter(r["first_recovery_bucket"] for r in cohorts if r["expanded_run_performed"])
    # Explicit zeros make the recovery-depth distribution complete.
    depth = {k: depth[k] for k in [*[f"NEW{i}" for i in range(1, 7)],
                                  "BEYOND_NEW6", "NOT_RECOVERED_IN_EXPANDED_POOL"]}
    metrics = {"status": "COMPLETED", "target_count": 224, "original_hit3": 1376,
               "cumulative_hit6": h6, "cumulative_hit9": h9,
               "cohort_counts": dict(counts), "first_recovery_depth": depth,
               "mean_costs_and_page_counts_target224": cost_summary,
               "unique_page_no_increase_counts": unique_no_gain,
               "same_page_only_addition_counts": same_page_only,
               "human_retrieve_more_cohorts": dict(Counter(r["cohort"] for r in human_retrieve)),
               "short_document_under30_count": sum(r["expanded_candidate_count"] < 30 for r in cost_rows),
               "short_document_under20_count": sum(r["expanded_candidate_count"] < 20 for r in cost_rows),
               "E6_incomplete_count": sum(not r["E6_complete"] for r in cost_rows),
               "E9_incomplete_count": sum(not r["E9_complete"] for r in cost_rows),
               "strict_nesting_unavailable_count": sum(not r["strict_E3_subset_E6_subset_E9"] for r in cost_rows),
               "expanded_candidate_total": sum(r["expanded_candidate_count"] for r in cost_rows),
               "workers": args.workers,
               "embedding_batch_count": len(embedding_batches),
               "embedding_batch_sizes": [b["batch_size"] for b in embedding_batches],
               "embedding_batch_latency_ms": [b["latency_ms"] for b in embedding_batches],
               "individual_embedding_query_count": sum(isinstance(r["query_embedding_latency_ms"], (int, float)) for r in cost_rows)}
    after = {str(p.relative_to(ROOT)): sha(p) for p in protected if p.is_file()}
    assert before == after, "Protected input changed during run"
    metrics["protected_input_hashes_unchanged"] = True
    write_csv("expanded_reranked_candidates.csv", expanded_rows)
    write_csv("retrieval_depth_cohorts.csv", cohorts)
    write_csv("retrieval_expansion_costs.csv", cost_rows)
    write_csv("human_review_expansion_join.csv", joined)
    with (OUT / "phase1b_metrics.json").open("x", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)
    report = ["# Phase 1B — Expanded Retrieval Oracle", "", "## A. Setup",
              "Original retrieval top_k=3, reranker top_n=3. Expanded requested top_k=30 and top_n=30; all actual retrieved candidates are reranked and retained (at least Top-20 when 20 chunks exist).",
              "Target: only the 224 original GT-MISS@3 queries, 181 documents. The 1,376 initial-hit queries are not expanded; their cumulative reachability remains true, while their E6/E9 text sizes are not estimated.",
              "Same original questions, per-row corpus, one-based physical PDF source pages, dataset expected_pages mapping (no offset), page-any-hit criterion, cached pypdf/PyMuPDF text and 700-word/100-overlap chunking, UTF-8 byte split, Qwen3-Embedding-8B, float32 NumPy cosine retriever, Qwen3-Reranker-8B /rerank scoring. No rewriting, OCR, alternative search, or page deduplication.",
              "Verified all target PDF hashes through original cache keys; reused existing embeddings/chunks without rebuilding. API models verified before run. No API top_k/top_n fallback was applied.",
              "E3 is original saved ACK evidence in original order. Remove original chunk IDs from expanded pool, stable-sort remaining candidates by descending rerank score, append NEW1..3 for E6 and NEW1..6 for E9. New reranked Top-3 is not substituted for E3 and differences are not reproduction failures.",
              f"Short documents: {metrics['short_document_under30_count']}/224 have fewer than 30 chunks, so actual search count=min(30, document chunks), as in the existing retriever. E6 incomplete={metrics['E6_incomplete_count']}; E9 incomplete={metrics['E9_incomplete_count']}. No padding/repeated chunks. Cumulative states are nested (subset-or-equal); strict E3⊂E6⊂E9 is impossible for {metrics['strict_nesting_unavailable_count']} exhausted documents. E6/E9 are capacity labels where incomplete. Counts remain page-any-hit over all available cumulative chunks.",
              "## B. Structural recovery", "| State | Count / 1600 | Percent |", "| --- | ---: | ---: |",
              f"| Original Hit@3 | 1376 | 86.0000% |",
              f"| Cumulative Hit@6 | {h6} | {h6/16:.4f}% |",
              f"| Cumulative Hit@9 | {h9} | {h9/16:.4f}% |", "",
              "```json", json.dumps(dict(counts), indent=2), "```",
              "## C. First recovery NEW rank", "```json", json.dumps(depth, indent=2), "```",
              "First recovery is searched across all retained new candidates, not only NEW1..6. BEYOND_NEW6 differs from not recovered anywhere in the expanded pool.",
              "## D. Unique-page diversity / E. Cost proxies",
              "Means below use the 224 expanded targets only. Duplicate pages and overlapping text are retained; character counts sum raw chunk text and exclude prompt headers/separators.",
              "```json", json.dumps(cost_summary, indent=2), "```",
              "No unique-page increase counts (including candidate exhaustion):", "```json", json.dumps(unique_no_gain, indent=2), "```",
              "Of these, actual added chunks but no new pages (excluding exhaustion):", "```json", json.dumps(same_page_only, indent=2), "```",
              f"Completed retrieval candidates / reranker pairs total={metrics['expanded_candidate_total']}. Each completed target has one query vector and one retained expanded reranker result. The first {metrics['individual_embedding_query_count']} queries used individual embedding requests; remaining queries used {metrics['embedding_batch_count']} requests with batch sizes {metrics['embedding_batch_sizes']}. Same original strings and embedding scoring model, same API; only transport batching changed. A checkpoint-preserving restart interrupted some in-flight requests, so these completed-work totals exclude discarded calls and must not be interpreted as total billed work.",
              f"Latency columns are observed perf_counter durations in milliseconds with up to {args.workers} simultaneous reranker requests. Individual query embedding, batch embedding, local retrieval, and API reranking are measured separately. Batch duration is repeated as batch metadata per query and must not be summed or treated as per-query time. The mean individual embedding latency above only covers individual requests. They include concurrency/service/network variation; they are not stable isolated benchmark latency or monetary costs. No fabricated latency or baseline latency subtraction. Local transformers/tokenizers packages and cached Qwen tokenizer files are unavailable; tokens and token deltas remain blank.",
              "## F. Human RETRIEVE_MORE cross-check",
              "72 human-review rows joined read-only. Count only human_judge_valid=YES and human_best_next_action=RETRIEVE_MORE (22 cases). This balanced diagnostic sample cannot estimate population prevalence.",
              "```json", json.dumps(metrics["human_retrieve_more_cohorts"], indent=2), "```",
              "The 6 HIT_AT_3 cases are explicitly retained as INITIAL_HIT / not expanded, because human need for more evidence is not equivalent to GT-page absence. The remaining 16 cases have measured expansion cohorts.",
              "| Sample | Query | Cohort | First recovery NEW rank | Human sufficient pages |", "| --- | --- | --- | ---: | --- |"]
    report += [f"| {r['sample_id']} | {r['query_id']} | {r['cohort']} | {r['first_recovery_new_rank']} | {r['human_sufficient_pages']} |" for r in human_retrieve]
    report += ["", "## G. Limitations and next-phase blockers",
               "- GT page recovery != answer recovery.", "- GT page != necessarily sufficient evidence.",
               "- Human RETRIEVE_MORE label != necessarily GT-page absence.",
               "- Larger candidate pool may improve reachability without improving final QA.",
               "- Answer-level utility must be measured separately by future counterfactual inference.",
               "- No VLM inference, final answer generation, answer judge, controller, or novelty/baseline-superiority claim.",
               "- Full-population expansion cost cannot be estimated from only original misses; 1,376 initial-hit queries were not expanded.",
               "- Short-document exhaustion precludes literal 6/9-chunk states in some cases; later inference must use recorded actual context counts.",
               "- Stable compute cost needs a controlled latency benchmark; exact token cost needs a verified model tokenizer.",
               "- Phase 2 must specify fixed-E3 action inputs, success evaluation, sufficient-page/multi-page and judge-validity handling before measuring answer-level utility.",
               "All protected ACK/error-analysis/review inputs and RESEARCH_NOTES.md verified unchanged by SHA-256. Phase 1 files were not overwritten."]
    with (OUT / "phase1b_summary.md").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(report) + "\n")
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == "__main__":
    main()
