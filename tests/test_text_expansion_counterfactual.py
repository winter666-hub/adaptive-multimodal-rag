"""Paired-evaluation arithmetic and fixed-evidence artifact checks."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_oracle_headroom_phase2a.py"
spec = importlib.util.spec_from_file_location("phase2a", SCRIPT)
phase2a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase2a)


class PairedEvaluationTests(unittest.TestCase):
    def test_recovery_degradation_and_error_denominators(self):
        values = [{"E3": False, "E6": True, "E9": True},
                  {"E3": True, "E6": False, "E9": True},
                  {"E3": False, "E6": False, "E9": False},
                  {"E3": True, "E6": True, "E9": False}]
        result = phase2a.stats(values)
        self.assertEqual(result["correct_counts"], {"E3": 2, "E6": 2, "E9": 2})
        self.assertEqual(result["accuracy_percent"], {"E3": 50, "E6": 50, "E9": 50})
        self.assertEqual(result["transitions"]["E3_to_E6"], {
            "WRONG_TO_CORRECT": 1, "CORRECT_TO_WRONG": 1,
            "WRONG_TO_WRONG": 1, "CORRECT_TO_CORRECT": 1})
        self.assertEqual(result["E6_recovery_rate_among_E3_errors_percent"], 50)
        self.assertEqual(result["E9_recovery_rate_among_E6_errors_percent"], 50)
        self.assertEqual(result["marginal_pp_E9_minus_E6"], 0)

    def test_zero_errors_are_not_an_invented_zero_recovery_rate(self):
        result = phase2a.stats([{"E3": True, "E6": True, "E9": True}])
        self.assertIsNone(result["E6_recovery_rate_among_E3_errors_percent"])
        self.assertIsNone(result["E9_recovery_rate_among_E6_errors_percent"])

    def test_prompt_keeps_original_question_and_chunk_order(self):
        chunks = [{"chunk_id": "page-7-chunk-2", "source_page": 7, "chunk_text": "original evidence"},
                  {"chunk_id": "page-3-chunk-1", "source_page": 3, "chunk_text": "new evidence"}]
        prompt = phase2a.state_prompt("Exact original question?", chunks)
        self.assertIn("Question: Exact original question?", prompt)
        self.assertLess(prompt.index("original evidence"), prompt.index("new evidence"))
        self.assertNotIn("BEGIN VISUAL CONTEXT", prompt)

    @unittest.skipUnless((ROOT / "results/oracle_headroom/phase2a_metrics.json").exists(),
                         "Final Phase 2A artifacts not yet available")
    def test_final_artifact_coverage_transitions_and_protected_inputs(self):
        out = ROOT / "results/oracle_headroom"
        def read(path):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        answers = read(out / "text_expansion_counterfactual_answers.csv")
        judgments = read(out / "text_expansion_counterfactual_judgments.csv")
        queue = read(out / "text_expansion_human_review_queue.csv")
        human = read(out / "human_retrieve_more_counterfactual.csv")
        metrics = json.loads((out / "phase2a_metrics.json").read_text(encoding="utf-8"))
        manifest = json.loads((out / "phase2a_run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((len(answers), len(judgments), len(human)), (690, 230, 22))
        self.assertEqual(len({(r["query_id"], r["evidence_state"]) for r in answers}), 690)
        master = {r["query_id"]: r for r in read(ROOT / "results/error_analysis/analysis_master.csv")}
        cohorts = {r["query_id"]: r for r in read(out / "retrieval_depth_cohorts.csv")}
        pool = {}
        for r in read(out / "expanded_reranked_candidates.csv"):
            pool.setdefault(r["query_id"], {})[r["chunk_id"]] = r
        for r in answers:
            if r["status"] != "OK":
                continue
            qid, state = r["query_id"], r["evidence_state"]
            original = [{"chunk_id": master[qid][f"reranked_chunk_id_{i}"],
                         "source_page": master[qid][f"reranked_page_{i}"],
                         "chunk_text": master[qid][f"reranked_chunk_text_{i}"]} for i in (1, 2, 3)]
            if state == "E3":
                chunks = original
            else:
                lookup = {**pool[qid], **{s["chunk_id"]: s for s in original}}
                chunks = [lookup[c] for c in json.loads(cohorts[qid][f"{state}_ranked_chunks"])]
            prompt = phase2a.state_prompt(master[qid]["question"], chunks)
            expected_hash = hashlib.sha256(prompt.encode()).hexdigest()
            self.assertEqual(r["generation_prompt_sha256"], expected_hash)
            self.assertEqual(manifest["generation_prompt_sha256"][f"{qid}:{state}"], expected_hash)
            self.assertEqual(int(r["input_character_count"]), len(prompt))
            self.assertEqual(int(r["actual_chunk_count"]), len(chunks))
        primary = [r for r in judgments if r["population"] == "PRIMARY_224"]
        self.assertEqual(len(primary), 224)
        changed = set()
        for r in primary:
            for a, b in (("E3", "E6"), ("E6", "E9")):
                if r[f"{a}_status"] == r[f"{b}_status"] == "OK" and r[f"{a}_correct"] != r[f"{b}_correct"]:
                    changed.add(r["query_id"])
        self.assertEqual({r["query_id"] for r in queue}, changed)
        self.assertTrue(all(r["human_verdict"] == r["human_notes"] == "" for r in queue))
        calculated = [{s: r[f"{s}_correct"] == "True" for s in phase2a.STATES} for r in primary
                      if all(r[f"{s}_status"] == "OK" for s in phase2a.STATES)]
        self.assertEqual(phase2a.stats(calculated), metrics["primary"])
        missing = [r for r in answers if r["status"] == "UNAVAILABLE_EVIDENCE"]
        self.assertEqual(len(missing), 12)
        self.assertTrue(all(r["answer"] == "" and r["generation_latency_ms"] == "" for r in missing))
        for path, expected in manifest["input_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected, path)


if __name__ == "__main__":
    unittest.main()
