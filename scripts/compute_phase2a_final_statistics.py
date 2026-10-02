"""Recompute diagnostic statistics using only the hash-verified frozen CSV.

This script never reads batch annotations, provisional reviews, or stored
automatic transition flags. Its two output files are deterministic. Audit-only
context supplied by the user is reported separately and never changes a metric.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Callable


EXPECTED_SHA256 = "683b0a9cb42a5a487bfff2b0b5b1210fa761ab7857ff0d4f2ae060646e0d7e71"
LABELS = ("YES", "NO", "UNCLEAR")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
STAGES = ("E3", "E6", "E9")
CORRECTNESS = {stage: f"human_{stage.lower()}_correct" for stage in STAGES}
EVIDENCE = {
    "E3_evidence_sufficient": "human_e3_evidence_sufficient",
    "E6_added_evidence_useful": "human_e6_added_evidence_useful",
    "E9_added_evidence_useful": "human_e9_added_evidence_useful",
}
SCOPE = (
    "Diagnostic statistics within the selected human-review packet only. "
    "No population prevalence, overall Top-3/6/9 answer accuracy, or "
    "population recovery rate over the 224 Phase 2A queries is estimated."
)
# User-provided provenance, not a frozen human annotation or a metric input.
AUDIT_CONTEXT = {
    "P2A_HR_046": {
        "audit_e6_evidence_sufficient": "YES",
        "source": "User-provided audit-only finding in the statistics request",
        "official_frozen_annotation_field": False,
        "affects_computed_metrics": False,
        "interpretation": (
            "HR046 must NOT be described as a strong evidence-attributable "
            "E6-to-E9 recovery. The audit finding says E6 evidence was already "
            "sufficient, and is provenance rather than an official frozen field."
        ),
    }
}
Row = dict[str, str]


def group(rows: list[Row], predicate: Callable[[Row], bool]) -> dict:
    ids = sorted(row["review_id"] for row in rows if predicate(row))
    return {"count": len(ids), "review_ids": ids}


def inventory(rows: list[Row], field: str, labels=LABELS) -> dict:
    return {label: group(rows, lambda row, v=label: row[field] == v) for label in labels}


def transition(rows: list[Row], start: str, end: str) -> dict:
    a, b = CORRECTNESS[start], CORRECTNESS[end]
    definitions = {
        "RAW_RECOVERY": ("NO", "YES"),
        "DEGRADATION": ("YES", "NO"),
        "SUSTAINED_INCORRECT": ("NO", "NO"),
        "SUSTAINED_CORRECT": ("YES", "YES"),
    }
    result = {
        name: group(rows, lambda r, pair=pair: (r[a], r[b]) == pair)
        for name, pair in definitions.items()
    }
    result["UNRESOLVED_HUMAN_LABEL"] = group(
        rows, lambda r: "UNCLEAR" in (r[a], r[b])
    )
    assert sum(value["count"] for value in result.values()) == len(rows)
    return result


def validate(rows: list[Row], columns: list[str]) -> None:
    if not rows:
        raise ValueError("Frozen CSV contains no rows")
    if len(columns) != len(set(columns)):
        raise ValueError("Duplicate CSV columns")
    categorical = [*CORRECTNESS.values(), *EVIDENCE.values(), "human_reference_valid"]
    required = [
        "review_id", "query_id", *categorical, "human_confidence", "human_notes",
        *(f"{stage}_auto_{suffix}" for stage in STAGES for suffix in ("correct", "status")),
    ]
    missing = set(required) - set(columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    for key in ("review_id", "query_id"):
        seen = set()
        for row in rows:
            if not row.get(key) or row[key] in seen:
                raise ValueError(f"Blank or duplicate {key}: {row.get(key)!r}")
            seen.add(row[key])
    for row in rows:
        rid = row["review_id"]
        if set(row) != set(columns) or any(value is None for value in row.values()):
            raise ValueError(f"Malformed CSV row: {rid}")
        for field in categorical:
            if row[field] not in LABELS:
                raise ValueError(f"Invalid enum: {rid} {field}={row[field]!r}")
        if row["human_confidence"] not in CONFIDENCE:
            raise ValueError(f"Invalid confidence: {rid}")
        if not row["human_notes"].strip():
            raise ValueError(f"Blank human_notes: {rid}")
        for stage in STAGES:
            status, value = row[f"{stage}_auto_status"], row[f"{stage}_auto_correct"]
            if status == "JUDGE_ERROR_UNAVAILABLE":
                if value not in ("", "UNAVAILABLE"):
                    raise ValueError(f"Unavailable state has a boolean label: {rid} {stage}")
            elif status != "OK" or value not in ("True", "False"):
                raise ValueError(f"Invalid automatic state: {rid} {stage}: {status!r}, {value!r}")


def e9_recovery_diagnostic(rows: list[Row]) -> dict:
    recovery = [r for r in rows if r[CORRECTNESS["E6"]] == "NO" and r[CORRECTNESS["E9"]] == "YES"]
    return {
        "raw_recovery": group(recovery, lambda r: True),
        "E9_added_evidence_useful_YES": group(recovery, lambda r: r[EVIDENCE["E9_added_evidence_useful"]] == "YES"),
        "NON_EVIDENCE_ATTRIBUTABLE_RECOVERY": group(recovery, lambda r: r[EVIDENCE["E9_added_evidence_useful"]] == "NO"),
        "E9_added_evidence_useful_UNCLEAR": group(recovery, lambda r: r[EVIDENCE["E9_added_evidence_useful"]] == "UNCLEAR"),
    }


def automatic_diagnostic(rows: list[Row], stage: str) -> dict:
    label_field, status_field = f"{stage}_auto_correct", f"{stage}_auto_status"
    human_field = CORRECTNESS[stage]
    unavailable = [r for r in rows if r[status_field] == "JUDGE_ERROR_UNAVAILABLE"]
    available = [r for r in rows if r[status_field] != "JUDGE_ERROR_UNAVAILABLE"]
    comparable = [r for r in available if r[human_field] in ("YES", "NO")]
    result = {
        "comparable_cases": group(comparable, lambda r: True),
        "agreements": group(comparable, lambda r: (r[label_field] == "True") == (r[human_field] == "YES")),
        "disagreements": group(comparable, lambda r: (r[label_field] == "True") != (r[human_field] == "YES")),
        "unavailable": group(unavailable, lambda r: True),
        "False_to_YES": group(comparable, lambda r: r[label_field] == "False" and r[human_field] == "YES"),
        "True_to_NO": group(comparable, lambda r: r[label_field] == "True" and r[human_field] == "NO"),
        "available_auto_but_human_UNCLEAR": group(available, lambda r: r[human_field] == "UNCLEAR"),
        "unavailable_final_human_labels": [
            {"review_id": r["review_id"], "human_correct": r[human_field]}
            for r in unavailable
        ],
    }
    assert result["agreements"]["count"] + result["disagreements"]["count"] == len(comparable)
    assert result["False_to_YES"]["count"] + result["True_to_NO"]["count"] == result["disagreements"]["count"]
    assert len(comparable) + len(unavailable) + result["available_auto_but_human_UNCLEAR"]["count"] == len(rows)
    return result


def compute(rows: list[Row], columns: list[str]) -> dict:
    validate(rows, columns)
    rows = sorted(rows, key=lambda r: r["review_id"])
    reference_groups = {label: [r for r in rows if r["human_reference_valid"] == label] for label in LABELS}
    primary = reference_groups["YES"]
    raw = [r for r in rows if r[CORRECTNESS["E3"]] == "NO" and r[CORRECTNESS["E6"]] == "YES"]
    strong = lambda r: r["human_reference_valid"] == "YES" and r[EVIDENCE["E3_evidence_sufficient"]] == "NO" and r[EVIDENCE["E6_added_evidence_useful"]] == "YES"
    generation = lambda r: r["human_reference_valid"] == "YES" and r[EVIDENCE["E3_evidence_sufficient"]] == "YES" and r[EVIDENCE["E6_added_evidence_useful"]] == "NO"
    taxonomy = {
        "STRONG_EVIDENCE_ATTRIBUTABLE": group(raw, strong),
        "GENERATION_REASONING_RECOVERY": group(raw, generation),
        "MIXED_OR_AMBIGUOUS": group(raw, lambda r: r["human_reference_valid"] == "YES" and not strong(r) and not generation(r)),
        "REFERENCE_INVALID_OR_UNCLEAR_RECOVERY": group(raw, lambda r: r["human_reference_valid"] in ("NO", "UNCLEAR")),
    }
    assert sum(value["count"] for value in taxonomy.values()) == len(raw)
    caution = []
    for row in rows:
        if row[CORRECTNESS["E6"]] != "NO" or row[CORRECTNESS["E9"]] != "YES":
            continue
        useful = row[EVIDENCE["E9_added_evidence_useful"]]
        reasons = ["No official human_e6_evidence_sufficient field; no general strong E6-to-E9 attribution claim is supported."]
        if useful == "NO":
            reasons.append("Use NON_EVIDENCE_ATTRIBUTABLE_RECOVERY; do not automatically label this pure generation/reasoning recovery.")
        elif useful == "YES":
            reasons.append("Useful E9 additions alone do not establish that necessary evidence was missing at E6.")
        else:
            reasons.append("E9 usefulness is unresolved.")
        if row["human_reference_valid"] != "YES":
            reasons.append("Reference is NO/UNCLEAR; exclude from primary reference-valid attribution.")
        if row["review_id"] in AUDIT_CONTEXT:
            reasons.append(AUDIT_CONTEXT[row["review_id"]]["interpretation"])
        caution.append({"review_id": row["review_id"], "reference_valid": row["human_reference_valid"], "E9_added_evidence_useful": useful, "reasons": reasons})
    interesting = {
        "E3_wrong_and_E3_sufficient_YES": group(rows, lambda r: r[CORRECTNESS["E3"]] == "NO" and r[EVIDENCE["E3_evidence_sufficient"]] == "YES"),
        "E3_correct_and_E3_sufficient_NO": group(rows, lambda r: r[CORRECTNESS["E3"]] == "YES" and r[EVIDENCE["E3_evidence_sufficient"]] == "NO"),
        "E3_correct_to_E6_wrong": transition(rows, "E3", "E6")["DEGRADATION"],
        "E6_correct_to_E9_wrong": transition(rows, "E6", "E9")["DEGRADATION"],
    }
    return {
        "schema_version": "phase2a-final-human-statistics-v1",
        "scope": SCOPE,
        "rows_analyzed": len(rows),
        "primary_reference_valid_denominator": len(primary),
        "reference_validity": inventory(rows, "human_reference_valid"),
        "human_correctness_inventories": {s: inventory(rows, CORRECTNESS[s]) for s in STAGES},
        "human_evidence_inventories": {name: inventory(rows, field) for name, field in EVIDENCE.items()},
        "confidence": inventory(rows, "human_confidence", CONFIDENCE),
        "human_transitions": {"E3_to_E6": transition(rows, "E3", "E6"), "E6_to_E9": transition(rows, "E6", "E9")},
        "E3_to_E6_causal_taxonomy": taxonomy,
        "E6_to_E9_diagnostics": {
            "all_selected": e9_recovery_diagnostic(rows),
            "reference_valid_primary": e9_recovery_diagnostic(primary),
            "reference_NO": e9_recovery_diagnostic(reference_groups["NO"]),
            "reference_UNCLEAR": e9_recovery_diagnostic(reference_groups["UNCLEAR"]),
            "strong_evidence_attributable_rate_claim_supported": False,
            "missing_official_field": "human_e6_evidence_sufficient",
            "cautious_interpretation": caution,
        },
        "reference_stratified_diagnostics": {
            label: {
                "rows": len(subset),
                "human_correctness": {s: inventory(subset, CORRECTNESS[s]) for s in STAGES},
                "E3_to_E6": transition(subset, "E3", "E6"),
                "E6_to_E9": transition(subset, "E6", "E9"),
            }
            for label, subset in reference_groups.items()
        },
        "logically_interesting_combinations": interesting,
        "automatic_judge_diagnostics": {s: automatic_diagnostic(rows, s) for s in STAGES},
        "automatic_diagnostic_interpretation": "Judge-quality diagnostic only; agreement is not proof of annotation validity.",
        "audit_only_context_not_used_for_metrics": AUDIT_CONTEXT,
        "method": {
            "annotation_truth": "Hash-verified frozen CSV only",
            "transitions": "Recomputed from human correctness; stored automatic transition flags are not used",
            "primary_reference_eligibility": "human_reference_valid == YES",
            "NO_UNCLEAR_retention": "Retained in general inventory and separate reference-stratified diagnostics",
            "UNCLEAR_correctness": "Never coerced to incorrect; reported as unresolved where applicable",
            "hard_coded_result_counts": False,
        },
    }


def ids(value: dict) -> str:
    return ", ".join(value["review_ids"]) or "NONE"


def render_markdown(result: dict) -> str:
    src = result["input"]
    lines = [
        "# Phase 2A final human-adjudication diagnostic statistics", "", SCOPE, "",
        f"Frozen input: `{src['path']}`",
        f"SHA-256 verified: `{src['sha256']}`", "",
        f"Rows analyzed: {result['rows_analyzed']}. Primary reference-valid attribution denominator: {result['primary_reference_valid_denominator']} selected cases.",
        "No paper conclusions or Phase 2B GO/STOP decision are made.", "",
        "## Reference validity", "", "| Label | Count | IDs (NO/UNCLEAR) |", "|---|---:|---|",
    ]
    for label, value in result["reference_validity"].items():
        lines.append(f"| {label} | {value['count']} | {ids(value) if label != 'YES' else 'See JSON'} |")
    lines += ["", "Reference NO and UNCLEAR remain in the general inventories below, but are excluded from primary causal attribution.", "", "## Human inventories", "", "| Selected-packet diagnostic | YES | NO | UNCLEAR |", "|---|---:|---:|---:|"]
    for name, values in {**result["human_correctness_inventories"], **result["human_evidence_inventories"]}.items():
        lines.append(f"| {name} | " + " | ".join(str(values[v]["count"]) for v in LABELS) + " |")
    lines += ["", "These are selected-packet counts, not overall Top-3/6/9 answer accuracies.", "", "| Confidence | Count |", "|---|---:|"]
    lines.extend(f"| {k} | {v['count']} |" for k, v in result["confidence"].items())
    lines += ["", "## Human transitions", "", "| Pair | Transition | Count | IDs |", "|---|---|---:|---|"]
    for pair, values in result["human_transitions"].items():
        for name, value in values.items():
            lines.append(f"| {pair} | {name} | {value['count']} | {ids(value)} |")
    raw = result["human_transitions"]["E3_to_E6"]["RAW_RECOVERY"]["count"]
    lines += ["", f"Among the selected {result['rows_analyzed']} cases sent for human review, {raw} exhibited a human-adjudicated E3-to-E6 wrong-to-correct transition. This is a diagnostic count within the selected packet, not a Top-6 population recovery rate.", "", "## E3-to-E6 causal taxonomy", "", "| Category | Count | IDs |", "|---|---:|---|"]
    lines.extend(f"| {name} | {value['count']} | {ids(value)} |" for name, value in result["E3_to_E6_causal_taxonomy"].items())
    lines += ["", "Definitions are applied only to raw E3=NO/E6=YES transitions:", "", "- STRONG_EVIDENCE_ATTRIBUTABLE: reference YES, E3 sufficient NO, E6 useful YES.", "- GENERATION_REASONING_RECOVERY: reference YES, E3 sufficient YES, E6 useful NO.", "- MIXED_OR_AMBIGUOUS: other reference-YES raw recoveries.", "- REFERENCE_INVALID_OR_UNCLEAR_RECOVERY: reference NO/UNCLEAR raw recoveries; excluded from primary attribution.", "", "Strong counts are within the reference-valid selected reviewed cases; they are not estimates over the 224-query population.", "", "## E6-to-E9 recovery diagnostics", "", "| Reference stratum | Diagnostic | Count | IDs |", "|---|---|---:|---|"]
    e9 = result["E6_to_E9_diagnostics"]
    for stratum in ("all_selected", "reference_valid_primary", "reference_NO", "reference_UNCLEAR"):
        for name, value in e9[stratum].items():
            lines.append(f"| {stratum} | {name} | {value['count']} | {ids(value)} |")
    lines += ["", "The frozen schema has no `human_e6_evidence_sufficient`. No general strong evidence-attributable E6-to-E9 recovery rate is claimed. E9 useful=YES alone does not show that E6 lacked necessary evidence. E9 useful=NO is described as NON_EVIDENCE_ATTRIBUTABLE_RECOVERY and is not automatically called pure generation/reasoning recovery.", "", AUDIT_CONTEXT["P2A_HR_046"]["interpretation"], "", "Cautious interpretation for individual E6-to-E9 recoveries:", ""]
    for case in e9["cautious_interpretation"]:
        lines.append(f"- {case['review_id']}: reference={case['reference_valid']}, E9 useful={case['E9_added_evidence_useful']}. " + " ".join(case["reasons"]))
    if not e9["cautious_interpretation"]:
        lines.append("NONE")
    lines += ["", "## Other logically interesting combinations", "", "| Combination | Count | IDs |", "|---|---:|---|"]
    lines.extend(f"| {name} | {value['count']} | {ids(value)} |" for name, value in result["logically_interesting_combinations"].items())
    lines += ["", "## Reference-stratified diagnostics", "", "| Reference | Cases | E3 YES/NO/UNCLEAR | E6 YES/NO/UNCLEAR | E9 YES/NO/UNCLEAR | E3-to-E6 recovery/degradation | E6-to-E9 recovery/degradation |", "|---|---:|---|---|---|---|---|"]
    for label, diag in result["reference_stratified_diagnostics"].items():
        parts = [" / ".join(str(diag["human_correctness"][s][v]["count"]) for v in LABELS) for s in STAGES]
        pairs = [f"{diag[p]['RAW_RECOVERY']['count']} / {diag[p]['DEGRADATION']['count']}" for p in ("E3_to_E6", "E6_to_E9")]
        lines.append(f"| {label} | {diag['rows']} | " + " | ".join([*parts, *pairs]) + " |")
    lines += ["", "## Automatic judge quality diagnostic", "", "JUDGE_ERROR_UNAVAILABLE is unavailable, never False. Agreement does not establish annotation validity.", "", "| Stage | Comparable | Agreements | Disagreements | Unavailable | False-to-YES | True-to-NO | Available auto / human UNCLEAR |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    keys = ("comparable_cases", "agreements", "disagreements", "unavailable", "False_to_YES", "True_to_NO", "available_auto_but_human_UNCLEAR")
    for stage, diag in result["automatic_judge_diagnostics"].items():
        lines.append(f"| {stage} | " + " | ".join(str(diag[k]["count"]) for k in keys) + " |")
    lines += ["", "Correction IDs and unavailable final human labels:", ""]
    for stage, diag in result["automatic_judge_diagnostics"].items():
        lines += [f"- {stage} False-to-YES: {ids(diag['False_to_YES'])}", f"- {stage} True-to-NO: {ids(diag['True_to_NO'])}", f"- {stage} unavailable: " + (", ".join(f"{v['review_id']}={v['human_correct']}" for v in diag['unavailable_final_human_labels']) or "NONE")]
    lines += ["", "## Integrity and reproducibility", "", "The script validates hash, schema, enums, complete human fields and unique review/query IDs before computing any statistics. It recomputes every transition from human labels and reads no other annotation file. JSON contains IDs for every group. No hard-coded outcome counts or provisional judgments are used. Both reports are deterministic for the same frozen bytes, input path and configured hash.", "", "Frozen CSV, official batch CSVs and RESEARCH_NOTES are not modified. No Phase 2B work is performed.", ""]
    return "\n".join(lines)


def run(frozen_csv: Path, expected_sha256: str, output_dir: Path) -> dict:
    data = frozen_csv.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    print(f"Frozen input SHA-256: {digest}")
    if digest != expected_sha256.lower():
        raise ValueError(f"STOP: frozen hash mismatch; expected {expected_sha256}, actual {digest}")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    rows = list(reader)
    result = compute(rows, reader.fieldnames or [])
    result["input"] = {"path": frozen_csv.as_posix(), "sha256": digest, "hash_verified": True}
    result["validation"] = {"duplicate_review_ids": 0, "duplicate_query_ids": 0, "human_field_blanks": 0, "invalid_enums": 0}
    json_data = (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    md_data = render_markdown(result).encode("utf-8")
    targets = [output_dir / "phase2a_final_statistics.json", output_dir / "phase2a_final_statistics.md"]
    for target in targets:
        if target.resolve() == frozen_csv.resolve():
            raise ValueError("Output path would replace the frozen source")
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")
    if frozen_csv.read_bytes() != data:
        raise ValueError("Frozen input changed during computation")
    output_dir.mkdir(parents=True, exist_ok=True)
    for target, payload in zip(targets, [json_data, md_data]):
        with target.open("xb") as stream:
            stream.write(payload)
    if frozen_csv.read_bytes() != data:
        raise ValueError("Frozen input changed after report writes")
    print(f"Verified {len(rows)} selected cases; final diagnostic reports written to {output_dir}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frozen_csv", type=Path, help="Only annotation input; SHA-256 checked before parsing")
    parser.add_argument("--expected-sha256", default=EXPECTED_SHA256)
    parser.add_argument("--output-dir", type=Path, help="Defaults to the frozen CSV's directory; refuses output overwrite")
    args = parser.parse_args()
    try:
        run(args.frozen_csv, args.expected_sha256, args.output_dir or args.frozen_csv.parent)
    except (ValueError, FileExistsError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
