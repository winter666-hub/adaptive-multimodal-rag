"""Read-only ACK artifact audit; refuse depth expansion when initial top_k is 3.

No model clients are imported. Existing results and review files are never written.
Outputs are deliberately marked partial: saved Top-3 agreement is not a fresh
retrieval reproduction, and unavailable deeper ranks are never treated as misses.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/oracle_headroom"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def indexed(rows, field):
    result = {r[field]: r for r in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate {field}")
    return result


def write_csv(name, rows):
    with (OUT / name).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    if OUT.exists() and any(OUT.iterdir()):
        raise SystemExit("Output directory is not empty; refusing to overwrite.")
    master_path = ROOT / "results/error_analysis/analysis_master.csv"
    human_path = ROOT / "results/error_analysis/human_review_progress.csv"
    dataset_path = ROOT / "benchmarks/unidoc.jsonl"
    checkpoint_paths = {
        s: ROOT / f"results/unidoc_full_{s}.checkpoint.jsonl"
        for s in ("forced_text", "forced_vision", "retrieval_aware")
    }
    protected = [master_path, human_path, dataset_path, *checkpoint_paths.values(),
                 ROOT / "docs/RESEARCH_NOTES.md"]
    before = {str(p.relative_to(ROOT)): sha(p) for p in protected}
    master = indexed(read_csv(master_path), "query_id")
    dataset = indexed(read_jsonl(dataset_path), "id")
    human = read_csv(human_path)
    checkpoints = {s: indexed(read_jsonl(p), "query_id") for s, p in checkpoint_paths.items()}
    assert len(master) == len(dataset) == 1600 and len(human) == 72
    assert all(set(rows) == set(master) for rows in checkpoints.values())
    assert set(dataset) == set(master)
    configs = [r["fingerprint_config"] for rows in checkpoints.values() for r in rows.values()]
    assert all(c["rag"] == {"chunk_overlap": 100, "chunk_size": 700,
                            "top_k": 3, "top_n": 3, "vision_dpi": 144.0} for c in configs)
    assert all(c["models"]["embedding"] == "furiosa-ai/Qwen3-Embedding-8B"
               and c["models"]["reranker"] == "furiosa-ai/Qwen3-Reranker-8B" for c in configs)
    for qid, row in master.items():
        d = dataset[qid]
        assert row["question"] == d["question"] and row["source_pdf"] == d["source_pdf"]
        assert set(json.loads(row["expected_pages"])) == set(d["expected_pages"])
        for rows in checkpoints.values():
            c = rows[qid]
            assert c["question"] == d["question"] and c["source_pdf"] == d["source_pdf"]
            assert c["expected_pages"] == d["expected_pages"]

    agreement = {}
    for strategy, records in checkpoints.items():
        counts = Counter()
        mismatches = []
        for qid, row in master.items():
            sources = json.loads(records[qid]["sources"])
            chunks = [row[f"reranked_chunk_id_{i}"] for i in (1, 2, 3)
                      if row[f"reranked_chunk_id_{i}"]]
            pages = [int(row[f"reranked_page_{i}"]) for i in (1, 2, 3)
                     if row[f"reranked_page_{i}"]]
            saved_chunks = [s["chunk"] for s in sources]
            saved_pages = [s["page"] for s in sources]
            flags = {"rank1_chunk": chunks[:1] == saved_chunks[:1],
                     "rank1_page": pages[:1] == saved_pages[:1],
                     "ordered_top3_chunks": chunks == saved_chunks,
                     "ordered_top3_pages": pages == saved_pages,
                     "top3_page_set": set(pages) == set(saved_pages)}
            counts.update({key: int(value) for key, value in flags.items()})
            if not all(flags.values()):
                mismatches.append(qid)
        agreement[strategy] = {**counts, "mismatch_query_count": len(mismatches),
                               "mismatch_samples": mismatches[:20]}
    # Independent saved-artifact disagreements invalidate subsequent partial joins.
    if any(a["mismatch_query_count"] for a in agreement.values()):
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "phase1_summary.md").open("x", encoding="utf-8") as handle:
            handle.write("# Phase 1 stopped: saved evidence disagreement\n\n" +
                         json.dumps(agreement, indent=2))
        raise SystemExit("Saved-artifact mismatch; stopped before cohort calculations.")

    evidence, cohorts, costs, visual = [], [], [], []
    cache_texts = {}
    hit1 = hit3 = 0
    for qid, row in master.items():
        sources = json.loads(checkpoints["retrieval_aware"][qid]["sources"])
        gt = set(json.loads(row["expected_pages"]))
        pages = [s["page"] for s in sources]
        chunks = [s["chunk"] for s in sources]
        h1, h3 = bool(gt.intersection(pages[:1])), bool(gt.intersection(pages[:3]))
        assert h1 == (row["gt_hit_at_1"] == "True")
        assert h3 == (row["gt_hit_at_3"] == "True")
        hit1 += h1
        hit3 += h3
        cache_path = ROOT / row["evidence_cache_path"]
        if cache_path not in cache_texts:
            with np.load(cache_path, allow_pickle=False) as cache:
                meta = json.loads(cache["metadata"][0])
                assert meta["chunk_size"] == 700 and meta["chunk_overlap"] == 100
                assert meta["embedding_model"] == "furiosa-ai/Qwen3-Embedding-8B"
                cache_texts[cache_path] = dict(zip(cache["chunk_ids"].tolist(),
                                                 cache["texts"].tolist(), strict=True))
        texts = [row[f"reranked_chunk_text_{i}"] for i in range(1, len(sources) + 1)]
        for rank, (source, text) in enumerate(zip(sources, texts, strict=True), 1):
            assert cache_texts[cache_path][source["chunk"]] == text
            evidence.append({"query_id": qid, "rank": rank, "chunk_id": source["chunk"],
                             "source_page": source["page"],
                             "retrieval_score": source["retrieval_score"],
                             "rerank_score": source["rerank_score"], "chunk_text": text,
                             "chunk_character_count": len(text),
                             "evidence_origin": "saved_ack_top3_not_fresh_retrieval"})
        cohort = "HIT_AT_3" if h3 else "MISS_AT_3_DEPTH_UNMEASURED"
        cohorts.append({"query_id": qid, "gt_pages": row["expected_pages"],
                        "top3_ranked_pages": json.dumps(pages),
                        "top3_ranked_chunks": json.dumps(chunks), "cohort": cohort,
                        "hit_at_3_not_at_1": h3 and not h1,
                        "depth_status": "BLOCKED_INITIAL_TOP_K_3"})
        costs.append({"query_id": qid, "top3_text_chars": sum(map(len, texts)),
                      "top6_text_chars": "", "top9_text_chars": "",
                      "top3_text_tokens": "", "top6_text_tokens": "", "top9_text_tokens": "",
                      "delta_tokens_3_to_6": "", "delta_tokens_6_to_9": "",
                      "delta_chars_3_to_6": "", "delta_chars_6_to_9": "",
                      "status": "DEPTH_UNMEASURED_TOKENIZER_NOT_USED"})
        if h3 and not h1:
            visual.append({"query_id": qid, "gt_pages": row["expected_pages"],
                           **{f"rank{i}_page": pages[i-1] if len(pages) >= i else ""
                              for i in (1, 2, 3)},
                           "gt_candidate_rank": next(i for i, p in enumerate(pages, 1) if p in gt),
                           "forced_text_judge_correct": row["forced_text_correct"],
                           "forced_vision_judge_correct": row["forced_vision_correct"],
                           "retrieval_aware_judge_correct": row["ra_correct"],
                           "retrieval_aware_route": row["ra_route"],
                           "retrieval_aware_selected_page": row["selected_page"],
                           "evidence_origin": "saved_ack_top3"})
    cohort_by_id = indexed(cohorts, "query_id")
    joined = [{**r, **{k: v for k, v in cohort_by_id[r["query_id"]].items() if k != "query_id"}}
              for r in human]
    valid = [r for r in joined if r["human_judge_valid"] == "YES"]
    action_counts = Counter((r["human_best_next_action"], r["cohort"]) for r in valid)
    failure_counts = Counter((r["human_failure_type"], r["cohort"]) for r in valid)
    subsets = {"GT_IN_TOP3_NOT_TOP1": len(visual),
               "AND_FORCED_VISION_INCORRECT": sum(r["forced_vision_judge_correct"] == "False" for r in visual),
               "AND_RA_INCORRECT": sum(r["retrieval_aware_judge_correct"] == "False" for r in visual),
               "AND_BOTH_INCORRECT": sum(r["forced_vision_judge_correct"] == "False" and
                                         r["retrieval_aware_judge_correct"] == "False" for r in visual),
               "AND_RA_TEXT_ONLY": sum(r["retrieval_aware_route"] == "TEXT_ONLY" for r in visual)}
    summary = {"status": "PARTIAL_BLOCKED_INITIAL_TOP_K_3", "query_count": 1600,
               "fresh_top3_reproduction": "NOT_RUN", "saved_artifact_agreement": agreement,
               "hit_counts_chunk_rank": {"1": hit1, "3": hit3, "6": None, "9": None},
               "cohorts": dict(Counter(r["cohort"] for r in cohorts)),
               "visual_subsets": subsets,
               "mean_top3_text_chars": sum(r["top3_text_chars"] for r in costs) / 1600,
               "human_valid_count": len(valid),
               "human_action_cohort_counts": [{"action": a, "cohort": c, "count": n}
                                               for (a, c), n in sorted(action_counts.items())],
               "human_failure_cohort_counts": [{"failure": a, "cohort": c, "count": n}
                                                for (a, c), n in sorted(failure_counts.items())],
               "input_sha256": before}
    OUT.mkdir(parents=True, exist_ok=True)
    write_csv("reranked_top3_audit.csv", evidence)
    write_csv("retrieval_depth_cohorts_partial.csv", cohorts)
    write_csv("text_depth_costs_partial.csv", costs)
    write_csv("visual_oracle_candidates.csv", visual)
    write_csv("human_review_oracle_join.csv", joined)
    report = ["# Oracle Headroom Phase 1 — partial / blocked", "",
              "## A. Reproducibility and actual ACK configuration",
              "Fresh Top-3 reproduction: NOT RUN. Saved-artifact consistency only:",
              "```json", json.dumps(agreement, indent=2), "```",
              "All 4,800 FT/FV/RA checkpoint records have initial retrieval top_k=3 and rerank top_n=3.",
              "The generic RagConfig default top_k=10 does not describe this experiment; benchmark CLI defaults and persisted fingerprints are 3.",
              "Therefore the unchanged candidate pool cannot provide reranked Top-9. No Top-9 file is produced, and no deeper cohort is imputed.",
              "Query set: benchmarks/unidoc.jsonl, 1,600 unique original queries, 1,028 per-row source PDFs under datasets/unidoc; no corpus-wide retrieval.",
              "Query/source_pdf fields and GT page sets agree across all three checkpoints and analysis_master. analysis_master sorts GT pages: 129 list-order differences, zero GT-set differences. Two historical dataset byte hashes occur (machine migration); current bytes match the later RA hash. Query semantics checked row by row.",
              "Models: furiosa-ai/Qwen3-Embedding-8B; furiosa-ai/Qwen3-Reranker-8B.",
              "Preprocessing: local PDFs; pypdf extract_text + strip, PyMuPDF get_text('text') fallback, no OCR. Existing cached chunks are reused for this artifact audit; no PDFs are re-extracted.",
              "Chunking: page-preserving 700 whitespace words, overlap 100 words; oversized inputs split at 7,600 UTF-8 bytes. Empty pages produce no chunks.",
              "Retrieval: original question embedding, NumPy float32 cosine similarity, np.argsort(scores)[::-1], top_k=3, no deduplication.",
              "Reranking: /rerank payload model/query/documents/top_n=3; no temperature or other inference parameters set by client. No reranker call in this audit.",
              "Top-k convention: ranked chunks, duplicate source pages retained; unique chunk IDs, no page/text deduplication. One query has only two saved chunks.",
              "Source pages: one-based physical PDF pages (enumeration starts at 1). GT: numeric dataset *_page_N image filename indices copied without +/-1 offset into expected_pages. ACK compares these directly; this audit preserves that mapping, without claiming every GT annotation is correct.",
              "All saved Top-3 texts match their document caches. All 1,600 referenced PDFs and cache paths exist; PDF bytes were not rehashed against the school manifest in this audit.",
              "Evidence: src/furiosa_rag/cli/benchmark_e2e.py, pipeline.py, retrieval.py, reranker.py, document.py, chunking.py; scripts/prepare_unidoc.py; persisted checkpoint fingerprint_config.",
              "## B. Retrieval depth structural reachability",
              f"Page-any-hit within ranked chunks: Hit@1={hit1}/1600 ({hit1/16:.4f}%); Hit@3={hit3}/1600 ({hit3/16:.4f}%). Matches historical 69.6875% / 86.0%.",
              "Hit@6, Hit@9 and incremental recovery: UNMEASURED. MISS_AT_3_HIT_AT_6, MISS_AT_6_HIT_AT_9, MISS_AT_9 counts: UNMEASURED.",
              f"HIT_AT_3={hit3}; MISS_AT_3_DEPTH_UNMEASURED={1600-hit3}; HIT_AT_3_NOT_AT_1={len(visual)} (overlapping flag).",
              "Unique-page-depth Hit@3/6/9: not computed, because only 2–3 chunks were saved; this is not a ranking of 3/6/9 unique pages. Unique-page Hit@1 equals chunk Hit@1.",
              "## C. Text evidence volume proxy",
              f"Mean Top-3 raw chunk characters: {summary['mean_top3_text_chars']:.4f}. Sum of chunk lengths; overlap and duplicate pages retained, headers/separators excluded.",
              "Top-3→6 and Top-6→9 character/token deltas: UNMEASURED. Tokens: not computed; no model tokenizer loaded. These are context-size proxies, not monetary inference cost.",
              "The baseline reranks only three retrieved candidates. Deeper evidence here would require expanding the initial retrieval candidate count and reranking the larger pool; it cannot be described as merely exposing already ranked evidence.",
              "## D. Visual escalation candidates (existing outcomes only)",
              "```json", json.dumps(subsets, indent=2), "```",
              "Correctness columns are existing answer-judge labels from analysis_master, not route-correctness flags. No new judge or inference ran. FALSE labels are counted explicitly; missing values are not interpreted as incorrect.",
              "## E. Human-review qualitative consistency",
              "All 72 reviewed rows joined; 49 valid YES cases used for the following tables. NO/UNCLEAR remain in the output but are excluded from valid counts.",
              "Log-state-balanced diagnostic sample: no population prevalence inference.",
              "| Valid human action | Available depth cohort | Count |", "| --- | --- | ---: |"]
    report += [f"| {a} | {c} | {n} |" for (a, c), n in sorted(action_counts.items())]
    report += ["", "| Valid primary failure | Available depth cohort | Count |", "| --- | --- | ---: |"]
    report += [f"| {a} | {c} | {n} |" for (a, c), n in sorted(failure_counts.items())]
    report += ["", "Human action/depth disagreement is not automatically annotation error: GT pages may omit sufficient pages, and multi-page sufficiency is not measured by page-any-hit.",
               "RETRIEVE_MORE despite HIT_AT_3: HR010, HR028, HR032, HR045, HR068, HR072. For example HR045 has GT [7,8,9,10] but human sufficient pages 8,9; hitting any GT page does not prove both required pages were acquired. HR072 has GT [8,9,10] but human sufficient page 9.",
               "## F. Limitations / Phase 2 blockers",
               "- GT page reachability != answer correctness.",
               "- GT page != necessarily sufficient page.",
               "- GT page != necessarily visual-required page.",
               "- Top-k expansion != guaranteed quality improvement.",
               "- Counterfactual action outcomes have not been measured; utility controller effects have not been validated.",
               "- Resolve the requirement conflict: preserving initial candidate count 3 precludes Top-9. An explicitly revised experiment must permit a larger initial pool, retain original queries/models/preprocessing, and distinguish this intervention from exposing existing ranks.",
               "- A larger pool can reorder Top-3. Independently compare new ranks to saved ACK evidence and apply a documented stop criterion; never force old candidates into new ranks.",
               "- Check fresh reproduction, candidate availability (including short documents), PDF/cache provenance and tokenizer availability before full depth/cost analysis.",
               "- Human review includes 21 NO and 2 UNCLEAR judge-validity cases; keep these separate from clean counterfactual comparisons. Human sufficient-page annotations can differ from benchmark GT.",
               "No controller, VLM, answer generation, answer judge, query rewrite, or new retrieval method was executed.",
               "No claim of accuracy improvement, baseline superiority, controller necessity, or novelty is supported by this partial audit."]
    with (OUT / "phase1_summary.md").open("x", encoding="utf-8") as handle:
        handle.write("\n\n".join(report) + "\n")
    assert before == {str(p.relative_to(ROOT)): sha(p) for p in protected}
    summary["protected_input_hashes_unchanged"] = True
    with (OUT / "phase1_audit.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in summary.items() if k != "input_sha256"}, indent=2))


if __name__ == "__main__":
    main()
