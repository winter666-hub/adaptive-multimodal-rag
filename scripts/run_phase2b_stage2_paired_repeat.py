"""Stage 2 strong-23 paired repeats. preflight/prepare NEVER call an API.

Only ``run --allow-api`` can call transport. Original annotations are immutable;
new PDF-based source checks are supplied separately to finalize. No population rate.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
BASE = ROOT / "results/oracle_headroom/phase2b_visual_oracle"
STAGE2 = BASE / "stage2_remaining"
REVIEW = BASE / "stage2_source_review"
OUTPUT_BASE = BASE / "stage2_paired_repeat"
PAGE_NUMBERS = {
    1: 4,
    3: 20,
    6: 16,
    8: 11,
    11: 21,
    12: 7,
    14: 18,
    23: 7,
    29: 5,
    30: 2,
    31: 28,
    37: 3,
    39: 7,
    42: 13,
    44: 5,
    57: 5,
    58: 4,
    70: 6,
    73: 18,
    89: 4,
    90: 8,
    101: 2,
    109: 4,
}
VERIFIED_PAGES = {f"P2B_S2_{number:03d}": page for number, page in PAGE_NUMBERS.items()}
HUMAN_FIELDS = (
    "human_reference_valid",
    "human_rank1_answer_correct",
    "human_alternative_answer_correct",
    "human_top3_text_evidence_sufficient",
    "human_rank1_visual_evidence_sufficient",
    "human_alternative_visual_evidence_useful",
    "source_verified_outcome_recovery",
    "strong_page_selection_attributable",
    "attribution_category",
    "human_confidence",
    "pdf_pages_checked",
    "notes",
)
SELECTION_RULE = (
    "Use established same-page human/source verification, never a new judge result. "
    "P2B_S2_006: p16 and p1 are verified; choose the highest original reranking rank "
    "among verified alternatives. Frozen candidate order p2 -> p16 -> p1; select p16. "
    "Retain original p1 answer and annotation."
)
SCOPE = "Selected Stage 2 diagnostic strong-23 subset; no population recovery rate."


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def load_executor():
    # Import execution utilities only. Never invoke Stage 1 prepare/run/summarize.
    spec = importlib.util.spec_from_file_location(
        "stage2_paired_original_executor", ROOT / "scripts/run_phase2b_stage1_pilot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_directory(run_id):
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id)), "Invalid run_id")
    directory = OUTPUT_BASE / run_id
    require(directory.resolve().is_relative_to(ROOT.resolve()), "Run directory escapes repository")
    require(
        directory.resolve().is_relative_to(OUTPUT_BASE.resolve()), "Run directory escapes output"
    )
    return directory


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


def response_flags(receipts):
    flags = []
    for receipt in receipts:
        choices = (receipt.get("response") or {}).get("choices") or [{}]
        reason = choices[0].get("finish_reason")
        flags.append(
            {
                "call_id": receipt["call_id"],
                "finish_reason": reason,
                "token_cap": reason == "length",
                "http_success": receipt["http_success"],
                "request_sha256": receipt["wire_json_sha256"],
            }
        )
    return flags


def decorate_result(result):
    """Separate generation availability from the diagnostic judge and source truth."""
    result = copy.deepcopy(result)
    result["condition"] = result["trial_role"].removeprefix("PAIRED_")
    result["original_executor_status"] = result["status"]
    result["stage_provenance"] = {
        stage: response_flags(result.get(stage + "_receipts", []))
        for stage in ("VLM", "FINAL", "JUDGE")
    }
    final_receipts = result.get("FINAL_receipts", [])
    generated = bool(
        result["VLM_status"] == "OK"
        and final_receipts
        and final_receipts[-1]["http_success"]
        and result["answer"].strip()
        and result["original_executor_status"] != "FINAL_GENERATION_ERROR"
    )
    result["visual_generation_available"] = generated
    result["generation_availability"] = "AVAILABLE" if generated else "UNAVAILABLE"
    result["diagnostic_judge_status"] = (
        "AVAILABLE" if result.get("judge_scores") is not None else "UNAVAILABLE"
    )
    result["diagnostic_automatic_correctness"] = result.get("analysis_correct")
    result["repeat_source_verification"] = {
        "source_correctness": None,
        "source_check_notes": None,
        "pdf_pages_checked": None,
        "human_confidence": None,
        "page_selection_attribution": None,
    }
    result["paired_classification"] = "PENDING_SOURCE_VERIFICATION"
    return result


def install_execution_guards(pilot):
    original_client = pilot.RecordedClient

    class PairedClient(original_client):
        def post_json(self, base_url, path, payload):
            if self.stage == "FINAL" and not self.journal.visual_ready.get(self.state_id, False):
                # The legacy executor attempts text fallback after visual failure.
                # Reject BEFORE reserve()/transport; legacy result remains UNAVAILABLE.
                raise RuntimeError("Visual acquisition unavailable; text fallback is prohibited")
            if self.stage == "VLM":
                expected = self.journal.expected_requests[self.state_id]
                require(
                    pilot.digest(json.dumps(payload).encode()) == expected,
                    "VLM request differs from the frozen original page treatment",
                )
            return super().post_json(base_url, path, payload)

    class PairedJournal(pilot.Journal):
        def __init__(self, manifest, api_key):
            super().__init__(manifest)
            self.secret = api_key
            self.visual_ready = {}
            self.expected_requests = {
                state["state_id"]: state["original_VLM_wire_sha256"]
                for pair in manifest["pairs"]
                for state in pair["conditions"].values()
            }
            self.expected_images = {
                (pair["query"]["document_id"], state["page"]): state["original_image_sha256"]
                for pair in manifest["pairs"]
                for state in pair["conditions"].values()
            }

        def save_png(self, path, data):
            identity = (path.parent.name, int(path.stem.removeprefix("page_")))
            require(
                pilot.digest(data) == self.expected_images[identity],
                "Fresh rendered image differs from the original treatment",
            )
            return super().save_png(path, data)

        def append(self, path, value):
            value = copy.deepcopy(value)
            if path.name == "raw_calls.jsonl" and value.get("event") == "request_completed":
                flags = response_flags([value])[0]
                value.update({k: flags[k] for k in ("finish_reason", "token_cap")})
                if value["stage"] == "VLM":
                    choices = (value.get("response") or {}).get("choices") or [{}]
                    content = choices[0].get("message", {}).get("content")
                    self.visual_ready[value["state_id"]] = bool(
                        value["http_success"] and isinstance(content, str) and content.strip()
                    )
            if path.name == "trial_results.checkpoint.jsonl":
                value = decorate_result(value)
            return super().append(path, redact(value, self.secret))

    pilot.RecordedClient = PairedClient
    return PairedJournal


def snapshot(pilot, excluded_directory):
    """Current baseline, including other runs and all existing original artifacts."""
    roots = [
        ROOT / name
        for name in (
            "results",
            "scripts",
            "tests",
            "src",
            "docs",
            "benchmarks",
            "datasets/unidoc",
        )
    ]
    roots += [p for p in ROOT.iterdir() if p.is_dir() and "upload" in p.name.lower()]
    paths = {p for p in ROOT.iterdir() if p.is_file()}
    for base in roots:
        if not base.exists():
            continue
        for directory, dirs, files in os.walk(
            base, onerror=lambda error: (_ for _ in ()).throw(error)
        ):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = Path(directory) / name
                if not path.is_relative_to(excluded_directory):
                    paths.add(path)
    return {p.relative_to(ROOT).as_posix(): pilot.file_hash(p) for p in sorted(paths)}


def preflight(pilot):
    """Read-only: frozen inputs, source mapping, environment, prompts and images."""
    original = read_json(STAGE2 / "stage2_manifest.json")
    integrity = read_json(STAGE2 / "stage2_execution_integrity_report.json")
    require(
        pilot.file_hash(STAGE2 / "stage2_manifest.json") == integrity["stage2_manifest_sha256"],
        "Original Stage 2 manifest changed",
    )
    require(
        pilot.file_hash(ROOT / "scripts/run_phase2b_stage1_pilot.py")
        == original["original_executor_sha256"],
        "Common executor changed",
    )
    require(
        pilot.file_hash(ROOT / "scripts/run_phase2b_stage2_remaining.py")
        == original["stage2_executor_sha256"],
        "Original Stage 2 runner changed",
    )
    old_snapshot = read_json(STAGE2 / "protected_inputs.json")
    require(
        pilot.digest(pilot.canonical(old_snapshot).encode())
        == original["protected_snapshot_sha256"],
        "Original protected snapshot changed",
    )
    for name, expected in old_snapshot.items():
        if name.startswith("src/") and name.endswith(".py"):
            require(
                pilot.file_hash(ROOT / name) == expected, f"Original dependency changed: {name}"
            )
    settings = pilot.Settings.from_env(ROOT / ".env")
    require(
        pilot.settings_dict(settings) == original["actual_config"],
        "Current models/parameters/prompts/runtime differ from original Stage 2",
    )
    rows, inputs = (
        [],
        [
            STAGE2 / "stage2_manifest.json",
            STAGE2 / "protected_inputs.json",
            STAGE2 / "trial_results.checkpoint.jsonl",
            STAGE2 / "stage2_execution_integrity_report.json",
        ],
    )
    schema = None
    import csv

    for batch in range(1, 7):
        path = REVIEW / f"review_batch_{batch:02d}.csv"
        inputs.append(path)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            batch_rows = list(reader)
            schema = schema or reader.fieldnames
            require(
                reader.fieldnames == schema and schema[-12:] == list(HUMAN_FIELDS),
                "Annotation schema differs",
            )
        require(len(batch_rows) == (9 if batch == 6 else 10), "Annotation batch row count differs")
        rows.extend(batch_rows)
    require(len(rows) == len({r["review_id"] for r in rows}) == 59, "Duplicate/missing review IDs")
    require(len({r["query_id"] for r in rows}) == 59, "Duplicate query IDs")
    require(all(r[k].strip() for r in rows for k in HUMAN_FIELDS), "Incomplete human annotation")
    for row in rows:
        require(
            all(row[k] in ("YES", "NO", "UNCLEAR") for k in HUMAN_FIELDS[:8]),
            "Invalid human/source enum",
        )
        require(row["human_confidence"] in ("HIGH", "MEDIUM", "LOW"), "Invalid confidence enum")
        require(
            row["attribution_category"]
            in (
                "STRONG_PAGE_SELECTION_ATTRIBUTABLE",
                "MIXED_OR_GENERATION_ASSISTED",
                "REFERENCE_INVALID_OR_UNCLEAR",
                "NOT_A_RECOVERY",
                "UNCLEAR",
            ),
            "Invalid attribution enum",
        )
    require(
        {r["review_id"] for r in rows if r["strong_page_selection_attributable"] == "YES"}
        == set(VERIFIED_PAGES),
        "Strong cases differ from the authorized 23 IDs",
    )
    queries = {q["review_id"]: q for q in original["frozen_queries"]}
    trials = read_jsonl(STAGE2 / "trial_results.checkpoint.jsonl")
    by_state = {r["state_id"]: r for r in trials}
    require(len(by_state) == len(trials) == 317, "Original checkpoint states missing/duplicated")
    renderer = pilot.PdfPageRenderer(
        dpi=original["actual_config"]["render_dpi"],
        max_pixels=original["actual_config"]["renderer_max_pixels"],
    )
    endpoints = {e.name: e for e in settings.endpoints}
    pairs = []
    for row in rows:
        rid = row["review_id"]
        if rid not in VERIFIED_PAGES:
            continue
        q, page = queries[rid], VERIFIED_PAGES[rid]
        expected_labels = {
            "human_reference_valid": "YES",
            "human_rank1_answer_correct": "NO",
            "human_alternative_answer_correct": "YES",
            "human_top3_text_evidence_sufficient": "NO",
            "human_rank1_visual_evidence_sufficient": "NO",
            "human_alternative_visual_evidence_useful": "YES",
            "source_verified_outcome_recovery": "YES",
            "attribution_category": "STRONG_PAGE_SELECTION_ATTRIBUTABLE",
        }
        require(
            all(row[k] == value for k, value in expected_labels.items()),
            f"Inconsistent strong source verification: {rid}",
        )
        require(row["human_confidence"] in ("HIGH", "MEDIUM"), f"Invalid confidence: {rid}")
        require(
            row["query_id"] == q["query_id"]
            and row["pdf_id"] == q["document_id"]
            and int(row["rank1_page"]) == q["rank1_page"],
            f"Frozen identity changed: {rid}",
        )
        require(
            json.loads(row["ordered_Top3_chunks"]) == q["ordered_chunks"]
            and json.loads(row["candidate_pages"]) == q["candidate_pages"]
            and row["question"] == q["question"]
            and row["reference_answer"] == q["reference_answer"],
            f"Frozen state differs: {rid}",
        )
        require(
            page != q["rank1_page"] and page in q["candidate_pages"], f"Invalid alternative: {rid}"
        )
        require(
            re.search(rf"\bp{page}\b", row["notes"], re.IGNORECASE),
            f"Selected verified page absent from source notes: {rid}",
        )
        checked = (
            json.loads(row["pdf_pages_checked"])
            if row["pdf_pages_checked"].startswith("[")
            else [int(p) for p in row["pdf_pages_checked"].split(",")]
        )
        require(
            q["rank1_page"] in checked and page in checked,
            f"Selected pages absent from human source-check provenance: {rid}",
        )
        if rid == "P2B_S2_006":
            require(
                q["candidate_pages"] == [2, 16, 1] and page == 16,
                "P2B_S2_006 highest-ranked verified alternative rule differs",
            )
            require(re.search(r"\bp1\b", row["notes"]), "P2B_S2_006 p1 verification missing")
        pdf = ROOT / "datasets/unidoc" / q["source_pdf"]
        require(pilot.file_hash(pdf) == q["pdf_sha256"], f"Frozen PDF changed: {rid}")
        require(
            q["decision_state_sha256"]
            == pilot.digest(
                pilot.canonical(
                    {k: q[k] for k in ("question", "pdf_sha256", "ordered_chunks")}
                ).encode()
            ),
            f"Decision-state hash differs: {rid}",
        )
        text_hash = pilot.digest(pilot.TextRagPipeline._text_context(pilot.sources_for(q)).encode())
        conditions = {}
        candidate_records = {c["page_id"]: c for c in json.loads(row["candidate_results"])}
        for condition, physical_page in (("A", q["rank1_page"]), ("B", page)):
            first = by_state[f"{rid}__PAGE_{physical_page}"]
            candidate = candidate_records[physical_page]
            require(candidate["answer"] == first["answer"], f"Candidate answer differs: {rid}")
            require(
                first["actual_config"] == original["actual_config"]
                and first["text_context_sha256"] == text_hash
                and first["decision_state_sha256"] == q["decision_state_sha256"],
                f"Original A/B configuration differs: {rid}",
            )
            png = renderer.render_png(pdf, physical_page)
            require(pilot.digest(png) == first["rendered_page_sha256"], f"Image differs: {rid}")
            conditions[condition] = {
                "state_id": f"{rid}__PAIRED_{condition}_PAGE_{physical_page}",
                "page": physical_page,
                "original_state_id": first["state_id"],
                "original_image_sha256": first["rendered_page_sha256"],
                "original_VLM_wire_sha256": first["VLM_receipts"][0]["wire_json_sha256"],
                "original_answer": first["answer"],
                "original_stage_flags": {
                    s: response_flags(first.get(s + "_receipts", []))
                    for s in ("VLM", "FINAL", "JUDGE")
                },
                "source_correctness": "NO" if condition == "A" else "YES",
                "visual_usefulness": "NO" if condition == "A" else "YES",
            }

            # Mock transport captures the CURRENT payload; this never touches an endpoint.
            class Capture:
                def post_json(self, base_url, route, payload):
                    self.payload = payload
                    return {"choices": [{"message": {"content": "capture-only"}}]}

            import base64

            capture = Capture()
            pilot.FuriosaVision(endpoints["vision"], capture).analyze(
                q["question"],
                "data:image/png;base64," + base64.b64encode(png).decode(),
                max_tokens=original["actual_config"]["vision_max_tokens"],
            )
            require(
                pilot.digest(json.dumps(capture.payload).encode())
                == conditions[condition]["original_VLM_wire_sha256"],
                f"Current VLM request differs from original: {rid}",
            )
            prompt = pilot.TextRagPipeline._answer_prompt(
                q["question"],
                pilot.TextRagPipeline._text_context(pilot.sources_for(q)),
                visual_context=first["visual_context"],
            )
            pilot.FuriosaLlm(endpoints["llm"], capture).generate(
                prompt, max_tokens=original["actual_config"]["final_max_tokens"]
            )
            require(
                pilot.digest(json.dumps(capture.payload).encode())
                == first["FINAL_receipts"][0]["wire_json_sha256"],
                f"Current answer-generation request differs from original: {rid}",
            )
        pairs.append(
            {
                "query": q,
                "conditions": conditions,
                "selected_alternative_page": page,
                "verified_alternative_pages": [16, 1] if rid == "P2B_S2_006" else [page],
                "original_human_annotation": {k: row[k] for k in HUMAN_FIELDS},
                "same_page_verification_basis": "Established per-page source notes + original candidate provenance",
                "new_source_checks": "PENDING; independent PDF verification of fresh answers",
            }
        )
    return {
        "schema_version": "phase2b-stage2-paired-repeat-v1",
        "scope": SCOPE,
        "selection_rule": SELECTION_RULE,
        "pairs": pairs,
        "actual_config": original["actual_config"],
        "workers": original["workers"],
        "ordering_seed": "phase2b-stage2-strong23-paired-v1",
        "planned_calls": {
            "VLM_calls": 46,
            "final_generation_calls": 46,
            "logical_judge_calls": 46,
            "max_judge_request_attempts": 92,
        },
        "input_hashes": {p.relative_to(ROOT).as_posix(): pilot.file_hash(p) for p in inputs},
        "original_executor_sha256": original["original_executor_sha256"],
        "repeat_executor_sha256": pilot.file_hash(SCRIPT),
        "source_policy": "Judge diagnostic only; classify only after both fresh PDF source checks.",
        "failure_policy": "No VLM/final retries or text fallback; judge parser at most two attempts.",
        "interpretation_limits": {
            "P2B_S2_008": "MEDIUM: bidirectional-influence wording exceeds the figure",
            "P2B_S2_014": "MEDIUM: example-style completeness; New Carrollton Platform omitted",
            "P2B_S2_039": "Descriptive comparison only; no statistically significant superiority claim",
            "P2B_S2_101": "Advertised accommodation terms, not realized financial benefits",
        },
    }


def jobs_for(manifest):
    jobs = []
    for pair in manifest["pairs"]:
        q = pair["query"]
        for condition, state in pair["conditions"].items():
            jobs.append(
                (
                    q,
                    "PAIRED_" + condition,
                    state["page"],
                    q["candidate_pages"].index(state["page"]) + 1,
                    state["state_id"],
                )
            )
    import hashlib

    return sorted(
        jobs,
        key=lambda j: hashlib.sha256((manifest["ordering_seed"] + "|" + j[4]).encode()).hexdigest(),
    )


def prepare(pilot, run_id):
    directory = run_directory(run_id)
    require(not directory.exists(), "Run directory exists; refusing overwrite")
    value = preflight(pilot)
    print("PREFLIGHT PASS; snapshotting CURRENT protected inputs", flush=True)
    baseline = snapshot(pilot, directory)
    value.update(
        {
            "run_id": run_id,
            "created_at_kst": pilot.stamp(),
            "protected_snapshot_sha256": pilot.digest(pilot.canonical(baseline).encode()),
            "protected_file_count": len(baseline),
        }
    )
    directory.mkdir(parents=True, exist_ok=False)
    pilot.write_json(directory / "protected_inputs.json", baseline, True)
    pilot.write_json(directory / "repeat_manifest.json", value, True)
    with (directory / "repeat_manifest.sha256").open("x", encoding="ascii") as stream:
        stream.write(pilot.file_hash(directory / "repeat_manifest.json") + "\n")
    template = {
        state["state_id"]: {
            "review_id": pair["query"]["review_id"],
            "condition": condition,
            "page": state["page"],
            "answer_sha256": None,
            "source_correctness": None,
            "source_check_notes": None,
            "pdf_pages_checked": None,
            "human_confidence": None,
            "page_selection_attribution": None,
        }
        for pair in value["pairs"]
        for condition, state in pair["conditions"].items()
    }
    pilot.write_json(directory / "repeat_source_checks.template.json", template, True)
    require(snapshot(pilot, directory) == baseline, "Protected inputs changed during prepare")
    print(f"PREPARED {run_id}: 23 pairs / 46 fresh states / zero API calls", flush=True)
    return value


def load_manifest(pilot, run_id):
    directory = run_directory(run_id)
    manifest_path = directory / "repeat_manifest.json"
    require(
        pilot.file_hash(manifest_path)
        == (directory / "repeat_manifest.sha256").read_text(encoding="ascii").strip(),
        "Prepared manifest changed",
    )
    value = read_json(manifest_path)
    require(value["run_id"] == run_id, "Run identity differs")
    require(value["repeat_executor_sha256"] == pilot.file_hash(SCRIPT), "Paired runner changed")
    require(
        value["original_executor_sha256"]
        == pilot.file_hash(ROOT / "scripts/run_phase2b_stage1_pilot.py"),
        "Common executor changed",
    )
    require(
        {p["query"]["review_id"] for p in value["pairs"]} == set(VERIFIED_PAGES)
        and len(value["pairs"]) == 23,
        "Prepared selected set changed",
    )
    for name, expected in value["input_hashes"].items():
        require(pilot.file_hash(ROOT / name) == expected, f"Prepared input changed: {name}")
    baseline = read_json(directory / "protected_inputs.json")
    require(
        pilot.digest(pilot.canonical(baseline).encode()) == value["protected_snapshot_sha256"],
        "Current-baseline snapshot changed",
    )
    require(snapshot(pilot, directory) == baseline, "Protected repository inputs changed")
    return directory, value, baseline


def validate_trials(manifest, rows):
    jobs = {j[4]: j for j in jobs_for(manifest)}
    require(len(rows) == len({r["state_id"] for r in rows}), "Duplicate fresh repeat state")
    for row in rows:
        require(row["state_id"] in jobs, "Unexpected cached/original/foreign state")
        q, role, page, rank, _ = jobs[row["state_id"]]
        require(
            row["review_id"] == q["review_id"]
            and row["query_id"] == q["query_id"]
            and row["document_id"] == q["document_id"]
            and row["page_id"] == page
            and row["trial_role"] == role
            and row["candidate_rank"] == rank
            and row["decision_state_sha256"] == q["decision_state_sha256"]
            and row["actual_config"] == manifest["actual_config"],
            "Fresh state identity differs",
        )
        require("visual_generation_available" in row, "Missing paired availability schema")
        if row["visual_generation_available"]:
            require(
                row["VLM_status"] == "OK"
                and row["FINAL_receipts"]
                and row["FINAL_receipts"][-1]["http_success"]
                and row["answer"].strip(),
                "Invalid visual state counted as available",
            )
    return {r["state_id"]: r for r in rows}


def validate_resume(manifest, rows, events):
    completed = validate_trials(manifest, rows)
    expected = {j[4] for j in jobs_for(manifest)}
    starts, ends = {}, {}
    for event in events:
        require(event["state_id"] in expected, "Foreign raw call state")
        require(event["event"] in ("request_started", "request_completed"), "Unknown raw event")
        require(event["stage"] in ("VLM", "FINAL", "JUDGE"), "Unknown API stage")
        bucket = starts if event["event"] == "request_started" else ends
        require(event["call_id"] not in bucket, "Duplicate raw call receipt")
        bucket[event["call_id"]] = event
    require(set(starts) == set(ends), "Interrupted raw call; no hidden retry allowed")
    require(
        {e["state_id"] for e in starts.values()} <= set(completed),
        "Interrupted state has raw calls but no checkpoint; no hidden retry",
    )
    for row in rows:
        receipts = [r for s in ("VLM", "FINAL", "JUDGE") for r in row.get(s + "_receipts", [])]
        require(
            {r["call_id"] for r in receipts} == set(row["call_ids"]), "Checkpoint receipt mismatch"
        )
        require(all(r["call_id"] in ends for r in receipts), "Checkpoint missing raw receipts")
        require(
            all(r["state_id"] == row["state_id"] for r in receipts), "Foreign checkpoint receipt"
        )
        require(
            {e["call_id"] for e in starts.values() if e["state_id"] == row["state_id"]}
            == set(row["call_ids"]),
            "Raw calls omitted from checkpoint",
        )
        for stage, limit in (("VLM", 1), ("FINAL", 1), ("JUDGE", 2)):
            require(len(row.get(stage + "_receipts", [])) <= limit, "Forbidden per-state retry")
    return completed


def run(pilot, run_id, allow_api=False):
    require(allow_api, "API execution requires separately authorized run --allow-api")
    directory, value, baseline = load_manifest(pilot, run_id)
    settings = pilot.Settings.from_env(ROOT / ".env")
    require(
        pilot.settings_dict(settings) == value["actual_config"], "Runtime configuration changed"
    )
    for pair in value["pairs"]:
        q = pair["query"]
        require(
            pair["selected_alternative_page"] == VERIFIED_PAGES[q["review_id"]]
            and pair["conditions"]["A"]["page"] == q["rank1_page"]
            and pair["conditions"]["B"]["page"] == VERIFIED_PAGES[q["review_id"]],
            "Prepared paired page mapping differs",
        )
        require(
            pilot.file_hash(ROOT / "datasets/unidoc" / q["source_pdf"]) == q["pdf_sha256"],
            "Frozen source PDF changed",
        )
    checkpoint = directory / "trial_results.checkpoint.jsonl"
    done = validate_resume(value, read_jsonl(checkpoint), read_jsonl(directory / "raw_calls.jsonl"))
    pilot.OUT = directory
    journal_type = install_execution_guards(pilot)
    journal = journal_type(value, settings.api_key)
    try:
        jobs = [job for job in jobs_for(value) if job[4] not in done]
        with ThreadPoolExecutor(max_workers=value["workers"]) as executor:
            for future in as_completed(
                [
                    executor.submit(pilot.execute_trial, job, settings, value, journal)
                    for job in jobs
                ]
            ):
                future.result()
        rows = read_jsonl(checkpoint)
        validate_resume(value, rows, read_jsonl(journal.events))
        require(len(rows) == 46, "Incomplete paired run")
        paired = paired_results(value, rows)
        # Resume never regenerates completed failures; derived pending output is create-only.
        destination = directory / "paired_results.pending.json"
        if not destination.exists():
            pilot.write_json(destination, paired, True)
        print(
            "46 fresh states captured; independent PDF source verification remains pending",
            flush=True,
        )
    finally:
        require(
            snapshot(pilot, directory) == baseline, "Protected inputs changed during API execution"
        )


def classify_pair(a, b, a_available=True, b_available=True):
    require(
        a in (None, "YES", "NO", "UNCLEAR") and b in (None, "YES", "NO", "UNCLEAR"),
        "Invalid independent source correctness",
    )
    if not a_available or not b_available:
        return "EXPERIMENT_UNAVAILABLE"
    if a is None or b is None:
        return "PENDING_SOURCE_VERIFICATION"
    if "UNCLEAR" in (a, b):
        return "SOURCE_UNCLEAR"
    return {
        ("NO", "YES"): "PAIRED_RECOVERY_REPRODUCED",
        ("YES", "YES"): "BOTH_CORRECT_NO_PAIRED_RECOVERY",
        ("NO", "NO"): "RECOVERY_REPRODUCTION_FAILED",
        ("YES", "NO"): "ALTERNATIVE_SELECTION_ADVERSE",
    }[(a, b)]


def paired_results(manifest, rows, source_checks=None):
    by = validate_trials(manifest, rows)
    require(len(by) == 46, "Need all 46 fresh states before paired classification")
    pairs = []
    if source_checks is not None:
        require(set(source_checks) == set(by), "Source checks must cover exactly 46 fresh states")
    for pair in manifest["pairs"]:
        states = {}
        for condition, identity in pair["conditions"].items():
            row = by[identity["state_id"]]
            check = source_checks[identity["state_id"]] if source_checks is not None else None
            if check is not None:
                require(
                    check["review_id"] == row["review_id"]
                    and check["condition"] == condition
                    and check["page"] == row["page_id"],
                    "Source check state/page mismatch",
                )
                import hashlib

                require(
                    check["answer_sha256"] == hashlib.sha256(row["answer"].encode()).hexdigest(),
                    "Source check does not bind the fresh answer",
                )
                require(
                    check["source_correctness"] in ("YES", "NO", "UNCLEAR")
                    and isinstance(check["source_check_notes"], str)
                    and check["source_check_notes"].strip()
                    and check["human_confidence"] in ("HIGH", "MEDIUM", "LOW")
                    and isinstance(check["pdf_pages_checked"], list)
                    and row["page_id"] in check["pdf_pages_checked"]
                    and all(type(p) is int and p > 0 for p in check["pdf_pages_checked"]),
                    "Missing/invalid independent source check",
                )
                require(
                    check.get("page_selection_attribution") in (None, "YES", "NO", "UNCLEAR"),
                    "Invalid new source attribution",
                )
                if not row["visual_generation_available"]:
                    require(
                        check["source_correctness"] == "UNCLEAR",
                        "Unavailable generation is not wrong",
                    )
            states[condition] = {
                "state_id": row["state_id"],
                "page": row["page_id"],
                "fresh_answer": row["answer"],
                "visual_generation_available": row["visual_generation_available"],
                "diagnostic_judge_status": row["diagnostic_judge_status"],
                "diagnostic_automatic_correctness": row["diagnostic_automatic_correctness"],
                "new_source_verification": check or row["repeat_source_verification"],
            }
        a, b = states["A"], states["B"]
        classification = classify_pair(
            a["new_source_verification"]["source_correctness"],
            b["new_source_verification"]["source_correctness"],
            a["visual_generation_available"],
            b["visual_generation_available"],
        )
        for state in states.values():
            if state["new_source_verification"].get("page_selection_attribution") == "YES":
                require(
                    classification == "PAIRED_RECOVERY_REPRODUCED",
                    "New strong attribution cannot bypass paired source correctness",
                )
        pairs.append(
            {
                "review_id": pair["query"]["review_id"],
                "query_id": pair["query"]["query_id"],
                "document_id": pair["query"]["document_id"],
                "original_human_annotation": pair["original_human_annotation"],
                "conditions": states,
                "paired_classification": classification,
            }
        )
    return {
        "scope": SCOPE,
        "source_verification_supplied": source_checks is not None,
        "pairs": pairs,
    }


def finalize(pilot, run_id, source_checks_path):
    directory, value, baseline = load_manifest(pilot, run_id)
    destination = directory / "paired_results.source_verified.json"
    require(not destination.exists(), "Source-verified output exists; refusing overwrite")
    rows = read_jsonl(directory / "trial_results.checkpoint.jsonl")
    validate_resume(value, rows, read_jsonl(directory / "raw_calls.jsonl"))
    source_checks = read_json(source_checks_path)
    result = paired_results(value, rows, source_checks)
    result["repeat_source_checks_sha256"] = pilot.file_hash(source_checks_path)
    settings = pilot.Settings.from_env(ROOT / ".env")
    pilot.write_json(destination, redact(result, settings.api_key), True)
    require(snapshot(pilot, directory) == baseline, "Protected inputs changed during finalize")
    print(
        "Independent source classifications saved; original human annotations unchanged", flush=True
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "prepare", "run", "finalize"))
    parser.add_argument("--run-id")
    parser.add_argument(
        "--allow-api",
        action="store_true",
        help="Use only after separate authorization for real inference",
    )
    parser.add_argument("--source-checks", type=Path)
    args = parser.parse_args(argv)
    if args.mode != "preflight" and not args.run_id:
        parser.error("--run-id is required")
    if args.mode == "run" and not args.allow_api:
        parser.error("run requires separately authorized --allow-api")
    if args.mode != "run" and args.allow_api:
        parser.error("--allow-api is valid only for run")
    if args.mode == "finalize" and args.source_checks is None:
        parser.error("finalize requires independently completed --source-checks")
    pilot = load_executor()
    if args.mode == "preflight":
        value = preflight(pilot)
        print(
            pilot.canonical(
                {
                    "preflight": "PASS",
                    "API_calls": 0,
                    "repository_writes": 0,
                    "selection_rule": value["selection_rule"],
                    "pairs": [
                        {
                            "review_id": p["query"]["review_id"],
                            "rank1": p["conditions"]["A"]["page"],
                            "alternative": p["conditions"]["B"]["page"],
                        }
                        for p in value["pairs"]
                    ],
                    "planned_calls": value["planned_calls"],
                }
            )
        )
    elif args.mode == "prepare":
        prepare(pilot, args.run_id)
    elif args.mode == "run":
        run(pilot, args.run_id, args.allow_api)
    else:
        finalize(pilot, args.run_id, args.source_checks)


if __name__ == "__main__":
    main()
