"""Analyze existing UniDoc audit CSVs offline; no inference or judge calls."""

from __future__ import annotations

import argparse
from pathlib import Path

from furiosa_rag.route_gt_audit import write_csv
from furiosa_rag.route_utility import (
    build_utility_cases,
    format_summary,
    load_csv,
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
    args = parser.parse_args(argv)
    inputs = [args.alignment, args.retrieval_aware, args.retrieval_aware_judged]
    outputs = [args.output] + ([args.cases_output] if args.cases_output else [])
    resolved = [path.resolve() for path in outputs]
    if len(set(resolved)) != len(resolved) or set(resolved) & {p.resolve() for p in inputs}:
        parser.error("output paths must be distinct and must not overwrite input files")
    try:
        cases = build_utility_cases(*(load_csv(path) for path in inputs))
        summaries = summarize_utility(cases)
        write_csv(summaries, args.output)
        if args.cases_output:
            write_csv(cases, args.cases_output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(format_summary(summaries))
    print(f"\naggregate_output={args.output}")
    if args.cases_output:
        print(f"cases_output={args.cases_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
