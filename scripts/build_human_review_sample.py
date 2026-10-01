"""Build a reproducible, unannotated human-review sample from existing results."""

import csv
import json
import random
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/error_analysis/analysis_master.csv"
OUTPUT = ROOT / "results/error_analysis/human_review_sample_72.csv"
SEED = 20260930
STATES = (
    "V_SELECTED_GT_WRONG",
    "V_GT_ABSENT_TOP3",
    "V_GT_IN_TOP3_WRONG_PAGE",
    "T_GT_TOP1_WRONG",
    "T_GT_ABSENT_TOP3",
    "T_GT_ONLY_TOP2_3",
)
EXPECTED = dict(zip(STATES, (151, 123, 113, 102, 49, 44)))
ANNOTATION_FIELDS = (
    "human_judge_valid",
    "human_failure_type",
    "human_gt_page_useful",
    "human_single_page_sufficient",
    "human_multi_page_required",
    "human_best_next_action",
    "human_confidence",
    "human_notes",
)
CONTEXT_FIELDS = (
    "query_id document_id domain question_type source_pdf source_pdf_path "
    "question gold_answer answer_type expected_route expected_pages expected_page_count "
    "ra_route vision_used selected_page reranked_page_1 reranked_page_2 reranked_page_3 "
    "reranked_chunk_id_1 reranked_chunk_id_2 reranked_chunk_id_3 "
    "reranked_chunk_text_1 reranked_chunk_text_2 reranked_chunk_text_3 "
    "gt_hit_at_1 gt_hit_at_3 selected_page_gt_hit "
    "forced_text_answer forced_text_score forced_text_correct forced_text_judge_reason "
    "forced_vision_answer forced_vision_score forced_vision_correct forced_vision_judge_reason "
    "ra_answer ra_score ra_correct ra_judge_reason multi_gt_review_candidate"
).split()


def state_matches(row):
    if row["ra_correct"] != "False":
        return []
    visual = row["ra_route"] == "VISUAL_REQUIRED"
    text = row["ra_route"] == "TEXT_ONLY"
    return [
        state
        for state, condition in (
            (STATES[0], visual and row["selected_page_gt_hit"] == "True"),
            (STATES[1], visual and row["gt_hit_at_3"] == "False"),
            (STATES[2], visual and row["gt_hit_at_3"] == "True" and row["selected_page_gt_hit"] == "False"),
            (STATES[3], text and row["gt_hit_at_1"] == "True"),
            (STATES[4], text and row["gt_hit_at_3"] == "False"),
            (STATES[5], text and row["gt_hit_at_3"] == "True" and row["gt_hit_at_1"] == "False"),
        )
        if condition
    ]


def quality(rows, used_documents, eligible_pages):
    """Lexicographic priorities; combination variety breaks coverage ties."""
    questions = Counter(r["question_type"] for r in rows)
    domains = Counter(r["domain"] for r in rows)
    documents = Counter(r["document_id"] for r in rows)
    flags = {r["multi_gt_review_candidate"] for r in rows}
    flag_counts = Counter(r["multi_gt_review_candidate"] for r in rows)
    pages = {r["expected_page_count"] for r in rows}
    combinations = {
        (r["domain"], r["question_type"], r["multi_gt_review_candidate"])
        for r in rows
    }
    return (
        len(questions),
        len(domains),
        len(documents),
        len({r["document_id"] for r in rows if r["document_id"] not in used_documents}),
        len(flags),
        min(min(flag_counts.values()), 3) if len(flags) == 2 else 0,
        len(combinations),
        len(pages & eligible_pages),
        -max(questions.values()),
        -max(domains.values()),
        -sum(n * n for n in questions.values()),
        -sum(n * n for n in domains.values()),
    )


def choose(pool, rng, used_documents):
    # Multiple fixed-seed starts and single-row improvements avoid CSV-order bias.
    pool = sorted(pool, key=lambda r: r["query_id"])
    page_frequencies = Counter(r["expected_page_count"] for r in pool)
    eligible_pages = {page for page, count in page_frequencies.items() if count >= 5}
    best = None
    best_score = None
    for _ in range(12):
        sample = rng.sample(pool, 12)
        for _ in range(12):
            current = quality(sample, used_documents, eligible_pages)
            move = None
            move_score = current
            sample_ids = {r["query_id"] for r in sample}
            for i in range(12):
                for candidate in pool:
                    if candidate["query_id"] in sample_ids:
                        continue
                    trial = sample[:i] + sample[i + 1 :] + [candidate]
                    score = quality(trial, used_documents, eligible_pages)
                    if score > move_score:
                        move, move_score = (i, candidate), score
            if move is None:
                break
            sample[move[0]] = move[1]
        score = quality(sample, used_documents, eligible_pages)
        if best_score is None or score > best_score:
            best, best_score = sample, score
    return sorted(best, key=lambda r: r["query_id"])


