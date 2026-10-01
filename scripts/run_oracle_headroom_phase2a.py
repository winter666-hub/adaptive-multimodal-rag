"""Fixed-evidence text-only counterfactual generation and existing ACK judging.

No retrieval, reranking, vision, or controller is executed. Missing expansion
states stay unavailable, never copied from E3 or invented from document caches.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from furiosa_rag.cli.audit_route_ground_truth import (
    AUDIT_CORRECTNESS_THRESHOLD, AUDIT_JUDGE_PROMPT, AUDIT_JUDGE_PROMPT_VERSION,
)
from furiosa_rag.cli.evaluate_answer_quality import judge_answer
from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.llm import FuriosaLlm
from furiosa_rag.models import Chunk, RetrievedChunk
from furiosa_rag.pipeline import RagConfig, TextRagPipeline, clean_internal_citations

OUT = ROOT / "results/oracle_headroom"
FINALS = ("text_expansion_counterfactual_answers.csv", "text_expansion_counterfactual_judgments.csv",
          "text_expansion_transition_matrix.csv", "text_expansion_human_review_queue.csv",
          "human_retrieve_more_counterfactual.csv", "phase2a_summary.md", "phase2a_metrics.json")
STATES = ("E3", "E6", "E9")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(name, rows, fields=None):
    with (OUT / name).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def state_prompt(question, chunks):
    sources = tuple(RetrievedChunk(Chunk(r["chunk_id"], int(r["source_page"]), r["chunk_text"]), 0)
                    for r in chunks)
    return TextRagPipeline._answer_prompt(question, TextRagPipeline._text_context(sources))


def transitions(values, start, end):
    counts = Counter((r[start], r[end]) for r in values)
    return {"WRONG_TO_CORRECT": counts[False, True], "CORRECT_TO_WRONG": counts[True, False],
            "WRONG_TO_WRONG": counts[False, False], "CORRECT_TO_CORRECT": counts[True, True]}


def stats(values):
    n = len(values)
    correct = {s: sum(r[s] for r in values) for s in STATES}
    trans = {f"{a}_to_{b}": transitions(values, a, b)
             for a, b in (("E3", "E6"), ("E6", "E9"), ("E3", "E9"))}
    return {"n": n, "correct_counts": correct,
            "accuracy_percent": {s: correct[s] * 100 / n if n else None for s in STATES},
            "transitions": trans,
            "absolute_pp_E6_minus_E3": (correct["E6"]-correct["E3"])*100/n if n else None,
            "absolute_pp_E9_minus_E3": (correct["E9"]-correct["E3"])*100/n if n else None,
            "marginal_pp_E9_minus_E6": (correct["E9"]-correct["E6"])*100/n if n else None,
            "E6_recovery_rate_among_E3_errors_percent": trans["E3_to_E6"]["WRONG_TO_CORRECT"]*100/(n-correct["E3"]) if n-correct["E3"] else None,
            "E9_recovery_rate_among_E6_errors_percent": trans["E6_to_E9"]["WRONG_TO_CORRECT"]*100/(n-correct["E6"]) if n-correct["E6"] else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--finalize-partial", action="store_true",
                        help="export existing complete generations with missing judges explicitly unavailable; no API calls")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("workers must be 1..4")
    if any((OUT / n).exists() for n in FINALS):
        raise SystemExit("Phase 2A final outputs already exist; refusing overwrite")
    manifest_path = OUT / "phase2a_run_manifest.json"
    gen_path = OUT / "phase2a_generation.checkpoint.jsonl"
    judge_path = OUT / "phase2a_judgments.checkpoint.jsonl"
    if not args.resume and any(p.exists() for p in (manifest_path, gen_path, judge_path)):
        raise SystemExit("Existing Phase 2A checkpoint; use --resume")
    inputs = [ROOT / "benchmarks/unidoc.jsonl", ROOT / "docs/RESEARCH_NOTES.md",
              *sorted((ROOT / "results").glob("unidoc_full*.*")),
              *sorted((ROOT / "results/error_analysis").glob("*.*")),
              OUT / "expanded_reranked_candidates.csv", OUT / "retrieval_depth_cohorts.csv",
              OUT / "retrieval_expansion_costs.csv", OUT / "phase1b_metrics.json"]
    protected = {str(p.relative_to(ROOT)): sha(p) for p in inputs if p.is_file()}
    master_rows = read_csv(ROOT / "results/error_analysis/analysis_master.csv")
    master = {r["query_id"]: r for r in master_rows}
    cohort_rows = read_csv(OUT / "retrieval_depth_cohorts.csv")
    cohorts = {r["query_id"]: r for r in cohort_rows}
    human = read_csv(ROOT / "results/error_analysis/human_review_progress.csv")
    h22 = [r for r in human if r["human_judge_valid"] == "YES" and r["human_best_next_action"] == "RETRIEVE_MORE"]
    diagnostic = [r for r in h22 if cohorts[r["query_id"]]["cohort"] == "HIT_AT_3"]
    primary_ids = [r["query_id"] for r in cohort_rows if r["expanded_run_performed"] == "True"]
    assert len(primary_ids) == 224 and len(diagnostic) == 6 and len(h22) == 22
    assert Counter(cohorts[q]["cohort"] for q in primary_ids) == {
        "MISS_AT_3_HIT_AT_6": 173, "MISS_AT_6_HIT_AT_9": 23, "MISS_AT_9": 28}
    expansion = {}
    for row in read_csv(OUT / "expanded_reranked_candidates.csv"):
        expansion.setdefault(row["query_id"], {})[row["chunk_id"]] = row
    evidence = {}
    target_ids = primary_ids + [r["query_id"] for r in diagnostic]
    for qid in target_ids:
        row = master[qid]
        original = [{"chunk_id": row[f"reranked_chunk_id_{i}"],
                     "source_page": int(row[f"reranked_page_{i}"]),
                     "chunk_text": row[f"reranked_chunk_text_{i}"]}
                    for i in (1, 2, 3) if row[f"reranked_chunk_id_{i}"]]
        initial_ids = [r["chunk_id"] for r in original]
        assert initial_ids == json.loads(cohorts[qid]["E3_ranked_chunks"])
        evidence[qid, "E3"] = original
        if qid in primary_ids:
            lookup = {**expansion[qid], **{r["chunk_id"]: r for r in original}}
            for state in ("E6", "E9"):
                ids = json.loads(cohorts[qid][f"{state}_ranked_chunks"])
                assert ids[:len(original)] == initial_ids and len(set(ids)) == len(ids)
                evidence[qid, state] = [lookup[c] for c in ids]
                assert [int(r["source_page"]) for r in evidence[qid, state]] == json.loads(cohorts[qid][f"{state}_ranked_pages"])
            assert {r["chunk_id"] for r in original} <= {r["chunk_id"] for r in evidence[qid, "E6"]} <= {r["chunk_id"] for r in evidence[qid, "E9"]}
    # Stored FT matches query/evidence/model, but historical prompt/decoding were not logged.
    ft = {r["query_id"]: r for r in map(json.loads, (ROOT / "results/unidoc_full_forced_text.checkpoint.jsonl").read_text(encoding="utf-8").splitlines())}
    for qid in target_ids:
        assert ft[qid]["question"] == master[qid]["question"]
        assert [s["chunk"] for s in json.loads(ft[qid]["sources"])] == [s["chunk_id"] for s in evidence[qid, "E3"]]
        assert ft[qid]["fingerprint_config"]["models"]["final_llm"] == "furiosa-ai/Qwen3-32B-FP8"
    judge_sha = hashlib.sha256(AUDIT_JUDGE_PROMPT.encode()).hexdigest()
    old_judges = [json.loads(x) for x in (ROOT / "results/unidoc_full_comparison_judge.checkpoint.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["judge_prompt_sha256"] == judge_sha and r["judge_model_id"] == "furiosa-ai/Qwen3-32B-FP8" and
               r["judge_temperature"] == 0 and r["judge_max_tokens"] == 512 and r["judge_thinking_enabled"] is False for r in old_judges)
    settings = Settings.from_env(ROOT / ".env")
    endpoint = next(e for e in settings.endpoints if e.name == "llm")
    assert endpoint.model == "furiosa-ai/Qwen3-32B-FP8"
    llm = FuriosaLlm(endpoint, FuriosaClient(settings.api_key, max(settings.request_timeout, 120)))
    max_tokens = RagConfig().answer_max_tokens
    assert max_tokens == 1024
    prompts = {key: state_prompt(master[key[0]]["question"], chunks) for key, chunks in evidence.items()}
    manifest = {"input_sha256": protected, "answer_model": endpoint.model,
                "messages": "one user message; existing SYSTEM INSTRUCTION embedded in ACK user prompt",
                "temperature": 0, "enable_thinking": False, "answer_max_tokens": max_tokens,
                "judge_max_tokens": 512, "judge_correctness_threshold": AUDIT_CORRECTNESS_THRESHOLD,
                "judge_prompt_sha256": judge_sha, "judge_prompt_version": AUDIT_JUDGE_PROMPT_VERSION,
                "workers": args.workers, "primary_ids": primary_ids,
                "diagnostic_ids": [r["query_id"] for r in diagnostic],
                "missing_diagnostic_states": "E6/E9 unavailable: no saved Phase 1B expansion; no retrieval rerun",
                "E3_reused": False, "E3_reuse_reason": "Historical generation prompt hash and full decoding config not recorded; regenerate for paired evaluation",
                "generation_prompt_sha256": {f"{q}:{s}": hashlib.sha256(p.encode()).hexdigest() for (q, s), p in prompts.items()},
                "source_code_sha256": {str(p.relative_to(ROOT)): sha(p) for p in
                                       (ROOT / "src/furiosa_rag/pipeline.py", ROOT / "src/furiosa_rag/llm.py",
                                        ROOT / "src/furiosa_rag/cli/audit_route_ground_truth.py", ROOT / "src/furiosa_rag/cli/evaluate_answer_quality.py")}}
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    else:
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
    def load_checkpoint(path):
        result = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                key = row["query_id"], row["evidence_state"]
                assert key in prompts and key not in result
                result[key] = row
        return result
    generations, judgments = load_checkpoint(gen_path), load_checkpoint(judge_path)
    lock = threading.Lock()
    def append(path, row):
        with lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(key):
        qid, state = key
        if key in generations:
            generation = generations[key]
        else:
            started = time.perf_counter()
            answer = clean_internal_citations(llm.generate(prompts[key], max_tokens=max_tokens))
            elapsed = (time.perf_counter() - started) * 1000
            chunks = evidence[key]
            generation = {"query_id": qid, "cohort": cohorts[qid]["cohort"], "evidence_state": state,
                          "population": "PRIMARY_224" if qid in primary_ids else "HUMAN_DIAGNOSTIC_6",
                          "actual_chunk_count": len(chunks), "actual_unique_page_count": len({int(r["source_page"]) for r in chunks}),
                          "answer": answer, "generation_latency_ms": elapsed,
                          "input_character_count": len(prompts[key]), "input_token_count": "",
                          "evidence_character_count": sum(len(r["chunk_text"]) for r in chunks),
                          "generation_prompt_sha256": manifest["generation_prompt_sha256"][f"{qid}:{state}"],
                          "status": "OK", "error": ""}
            append(gen_path, generation)
        if key not in judgments:
            started = time.perf_counter()
            try:
                score = judge_answer(llm, question=master[qid]["question"], reference=master[qid]["gold_answer"],
                                     candidate=generation["answer"], prompt_template=AUDIT_JUDGE_PROMPT)
            except Exception as exc:
                append(OUT / "phase2a_judge_failures.checkpoint.jsonl",
                       {"query_id": qid, "evidence_state": state, "error": str(exc),
                        "judge_raw_response": getattr(exc, "raw_response", ""),
                        "judge_attempt_count": getattr(exc, "attempt_count", ""),
                        "timestamp_unix": time.time()})
                raise
            judgment = {"query_id": qid, "evidence_state": state, "judge_correct": score.correctness >= AUDIT_CORRECTNESS_THRESHOLD,
                        "correctness_score": score.correctness, "completeness_score": score.completeness,
                        "grounding_score": score.grounding, "task_satisfaction_score": score.task_satisfaction,
                        "judge_reason": score.reason, "judge_raw_response": score.raw_response,
                        "judge_parser_mode": score.parser_mode, "judge_parser_policy_version": score.parser_policy_version,
                        "judge_attempt_count": score.attempt_count, "judge_latency_ms": (time.perf_counter()-started)*1000,
                        "status": "OK", "error": ""}
            append(judge_path, judgment)
            return generation, judgment
        return generation, judgments[key]

    jobs = [key for key in prompts if key not in judgments]
    if args.finalize_partial:
        jobs = []
    random.Random(42).shuffle(jobs)
    print(f"Preflight: 224 primary x 3 states + 6 diagnostic E3; {len(prompts)} evaluable states, {len(jobs)} pending. No retrieval/reranking/vision calls.", flush=True)
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, key): key for key in jobs}
        for future in as_completed(futures):
            key = futures[future]
            try:
                generation, judgment = future.result()
                generations[key], judgments[key] = generation, judgment
                if len(judgments) % 20 == 0 or len(judgments) == len(prompts):
                    print(f"Generated and judged {len(judgments)}/{len(prompts)}", flush=True)
            except Exception as exc:
                errors.append({"query_id": key[0], "state": key[1], "error": str(exc)})
                print(f"Failed {key}: {type(exc).__name__}", flush=True)
    if errors:
        print(json.dumps(errors, ensure_ascii=True), flush=True)
        raise SystemExit("Incomplete state evaluations; generation checkpoints preserved. Resume missing judges/states; no complete paired statistics written.")
    assert len(generations) == len(prompts), "Cannot finalize: answers still missing"
    if not args.finalize_partial:
        assert len(judgments) == len(prompts)
    primary_values = [{"query_id": q, "cohort": cohorts[q]["cohort"],
                       **{s: judgments[q, s]["judge_correct"] for s in STATES}} for q in primary_ids
                      if all((q, s) in judgments for s in STATES)]
    metrics = {"status": "PARTIAL_JUDGE_ERRORS" if len(judgments) < len(prompts) else "PRIMARY_COMPLETE_DIAGNOSTIC_EXPANSION_UNAVAILABLE", "E3_reused": False,
               "primary": stats(primary_values), "by_cohort": {},
               "primary_target_count": 224, "complete_paired_primary_count": len(primary_values),
               "missing_judgments": [{"query_id": q, "state": s} for q, s in prompts if (q, s) not in judgments],
               "diagnostic_missing_E6_E9": [r["query_id"] for r in diagnostic]}
    metrics["primary_state_judgment_coverage"] = {
        s: {"judged_n": sum((q, s) in judgments for q in primary_ids),
            "known_correct": sum(judgments.get((q, s), {}).get("judge_correct") is True for q in primary_ids),
            "unavailable_n": sum((q, s) not in judgments for q in primary_ids)} for s in STATES}
    for coverage in metrics["primary_state_judgment_coverage"].values():
        coverage["full224_accuracy_lower_bound_percent"] = coverage["known_correct"]*100/224
        coverage["full224_accuracy_upper_bound_percent"] = (coverage["known_correct"] + coverage["unavailable_n"])*100/224
    metrics["available_pair_transitions"] = {}
    for a, b in (("E3", "E6"), ("E6", "E9"), ("E3", "E9")):
        available = [{a: judgments[q, a]["judge_correct"], b: judgments[q, b]["judge_correct"]}
                     for q in primary_ids if (q, a) in judgments and (q, b) in judgments]
        counts = transitions(available, a, b)
        metrics["available_pair_transitions"][f"{a}_to_{b}"] = {
            "n": len(available), "counts": counts,
            "net_change_count": counts["WRONG_TO_CORRECT"] - counts["CORRECT_TO_WRONG"]}
    for cohort in ("MISS_AT_3_HIT_AT_6", "MISS_AT_6_HIT_AT_9", "MISS_AT_9"):
        metrics["by_cohort"][cohort] = stats([r for r in primary_values if r["cohort"] == cohort])
    answers_out = [generations[q, s] for q in target_ids for s in STATES if (q, s) in generations]
    blank_answer = {k: "" for k in answers_out[0]}
    for row in diagnostic:
        for state in ("E6", "E9"):
            answers_out.append({**blank_answer, "query_id": row["query_id"], "cohort": "HIT_AT_3",
                                "evidence_state": state, "population": "HUMAN_DIAGNOSTIC_6", "status": "UNAVAILABLE_EVIDENCE",
                                "error": "No Phase 1B expanded evidence; retrieval rerun prohibited"})
    judged_out = []
    for qid in target_ids:
        row = {"query_id": qid, "cohort": cohorts[qid]["cohort"], "population": "PRIMARY_224" if qid in primary_ids else "HUMAN_DIAGNOSTIC_6"}
        for s in STATES:
            j = judgments.get((qid, s), {})
            row.update({f"{s}_correct": j.get("judge_correct", ""), f"{s}_correctness_score": j.get("correctness_score", ""),
                        f"{s}_judge_reason": j.get("judge_reason", ""),
                        f"{s}_status": j.get("status", "JUDGE_ERROR" if (qid, s) in prompts else "UNAVAILABLE_EVIDENCE")})
        judged_out.append(row)
    joined_by_id = {r["query_id"]: r for r in judged_out}
    human_out = [{"sample_id": r["sample_id"], "query_id": r["query_id"],
                  "original_depth_cohort": cohorts[r["query_id"]]["cohort"],
                  **{f"{s}_answer": generations.get((r["query_id"], s), {}).get("answer", "") for s in STATES},
                  **{k: v for k, v in joined_by_id[r["query_id"]].items() if k not in ("query_id", "cohort")},
                  "human_judge_valid": r["human_judge_valid"], "human_best_next_action": r["human_best_next_action"],
                  "human_sufficient_pages": r["human_sufficient_pages"]} for r in h22]
    h16_ids = {r["query_id"] for r in h22} & set(primary_ids)
    metrics["human_miss16"] = stats([r for r in primary_values if r["query_id"] in h16_ids])
    metrics["human_initial_hit6"] = {"n": 6, "E3_judged_n": sum((r["query_id"], "E3") in judgments for r in diagnostic),
                                     "E3_correct_count": sum(judgments.get((r["query_id"], "E3"), {}).get("judge_correct") is True for r in diagnostic),
                                     "E6_E9": "UNAVAILABLE"}
    transition_rows = []
    for label, group in [("PRIMARY_COMPLETE_PAIRED", metrics["primary"]), *metrics["by_cohort"].items(), ("HUMAN_MISS16", metrics["human_miss16"])]:
        for pair, counts in group["transitions"].items():
            for transition, count in counts.items():
                transition_rows.append({"population_or_cohort": label, "n": group["n"], "state_pair": pair,
                                        "transition": transition, "count": count})
    for pair, group in metrics["available_pair_transitions"].items():
        for transition, count in group["counts"].items():
            transition_rows.append({"population_or_cohort": "PRIMARY_AVAILABLE_PAIR",
                                    "n": group["n"], "state_pair": pair, "transition": transition, "count": count})
    queue = []
    identical_input_changes = []
    for qid in primary_ids:
        labels = []
        for a, b in (("E3", "E6"), ("E6", "E9")):
            if (qid, a) not in judgments or (qid, b) not in judgments:
                continue
            ja, jb = judgments[qid, a]["judge_correct"], judgments[qid, b]["judge_correct"]
            if ja != jb:
                labels.append(f"{a}_TO_{b}_{'CORRECT_TO_WRONG' if ja else 'WRONG_TO_CORRECT'}")
                if prompts[qid, a] == prompts[qid, b]:
                    identical_input_changes.append({"query_id": qid, "pair": f"{a}_to_{b}"})
        if labels:
            e3, e6, e9 = (evidence[qid, s] for s in STATES)
            queue.append({"query_id": qid, "question": master[qid]["question"], "gold_reference": master[qid]["gold_answer"],
                          "gt_pages": master[qid]["expected_pages"], "cohort": cohorts[qid]["cohort"],
                          "E3_evidence": json.dumps(e3, ensure_ascii=False),
                          "E6_added_evidence": json.dumps(e6[len(e3):], ensure_ascii=False),
                          "E9_added_evidence": json.dumps(e9[len(e6):], ensure_ascii=False),
                          **{f"{s}_answer": generations[qid, s]["answer"] for s in STATES},
                          **{f"{s}_automatic_correct": judgments.get((qid, s), {}).get("judge_correct", "") for s in STATES},
                          **{f"{s}_judge_reason": judgments.get((qid, s), {}).get("judge_reason", "") for s in STATES},
                          "transition_types": ";".join(labels),
                          "identical_E6_E9_input": prompts[qid, "E6"] == prompts[qid, "E9"],
                          "human_verdict": "", "human_notes": ""})
    metrics["human_review_queue_count"] = len(queue)
    metrics["identical_input_judgment_changes"] = identical_input_changes
    metrics["primary_state_costs"] = {}
    for state in STATES:
        rows = [generations[q, state] for q in primary_ids]
        metrics["primary_state_costs"][state] = {k: sum(r[k] for r in rows)/224 for k in
                                               ("actual_chunk_count", "actual_unique_page_count", "input_character_count",
                                                "evidence_character_count", "generation_latency_ms")}
    assert protected == {str(p.relative_to(ROOT)): sha(p) for p in inputs if p.is_file()}
    metrics["protected_input_hashes_unchanged"] = True
    write_csv("text_expansion_counterfactual_answers.csv", answers_out)
    write_csv("text_expansion_counterfactual_judgments.csv", judged_out)
    write_csv("text_expansion_transition_matrix.csv", transition_rows)
    queue_fields = ["query_id", "question", "gold_reference", "gt_pages", "cohort", "E3_evidence", "E6_added_evidence", "E9_added_evidence",
                    *[f"{s}_answer" for s in STATES], *[f"{s}_automatic_correct" for s in STATES], *[f"{s}_judge_reason" for s in STATES],
                    "transition_types", "identical_E6_E9_input", "human_verdict", "human_notes"]
    write_csv("text_expansion_human_review_queue.csv", queue, queue_fields)
    write_csv("human_retrieve_more_counterfactual.csv", human_out)
    with (OUT / "phase2a_metrics.json").open("x", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)
    report = ["# Phase 2A — Text Expansion Counterfactual Evaluation", "", "## Setup / E3 reuse decision",
              f"Evaluation status={metrics['status']}; primary target=224; complete paired judgments={len(primary_values)}. If incomplete, statistics below use only complete paired cases and do not represent all 224. Missing judges are never counted as incorrect. Strict parser failures require resolution before full-cohort kill-criteria calculations.",
              "E3 answers regenerated. Original FT query, original chunk order, and final model match, but historical generation prompt hash and full decoding config were not recorded; exact equality cannot be certified.",
              "All states use existing TextRagPipeline._text_context / _answer_prompt / clean_internal_citations and FuriosaLlm.generate. Model=furiosa-ai/Qwen3-32B-FP8; one user message with embedded SYSTEM INSTRUCTION, no separate system role; temperature=0, max_tokens=1024, enable_thinking=False, no other decoding parameters explicitly set. Per-request prompts and source code hashes are in phase2a_run_manifest.json.",
              "Judge is the unchanged UniDoc audit judge, not the Transformer-specific default judge. Existing judge prompt hash verified against all original records; model=Qwen3-32B-FP8, temperature=0, max_tokens=512, thinking=False, correctness>=3. Same parser and up-to-two format retries. Judge is blinded to state/cohort/latency and receives only question/reference/candidate.",
              "Only saved Phase 1B expansion and original ACK chunks used. E3 subset-or-equal E6 subset-or-equal E9 verified; short documents remain unpadded. Each available state is generated and judged independently, even if E6=E9; numerical/service nondeterminism or judge variability can create transitions with identical inputs.",
              "No retrieval/reranking, query rewriting, controller, or vision inference ran. Tokens not computed: local tokenizer libraries/cache unavailable.",
              "## Primary population: 224 original GT-MISS@3 queries",
              "Automatic judge results are first-pass paired evaluations, not ground truth and not full UniDoc-Bench accuracy.",
              "Per-state coverage (not exact full-224 accuracy when labels are missing). Bounds assume every unjudged state incorrect versus correct; they are bounds on this automatic evaluation, not ground-truth answer accuracy:", "```json", json.dumps(metrics["primary_state_judgment_coverage"], indent=2), "```",
              "```json", json.dumps(metrics["primary"], indent=2), "```", "## Retrieval cohorts",
              "```json", json.dumps(metrics["by_cohort"], indent=2), "```", "## Human diagnostic subset",
              "Primary available-pair transitions (separate from complete-three-state paired subset; missing judge excluded, not incorrect):",
              "```json", json.dumps(metrics["available_pair_transitions"], indent=2), "```",
              "22 valid RETRIEVE_MORE cases reported separately, never used to estimate population prevalence. The 16 original misses overlap primary; the additional 6 initial-hit cases have E3 only. Their E6/E9 evidence was never saved in Phase 1B; this stage forbids retrieval reruns. Those 12 state rows remain explicitly UNAVAILABLE, with no fabricated answer/correctness.",
              "```json", json.dumps({"miss16": metrics["human_miss16"], "initial_hit6": metrics["human_initial_hit6"]}, indent=2), "```",
              "## Human verification queue / anomalies",
              f"Queue size={len(queue)} unique queries with any E3->E6 or E6->E9 automatic binary-label change. No human verdict is filled automatically.",
              "Identical-input transitions (potential generation/judge variability):", "```json", json.dumps(identical_input_changes, indent=2), "```",
              "## Context and latency observations",
              "Primary-224 averages. Evidence characters sum raw chunk text; input characters include instructions/question/source headers. Tokens are blank, never estimated from characters. Generation latency is observed per-call wall time in ms, not isolated benchmark or monetary cost.",
              "```json", json.dumps(metrics["primary_state_costs"], indent=2), "```",
              f"Up to {args.workers} concurrent generation/judge workers, shuffled state request order (seed 42). Phase 1B candidates/pairs and observed retrieval/reranking timings remain in retrieval_expansion_costs.csv, separately from these generation timings. Its 15.27s reranking average with three concurrent requests is preliminary, not a final latency claim.",
              "## Limitations / next decisions",
              "- GT page recovery != answer recovery; GT page does not guarantee sufficient evidence.",
              "- Automatic judgment may be wrong; all changed labels need human verification. No human labels were inferred.",
              "- Primary accuracy is conditional on original GT-MISS@3, not the 1,600-query population.",
              "- Human balanced diagnostic sample cannot estimate population prevalence.",
              "- Six initial-hit diagnostic cases lack expansion evidence; resolve saved evidence availability separately without silently rerunning retrieval.",
              "- Same-input state changes are not attributable to added evidence; review before kill-criteria decisions.",
              "- Exact tokenizer and controlled compute timings remain needed for final cost claims.",
              "- No conclusion about controller effectiveness, full-benchmark superiority, adaptive routing, novelty, or kill/continue decision is made.",
              "All original ACK, human review, research notes, and consumed Phase 1B inputs verified unchanged by SHA-256."]
    with (OUT / "phase2a_summary.md").open("x", encoding="utf-8") as handle:
        handle.write("\n".join(report)+"\n")
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == "__main__":
    main()
