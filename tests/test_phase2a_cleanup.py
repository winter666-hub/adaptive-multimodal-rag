"""Validate canonical treatment equality and final review queue provenance."""
import csv
import hashlib
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/oracle_headroom"
sys.path.insert(0, str(ROOT / "src"))
from furiosa_rag.cli.evaluate_answer_quality import parse_judge_output


def csv_rows(name):
    with (OUT / name).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class CleanupTests(unittest.TestCase):
    def test_canonical_labels_transitions_queue_and_protected_files(self):
        metrics = json.loads((OUT / "phase2a_automatic_final_metrics.json").read_text(encoding="utf-8"))
        manifest = json.loads((OUT / "phase2a_cleanup_manifest.json").read_text(encoding="utf-8"))
        for path, digest in manifest["protected_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest, path)
        canonical = csv_rows("text_expansion_canonical_evaluation_final.csv")
        audit = {r["query_id"]: r for r in csv_rows("text_expansion_input_consistency_final.csv")}
        queue = csv_rows("text_expansion_human_review_queue_final.csv")
        generations = {(r["query_id"], r["evidence_state"]): r for r in map(json.loads, (OUT / "phase2a_generation.checkpoint.jsonl").read_text(encoding="utf-8").splitlines())}
        self.assertEqual(len(canonical), 224)
        self.assertEqual(len({r["query_id"] for r in queue}), len(queue))
        changed = set()
        counts = {s: 0 for s in ("E3", "E6", "E9")}
        transitions = {p: {t: 0 for t in ("WRONG_TO_CORRECT", "CORRECT_TO_WRONG", "WRONG_TO_WRONG", "CORRECT_TO_CORRECT")} for p in ("E3_to_E6", "E6_to_E9")}
        for r in canonical:
            q = r["query_id"]
            identical = r["e6_e9_input_identical"] == "True"
            self.assertEqual(identical, all(audit[q][k] == "True" for k in ("ordered_chunk_ids_identical", "evidence_text_identical", "actual_chunk_count_identical", "prompt_input_identical")))
            source = "E6" if identical else "E9"
            self.assertEqual(r["canonical_e9_answer"], generations[q, source]["answer"])
            self.assertEqual(r["E9_canonical_correct"], r[f"{source}_raw_correct"])
            if identical:
                self.assertEqual(generations[q, "E6"]["generation_prompt_sha256"], generations[q, "E9"]["generation_prompt_sha256"])
            for a, b in (("E3", "E6"), ("E6", "E9")):
                va, vb = r[f"{a}_canonical_correct"], r[f"{b}_canonical_correct"]
                if va and vb and va != vb:
                    changed.add(q)
            if r["canonical_complete"] == "True":
                for s in counts:
                    counts[s] += r[f"{s}_canonical_correct"] == "True"
                for a, b in (("E3", "E6"), ("E6", "E9")):
                    key = ("CORRECT" if r[f"{a}_canonical_correct"] == "True" else "WRONG") + "_TO_" + ("CORRECT" if r[f"{b}_canonical_correct"] == "True" else "WRONG")
                    transitions[f"{a}_to_{b}"][key] += 1
        self.assertEqual(counts, metrics["primary"]["correct_counts"])
        for pair, expected in transitions.items():
            for key, value in expected.items():
                self.assertEqual(metrics["primary"]["transitions"][pair][key], value)
        self.assertEqual(changed, {r["query_id"] for r in queue})
        for r in queue:
            self.assertTrue(all(r[k] == "" for k in ("human_e3_correct", "human_e6_correct", "human_e9_correct", "human_notes", "human_confidence")))
            if r["e6_e9_input_identical"] == "True":
                self.assertEqual(r["E6_answer"], r["E9_answer"])
                self.assertEqual(r["E6_automatic_correct"], r["E9_automatic_correct"])
                self.assertEqual(r["E6_to_E9_wrong_to_correct"], "False")
                self.assertEqual(r["E6_to_E9_correct_to_wrong"], "False")
        self.assertEqual(len(csv_rows("human_retrieve_more_hit3_diagnostic_final.csv")), 6)
        recovered = [json.loads(x) for x in (OUT / "phase2a_cleanup_judgments.checkpoint.jsonl").read_text(encoding="utf-8").splitlines()] if (OUT / "phase2a_cleanup_judgments.checkpoint.jsonl").exists() else []
        self.assertEqual(len(recovered), metrics["recovered_states"])
        self.assertTrue({(r["query_id"], r["evidence_state"]) for r in recovered} <= {tuple(k) for k in manifest["missing_states"]})
        for r in recovered:
            score = parse_judge_output(r["judge_raw_response"])
            self.assertEqual(score.correctness, r["correctness_score"])
            self.assertEqual(score.correctness >= manifest["correctness_threshold"], r["judge_correct"])


if __name__ == "__main__":
    unittest.main()
