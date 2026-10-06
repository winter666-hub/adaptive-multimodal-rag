"""Execute only the predeclared page-specific 24-query Phase 2B pilot.

Prepare first (no API calls), then run. Immutable original ACK evidence is used
directly; no retrieval, reranking, router, GT page selection or adjudication.
Transport receipts and completed states are append-only. Resume skips completed
states, including failures; no hidden retries beyond the existing judge parser.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.llm import FuriosaLlm
from furiosa_rag.models import Chunk, RetrievedChunk
from furiosa_rag.pdf_images import PdfPageRenderer
from furiosa_rag.pipeline import TextRagPipeline, clean_internal_citations
from furiosa_rag.vision import FuriosaVision, VISION_SYSTEM_PROMPT, VISION_USER_PROMPT
from furiosa_rag.cli.audit_route_ground_truth import (
    AUDIT_JUDGE_PROMPT, AUDIT_CORRECTNESS_THRESHOLD,
)
from furiosa_rag.cli.evaluate_answer_quality import (
    judge_answer, JUDGE_PARSER_POLICY_VERSION,
)

BASE = ROOT / "results/oracle_headroom/phase2b_visual_oracle"
OUT = BASE / "stage1_pilot"
SCRIPT = Path(__file__).resolve()
KST = timezone(timedelta(hours=9))


def stamp():
    return datetime.now(KST).isoformat(timespec="milliseconds")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def settings_dict(settings):
    endpoints = {e.name: e for e in settings.endpoints}
    if endpoints["vision"].model != "furiosa-ai/Qwen3-VL-32B-Instruct":
        raise ValueError("STOP: configured VLM differs from ACK")
    if endpoints["llm"].model != "furiosa-ai/Qwen3-32B-FP8":
        raise ValueError("STOP: configured final/judge model differs from ACK")
    if not settings.api_key or settings.api_key == "EMPTY":
        raise ValueError("API credential is not configured")
    return {
        "vision_model": endpoints["vision"].model,
        "final_model": endpoints["llm"].model,
        "judge_model": endpoints["llm"].model,
        "vision_endpoint": endpoints["vision"].base_url,
        "llm_endpoint": endpoints["llm"].base_url,
        "vision_max_tokens": settings.vision_max_tokens,
        "final_max_tokens": 1024, "judge_max_tokens": 512,
        "temperature": 0, "enable_thinking": False,
        "top_p_and_seed": "Not sent, matching ACK request format; server defaults unverified",
        "text_timeout_seconds": settings.request_timeout,
        "vision_timeout_seconds": settings.vision_request_timeout,
        "render_dpi": 144.0, "render_alpha": False,
        "renderer_max_pixels": 20_000_000,
        "judge_prompt_sha256": digest(AUDIT_JUDGE_PROMPT.encode()),
        "judge_parser_policy_version": JUDGE_PARSER_POLICY_VERSION,
        "judge_max_attempts": 2, "correctness_threshold": AUDIT_CORRECTNESS_THRESHOLD,
        "vision_system_prompt": VISION_SYSTEM_PROMPT,
        "vision_user_prompt": VISION_USER_PROMPT,
        "runtime": {name: importlib.metadata.version(name) for name in ("PyMuPDF", "numpy", "pypdf")},
    }


def protected_paths():
    paths = []
    for name in ("results", "scripts", "tests", "src", "docs", "benchmarks", "datasets/unidoc"):
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file()
                     and OUT not in p.parents and p != SCRIPT and "__pycache__" not in p.parts)
    for folder in ROOT.glob("review_upload_batch*"):
        paths.extend(p for p in folder.rglob("*") if p.is_file())
    return sorted(set(paths))


def prepare():
    if OUT.exists():
        raise FileExistsError("Pilot directory already exists; preparation refuses overwrite")
    audit_path = BASE / "phase2b_design_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    for relative, expected in audit["input_source_sha256"].items():
        if file_hash(ROOT / relative) != expected:
            raise ValueError(f"Audited input changed: {relative}")
    config = settings_dict(Settings.from_env(ROOT / ".env"))
    rows = read_csv(ROOT / "results/error_analysis/analysis_master.csv")
    pool = [r for r in rows if r["gt_hit_at_3"] == "True" and r["gt_hit_at_1"] == "False"
            and r["forced_text_correct"] == "False" and r["forced_vision_correct"] == "False"]
    declared = audit["execution_plan"]["stage1A_smallest_pilot"]
    expected_pool = audit["cohorts"]["STAGE1_GT_IN_TOP3_NOT_TOP1_AND_TEXT_WRONG_AND_VISION_WRONG"]["queries"]
    if len(pool) != expected_pool:
        raise ValueError("Retrospective pool differs from audited pool")
    groups = defaultdict(list)
    seed = declared["seed"]
    for row in pool:
        groups[row["source_pdf"].split("/")[0]].append(row)
    for group in groups.values():
        group.sort(key=lambda r: (digest((seed + "|" + r["query_id"]).encode()), r["query_id"]))
    selected, used = [], set()
    while len(selected) < declared["queries"]:
        changed = False
        for domain in sorted(groups):
            group = groups[domain]
            while group and group[0]["source_pdf"] in used:
                group.pop(0)
            if group and len(selected) < declared["queries"]:
                row = group.pop(0)
                selected.append(row)
                used.add(row["source_pdf"])
                changed = True
        if not changed:
            raise ValueError("Cannot fill distinct-PDF pilot")
    selected.sort(key=lambda r: r["query_id"])
    if [r["query_id"] for r in selected] != [r["query_id"] for r in declared["candidate_inventory"]]:
        raise ValueError("Recomputed sample differs from predeclared design sample")
    with (ROOT / "results/unidoc_full_forced_text.checkpoint.jsonl").open(encoding="utf-8") as f:
        historical = {r["query_id"]: r for line in f if (r := json.loads(line))}
    frozen = []
    for index, row in enumerate(selected, 1):
        old = historical[row["query_id"]]
        sources = json.loads(old["sources"])
        chunks = []
        for rank, source in enumerate(sources, 1):
            if (source["chunk"], source["page"]) != (row[f"reranked_chunk_id_{rank}"], int(row[f"reranked_page_{rank}"])):
                raise ValueError("Stored original source mapping mismatch")
            chunks.append({"rank": rank, "chunk_id": source["chunk"], "page": source["page"],
                           "text": row[f"reranked_chunk_text_{rank}"],
                           "retrieval_score": source["retrieval_score"], "rerank_score": source["rerank_score"]})
        pages = list(dict.fromkeys(c["page"] for c in chunks))
        expected = declared["candidate_inventory"][index - 1]
        if pages != expected["candidate_pages"] or old["question"] != row["question"]:
            raise ValueError("Frozen state differs from design")
        pdf = ROOT / "datasets/unidoc" / row["source_pdf"]
        if not pdf.is_file():
            raise FileNotFoundError(pdf)
        state = {"review_id": f"P2B_S1_{index:03d}", "query_id": row["query_id"],
                 "document_id": row["document_id"], "source_pdf": row["source_pdf"],
                 "pdf_sha256": file_hash(pdf), "question": row["question"],
                 "reference_answer": row["gold_answer"], "ordered_chunks": chunks,
                 "candidate_pages": pages, "rank1_page": chunks[0]["page"],
                 "historical_FT_answer": row["forced_text_answer"],
                 "historical_FV_answer": row["forced_vision_answer"],
                 "historical_FT_correct": row["forced_text_correct"] == "True",
                 "historical_FV_correct": row["forced_vision_correct"] == "True"}
        state["decision_state_sha256"] = digest(canonical({k: state[k] for k in (
            "question", "pdf_sha256", "ordered_chunks")}).encode())
        frozen.append(state)
    costs = {"queries": len(frozen), "unique_PDFs": len(used),
             "candidate_pages": sum(len(q["candidate_pages"]) for q in frozen),
             "rank1_repeat_controls": len(frozen), "text_controls": len(frozen)}
    costs["VLM_calls"] = costs["candidate_pages"] + costs["rank1_repeat_controls"]
    costs["final_generation_calls"] = costs["VLM_calls"] + costs["text_controls"]
    costs["logical_judge_calls"] = costs["final_generation_calls"]
    costs["max_judge_request_attempts"] = costs["logical_judge_calls"] * 2
    if costs["VLM_calls"] != declared["total_primary_pilot_VLM_calls"] or costs["VLM_calls"] > 96:
        raise ValueError(f"STOP: pilot scope exceeds intended VLM budget: {costs}")
    for q in frozen:
        print(q["review_id"], q["query_id"], "PDF=" + q["document_id"], "pages=" + str(q["candidate_pages"]), flush=True)
    print("PRE-INFERENCE SCOPE:", canonical(costs), flush=True)
    print("Creating protected-input hash snapshot; no API calls yet", flush=True)
    protected = {p.relative_to(ROOT).as_posix(): file_hash(p) for p in protected_paths()}
    manifest = {
        "schema_version": "phase2b-stage1-pilot-v1", "created_at_kst": stamp(),
        "design_audit_sha256": file_hash(audit_path),
        "design_audit_markdown_sha256": file_hash(BASE / "phase2b_design_audit.md"),
        "script_sha256": file_hash(SCRIPT), "retrospective_pool_size": len(pool),
        "sampling": {"seed": seed, "procedure": declared["selection_algorithm"],
                     "domain_distribution": dict(Counter(q["source_pdf"].split("/")[0] for q in frozen)),
                     "sample_fixed_before_inference": True},
        "frozen_queries": frozen, "planned_calls": costs, "actual_config": config,
        "GT_policy": "GT used only to define retrospective pool; no GT fields supplied to visual-page selection or runtime states.",
        "execution_order": "All 120 states sorted by SHA256(seed|state_id), interleaving candidates and controls; worker completion can vary.",
        "workers": 3, "VLM_retry_policy": "No automatic VLM or final-generation retries; judge only uses original two-attempt parser. Each raw attempt retained.",
        "visual_failure_policy": "ACK text fallback retained on visual/render failure, but that state is marked VISUAL_ACQUISITION_UNAVAILABLE and excluded from visual recovery events.",
        "scope": "24-query diagnostic only; no candidate confirmation/Stage1B/Stage2 expansion; source-verification fields remain blank.",
        "protected_file_count": len(protected), "protected_snapshot_sha256": digest(canonical(protected).encode()),
    }
    OUT.mkdir()
    write_json(OUT / "protected_inputs.json", protected, True)
    write_json(OUT / "pilot_manifest.json", manifest, True)
    print("MANIFEST SAVED BEFORE INFERENCE", file_hash(OUT / "pilot_manifest.json"), flush=True)


class Journal:
    def __init__(self, manifest):
        self.lock = threading.Lock()
        self.limits = {"VLM": manifest["planned_calls"]["VLM_calls"],
                       "FINAL": manifest["planned_calls"]["final_generation_calls"],
                       "JUDGE": manifest["planned_calls"]["max_judge_request_attempts"]}
        self.counts = Counter()
        self.events = OUT / "raw_calls.jsonl"
        if self.events.exists():
            for line in self.events.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event["event"] == "request_started":
                    self.counts[event["stage"]] += 1

    def append(self, path, value):
        with self.lock:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(canonical(value) + "\n")
                f.flush()

    def reserve(self, stage):
        with self.lock:
            if self.counts[stage] >= self.limits[stage]:
                raise RuntimeError(f"STOP: {stage} request budget exhausted")
            self.counts[stage] += 1
            return self.counts[stage]

    def save_png(self, path, data):
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("Same page rendered with different bytes")
            else:
                with path.open("xb") as f:
                    f.write(data)


class RecordedClient(FuriosaClient):
    def __init__(self, settings, journal, state_id, stage, timeout, image_path=None):
        super().__init__(settings.api_key, timeout)
        self.journal, self.state_id, self.stage = journal, state_id, stage
        self.image_path = image_path
        self.receipts = []

    def post_json(self, base_url, path, payload):
        sequence = self.journal.reserve(self.stage)
        request = json.loads(json.dumps(payload))
        for message in request.get("messages", []):
            if isinstance(message.get("content"), list):
                for item in message["content"]:
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        item["image_url"]["url"] = {
                            "reconstruct_from_png": str(self.image_path.relative_to(ROOT)),
                            "original_data_url_sha256": digest(url.encode()),
                            "original_data_url_characters": len(url)}
        call_id = f"{self.state_id}:{self.stage}:{sequence}"
        meta = {"call_id": call_id, "state_id": self.state_id, "stage": self.stage,
                "stage_sequence": sequence, "attempt_in_state": len(self.receipts) + 1,
                "model": payload["model"], "request": request,
                "wire_json_sha256": digest(json.dumps(payload).encode()),
                "endpoint": self.api_url(base_url, path), "timeout_seconds": self.timeout}
        self.journal.append(self.journal.events, {**meta, "event": "request_started", "timestamp_kst": stamp()})
        started = time.perf_counter()
        try:
            response = super().post_json(base_url, path, payload)
            receipt = {**meta, "event": "request_completed", "timestamp_kst": stamp(),
                       "latency_ms": (time.perf_counter() - started) * 1000,
                       "http_success": True, "response": response, "error": None}
        except Exception as error:
            safe_error = str(error).replace(self.api_key, "[REDACTED]") if self.api_key else str(error)
            receipt = {**meta, "event": "request_completed", "timestamp_kst": stamp(),
                       "latency_ms": (time.perf_counter() - started) * 1000,
                       "http_success": False, "response": None,
                       "error": type(error).__name__ + ": " + safe_error}
            self.receipts.append(receipt)
            self.journal.append(self.journal.events, receipt)
            raise
        self.receipts.append(receipt)
        self.journal.append(self.journal.events, receipt)
        return response


def sources_for(query):
    return tuple(RetrievedChunk(chunk=Chunk(chunk_id=c["chunk_id"], page_number=c["page"], text=c["text"]),
                                retrieval_score=c["retrieval_score"], rerank_score=c["rerank_score"])
                 for c in query["ordered_chunks"])


def trial_jobs(manifest):
    jobs = []
    for q in manifest["frozen_queries"]:
        for rank, page in enumerate(q["candidate_pages"], 1):
            jobs.append((q, "VISUAL_CANDIDATE", page, rank, f"{q['review_id']}__PAGE_{page}"))
        jobs.append((q, "RANK1_REPEAT", q["rank1_page"], 1, q["review_id"] + "__RANK1_REPEAT"))
        jobs.append((q, "TEXT_ONLY", None, None, q["review_id"] + "__TEXT_ONLY"))
    seed = manifest["sampling"]["seed"]
    return sorted(jobs, key=lambda j: (digest((seed + "|" + j[4]).encode()), j[4]))


def execute_trial(job, settings, manifest, journal):
    q, role, page, rank, state_id = job
    config = manifest["actual_config"]
    result = {"state_id": state_id, "review_id": q["review_id"], "query_id": q["query_id"],
              "document_id": q["document_id"], "source_pdf": q["source_pdf"],
              "trial_role": role, "page_id": page, "candidate_rank": rank,
              "source_chunk_ranks": [c["rank"] for c in q["ordered_chunks"] if c["page"] == page],
              "decision_state_sha256": q["decision_state_sha256"],
              "started_at_kst": stamp(), "errors": [], "VLM_raw_output": "",
              "visual_context": None, "VLM_status": "NOT_APPLICABLE", "final_raw_answer": "",
              "answer": "", "judge_raw_outputs": [], "judge_scores": None,
              "judge_correct": None, "analysis_correct": None, "status": "STARTED",
              "latency_ms": {}, "call_ids": [], "actual_config": config}
    endpoint = {e.name: e for e in settings.endpoints}
    begin = time.perf_counter()
    visual = None
    if page is not None:
        started = time.perf_counter()
        try:
            png = PdfPageRenderer(dpi=config["render_dpi"], max_pixels=config["renderer_max_pixels"]).render_png(
                ROOT / "datasets/unidoc" / q["source_pdf"], page)
            image_path = OUT / "renders" / q["document_id"] / f"page_{page:05d}.png"
            journal.save_png(image_path, png)
            result["rendered_page_path"] = str(image_path.relative_to(ROOT))
            result["rendered_page_sha256"] = digest(png)
            result["latency_ms"]["render"] = (time.perf_counter() - started) * 1000
            import base64
            image_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            client = RecordedClient(settings, journal, state_id, "VLM", settings.vision_request_timeout, image_path)
            started = time.perf_counter()
            try:
                visual = FuriosaVision(endpoint["vision"], client).analyze(q["question"], image_url, max_tokens=config["vision_max_tokens"])
                result["VLM_status"] = "OK"
            finally:
                result["latency_ms"]["VLM"] = (time.perf_counter() - started) * 1000
                result["VLM_receipts"] = client.receipts
                if client.receipts and client.receipts[-1]["response"]:
                    response = client.receipts[-1]["response"]
                    result["VLM_raw_output"] = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as error:
            result["VLM_status"] = "ERROR"
            result["errors"].append({"stage": "VISUAL", "error": str(error).replace(settings.api_key, "[REDACTED]")})
        result["visual_context"] = visual
    text = TextRagPipeline._text_context(sources_for(q))
    prompt = TextRagPipeline._answer_prompt(q["question"], text, visual_context=visual)
    result["text_context_sha256"] = digest(text.encode())
    result["final_prompt_sha256"] = digest(prompt.encode())
    final_client = RecordedClient(settings, journal, state_id, "FINAL", settings.request_timeout)
    started = time.perf_counter()
    try:
        result["final_raw_answer"] = FuriosaLlm(endpoint["llm"], final_client).generate(prompt, max_tokens=config["final_max_tokens"])
        result["answer"] = clean_internal_citations(result["final_raw_answer"])
    except Exception as error:
        result["errors"].append({"stage": "FINAL", "error": str(error).replace(settings.api_key, "[REDACTED]")})
        result["status"] = "FINAL_GENERATION_ERROR"
    finally:
        result["latency_ms"]["FINAL"] = (time.perf_counter() - started) * 1000
        result["FINAL_receipts"] = final_client.receipts
    if result["status"] != "FINAL_GENERATION_ERROR":
        judge_client = RecordedClient(settings, journal, state_id, "JUDGE", settings.request_timeout)
        started = time.perf_counter()
        try:
            score = judge_answer(FuriosaLlm(endpoint["llm"], judge_client), question=q["question"],
                                 reference=q["reference_answer"], candidate=result["answer"],
                                 prompt_template=AUDIT_JUDGE_PROMPT)
            result["judge_scores"] = asdict(score)
            result["judge_correct"] = score.correctness >= AUDIT_CORRECTNESS_THRESHOLD
            result["analysis_correct"] = result["judge_correct"]
            result["status"] = "OK"
        except Exception as error:
            result["errors"].append({"stage": "JUDGE", "error": str(error).replace(settings.api_key, "[REDACTED]")})
            result["status"] = "JUDGE_ERROR_UNAVAILABLE"
        finally:
            result["latency_ms"]["JUDGE"] = (time.perf_counter() - started) * 1000
            result["JUDGE_receipts"] = judge_client.receipts
            result["judge_raw_outputs"] = [r["response"].get("choices", [{}])[0].get("message", {}).get("content", "")
                                            for r in judge_client.receipts if r["response"]]
    if page is not None and result["VLM_status"] != "OK":
        result["analysis_correct"] = None
        result["status"] = "VISUAL_ACQUISITION_UNAVAILABLE"
    for key in ("VLM_receipts", "FINAL_receipts", "JUDGE_receipts"):
        result["call_ids"].extend(r["call_id"] for r in result.get(key, []))
    result["finished_at_kst"] = stamp()
    result["latency_ms"]["total"] = (time.perf_counter() - begin) * 1000
    journal.append(OUT / "trial_results.checkpoint.jsonl", result)
    print("DONE", state_id, result["status"], "correct=" + str(result["analysis_correct"]), flush=True)
    return result


def load_manifest():
    manifest = json.loads((OUT / "pilot_manifest.json").read_text(encoding="utf-8"))
    if file_hash(SCRIPT) != manifest["script_sha256"]:
        raise ValueError("Execution script changed since pre-inference manifest")
    if file_hash(BASE / "phase2b_design_audit.json") != manifest["design_audit_sha256"]:
        raise ValueError("Design audit changed")
    return manifest


def run():
    manifest = load_manifest()
    settings = Settings.from_env(ROOT / ".env")
    if settings_dict(settings) != manifest["actual_config"]:
        raise ValueError("Configured parameters changed since manifest")
    for q in manifest["frozen_queries"]:
        if file_hash(ROOT / "datasets/unidoc" / q["source_pdf"]) != q["pdf_sha256"]:
            raise ValueError("Frozen PDF changed")
    completed = {}
    checkpoint = OUT / "trial_results.checkpoint.jsonl"
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["state_id"] in completed:
                raise ValueError("Duplicate completed state; no silent overwrite")
            completed[row["state_id"]] = row
    journal = Journal(manifest)
    pending = [job for job in trial_jobs(manifest) if job[4] not in completed]
    # In-flight interrupted states are never silently rerun. They require explicit audit.
    if journal.events.exists():
        started = {json.loads(line)["state_id"] for line in journal.events.read_text(encoding="utf-8").splitlines()
                   if json.loads(line)["event"] == "request_started"}
        unfinished = started - set(completed)
        if unfinished:
            raise ValueError(f"Interrupted states have receipts but no checkpoint; refusing hidden retry: {sorted(unfinished)}")
    print("RUN PREFLIGHT", canonical(manifest["planned_calls"]), "pending states=" + str(len(pending)), flush=True)
    with ThreadPoolExecutor(max_workers=manifest["workers"]) as executor:
        futures = [executor.submit(execute_trial, job, settings, manifest, journal) for job in pending]
        for future in as_completed(futures):
            future.result()
    summarize()


def csv_write(path, rows, fields=None):
    fields = fields or list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: canonical(row[k]) if isinstance(row[k], (dict, list)) else
                             "" if row[k] is None else row[k] for k in fields})


def summarize():
    manifest = load_manifest()
    with (OUT / "trial_results.checkpoint.jsonl").open(encoding="utf-8") as f:
        raw = [json.loads(line) for line in f]
    by = {r["state_id"]: r for r in raw}
    jobs = trial_jobs(manifest)
    if len(by) != len(raw) or set(by) != {j[4] for j in jobs}:
        raise ValueError("Pilot state inventory incomplete or duplicated")
    flat = []
    for q, role, page, rank, sid in sorted(jobs, key=lambda j: (j[0]["review_id"], j[1], j[3] or 0)):
        r = by[sid]
        flat.append({k: r.get(k) for k in ["state_id", "review_id", "query_id", "document_id", "source_pdf",
            "trial_role", "page_id", "candidate_rank", "source_chunk_ranks", "decision_state_sha256",
            "rendered_page_path", "rendered_page_sha256", "VLM_status", "VLM_raw_output", "visual_context",
            "final_raw_answer", "answer", "judge_raw_outputs", "judge_scores", "judge_correct", "analysis_correct",
            "status", "errors", "latency_ms", "call_ids", "actual_config", "text_context_sha256", "final_prompt_sha256"]})
    csv_write(OUT / "pilot_results.csv", flat)
    controls, positives = [], []
    for q in manifest["frozen_queries"]:
        primary = by[f"{q['review_id']}__PAGE_{q['rank1_page']}"]
        repeat = by[q["review_id"] + "__RANK1_REPEAT"]
        text = by[q["review_id"] + "__TEXT_ONLY"]
        controls.append({"review_id": q["review_id"], "query_id": q["query_id"], "document_id": q["document_id"],
            "rank1_page": q["rank1_page"], "historical_FV_correct": q["historical_FV_correct"],
            "fresh_rank1_correct": primary["analysis_correct"], "rank1_repeat_correct": repeat["analysis_correct"],
            "historical_FT_correct": q["historical_FT_correct"], "fresh_TEXT_ONLY_correct": text["analysis_correct"],
            "FV_vs_fresh_answer_changed": q["historical_FV_answer"] != primary["answer"],
            "rank1_vs_repeat_answer_changed": primary["answer"] != repeat["answer"],
            "FT_vs_fresh_text_answer_changed": q["historical_FT_answer"] != text["answer"],
            "FV_vs_fresh_label_changed": None if primary["analysis_correct"] is None else q["historical_FV_correct"] != primary["analysis_correct"],
            "rank1_vs_repeat_label_changed": None if None in (primary["analysis_correct"], repeat["analysis_correct"]) else primary["analysis_correct"] != repeat["analysis_correct"],
            "FT_vs_fresh_label_changed": None if text["analysis_correct"] is None else q["historical_FT_correct"] != text["analysis_correct"],
            "fresh_rank1_status": primary["status"], "repeat_status": repeat["status"], "fresh_text_status": text["status"],
            "historical_FV_answer": q["historical_FV_answer"], "fresh_rank1_answer": primary["answer"],
            "rank1_repeat_answer": repeat["answer"], "historical_FT_answer": q["historical_FT_answer"], "fresh_TEXT_ONLY_answer": text["answer"]})
        alternatives = [by[f"{q['review_id']}__PAGE_{page}"] for page in q["candidate_pages"]
                        if page != q["rank1_page"] and by[f"{q['review_id']}__PAGE_{page}"]["analysis_correct"] is True]
        if primary["analysis_correct"] is False and alternatives:
            positives.append({"review_id": q["review_id"], "query_id": q["query_id"], "document_id": q["document_id"],
                "pdf_path": str((ROOT / "datasets/unidoc" / q["source_pdf"]).resolve()), "question": q["question"],
                "reference_answer": q["reference_answer"], "ordered_Top3_chunks": q["ordered_chunks"],
                "candidate_pages": q["candidate_pages"], "fresh_rank1_page": q["rank1_page"],
                "fresh_rank1_answer": primary["answer"], "fresh_rank1_judge": primary["judge_scores"],
                "fresh_rank1_judge_raw_outputs": primary["judge_raw_outputs"],
                "rank1_repeat_answer": repeat["answer"], "rank1_repeat_correct": repeat["analysis_correct"],
                "rank1_repeat_judge": repeat["judge_scores"],
                "alternative_correct_pages": [r["page_id"] for r in alternatives],
                "alternative_page_results": [{k: r.get(k) for k in ("page_id", "answer", "VLM_raw_output", "judge_scores", "judge_raw_outputs", "rendered_page_path")} for r in alternatives],
                "SOURCE_VERIFIED_PAGE_SELECTION_RECOVERY": "", "human_reference_valid": "",
                "human_notes": "", "human_confidence": "", "source_verification_status": "PENDING"})
    csv_write(OUT / "pilot_controls.csv", controls)
    queue_fields = list(positives[0]) if positives else ["review_id", "query_id", "document_id", "pdf_path", "question",
        "reference_answer", "ordered_Top3_chunks", "candidate_pages", "fresh_rank1_page", "fresh_rank1_answer",
        "fresh_rank1_judge", "fresh_rank1_judge_raw_outputs", "rank1_repeat_answer", "rank1_repeat_correct",
        "rank1_repeat_judge", "alternative_correct_pages", "alternative_page_results",
        "SOURCE_VERIFIED_PAGE_SELECTION_RECOVERY", "human_reference_valid", "human_notes", "human_confidence", "source_verification_status"]
    csv_write(OUT / "pilot_positive_review_queue.csv", positives, queue_fields)
    events = [json.loads(line) for line in (OUT / "raw_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = {}
    for stage in ("VLM", "FINAL", "JUDGE"):
        completed = [r for r in events if r["stage"] == stage and r["event"] == "request_completed"]
        calls[stage] = {"attempted": sum(r["stage"] == stage and r["event"] == "request_started" for r in events),
                        "transport_succeeded": sum(r["http_success"] for r in completed),
                        "transport_failed": sum(not r["http_success"] for r in completed)}
    comparisons = {}
    for name, field, answer_field in [("historical_FV_vs_fresh_rank1", "FV_vs_fresh_label_changed", "FV_vs_fresh_answer_changed"),
                                      ("fresh_rank1_vs_repeat", "rank1_vs_repeat_label_changed", "rank1_vs_repeat_answer_changed"),
                                      ("historical_FT_vs_fresh_TEXT_ONLY", "FT_vs_fresh_label_changed", "FT_vs_fresh_text_answer_changed")]:
        valid = [c for c in controls if c[field] is not None]
        comparisons[name] = {"comparable": len(valid), "label_agreements": sum(not c[field] for c in valid),
                             "label_changes": sum(c[field] for c in valid), "unavailable": len(controls) - len(valid),
                             "answer_changes": sum(c[answer_field] for c in controls),
                             "label_changed_ids": [c["review_id"] for c in valid if c[field]]}
    summary = {"scope": "Selected 24-query diagnostic pilot only; no population recovery rate", "planned_calls": manifest["planned_calls"],
        "call_counts": calls, "state_status_counts": dict(Counter(r["status"] for r in raw)),
        "VLM_operation_succeeded": sum(r["VLM_status"] == "OK" for r in raw),
        "VLM_operation_failed": sum(r["page_id"] is not None and r["VLM_status"] != "OK" for r in raw),
        "fresh_rank1_wrong": sum(c["fresh_rank1_correct"] is False for c in controls),
        "fresh_rank1_correct": sum(c["fresh_rank1_correct"] is True for c in controls),
        "automatic_positive_queries": len(positives),
        "automatic_positive_cases": [{k: r[k] for k in ("review_id", "query_id", "document_id", "fresh_rank1_page", "alternative_correct_pages", "rank1_repeat_correct")} for r in positives],
        "source_verification_queue_size": len(positives), "source_verified_recoveries": None,
        "decision": "DEFERRED_PENDING_HUMAN_SOURCE_VERIFICATION", "control_comparisons": comparisons,
        "selected_queries": [{k: q[k] for k in ("review_id", "query_id", "document_id", "candidate_pages")} for q in manifest["frozen_queries"]],
        "decision_rules": {"GO_signal": "At least 2 source-verified recoveries from different PDFs",
                           "BORDERLINE": "Exactly one verified recovery or unstable repeat/control", "STOP_signal": "Zero verified recoveries after source verification"},
        "stage2_started": False, "router_built": False,
        "protected_files_integrity": "PENDING_FINAL_HASH_CHECK"}
    snapshot = json.loads((OUT / "protected_inputs.json").read_text(encoding="utf-8"))
    print("Verifying protected files after execution", flush=True)
    current = {p.relative_to(ROOT).as_posix(): file_hash(p) for p in protected_paths()}
    if current != snapshot:
        changes = [p for p in set(current) | set(snapshot) if current.get(p) != snapshot.get(p)]
        raise ValueError(f"Protected files changed: {changes}")
    summary["protected_files_integrity"] = "PASS"
    summary["protected_file_count"] = len(snapshot)
    write_json(OUT / "pilot_summary.json", summary)
    lines = ["# Phase 2B Stage 1 pilot automatic diagnostic summary", "", summary["scope"], "",
             "Source verification is PENDING. Automatic-positive cases are not confirmed recoveries. No GO/STOP decision or expansion is made.", "",
             "| API stage | Attempted | Transport succeeded | Transport failed |", "|---|---:|---:|---:|"]
    for stage, counts in calls.items():
        lines.append(f"| {stage} | {counts['attempted']} | {counts['transport_succeeded']} | {counts['transport_failed']} |")
    lines += ["", f"Candidate pages: {manifest['planned_calls']['candidate_pages']}; PDFs: {manifest['planned_calls']['unique_PDFs']}.",
              f"Fresh rank1 wrong: {summary['fresh_rank1_wrong']}; automatic-positive queries / review queue: {len(positives)}.", "",
              "## Automatic-positive cases", ""]
    for row in positives:
        lines.append(f"- {row['review_id']} / {row['query_id']}: rank1 page {row['fresh_rank1_page']} wrong; automatic-correct alternatives {row['alternative_correct_pages']}; rank1 repeat correct={row['rank1_repeat_correct']}.")
    if not positives:
        lines.append("NONE")
    lines += ["", "## Controls", "", "| Comparison | Comparable | Label agreements | Label changes | Unavailable | Answer changes |", "|---|---:|---:|---:|---:|---:|"]
    for name, counts in comparisons.items():
        lines.append(f"| {name} | {counts['comparable']} | {counts['label_agreements']} | {counts['label_changes']} | {counts['unavailable']} | {counts['answer_changes']} |")
    lines += ["", "## Selected fixed sample", "", "| Review ID | Query ID | PDF ID | Candidate pages |", "|---|---|---|---|"]
    lines.extend(f"| {q['review_id']} | {q['query_id']} | {q['document_id']} | {q['candidate_pages']} |" for q in manifest["frozen_queries"])
    lines += ["", "Manifest and original question/chunks/order/PDF were frozen before inference. No retrieval or reranking occurred. Every raw request/response attempt is retained in raw_calls.jsonl, with full response, reconstructed image reference/hash and parameters; per-state checkpoints are append-only.",
              "Visual failures retain the explicit ACK text fallback while remaining unavailable for visual-event analysis. Judge failures remain unavailable, not incorrect.",
              "Source-verification fields are blank. Relevant source PDFs and rendered candidate pages accompany the queue; no speculative adjudication was performed.",
              f"Protected file hashes unchanged: {len(snapshot)}. Phase 2A, human annotations, design audit and RESEARCH_NOTES untouched. No Stage 1B/Stage 2/router/population recovery rate.", ""]
    (OUT / "pilot_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("SUMMARY", canonical({k: summary[k] for k in ("call_counts", "fresh_rank1_wrong", "automatic_positive_cases", "control_comparisons", "protected_files_integrity")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run", "summarize"))
    args = parser.parse_args()
    {"prepare": prepare, "run": run, "summarize": summarize}[args.mode]()


if __name__ == "__main__":
    main()
