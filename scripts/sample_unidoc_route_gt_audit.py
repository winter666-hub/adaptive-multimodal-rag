"""Create a deterministic answer-type/domain-stratified UniDoc audit subset."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.route_gt_audit import (
    AUDIT_SAMPLE_METHOD,
    stratified_audit_sample,
    write_audit_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("benchmarks/unidoc.jsonl"))
    parser.add_argument(
        "--output", type=Path, default=Path("benchmarks/unidoc_route_gt_audit_160.jsonl")
    )
    parser.add_argument("--per-answer-type", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = stratified_audit_sample(
        load_benchmark_jsonl(args.input),
        per_answer_type=args.per_answer_type,
        seed=args.seed,
    )
    write_audit_jsonl(rows, args.output)
    print(f"sampling_method={AUDIT_SAMPLE_METHOD}")
    print(f"sampling_seed={args.seed}")
    print(f"total={len(rows)}")
    for (answer_type, domain), count in sorted(
        Counter((row["answer_type"], row["domain"]) for row in rows).items()
    ):
        print(f"answer_type={answer_type} domain={domain} count={count}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
