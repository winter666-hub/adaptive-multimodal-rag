"""Analyze existing UniDoc audit CSVs offline; no inference or judge calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from furiosa_rag.route_gt_audit import write_csv
from furiosa_rag.route_utility import (
    NOTES,
    build_utility_cases,
    efficiency_statistics,
    format_summary,
    load_csv,
    paired_correctness_rows,
    paired_correctness_statistics,
    summarize_utility,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option, filename in (
        ("alignment", "alignment"),
        ("retrieval-aware", "retrieval_aware"),
        ("retrieval-aware-judged", "retrieval_aware_judged"),
        ("output", "route_utility"),
    ):
        parser.add_argument(
            f"--{option}", type=Path, default=Path(f"results/unidoc_gt_audit_{filename}.csv")
        )
    parser.add_argument("--cases-output", type=Path, help="Optional question-level diagnostic CSV")
    parser.add_argument("--answer-type-output", type=Path)
    parser.add_argument("--paired-output", type=Path)
    parser.add_argument("--statistics-output", type=Path)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    args = parser.parse_args(argv)
    inputs = [args.alignment, args.retrieval_aware, args.retrieval_aware_judged]
    outputs = [
        path
        for path in (
            args.output,
            args.cases_output,
            args.answer_type_output,
            args.paired_output,
            args.statistics_output,
        )
        if path is not None
    ]
    resolved = [path.resolve() for path in outputs]
    if len(set(resolved)) != len(resolved) or set(resolved) & {p.resolve() for p in inputs}:
        parser.error("output paths must be distinct and must not overwrite input files")
    try:
        cases = build_utility_cases(*(load_csv(path) for path in inputs))
        summaries = summarize_utility(cases)
        write_csv(summaries, args.output)
        if args.cases_output:
            write_csv(cases, args.cases_output)
        if args.answer_type_output:
            write_csv(
                [row for row in summaries if row["group_type"] == "answer_type"],
                args.answer_type_output,
            )
        paired = paired_correctness_rows(cases)
        if args.paired_output:
            write_csv(paired, args.paired_output)
        statistics = {
            "route_utility_overall": summaries[0],
            "route_utility_by_answer_type": [
                row for row in summaries if row["group_type"] == "answer_type"
            ],
            "paired_correctness": paired_correctness_statistics(
                cases,
                bootstrap_seed=args.bootstrap_seed,
                bootstrap_samples=args.bootstrap_samples,
            ),
            "efficiency": efficiency_statistics(cases),
            "notes": list(NOTES),
        }
        if args.statistics_output:
            args.statistics_output.parent.mkdir(parents=True, exist_ok=True)
            args.statistics_output.write_text(
                json.dumps(statistics, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(format_summary(summaries))
    print(f"\naggregate_output={args.output}")
    if args.cases_output:
        print(f"cases_output={args.cases_output}")
    if args.answer_type_output:
        print(f"answer_type_output={args.answer_type_output}")
    if args.paired_output:
        print(f"paired_output={args.paired_output}")
    if args.statistics_output:
        print(f"statistics_output={args.statistics_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
