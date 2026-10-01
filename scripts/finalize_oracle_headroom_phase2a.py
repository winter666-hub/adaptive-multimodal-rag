"""Recover only missing judges, then export immutable-source canonical analysis."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import run_oracle_headroom_phase2a as base

OUT, ROOT, STATES = base.OUT, base.ROOT, base.STATES
RECOVERY = OUT / "phase2a_cleanup_judgments.checkpoint.jsonl"
FAILURES = OUT / "phase2a_cleanup_failures.checkpoint.jsonl"
MANIFEST = OUT / "phase2a_cleanup_manifest.json"


def records(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def index(rows):
    result = {(r["query_id"], r["evidence_state"]): r for r in rows}
    assert len(result) == len(rows)
    return result


def append(path, row):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-rounds", type=int, default=0)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    old_manifest = json.loads((OUT / "phase2a_run_manifest.json").read_text(encoding="utf-8"))
    master = {r["query_id"]: r for r in base.read_csv(ROOT / "results/error_analysis/analysis_master.csv")}
    cohorts = {r["query_id"]: r for r in base.read_csv(OUT / "retrieval_depth_cohorts.csv")}
    generations = index(records(OUT / "phase2a_generation.checkpoint.jsonl"))
    original = index(records(OUT / "phase2a_judgments.checkpoint.jsonl"))
    ids = old_manifest["primary_ids"]
    missing = [(q, s) for q in ids for s in STATES if (q, s) not in original]
    wide = base.read_csv(OUT / "text_expansion_counterfactual_judgments.csv")
    assert set(missing) == {(r["query_id"], s) for r in wide for s in STATES if r[f"{s}_status"] == "JUDGE_ERROR"}
    assert len(missing) == 16
    if not MANIFEST.exists():
        protected = {str(p.relative_to(ROOT)): base.sha(p) for p in OUT.iterdir() if p.is_file()}
        protected.update(old_manifest["input_sha256"])
        manifest = {"protected_sha256": protected, "missing_states": missing,
                    "judge_model": old_manifest["answer_model"],
                    "judge_prompt_sha256": old_manifest["judge_prompt_sha256"],
                    "judge_max_tokens": 512, "temperature": 0, "enable_thinking": False,
                    "correctness_threshold": base.AUDIT_CORRECTNESS_THRESHOLD,
                    "policy": "unchanged judge_answer and strict parser; no total repair; successes never rerun"}
        with MANIFEST.open("x", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for path, digest in manifest["protected_sha256"].items():
        assert base.sha(ROOT / path) == digest, path
    for path, digest in old_manifest["source_code_sha256"].items():
        assert base.sha(ROOT / path) == digest, path
    assert hashlib.sha256(base.AUDIT_JUDGE_PROMPT.encode()).hexdigest() == manifest["judge_prompt_sha256"]
    recovered = index(records(RECOVERY))
    assert set(recovered) <= set(missing) and not set(recovered) & set(original)
    if args.retry_rounds:
        settings = base.Settings.from_env(ROOT / ".env")
        endpoint = next(e for e in settings.endpoints if e.name == "llm")
        assert endpoint.model == manifest["judge_model"]
        llm = base.FuriosaLlm(endpoint, base.FuriosaClient(settings.api_key, max(settings.request_timeout, 120)))

        def judge(key):
            q, s = key
            started = time.perf_counter()
            try:
                score = base.judge_answer(llm, question=master[q]["question"], reference=master[q]["gold_answer"],
                                         candidate=generations[key]["answer"], prompt_template=base.AUDIT_JUDGE_PROMPT)
                return {"query_id": q, "evidence_state": s, "status": "OK",
                        "judge_correct": score.correctness >= base.AUDIT_CORRECTNESS_THRESHOLD,
                        "correctness_score": score.correctness, "completeness_score": score.completeness,
                        "grounding_score": score.grounding, "task_satisfaction_score": score.task_satisfaction,
                        "judge_reason": score.reason, "judge_raw_response": score.raw_response,
                        "judge_parser_mode": score.parser_mode, "judge_parser_policy_version": score.parser_policy_version,
                        "judge_attempt_count": score.attempt_count,
                        "judge_latency_ms": (time.perf_counter()-started)*1000, "timestamp_unix": time.time()}
            except Exception as exc:
                return {"query_id": q, "evidence_state": s, "status": "JUDGE_ERROR", "error": str(exc),
                        "judge_raw_response": getattr(exc, "raw_response", ""),
                        "judge_attempt_count": getattr(exc, "attempt_count", ""), "timestamp_unix": time.time()}

        for round_number in range(args.retry_rounds):
            pending = [k for k in missing if k not in recovered]
            if not pending:
                break
            with ThreadPoolExecutor(max_workers=4) as pool:
                for future in as_completed([pool.submit(judge, k) for k in pending]):
                    row = future.result()
                    append(RECOVERY if row["status"] == "OK" else FAILURES, row)
                    if row["status"] == "OK":
                        recovered[row["query_id"], row["evidence_state"]] = row
            print(f"Round {round_number+1}: recovered {len(recovered)}/16; remaining {16-len(recovered)}", flush=True)
    if args.export:
        export(ids, master, cohorts, generations, {**original, **recovered}, recovered, missing, old_manifest)
    for path, digest in manifest["protected_sha256"].items():
        assert base.sha(ROOT / path) == digest, path
    print(f"Recovery: {len(recovered)}/16", flush=True)


def export(ids, master, cohorts, generations, judgments, recovered, missing, old_manifest):
    pool = {}
    for r in base.read_csv(OUT / "expanded_reranked_candidates.csv"):
        pool.setdefault(r["query_id"], {})[r["chunk_id"]] = r
    canonical, queue, values, evidence_audit = [], [], [], []
    for q in ids:
        original = [{"chunk_id": master[q][f"reranked_chunk_id_{i}"], "source_page": int(master[q][f"reranked_page_{i}"]),
                     "chunk_text": master[q][f"reranked_chunk_text_{i}"]} for i in (1, 2, 3)]
        lookup = {**pool[q], **{r["chunk_id"]: r for r in original}}
        evidence = {s: [lookup[c] for c in json.loads(cohorts[q][f"{s}_ranked_chunks"])] for s in STATES}
        prompts = {s: base.state_prompt(master[q]["question"], evidence[s]) for s in STATES}
        for s in STATES:
            assert hashlib.sha256(prompts[s].encode()).hexdigest() == generations[q, s]["generation_prompt_sha256"]
            assert len(evidence[s]) == generations[q, s]["actual_chunk_count"]
        checks = {"ordered_chunk_ids_identical": [r["chunk_id"] for r in evidence["E6"]] == [r["chunk_id"] for r in evidence["E9"]],
                  "evidence_text_identical": [r["chunk_text"] for r in evidence["E6"]] == [r["chunk_text"] for r in evidence["E9"]],
                  "actual_chunk_count_identical": len(evidence["E6"]) == len(evidence["E9"]),
                  "prompt_input_identical": prompts["E6"] == prompts["E9"]}
        identical = all(checks.values())
        labels = {s: judgments.get((q, "E6" if identical and s == "E9" else s), {}).get("judge_correct") for s in STATES}
        answers = {s: generations[q, "E6" if identical and s == "E9" else s]["answer"] for s in STATES}
        reasons = {s: judgments.get((q, "E6" if identical and s == "E9" else s), {}).get("judge_reason", "") for s in STATES}
        raw6, raw9 = judgments.get((q, "E6")), judgments.get((q, "E9"))
        evidence_audit.append({"query_id": q, **checks, "e6_e9_input_identical": identical,
                               "E6_ordered_chunk_ids": json.dumps([r["chunk_id"] for r in evidence["E6"]]),
                               "E9_ordered_chunk_ids": json.dumps([r["chunk_id"] for r in evidence["E9"]]),
                               "E6_actual_chunk_count": len(evidence["E6"]), "E9_actual_chunk_count": len(evidence["E9"]),
                               "E6_prompt_sha256": generations[q, "E6"]["generation_prompt_sha256"],
                               "E9_prompt_sha256": generations[q, "E9"]["generation_prompt_sha256"],
                               "raw_answer_differs": generations[q, "E6"]["answer"] != generations[q, "E9"]["answer"],
                               "raw_correctness_differs": raw6["judge_correct"] != raw9["judge_correct"] if raw6 and raw9 else "",
                               "raw_judgment_differs": any(raw6[k] != raw9[k] for k in ("correctness_score", "completeness_score", "grounding_score", "task_satisfaction_score", "judge_reason")) if raw6 and raw9 else ""})
        row = {"query_id": q, "cohort": cohorts[q]["cohort"], "e6_e9_input_identical": identical,
               "canonical_e9_answer": answers["E9"], "canonical_e9_correct": labels["E9"] if labels["E9"] is not None else "",
               "canonical_e9_judgment_source": "E6" if identical else "E9",
               **{f"{s}_canonical_correct": labels[s] if labels[s] is not None else "" for s in STATES},
               **{f"{s}_raw_correct": judgments.get((q, s), {}).get("judge_correct", "") for s in STATES},
               "canonical_complete": all(v is not None for v in labels.values()),
               "raw_three_state_complete": all((q, s) in judgments for s in STATES)}
        canonical.append(row)
        if row["canonical_complete"]:
            values.append({"query_id": q, "cohort": cohorts[q]["cohort"], **labels})
        flags = {}
        for a, b in (("E3", "E6"), ("E6", "E9")):
            for name, x, y in (("wrong_to_correct", False, True), ("correct_to_wrong", True, False)):
                flags[f"{a}_to_{b}_{name}"] = labels[a] is x and labels[b] is y
        if any(flags.values()):
            queue.append({"query_id": q, "question": master[q]["question"], "gold_reference": master[q]["gold_answer"],
                          "gt_pages": master[q]["expected_pages"], "retrieval_cohort": cohorts[q]["cohort"],
                          "E3_evidence": json.dumps(evidence["E3"], ensure_ascii=False),
                          "E6_added_evidence": json.dumps(evidence["E6"][len(evidence["E3"]):], ensure_ascii=False),
                          "E9_added_evidence": json.dumps(evidence["E9"][len(evidence["E6"]):], ensure_ascii=False),
                          **{f"{s}_answer": answers[s] for s in STATES},
                          **{f"{s}_automatic_correct": labels[s] if labels[s] is not None else "" for s in STATES},
                          **{f"{s}_judge_reason": reasons[s] for s in STATES}, **flags,
                          "e6_e9_input_identical": identical,
                          **{f"human_{s.lower()}_correct": "" for s in STATES}, "human_notes": "", "human_confidence": ""})
    human = base.read_csv(ROOT / "results/error_analysis/human_review_progress.csv")
    diagnostic = [{"sample_id": r["sample_id"], "query_id": r["query_id"], "original_cohort": "HIT_AT_3",
                   "phase1b_exclusion_reason": "Phase 1B expanded only 224 original GT-MISS@3 queries; original GT-HIT@3 excluded by design.",
                   "E6_evidence_status": "UNAVAILABLE_NOT_RUN", "future_diagnostic_expansion_possible": True,
                   "future_requirement": "Separately authorized retrieval/reranking expansion of saved original E3; keep separate from primary 224.",
                   "cleanup_expansion_inference_run": False} for r in human if r["human_judge_valid"] == "YES" and r["human_best_next_action"] == "RETRIEVE_MORE" and cohorts[r["query_id"]]["cohort"] == "HIT_AT_3"]
    assert len(diagnostic) == 6
    unresolved = [{"query_id": q, "evidence_state": s} for q, s in missing if (q, s) not in judgments]
    metrics = {"initial_missing_states": 16, "recovered_states": len(recovered), "remaining_missing_states": unresolved,
               "raw_complete_queries": sum(r["raw_three_state_complete"] for r in canonical),
               "canonical_complete_queries": len(values), "primary_target": 224,
               "canonicalization_count": sum(r["e6_e9_input_identical"] for r in canonical),
               "identical_input_raw_answer_changes": sum(r["e6_e9_input_identical"] and r["raw_answer_differs"] for r in evidence_audit),
               "identical_input_raw_correctness_changes": sum(r["e6_e9_input_identical"] and r["raw_correctness_differs"] is True for r in evidence_audit),
               "primary": base.stats(values), "by_cohort": {c: base.stats([r for r in values if r["cohort"] == c]) for c in ("MISS_AT_3_HIT_AT_6", "MISS_AT_6_HIT_AT_9", "MISS_AT_9")},
               "human_review_queue_unique_queries": len(queue), "human_verified": False,
               "protected_sources_unchanged": True}
    for m in [metrics["primary"], *metrics["by_cohort"].values()]:
        for t in m["transitions"].values():
            t["net_change"] = t["WRONG_TO_CORRECT"] - t["CORRECT_TO_WRONG"]
    for name, rows in (("text_expansion_canonical_evaluation_final.csv", canonical),
                       ("text_expansion_input_consistency_final.csv", evidence_audit),
                       ("text_expansion_human_review_queue_final.csv", queue),
                       ("human_retrieve_more_hit3_diagnostic_final.csv", diagnostic)):
        base.write_csv(name, rows)
    base.write_csv("text_expansion_judge_recovery_final.csv", [{"query_id": q, "evidence_state": s,
                    "status": "RECOVERED" if (q, s) in recovered else "JUDGE_ERROR",
                    "judge_correct": recovered.get((q, s), {}).get("judge_correct", ""),
                    "judge_reason": recovered.get((q, s), {}).get("judge_reason", "")} for q, s in missing])
    with (OUT / "phase2a_automatic_final_metrics.json").open("x", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    lines = ["# Phase 2A automatic evaluation final", "", "This is automatic evaluation final, not a human-verified final result.", "",
             f"Missing judge states recovered: {len(recovered)}/16. Residual failures: {len(unresolved)}.",
             f"Raw fully judged queries: {metrics['raw_complete_queries']}/224; canonical complete: {len(values)}/224.",
             f"Identical E6/E9 inputs canonicalized: {metrics['canonicalization_count']}.",
             "Equality requires ordered chunk IDs, ordered evidence text, actual chunk count, and reconstructed full prompt all to match. Prompt hashes were checked against original generation records. For identical treatments, canonical E9 copies the E6 answer and unchanged judgment; independent generation/judge differences cannot be counted as added-evidence effects. All raw outputs remain unchanged.",
             "Recovery used the unchanged model, audit prompt, temperature 0, max_tokens 512, thinking disabled, one-user-message request, strict parser, two attempts per judge_answer call and correctness >=3. No total normalization, generation, retrieval, reranking, vision or new research experiment ran. Previously successful judgments were never rerun.",
             f"Human review queue: {len(queue)} unique queries; all human annotation fields blank.",
             "Six original HIT_AT_3 RETRIEVE_MORE diagnostics were outside Phase 1B's original-miss-only scope; no E6 was generated. Future separate diagnostic expansion is possible; none executed here.", "",
             "If any judgments remain unavailable, metrics use complete canonical three-state cases only, and are not exact full-224 accuracies. Missing judgments are never counted as wrong.",
             "", "| Population | complete/target | E3 accuracy | E6 accuracy | canonical E9 accuracy |", "|---|---:|---:|---:|---:|"]
    for name, m, target in [("PRIMARY", metrics["primary"], 224), *[(c, m, sum(cohorts[q]["cohort"] == c for q in ids)) for c, m in metrics["by_cohort"].items()]]:
        lines.append(f"| {name} | {m['n']}/{target} | " + " | ".join(f"{m['correct_counts'][s]}/{m['n']} ({m['accuracy_percent'][s]:.4f}%)" if m['n'] else "unavailable" for s in STATES) + " |")
    lines.extend(["", "Full transition counts and residual judge states:", "```json", json.dumps(metrics, indent=2), "```",
                  "Remaining blockers: residual invalid judge outputs if listed above; human verification pending. The six diagnostic expansions remain unmeasured by design. Protected source SHA-256 checks passed."])
    with (OUT / "phase2a_automatic_final_summary.md").open("x", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == "__main__":
    main()