def page_number(value):
    return int(value) if value.strip() else None


def main():
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_fields = reader.fieldnames
        rows = list(reader)
    if len(rows) != 1600 or len({r["query_id"] for r in rows}) != len(rows):
        raise ValueError("Expected 1,600 unique source queries")
    if not set(CONTEXT_FIELDS).issubset(source_fields):
        raise ValueError("Required review context is missing from source schema")
    wrong = [r for r in rows if r["ra_correct"] == "False"]
    groups = {state: [] for state in STATES}
    overlaps = unassigned = 0
    for row in wrong:
        matches = state_matches(row)
        overlaps += len(matches) > 1
        unassigned += not matches
        for state in matches:
            groups[state].append(row)
    actual = {state: len(groups[state]) for state in STATES}
    print("RA wrong:", len(wrong), "state counts:", actual)
    print("Overlap:", overlaps, "unassigned:", unassigned)
    if len(wrong) != 582 or actual != EXPECTED or overlaps or unassigned:
        raise ValueError("Partition differs from expected counts; no output written")

    rng = random.Random(SEED)
    selected_by_state = {}
    used_documents = set()
    for state in STATES:
        sample = choose(groups[state], rng, used_documents)
        selected_by_state[state] = sample
        used_documents.update(r["document_id"] for r in sample)

    # Repair document repeats across states when an equally diverse substitute exists.
    all_selected = [r for state in STATES for r in selected_by_state[state]]
    document_counts = Counter(r["document_id"] for r in all_selected)
    for state in STATES:
        sample = selected_by_state[state]
        page_frequencies = Counter(r["expected_page_count"] for r in groups[state])
        eligible_pages = {page for page, count in page_frequencies.items() if count >= 5}
        for index, original in enumerate(sample):
            if document_counts[original["document_id"]] < 2:
                continue
            selected_ids = {r["query_id"] for r in all_selected}
            available = [
                r for r in groups[state]
                if r["query_id"] not in selected_ids and document_counts[r["document_id"]] == 0
            ]
            current_score = quality(sample, set(), eligible_pages)
            alternatives = [
                r for r in available
                if quality(sample[:index] + sample[index + 1:] + [r], set(), eligible_pages)
                >= current_score
            ]
            if alternatives:
                replacement = min(alternatives, key=lambda r: r["query_id"])
                sample[index] = replacement
                all_selected = [r for group in STATES for r in selected_by_state[group]]
                document_counts = Counter(r["document_id"] for r in all_selected)

    result = []
    for state in STATES:
        for index, original in enumerate(sorted(selected_by_state[state], key=lambda r: r["query_id"]), start=1):
            row = {
                "sample_id": f"HR{len(result) + 1:03d}",
                "log_state": state,
                "state_sample_index": index,
                **original,
            }
            ranked = [page_number(row[f"reranked_page_{n}"]) for n in (1, 2, 3)]
            unique_pages = list(dict.fromkeys(p for p in ranked if p is not None))
            gt_pages = set(json.loads(row["expected_pages"]))
            row["unique_reranked_pages"] = json.dumps(unique_pages)
            row["gt_pages_in_top3"] = json.dumps([p for p in unique_pages if p in gt_pages])
            selected = page_number(row["selected_page"])
            row["selected_page_rank_in_reranked_chunks"] = (
                ranked.index(selected) + 1 if selected is not None and selected in ranked else ""
            )
            row.update({field: "" for field in ANNOTATION_FIELDS})
            result.append(row)

    fields = ["sample_id", "log_state", "state_sample_index"] + source_fields + [
        "unique_reranked_pages",
        "gt_pages_in_top3",
        "selected_page_rank_in_reranked_chunks",
    ] + list(ANNOTATION_FIELDS)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result)
    print("Sample rows:", len(result))
    print("Duplicate query IDs:", len(result) - len({r["query_id"] for r in result}))
    print("Duplicate sample IDs:", len(result) - len({r["sample_id"] for r in result}))
    print("Queries missing from source:", len({r["query_id"] for r in result} - {r["query_id"] for r in rows}))
    for state in STATES:
        sample = [r for r in result if r["log_state"] == state]
        print(state, "sample:", len(sample), "domains:", len({r["domain"] for r in sample}),
              "question types:", len({r["question_type"] for r in sample}),
              "documents:", len({r["document_id"] for r in sample}),
              "multi-GT:", dict(Counter(r["multi_gt_review_candidate"] for r in sample)),
              "page counts:", len({r["expected_page_count"] for r in sample}))
    missing = Counter(field for row in result for field in CONTEXT_FIELDS if not row[field].strip())
    print("Missing context cells:", dict(missing))


if __name__ == "__main__":
    main()
