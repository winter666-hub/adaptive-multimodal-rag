"""Semantic checks for fixed-E3 oracle evidence acquisition."""
import importlib.util
import csv
import hashlib
import json
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_oracle_headroom_phase1b.py"
spec = importlib.util.spec_from_file_location("oracle_expansion", SCRIPT)
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


def chunk(name, page, score=0):
    return {"chunk_id": name, "source_page": page, "chunk_text": name, "rerank_score": score}


class EvidenceAcquisitionTests(unittest.TestCase):
    def test_initial_order_survives_reranking_and_new_ids_are_appended(self):
        old = [chunk("a", 1), chunk("b", 2), chunk("c", 3)]
        expanded = [chunk("new4", 9, 0.4), chunk("c", 3, 1), chunk("new1", 4, 0.9),
                    chunk("new2", 5, 0.8), chunk("new3", 6, 0.7),
                    chunk("a", 1, 0.6), chunk("new5", 10, 0.3), chunk("new6", 11, 0.2)]
        states, new, first, cohort = oracle.cumulative_states(old, expanded, {9})
        self.assertEqual([c["chunk_id"] for c in states[1]], ["a", "b", "c", "new1", "new2", "new3"])
        self.assertEqual([c["chunk_id"] for c in new], [f"new{i}" for i in range(1, 7)])
        self.assertEqual(states[2][:3], old)
        self.assertEqual((first, cohort), (4, "MISS_AT_6_HIT_AT_9"))

    def test_duplicate_page_is_kept_and_any_gt_page_recovers(self):
        old = [chunk("a", 1), chunk("b", 1), chunk("c", 2)]
        expanded = [chunk("new1", 1, 0.9), chunk("new2", 7, 0.8), chunk("new3", 8, 0.7)]
        states, new, first, cohort = oracle.cumulative_states(old, expanded, {7, 99})
        self.assertEqual(len(new), 3)
        self.assertEqual(len(states[1]), 6)
        self.assertEqual((first, cohort), (2, "MISS_AT_3_HIT_AT_6"))

    def test_exhausted_pool_is_not_padded_and_beyond_six_is_distinct(self):
        old = [chunk("a", 1), chunk("b", 2), chunk("c", 3)]
        states, _, first, cohort = oracle.cumulative_states(old, [chunk("d", 4, 1)], {9})
        self.assertEqual(len(states[2]), 4)
        self.assertEqual(states[1], states[2])
        self.assertIsNone(first)
        self.assertEqual(cohort, "MISS_AT_9")
        expanded = [chunk(f"n{i}", i+3, 1/i) for i in range(1, 8)]
        _, _, first, cohort = oracle.cumulative_states(old, expanded, {10})
        self.assertEqual((first, cohort), (7, "MISS_AT_9"))

    @unittest.skipUnless((SCRIPT.parents[1] / "results/oracle_headroom/phase1b_metrics.json").exists(),
                         "Phase 1B final artifacts not yet available")
    def test_final_artifacts_preserve_original_state_and_match_page_recovery(self):
        root = SCRIPT.parents[1]
        out = root / "results/oracle_headroom"

        def read(path):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))

        master = {r["query_id"]: r for r in read(root / "results/error_analysis/analysis_master.csv")}
        cohorts = read(out / "retrieval_depth_cohorts.csv")
        costs = {r["query_id"]: r for r in read(out / "retrieval_expansion_costs.csv")}
        expanded = {}
        for r in read(out / "expanded_reranked_candidates.csv"):
            expanded.setdefault(r["query_id"], []).append(r)
        self.assertEqual((len(cohorts), len(costs), len(expanded)), (1600, 224, 224))
        hit6 = hit9 = 0
        for row in cohorts:
            qid = row["query_id"]
            initial = oracle.original_sources(master[qid])
            original_ids = [s["chunk_id"] for s in initial]
            self.assertEqual(json.loads(row["E3_ranked_chunks"]), original_ids)
            gt = set(json.loads(master[qid]["expected_pages"]))
            if row["expanded_run_performed"] == "False":
                self.assertEqual(row["cohort"], "HIT_AT_3")
                self.assertTrue(gt.intersection(s["source_page"] for s in initial))
                self.assertEqual(row["E6_ranked_chunks"], "")
                hit6 += 1
                hit9 += 1
                continue
            pool = expanded[qid]
            self.assertEqual([int(s["expanded_rank"]) for s in pool], list(range(1, len(pool) + 1)))
            self.assertEqual(len(pool), min(30, int(costs[qid]["document_chunk_count"])))
            self.assertEqual(len({s["chunk_id"] for s in pool}), len(pool))
            scores = [float(s["rerank_score"]) for s in pool]
            self.assertEqual(scores, sorted(scores, reverse=True))
            additions = [s for s in pool if s["chunk_id"] not in set(original_ids)]
            e6, e9 = initial + additions[:3], initial + additions[:6]
            self.assertEqual(json.loads(row["E6_ranked_chunks"]), [s["chunk_id"] for s in e6])
            self.assertEqual(json.loads(row["E9_ranked_chunks"]), [s["chunk_id"] for s in e9])
            self.assertEqual(json.loads(row["E6_ranked_pages"]), [int(s["source_page"]) for s in e6])
            self.assertEqual(json.loads(row["E9_ranked_pages"]), [int(s["source_page"]) for s in e9])
            first = next((i for i, s in enumerate(additions, 1) if int(s["source_page"]) in gt), 0)
            self.assertEqual(int(row["first_recovery_new_rank"] or 0), first)
            h6 = any(int(s["source_page"]) in gt for s in e6)
            h9 = any(int(s["source_page"]) in gt for s in e9)
            self.assertEqual(row["cohort"], "MISS_AT_3_HIT_AT_6" if h6 else
                             "MISS_AT_6_HIT_AT_9" if h9 else "MISS_AT_9")
            hit6 += h6
            hit9 += h9
            for label, state in (("E3", initial), ("E6", e6), ("E9", e9)):
                self.assertEqual(int(costs[qid][f"{label}_text_chars"]), sum(len(s["chunk_text"]) for s in state))
            self.assertEqual(int(costs[qid]["delta_chars_3_to_6"]), sum(len(s["chunk_text"]) for s in additions[:3]))
            self.assertEqual(int(costs[qid]["delta_chars_6_to_9"]), sum(len(s["chunk_text"]) for s in additions[3:6]))
        metrics = json.loads((out / "phase1b_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual((hit6, hit9), (metrics["cumulative_hit6"], metrics["cumulative_hit9"]))
        original_human = read(root / "results/error_analysis/human_review_progress.csv")
        joined_human = read(out / "human_review_expansion_join.csv")
        self.assertEqual(len(joined_human), 72)
        for original, joined in zip(original_human, joined_human, strict=True):
            self.assertTrue(all(joined[k] == v for k, v in original.items()))
        manifest = json.loads((out / "phase1b_run_manifest.json").read_text(encoding="utf-8"))
        for path, expected in manifest["input_sha256"].items():
            self.assertEqual(hashlib.sha256((root / path).read_bytes()).hexdigest(), expected, path)


if __name__ == "__main__":
    unittest.main()
