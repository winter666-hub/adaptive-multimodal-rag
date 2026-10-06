"""Complete ONLY the fixed cohort remainder; no controls, repeats or adjudication.

Reuses immutable Stage 1 execute_trial / transport journaling. It does not call
the original pilot runner and never retrieves, reranks or chooses a GT page.
Prepare freezes all 109 states before any inference. Completed failures remain
unavailable and are not silently retried by resume.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
BASE = ROOT / "results/oracle_headroom/phase2b_visual_oracle"
STAGE1 = BASE / "stage1_pilot"
OUT = BASE / "stage2_remaining"
SCRIPT = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location("stage1_execution", ROOT / "scripts/run_phase2b_stage1_pilot.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
pilot.OUT = OUT
HUMAN_FIELDS = [
    "human_reference_valid", "human_rank1_answer_correct", "human_alternative_answer_correct",
    "human_top3_text_evidence_sufficient", "human_rank1_visual_evidence_sufficient",
    "human_alternative_visual_evidence_useful", "source_verified_outcome_recovery",
    "strong_page_selection_attributable", "attribution_category", "human_confidence",
    "pdf_pages_checked", "notes",
]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def protected():
    paths = []
    for folder in ("results", "scripts", "tests", "src", "docs", "benchmarks", "datasets/unidoc"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file()
                     and OUT not in p.parents and p != SCRIPT and "__pycache__" not in p.parts)
    for folder in ROOT.glob("review_upload_batch*"):
        paths.extend(p for p in folder.rglob("*") if p.is_file())
    return {p.relative_to(ROOT).as_posix(): pilot.file_hash(p) for p in sorted(set(paths))}


def prepare():
    if OUT.exists():
        raise FileExistsError("Stage 2 directory exists; refusing manifest overwrite")
    audit = read_json(BASE / "phase2b_design_audit.json")
    original = read_json(STAGE1 / "pilot_manifest.json")
    if not read_json(STAGE1 / "pilot_alt_repeat_summary.json")["pre_specified_GO_criterion_satisfied"]:
        raise ValueError("Stage 1 GO prerequisite not satisfied")
    if pilot.file_hash(ROOT / "scripts/run_phase2b_stage1_pilot.py") != original["script_sha256"]:
        raise ValueError("Original Stage 1 execution functions changed")
    settings = pilot.Settings.from_env(ROOT / ".env")
    if pilot.settings_dict(settings) != original["actual_config"]:
        raise ValueError("Model/configuration/runtime differs from Stage 1; no silent substitution")
    for path, expected in audit["input_source_sha256"].items():
        if pilot.file_hash(ROOT / path) != expected:
            raise ValueError(f"Audited source changed: {path}")
    master = pilot.read_csv(ROOT / "results/error_analysis/analysis_master.csv")
    if len(master) != len({r["query_id"] for r in master}):
        raise ValueError("Duplicate master query")
    cohort = sorted([r for r in master if r["gt_hit_at_3"] == "True" and r["gt_hit_at_1"] == "False"
                     and r["forced_text_correct"] == "False" and r["forced_vision_correct"] == "False"],
                    key=lambda r: r["query_id"])
    cohort_ids = {r["query_id"] for r in cohort}
    first_ids = {q["query_id"] for q in original["frozen_queries"]}
    remaining = [r for r in cohort if r["query_id"] not in first_ids]
    remaining_ids = {r["query_id"] for r in remaining}
    declared = audit["cohorts"]["STAGE1_GT_IN_TOP3_NOT_TOP1_AND_TEXT_WRONG_AND_VISION_WRONG"]
    ids_hash = hashlib.sha256(json.dumps([r["query_id"] for r in cohort], separators=(",", ":")).encode()).hexdigest()
    if ids_hash != declared["query_id_set_sha256"]:
        raise ValueError("Fixed cohort membership differs from design audit")
    if not (len(cohort) == 133 and len(first_ids) == 24 and len(remaining) == 109
            and not first_ids & remaining_ids and first_ids | remaining_ids == cohort_ids):
        raise ValueError("STOP: fixed cohort reconciliation failed")
    total_pages = sum(len({int(r[f"reranked_page_{i}"]) for i in range(1, 4)
                           if r[f"reranked_chunk_id_{i}"]}) for r in cohort)
    if total_pages != declared["unique_top3_page_candidates"]:
        raise ValueError("Full cohort candidate count differs from audit")
    first_pages = sum(len(q["candidate_pages"]) for q in original["frozen_queries"])
    with (ROOT / "results/unidoc_full_forced_text.checkpoint.jsonl").open(encoding="utf-8") as f:
        checkpoints = {r["query_id"]: r for line in f if (r := json.loads(line))}
    frozen, pdf_hashes = [], {}
    for index, row in enumerate(remaining, 1):
        old = checkpoints[row["query_id"]]
        if old["question"] != row["question"] or old["source_pdf"] != row["source_pdf"]:
            raise ValueError("Original ACK question/PDF identity differs")
        chunks = []
        for rank, source in enumerate(json.loads(old["sources"]), 1):
            if (source["chunk"], source["page"]) != (row[f"reranked_chunk_id_{rank}"], int(row[f"reranked_page_{rank}"])):
                raise ValueError("Original ACK ordered source mapping differs")
            chunks.append({"rank": rank, "chunk_id": source["chunk"], "page": source["page"],
                           "text": row[f"reranked_chunk_text_{rank}"],
                           "retrieval_score": source["retrieval_score"], "rerank_score": source["rerank_score"]})
        if not chunks or len(chunks) > 3:
            raise ValueError("Invalid original source count")
        # Deployable candidates are constructed ONLY from the original chunk pages.
        pages = list(dict.fromkeys(c["page"] for c in chunks))
        if any(type(p) is not int or p <= 0 for p in pages):
            raise ValueError("Invalid physical page")
        pdf = ROOT / "datasets/unidoc" / row["source_pdf"]
        if row["source_pdf"] not in pdf_hashes:
            pdf_hashes[row["source_pdf"]] = pilot.file_hash(pdf)
        query = {"review_id": f"P2B_S2_{index:03d}", "query_id": row["query_id"],
                 "document_id": row["document_id"], "source_pdf": row["source_pdf"],
                 "pdf_sha256": pdf_hashes[row["source_pdf"]], "question": row["question"],
                 "reference_answer": row["gold_answer"], "ordered_chunks": chunks,
                 "candidate_pages": pages, "rank1_page": chunks[0]["page"]}
        query["decision_state_sha256"] = pilot.digest(pilot.canonical({k: query[k] for k in (
            "question", "pdf_sha256", "ordered_chunks")}).encode())
        frozen.append(query)
    candidates = sum(len(q["candidate_pages"]) for q in frozen)
    if candidates != total_pages - first_pages or candidates != 317:
        raise ValueError("STOP: remaining candidate count does not reconcile")
    costs = {"VLM_calls": candidates, "final_generation_calls": candidates,
             "logical_judge_calls": candidates, "max_judge_request_attempts": 2 * candidates,
             "rank1_repeat_controls": 0, "text_controls": 0, "alternative_confirmation_repeats": 0}
    reconciliation = {"fixed_cohort_queries": len(cohort), "stage1_queries": len(first_ids),
                      "stage2_queries": len(frozen), "overlap": len(first_ids & remaining_ids),
                      "union": len(first_ids | remaining_ids), "cohort_pages": total_pages,
                      "stage1_pages": first_pages, "stage2_pages": candidates,
                      "stage2_distinct_PDFs": len(pdf_hashes)}
    print("PRE-RUN COST GATE", pilot.canonical(reconciliation), pilot.canonical(costs), flush=True)
    print("Fixed remainder IDs saved in manifest, ordered by query_id; no resampling or GT page selection", flush=True)
    snapshot = protected()
    manifest = {"schema_version": "phase2b-stage2-fixed-remainder-v1", "created_at_kst": pilot.stamp(),
        "reconciliation": reconciliation, "planned_calls": costs, "frozen_queries": frozen,
        "stage1_excluded_query_ids": sorted(first_ids), "fixed_cohort_query_ids": sorted(cohort_ids),
        "fixed_cohort_id_sha256": ids_hash, "actual_config": original["actual_config"],
        "original_executor_sha256": original["script_sha256"], "stage2_executor_sha256": pilot.file_hash(SCRIPT),
        "stage1_manifest_sha256": pilot.file_hash(STAGE1 / "pilot_manifest.json"),
        "design_audit_sha256": pilot.file_hash(BASE / "phase2b_design_audit.json"),
        "source_input_hashes": {p: pilot.file_hash(ROOT / p) for p in audit["input_source_sha256"]},
        "ordering_seed": "phase2b-stage2-fixed-remainder-v1", "workers": 3,
        "GT_policy": "Retrospective cohort construction only; no GT pages in runtime state or candidate selection",
        "retry_policy": "No VLM/final retry; original judge two attempts only; append-only receipts preserve every attempt; completed failures skipped on resume",
        "scope": "User Stage2 means remaining 109 of the fixed 133 cohort; broader 499-case option in the design is NOT executed. No controls, positive repeats, human adjudication or router.",
        "protected_file_count": len(snapshot), "protected_snapshot_sha256": pilot.digest(pilot.canonical(snapshot).encode())}
    OUT.mkdir()
    pilot.write_json(OUT / "protected_inputs.json", snapshot, True)
    pilot.write_json(OUT / "stage2_manifest.json", manifest, True)
    print("STAGE2 MANIFEST SAVED BEFORE API CALLS", pilot.file_hash(OUT / "stage2_manifest.json"), flush=True)


def manifest():
    value = read_json(OUT / "stage2_manifest.json")
    identities = [(SCRIPT, value["stage2_executor_sha256"]),
                  (ROOT / "scripts/run_phase2b_stage1_pilot.py", value["original_executor_sha256"]),
                  (STAGE1 / "pilot_manifest.json", value["stage1_manifest_sha256"]),
                  (BASE / "phase2b_design_audit.json", value["design_audit_sha256"])]
    for path, expected in identities:
        if pilot.file_hash(path) != expected:
            raise ValueError(f"Frozen configuration source changed: {path}")
    return value


def jobs_for(value):
    jobs = [(q, "VISUAL_CANDIDATE", page, rank, f"{q['review_id']}__PAGE_{page}")
            for q in value["frozen_queries"] for rank, page in enumerate(q["candidate_pages"], 1)]
    return sorted(jobs, key=lambda j: (pilot.digest((value["ordering_seed"] + "|" + j[4]).encode()), j[4]))


def run():
    value = manifest()
    settings = pilot.Settings.from_env(ROOT / ".env")
    if pilot.settings_dict(settings) != value["actual_config"]:
        raise ValueError("Configured model/decoding/runtime changed; refusing execution")
    for path, expected in {q["source_pdf"]: q["pdf_sha256"] for q in value["frozen_queries"]}.items():
        if pilot.file_hash(ROOT / "datasets/unidoc" / path) != expected:
            raise ValueError("Frozen source PDF changed")
    journal = pilot.Journal(value)
    checkpoint = OUT / "trial_results.checkpoint.jsonl"
    previous = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()] if checkpoint.exists() else []
    completed = {r["state_id"] for r in previous}
    if len(completed) != len(previous):
        raise ValueError("Duplicate completed state")
    if journal.events.exists():
        started = {json.loads(line)["state_id"] for line in journal.events.read_text(encoding="utf-8").splitlines()
                   if json.loads(line)["event"] == "request_started"}
        if started - completed:
            raise ValueError("Interrupted state has receipts; no hidden retry allowed")
    pending = [j for j in jobs_for(value) if j[4] not in completed]
    print("RUN COST GATE", pilot.canonical(value["reconciliation"]), pilot.canonical(value["planned_calls"]),
          "pending=" + str(len(pending)), flush=True)
    with ThreadPoolExecutor(max_workers=value["workers"]) as executor:
        for future in as_completed([executor.submit(pilot.execute_trial, job, settings, value, journal) for job in pending]):
            future.result()
    summarize()


def finish_reasons(result, key):
    return [r["response"].get("choices", [{}])[0].get("finish_reason", "NOT_REPORTED")
            for r in result.get(key, []) if r["response"]]


def automatic_events(value, by):
    positives, unresolved, any_alternative = [], [], []
    for q in value["frozen_queries"]:
        base = by[f"{q['review_id']}__PAGE_{q['rank1_page']}"]
        alternatives = [by[f"{q['review_id']}__PAGE_{p}"] for p in q["candidate_pages"] if p != q["rank1_page"]]
        correct = [r for r in alternatives if r["analysis_correct"] is True]
        info = {"review_id": q["review_id"], "query_id": q["query_id"], "document_id": q["document_id"],
                "rank1_page": q["rank1_page"], "rank1_status": base["status"],
                "correct_alternative_pages": [r["page_id"] for r in correct],
                "unavailable_alternative_pages": [r["page_id"] for r in alternatives if r["analysis_correct"] is None]}
        if correct:
            any_alternative.append(info)
        if base["analysis_correct"] is False and correct:
            positives.append(info)
        if base["analysis_correct"] is None or (base["analysis_correct"] is False and not correct and info["unavailable_alternative_pages"]):
            unresolved.append({**info, "reason": "RANK1_UNAVAILABLE" if base["analysis_correct"] is None else "NO_KNOWN_CORRECT_ALTERNATIVE_BUT_INCOMPLETE"})
    return positives, unresolved, any_alternative


def summarize():
    value = manifest()
    rows = [json.loads(line) for line in (OUT / "trial_results.checkpoint.jsonl").read_text(encoding="utf-8").splitlines()]
    by = {r["state_id"]: r for r in rows}
    jobs = jobs_for(value)
    if len(by) != len(rows) or set(by) != {j[4] for j in jobs}:
        raise ValueError("Stage2 state inventory incomplete/duplicated")
    positives, unresolved, any_alt = automatic_events(value, by)
    expected_event_ids = {r["query_id"] for r in positives}
    flat = []
    fields = ["state_id", "review_id", "query_id", "document_id", "source_pdf", "trial_role", "page_id",
              "candidate_rank", "source_chunk_ranks", "decision_state_sha256", "rendered_page_path", "rendered_page_sha256",
              "VLM_status", "VLM_raw_output", "visual_context", "final_raw_answer", "answer", "judge_raw_outputs",
              "judge_scores", "judge_correct", "analysis_correct", "status", "errors", "latency_ms", "call_ids",
              "actual_config", "text_context_sha256", "final_prompt_sha256"]
    for result in sorted(rows, key=lambda r: (r["review_id"], r["candidate_rank"])):
        flat.append({**{k: result.get(k) for k in fields},
                     "VLM_finish_reasons": finish_reasons(result, "VLM_receipts"),
                     "FINAL_finish_reasons": finish_reasons(result, "FINAL_receipts"),
                     "JUDGE_finish_reasons": finish_reasons(result, "JUDGE_receipts")})
    pilot.csv_write(OUT / "stage2_results.csv", flat)
    queue = []
    packet = ["# Phase 2B Stage 2 automatic-positive human/source review packet", "",
              "All correctness labels below are AUTOMATIC only. No source adjudication or repeat confirmation has been performed. Human annotation fields are blank; this packet is not a confirmed-recovery result.", "",
              "Use the Stage1 taxonomy: outcome recovery; STRONG_PAGE_SELECTION_ATTRIBUTABLE; MIXED_OR_GENERATION_ASSISTED; REFERENCE_INVALID_OR_UNCLEAR; NOT_A_RECOVERY; UNCLEAR. Verify the original PDF and reference claim independently. Page numbers are one-based physical/source pages.", ""]
    qby = {q["review_id"]: q for q in value["frozen_queries"]}
    for info in positives:
        q = qby[info["review_id"]]
        primary = by[f"{q['review_id']}__PAGE_{q['rank1_page']}"]
        alternative_results = [by[f"{q['review_id']}__PAGE_{p}"] for p in info["correct_alternative_pages"]]
        entry = {**info, "pdf_path": str((ROOT / "datasets/unidoc" / q["source_pdf"]).resolve()),
                 "question": q["question"], "reference_answer": q["reference_answer"],
                 "ordered_Top3_chunks": q["ordered_chunks"], "candidate_pages": q["candidate_pages"],
                 "rank1_answer": primary["answer"], "rank1_judge_scores": primary["judge_scores"],
                 "rank1_judge_raw_outputs": primary["judge_raw_outputs"],
                 "rank1_rendered_page_path": primary.get("rendered_page_path"),
                 "alternative_page_results": [{k: r.get(k) for k in ("page_id", "source_chunk_ranks", "answer",
                        "VLM_raw_output", "judge_scores", "judge_raw_outputs", "rendered_page_path", "rendered_page_sha256", "status")}
                                               for r in alternative_results],
                 **{field: "" for field in HUMAN_FIELDS}, "source_verification_status": "PENDING"}
        queue.append(entry)
        packet += ["## " + q["review_id"] + " / " + q["query_id"], "",
                   "Original PDF: [" + q["document_id"] + ".pdf](" + (ROOT / "datasets/unidoc" / q["source_pdf"]).as_posix() + ")", "",
                   "Candidate physical pages in first-occurrence rank order: " + str(q["candidate_pages"]), "",
                   "### Question", "", q["question"], "", "### Reference claim to verify", "", q["reference_answer"], "",
                   "### Original frozen Top-3 evidence", ""]
        for chunk in q["ordered_chunks"]:
            packet += [f"#### Rank {chunk['rank']} / {chunk['chunk_id']} / physical page {chunk['page']}",
                       "", "```text", chunk["text"], "```", ""]
        for title, result in [("Fresh rank1 candidate", primary)] + [("Automatic-correct alternative", r) for r in alternative_results]:
            png = Path(result["rendered_page_path"]).relative_to(OUT.relative_to(ROOT)).as_posix()
            packet += ["### " + title + " / physical page " + str(result["page_id"]), "",
                       "Rendered source page: [PNG](" + png + ")", "",
                       "Automatic correctness: " + str(result["analysis_correct"]) + "; status: " + result["status"], "",
                       "Answer:", "", "```text", result["answer"], "```", "", "Raw VLM evidence:", "", "```text",
                       result["VLM_raw_output"], "```", "", "Judge scores:", "", "```json",
                       json.dumps(result["judge_scores"], ensure_ascii=False, indent=2), "```", "",
                       "Raw judge attempts:", "", "```text", "\n\n".join(result["judge_raw_outputs"]), "```", ""]
        packet += ["### Human/source fields — blank", ""] + ["- " + f + ":" for f in HUMAN_FIELDS] + [""]
    queue_fields = list(queue[0]) if queue else ["review_id", "query_id", "document_id", "rank1_page", "correct_alternative_pages", *HUMAN_FIELDS, "source_verification_status"]
    if len(queue) != len(expected_event_ids) or any(entry[f] for entry in queue for f in HUMAN_FIELDS):
        raise ValueError("Positive queue duplicate or nonblank human field")
    pilot.csv_write(OUT / "stage2_positive_review_queue.csv", queue, queue_fields)
    (OUT / "stage2_positive_review_packet.md").write_text("\n".join(packet), encoding="utf-8")
    events = [json.loads(line) for line in (OUT / "raw_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    starts = [r for r in events if r["event"] == "request_started"]
    ends = [r for r in events if r["event"] == "request_completed"]
    if len({r["call_id"] for r in starts}) != len(starts) or {r["call_id"] for r in starts} != {r["call_id"] for r in ends}:
        raise ValueError("Raw call provenance incomplete/duplicated")
    counts = {stage: {"attempted": sum(r["stage"] == stage for r in starts),
                      "transport_succeeded": sum(r["stage"] == stage and r["http_success"] for r in ends),
                      "transport_failed": sum(r["stage"] == stage and not r["http_success"] for r in ends)}
              for stage in ("VLM", "FINAL", "JUDGE")}
    if any(counts[stage]["attempted"] > value["planned_calls"][key] for stage, key in (
            ("VLM", "VLM_calls"), ("FINAL", "final_generation_calls"), ("JUDGE", "max_judge_request_attempts"))):
        raise ValueError("API cost cap exceeded")
    if any(r["trial_role"] != "VISUAL_CANDIDATE" for r in rows):
        raise ValueError("Forbidden control/repeat trial")
    rank1 = [by[f"{q['review_id']}__PAGE_{q['rank1_page']}"] for q in value["frozen_queries"]]
    for q in value["frozen_queries"]:
        qr = [by[f"{q['review_id']}__PAGE_{p}"] for p in q["candidate_pages"]]
        if len({r["text_context_sha256"] for r in qr}) != 1:
            raise ValueError("Original text/order changed across candidates")
        if any(r["decision_state_sha256"] != q["decision_state_sha256"] for r in qr):
            raise ValueError("Original state identity changed")
    unavailable = [{k: r[k] for k in ("state_id", "review_id", "query_id", "page_id", "status", "errors")}
                   for r in rows if r["analysis_correct"] is None]
    truncation = {stage: [{"call_id": r["call_id"], "state_id": r["state_id"]}
                         for r in ends if r["stage"] == stage and r["http_success"]
                         and r["response"].get("choices", [{}])[0].get("finish_reason") == "length"]
                  for stage in ("VLM", "FINAL", "JUDGE")}
    print("Checking all protected original files", flush=True)
    snapshot = read_json(OUT / "protected_inputs.json")
    if protected() != snapshot:
        raise ValueError("Protected files changed")
    integrity = {"status": "PASS", "reconciliation": value["reconciliation"], "all_expected_states_once": True,
                 "completed_states": len(rows), "raw_started_completed_call_pairs_match": True,
                 "no_rank1_repeat_or_text_control": True, "candidate_pages_from_original_Top3_only": True,
                 "fixed_original_text_contexts": True, "Stage1_overlap": 0,
                 "judge_unavailable_not_false": True, "human_annotation_fields_blank": True,
                 "protected_files_unchanged": True, "protected_file_count": len(snapshot),
                 "protected_snapshot_sha256": value["protected_snapshot_sha256"],
                 "stage2_manifest_sha256": pilot.file_hash(OUT / "stage2_manifest.json"),
                 "source_adjudication_performed": False, "positive_confirmation_repeats": 0,
                 "router_controller_work": False, "population_recovery_rate_computed": False}
    summary = {"scope": "Fixed selected diagnostic-cohort remainder only; not a population recovery rate", "reconciliation": value["reconciliation"],
               "call_counts": counts, "VLM_operations_succeeded": sum(r["VLM_status"] == "OK" for r in rows),
               "VLM_operations_failed": sum(r["VLM_status"] != "OK" for r in rows),
               "judge_logical_evaluations_attempted": sum(bool(r.get("JUDGE_receipts")) for r in rows),
               "judge_validation_unavailable_states": [r for r in unavailable if r["status"] == "JUDGE_ERROR_UNAVAILABLE"],
               "all_unavailable_states": unavailable, "state_status_counts": dict(Counter(r["status"] for r in rows)),
               "fresh_rank1_correct": sum(r["analysis_correct"] is True for r in rank1),
               "fresh_rank1_wrong": sum(r["analysis_correct"] is False for r in rank1),
               "fresh_rank1_unavailable": sum(r["analysis_correct"] is None for r in rank1),
               "queries_with_correct_alternative": len(any_alt), "queries_with_correct_alternative_cases": any_alt,
               "automatic_positive_count": len(positives), "automatic_positive_cases": positives,
               "multiple_positive_page_cases": [r for r in positives if len(r["correct_alternative_pages"]) > 1],
               "multiple_correct_alternative_cases_all_queries": [r for r in any_alt if len(r["correct_alternative_pages"]) > 1],
               "unresolved_queries": unresolved, "rank1_unavailable_queries": [r for r in unresolved if r["reason"] == "RANK1_UNAVAILABLE"],
               "review_queue_unique_queries": len(queue), "token_cap_cases": truncation,
               "human_source_verification": "PENDING", "protected_files_unchanged": True,
               "protected_file_count": len(snapshot), "rank1_repeat_calls": 0, "fresh_text_calls": 0,
               "positive_repeat_confirmation_calls": 0, "router_controller_work": False,
               "population_recovery_rate_computed": False, "paper_conclusions_written": False}
    pilot.write_json(OUT / "stage2_execution_integrity_report.json", integrity)
    pilot.write_json(OUT / "stage2_summary.json", summary)
    md = ["# Phase 2B Stage 2 fixed-cohort remainder: automatic diagnostic summary", "", summary["scope"], "",
          "No human/source adjudication or repeat confirmation was performed. Automatic-positive cases are not source-verified recoveries. Stop for review; no paper conclusions or controller work.", "",
          "Reconciliation: " + pilot.canonical(value["reconciliation"]), "",
          "| API stage | Attempted | Transport succeeded | Transport failed |", "|---|---:|---:|---:|"]
    md.extend(f"| {stage} | {c['attempted']} | {c['transport_succeeded']} | {c['transport_failed']} |" for stage, c in counts.items())
    md += ["", f"Logical judges attempted: {summary['judge_logical_evaluations_attempted']}; judge validation UNAVAILABLE states: {len(summary['judge_validation_unavailable_states'])}.",
           f"Fresh normal rank1 correct/wrong/unavailable: {summary['fresh_rank1_correct']}/{summary['fresh_rank1_wrong']}/{summary['fresh_rank1_unavailable']}.",
           f"Queries with any known-correct alternative: {len(any_alt)}. Automatic positives / unique review queue: {len(positives)}.",
           f"Multiple-correct-alternative cases within automatic positives: {len(summary['multiple_positive_page_cases'])}.", "",
           "## Automatic-positive cases", "", "| Review ID | Query ID | PDF | Rank1 page | All correct alternative pages |", "|---|---|---|---:|---|"]
    md.extend(f"| {r['review_id']} | {r['query_id']} | {r['document_id']} | {r['rank1_page']} | {r['correct_alternative_pages']} |" for r in positives)
    md += ["", "## Unresolved status bucket", ""]
    md.extend(f"- {r['review_id']} / {r['query_id']}: {r['reason']}; known correct alternatives {r['correct_alternative_pages']}; unavailable alternatives {r['unavailable_alternative_pages']}." for r in unresolved)
    if not unresolved:
        md.append("NONE")
    md += ["", "## Unavailable states", ""]
    md.extend(f"- {r['state_id']}: {r['status']}; {r['errors']}" for r in unavailable)
    if not unavailable:
        md.append("NONE")
    md += ["", "## Token caps / truncation", ""]
    md.extend(f"- {stage}: {len(cases)} length-limited calls; states {[c['state_id'] for c in cases]}" for stage, cases in truncation.items())
    md += ["", "All original questions, chunks/order, PDFs and candidate mapping are frozen in stage2_manifest.json. Candidate and VLM/final request parameters match Stage1; limits were not increased mid-run. Every raw attempt is retained in append-only raw_calls.jsonl and trial_results.checkpoint.jsonl. Judge errors remain UNAVAILABLE, not incorrect.",
           f"Protected original file hashes unchanged: {len(snapshot)}, including all Stage1 files, Phase2A, annotations, design audit and RESEARCH_NOTES. No rank1 repeat controls, text controls, positive repeat confirmation, router or population recovery-rate calculation.", ""]
    (OUT / "stage2_summary.md").write_text("\n".join(md), encoding="utf-8")
    print("STAGE2 SUMMARY", pilot.canonical({k: summary[k] for k in (
        "reconciliation", "call_counts", "judge_logical_evaluations_attempted", "state_status_counts",
        "fresh_rank1_correct", "fresh_rank1_wrong", "fresh_rank1_unavailable", "queries_with_correct_alternative",
        "automatic_positive_count", "multiple_positive_page_cases", "review_queue_unique_queries", "protected_files_unchanged")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run", "summarize"))
    args = parser.parse_args()
    {"prepare": prepare, "run": run, "summarize": summarize}[args.mode]()


if __name__ == "__main__":
    main()
