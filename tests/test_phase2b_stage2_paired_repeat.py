"""Mock-only paired experiment safety tests. No network transport is permitted."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "stage2_paired_runner_tests", ROOT / "scripts/run_phase2b_stage2_paired_repeat.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import furiosa_rag.clients.furiosa as transport

    def forbidden(*args, **kwargs):
        pytest.fail("A mock/preflight test attempted real network transport")

    monkeypatch.setattr(transport, "urlopen", forbidden)


def response(content, reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": reason}]}


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    pilot = runner.load_executor()
    output = tmp_path / "results/new_run"
    output.mkdir(parents=True)
    pilot.ROOT, pilot.OUT = tmp_path, output
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "OUTPUT_BASE", tmp_path / "results/paired")
    query = {
        "review_id": "P2B_S2_001",
        "query_id": "query",
        "document_id": "pdf",
        "source_pdf": "mock.pdf",
        "question": "What value?",
        "reference_answer": "42",
        "candidate_pages": [1, 2],
        "rank1_page": 1,
        "decision_state_sha256": "frozen",
        "ordered_chunks": [
            {
                "rank": 1,
                "chunk_id": "chunk",
                "page": 1,
                "text": "Frozen evidence",
                "retrieval_score": 1.0,
                "rerank_score": 1.0,
            }
        ],
    }
    config = {
        "render_dpi": 144.0,
        "renderer_max_pixels": 20_000_000,
        "vision_max_tokens": 256,
        "final_max_tokens": 1024,
    }
    from furiosa_rag.config import ModelEndpoint

    settings = SimpleNamespace(
        api_key="SECRET_TEST_KEY",
        request_timeout=120,
        vision_request_timeout=120,
        endpoints=(
            ModelEndpoint("vision", "https://mock.invalid/v1", "vision"),
            ModelEndpoint("llm", "https://mock.invalid/v1", "llm"),
        ),
    )
    png = b"mock image"
    monkeypatch.setattr(pilot.PdfPageRenderer, "render_png", lambda self, pdf, page: png)

    class Capture:
        def post_json(self, url, route, payload):
            self.payload = payload
            return response("visual")

    import base64

    capture = Capture()
    pilot.FuriosaVision(settings.endpoints[0], capture).analyze(
        query["question"], "data:image/png;base64," + base64.b64encode(png).decode(), max_tokens=256
    )
    wire_hash = pilot.digest(json.dumps(capture.payload).encode())
    pair = {
        "query": query,
        "original_human_annotation": {"notes": "original immutable"},
        "conditions": {
            c: {
                "state_id": f"P2B_S2_001__PAIRED_{c}_PAGE_{p}",
                "page": p,
                "original_image_sha256": pilot.digest(png),
                "original_VLM_wire_sha256": wire_hash,
            }
            for c, p in [("A", 1), ("B", 2)]
        },
    }
    manifest = {
        "pairs": [pair],
        "actual_config": config,
        "ordering_seed": "test",
        "workers": 1,
        "planned_calls": {
            "VLM_calls": 2,
            "final_generation_calls": 2,
            "logical_judge_calls": 2,
            "max_judge_request_attempts": 4,
        },
    }
    journal_type = runner.install_execution_guards(pilot)
    journal = journal_type(manifest, settings.api_key)
    calls = []

    def install_transport(mode="ok"):
        def mock_transport(client, url, route, payload):
            calls.append(copy.deepcopy(payload))
            if payload["model"] == "vision":
                if mode == "vlm_error":
                    raise RuntimeError("Failure including " + settings.api_key)
                if mode == "vlm_empty":
                    return response("   ")
                return response("Visual value 42", "length" if mode == "token_cap" else "stop")
            prompt = payload["messages"][0]["content"]
            if "BEGIN TEXT CONTEXT" in prompt:
                if mode == "final_error":
                    raise RuntimeError("Final failure " + settings.api_key)
                return response(
                    "Fresh answer 42 " + settings.api_key,
                    "length" if mode == "token_cap" else "stop",
                )
            if mode == "judge_transport_error":
                raise RuntimeError("Judge transport failed " + settings.api_key)
            if mode == "judge_invalid":
                return response("not JSON")
            return response(
                json.dumps(
                    {
                        "correctness": 4,
                        "completeness": 2,
                        "grounding": 2,
                        "task_satisfaction": 2,
                        "total": 10,
                        "reason": "Source matches",
                    }
                )
            )

        monkeypatch.setattr(pilot.FuriosaClient, "post_json", mock_transport)

    def execute(condition="A"):
        job = next(j for j in runner.jobs_for(manifest) if j[1] == "PAIRED_" + condition)
        pilot.execute_trial(job, settings, manifest, journal)
        return runner.read_jsonl(output / "trial_results.checkpoint.jsonl")[-1]

    return SimpleNamespace(
        pilot=pilot,
        manifest=manifest,
        output=output,
        query=query,
        calls=calls,
        install_transport=install_transport,
        execute=execute,
        journal=journal,
        settings=settings,
    )


@pytest.mark.parametrize("mode", ["vlm_error", "vlm_empty"])
def test_visual_failure_blocks_text_fallback_and_judge(experiment, mode):
    experiment.install_transport(mode)
    row = experiment.execute()
    assert len(experiment.calls) == 1
    assert row["generation_availability"] == "UNAVAILABLE"
    assert row["status"] == "VISUAL_ACQUISITION_UNAVAILABLE"
    assert row["FINAL_receipts"] == []
    assert row["judge_scores"] is None


def test_render_failure_makes_no_transport_calls(experiment, monkeypatch):
    def bad_render(*args):
        raise ValueError("cannot render")

    monkeypatch.setattr(experiment.pilot.PdfPageRenderer, "render_png", bad_render)
    experiment.install_transport()
    row = experiment.execute()
    assert experiment.calls == []
    assert row["generation_availability"] == "UNAVAILABLE"


def test_image_drift_blocks_transport(experiment, monkeypatch):
    monkeypatch.setattr(
        experiment.pilot.PdfPageRenderer, "render_png", lambda *args: b"different pixels"
    )
    experiment.install_transport()
    row = experiment.execute()
    assert experiment.calls == []
    assert not row["visual_generation_available"]


def test_request_drift_blocks_transport(experiment):
    experiment.manifest["actual_config"]["vision_max_tokens"] = 257
    experiment.install_transport()
    row = experiment.execute()
    assert experiment.calls == []
    assert not row["visual_generation_available"]


def test_final_failure_has_no_retry_or_judge(experiment):
    experiment.install_transport("final_error")
    row = experiment.execute()
    assert len(experiment.calls) == 2
    assert row["status"] == "FINAL_GENERATION_ERROR"
    assert row["generation_availability"] == "UNAVAILABLE"
    assert row.get("JUDGE_receipts", []) == []


@pytest.mark.parametrize("mode,attempts", [("judge_invalid", 4), ("judge_transport_error", 3)])
def test_judge_unavailable_is_separate_from_source_correctness(experiment, mode, attempts):
    experiment.install_transport(mode)
    row = experiment.execute()
    assert len(experiment.calls) == attempts
    assert row["diagnostic_judge_status"] == "UNAVAILABLE"
    assert row["visual_generation_available"]
    assert row["repeat_source_verification"]["source_correctness"] is None


def test_token_caps_preserve_available_answer_without_source_label(experiment):
    experiment.install_transport("token_cap")
    row = experiment.execute()
    assert row["visual_generation_available"]
    assert row["stage_provenance"]["VLM"][0]["token_cap"]
    assert row["stage_provenance"]["FINAL"][0]["token_cap"]
    assert row["repeat_source_verification"]["source_correctness"] is None
    assert row["paired_classification"] == "PENDING_SOURCE_VERIFICATION"


def test_credentials_redacted_from_all_saved_receipts(experiment):
    experiment.install_transport()
    experiment.execute()
    for path in experiment.output.glob("*.jsonl"):
        assert experiment.settings.api_key not in path.read_text(encoding="utf-8")
    text = (experiment.output / "raw_calls.jsonl").read_text(encoding="utf-8")
    assert "[REDACTED]" in text
    assert "wire_json_sha256" in text and "finish_reason" in text


def test_a_b_are_fresh_and_share_frozen_text(experiment):
    experiment.install_transport()
    a = experiment.execute("A")
    b = experiment.execute("B")
    assert len(experiment.calls) == 6
    assert a["text_context_sha256"] == b["text_context_sha256"]
    assert a["condition"] == "A" and b["condition"] == "B"
    assert a["page_id"] == 1 and b["page_id"] == 2
    assert a["state_id"] != b["state_id"]


def test_full_46_state_mock_run_and_resume_make_no_hidden_calls(experiment, monkeypatch):
    manifest = experiment.manifest
    prototype = manifest["pairs"][0]
    pairs = []
    pdf = runner.ROOT / "datasets/unidoc/mock.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"mock PDF")
    for rid, page in runner.VERIFIED_PAGES.items():
        pair = copy.deepcopy(prototype)
        pair["query"].update(
            {
                "review_id": rid,
                "query_id": rid + "query",
                "document_id": rid + "pdf",
                "candidate_pages": [1, page],
                "pdf_sha256": experiment.pilot.file_hash(pdf),
            }
        )
        pair["selected_alternative_page"] = page
        for c, p in [("A", 1), ("B", page)]:
            pair["conditions"][c].update({"state_id": rid + "__" + c, "page": p})
        pairs.append(pair)
    manifest.update(
        {
            "pairs": pairs,
            "workers": 3,
            "planned_calls": {
                "VLM_calls": 46,
                "final_generation_calls": 46,
                "logical_judge_calls": 46,
                "max_judge_request_attempts": 92,
            },
        }
    )
    monkeypatch.setattr(runner, "load_manifest", lambda *args: (experiment.output, manifest, {}))
    monkeypatch.setattr(runner, "snapshot", lambda *args: {})
    monkeypatch.setattr(experiment.pilot.Settings, "from_env", lambda *args: experiment.settings)
    monkeypatch.setattr(
        experiment.pilot, "settings_dict", lambda settings: manifest["actual_config"]
    )
    experiment.install_transport()
    runner.run(experiment.pilot, "mock_run", allow_api=True)
    assert len(experiment.calls) == 138
    rows = runner.read_jsonl(experiment.output / "trial_results.checkpoint.jsonl")
    assert len(rows) == 46
    assert sum(r["condition"] == "A" for r in rows) == 23
    assert sum(r["condition"] == "B" for r in rows) == 23
    result = runner.read_json(experiment.output / "paired_results.pending.json")
    assert all(p["paired_classification"] == "PENDING_SOURCE_VERIFICATION" for p in result["pairs"])
    runner.run(experiment.pilot, "mock_run", allow_api=True)
    assert len(experiment.calls) == 138


def test_resume_skips_completed_failed_state_and_rejects_interruption(experiment):
    experiment.install_transport("vlm_error")
    row = experiment.execute()
    events = runner.read_jsonl(experiment.output / "raw_calls.jsonl")
    done = runner.validate_resume(experiment.manifest, [row], events)
    assert row["state_id"] in done
    with pytest.raises(ValueError, match="Interrupted state"):
        runner.validate_resume(experiment.manifest, [], events)
    with pytest.raises(ValueError, match="Interrupted raw call"):
        runner.validate_resume(experiment.manifest, [], events[:1])


def test_duplicate_and_original_cached_state_rejected(experiment):
    experiment.install_transport()
    row = experiment.execute()
    with pytest.raises(ValueError, match="Duplicate"):
        runner.validate_trials(experiment.manifest, [row, row])
    row["state_id"] = "P2B_S2_001__PAGE_1"
    with pytest.raises(ValueError, match="cached/original/foreign"):
        runner.validate_trials(experiment.manifest, [row])


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("NO", "YES", "PAIRED_RECOVERY_REPRODUCED"),
        ("YES", "YES", "BOTH_CORRECT_NO_PAIRED_RECOVERY"),
        ("NO", "NO", "RECOVERY_REPRODUCTION_FAILED"),
        ("YES", "NO", "ALTERNATIVE_SELECTION_ADVERSE"),
        (None, "YES", "PENDING_SOURCE_VERIFICATION"),
        ("UNCLEAR", "YES", "SOURCE_UNCLEAR"),
    ],
)
def test_source_classifications(a, b, expected):
    assert runner.classify_pair(a, b) == expected
    assert runner.classify_pair(a, b, a_available=False) == "EXPERIMENT_UNAVAILABLE"


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "a\\b", ".", "", "a b"])
def test_unsafe_run_directory_rejected(run_id):
    with pytest.raises(ValueError, match="run_id"):
        runner.run_directory(run_id)


def test_run_requires_explicit_api_gate_before_loading_anything():
    with pytest.raises(ValueError, match="separately authorized"):
        runner.run(None, "not-prepared")
    with pytest.raises(SystemExit):
        runner.main(["run", "--run-id", "not-prepared"])


def test_prepare_create_only_and_current_snapshot_integrity(tmp_path, monkeypatch):
    pilot = runner.load_executor()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "OUTPUT_BASE", tmp_path / "results/paired")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "runner.py"
    script.write_text("immutable runner", encoding="utf-8")
    original = scripts / "run_phase2b_stage1_pilot.py"
    original.write_text("immutable common", encoding="utf-8")
    source = tmp_path / "results/source.csv"
    source.parent.mkdir()
    source.write_text("human values", encoding="utf-8")
    monkeypatch.setattr(runner, "SCRIPT", script)
    pairs = [
        {
            "query": {"review_id": rid},
            "conditions": {c: {"state_id": rid + c, "page": p} for c, p in [("A", 1), ("B", page)]},
        }
        for rid, page in runner.VERIFIED_PAGES.items()
    ]
    value = {
        "pairs": pairs,
        "repeat_executor_sha256": pilot.file_hash(script),
        "original_executor_sha256": pilot.file_hash(original),
        "input_hashes": {"results/source.csv": pilot.file_hash(source)},
    }
    monkeypatch.setattr(runner, "preflight", lambda executor: copy.deepcopy(value))
    runner.prepare(pilot, "safe_run")
    directory, manifest, _ = runner.load_manifest(pilot, "safe_run")
    assert (directory / "repeat_source_checks.template.json").is_file()
    assert manifest["protected_file_count"] >= 3
    with pytest.raises(ValueError, match="refusing overwrite"):
        runner.prepare(pilot, "safe_run")
    source.write_text("changed annotation", encoding="utf-8")
    with pytest.raises(ValueError, match="Prepared input changed"):
        runner.load_manifest(pilot, "safe_run")


def test_p006_selection_manifest_rule_and_exact_23_jobs():
    assert runner.VERIFIED_PAGES["P2B_S2_006"] == 16
    assert len(runner.VERIFIED_PAGES) == 23
    assert "p2 -> p16 -> p1" in runner.SELECTION_RULE
    pairs = [
        {
            "query": {"review_id": rid, "candidate_pages": [2, 16, 1], "rank1_page": 2},
            "conditions": {c: {"state_id": rid + c, "page": p} for c, p in [("A", 2), ("B", 16)]},
        }
        for rid in runner.VERIFIED_PAGES
    ]
    jobs = runner.jobs_for({"pairs": pairs, "ordering_seed": "fixed"})
    assert len(jobs) == len({j[4] for j in jobs}) == 46
    assert sum(j[1] == "PAIRED_A" for j in jobs) == 23
    assert sum(j[1] == "PAIRED_B" for j in jobs) == 23


def test_finalization_requires_new_answer_bound_source_checks(monkeypatch):
    # Full 46-state schema with deliberately contradictory automatic diagnostics.
    pairs, rows, checks = [], [], {}
    for rid, page in runner.VERIFIED_PAGES.items():
        q = {
            "review_id": rid,
            "query_id": rid + "query",
            "document_id": rid + "pdf",
            "candidate_pages": [999, page],
            "decision_state_sha256": "frozen",
        }
        conditions = {}
        for c, p in [("A", 999), ("B", page)]:
            sid = rid + "__" + c
            conditions[c] = {"state_id": sid, "page": p}
            rows.append(
                {
                    "state_id": sid,
                    "review_id": rid,
                    "query_id": q["query_id"],
                    "document_id": q["document_id"],
                    "page_id": p,
                    "trial_role": "PAIRED_" + c,
                    "candidate_rank": 1 if c == "A" else 2,
                    "decision_state_sha256": "frozen",
                    "actual_config": {},
                    "VLM_status": "OK",
                    "FINAL_receipts": [{"http_success": True}],
                    "answer": "fresh answer",
                    "visual_generation_available": True,
                    "diagnostic_judge_status": "UNAVAILABLE",
                    "diagnostic_automatic_correctness": c == "A",
                    "repeat_source_verification": {"source_correctness": None},
                }
            )
            checks[sid] = {
                "review_id": rid,
                "condition": c,
                "page": p,
                "answer_sha256": hashlib.sha256(b"fresh answer").hexdigest(),
                "source_correctness": "NO" if c == "A" else "YES",
                "source_check_notes": "independent PDF comparison",
                "pdf_pages_checked": [p],
                "human_confidence": "HIGH",
                "page_selection_attribution": None,
            }
        pairs.append(
            {
                "query": q,
                "conditions": conditions,
                "original_human_annotation": {"notes": "original unchanged"},
            }
        )
    manifest = {"pairs": pairs, "ordering_seed": "test", "actual_config": {}}
    pending = runner.paired_results(manifest, rows)
    assert all(
        p["paired_classification"] == "PENDING_SOURCE_VERIFICATION" for p in pending["pairs"]
    )
    result = runner.paired_results(manifest, rows, checks)
    assert all(p["paired_classification"] == "PAIRED_RECOVERY_REPRODUCED" for p in result["pairs"])
    bad = copy.deepcopy(checks)
    next(iter(bad.values()))["answer_sha256"] = "old cached answer hash"
    with pytest.raises(ValueError, match="fresh answer"):
        runner.paired_results(manifest, rows, bad)
    bad = copy.deepcopy(checks)
    sid = next(iter(bad))
    bad[sid]["page"] = -1
    with pytest.raises(ValueError, match="page mismatch"):
        runner.paired_results(manifest, rows, bad)
    rows[0]["visual_generation_available"] = False
    with pytest.raises(ValueError, match="Unavailable generation"):
        runner.paired_results(manifest, rows, checks)
