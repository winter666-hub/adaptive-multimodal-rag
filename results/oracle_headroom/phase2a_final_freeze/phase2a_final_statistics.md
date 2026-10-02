# Phase 2A final human-adjudication diagnostic statistics

Diagnostic statistics within the selected human-review packet only. No population prevalence, overall Top-3/6/9 answer accuracy, or population recovery rate over the 224 Phase 2A queries is estimated.

Frozen input: `results/oracle_headroom/phase2a_final_freeze/phase2a_human_annotations_frozen.csv`
SHA-256 verified: `683b0a9cb42a5a487bfff2b0b5b1210fa761ab7857ff0d4f2ae060646e0d7e71`

Rows analyzed: 77. Primary reference-valid attribution denominator: 70 selected cases.
No paper conclusions or Phase 2B GO/STOP decision are made.

## Reference validity

| Label | Count | IDs (NO/UNCLEAR) |
|---|---:|---|
| YES | 70 | See JSON |
| NO | 4 | P2A_HR_012, P2A_HR_023, P2A_HR_060, P2A_HR_075 |
| UNCLEAR | 3 | P2A_HR_052, P2A_HR_054, P2A_HR_056 |

Reference NO and UNCLEAR remain in the general inventories below, but are excluded from primary causal attribution.

## Human inventories

| Selected-packet diagnostic | YES | NO | UNCLEAR |
|---|---:|---:|---:|
| E3 | 21 | 56 | 0 |
| E6 | 58 | 19 | 0 |
| E9 | 59 | 18 | 0 |
| E3_evidence_sufficient | 27 | 50 | 0 |
| E6_added_evidence_useful | 56 | 21 | 0 |
| E9_added_evidence_useful | 3 | 74 | 0 |

These are selected-packet counts, not overall Top-3/6/9 answer accuracies.

| Confidence | Count |
|---|---:|
| HIGH | 60 |
| MEDIUM | 17 |
| LOW | 0 |

## Human transitions

| Pair | Transition | Count | IDs |
|---|---|---:|---|
| E3_to_E6 | RAW_RECOVERY | 41 | P2A_HR_001, P2A_HR_002, P2A_HR_003, P2A_HR_004, P2A_HR_005, P2A_HR_006, P2A_HR_007, P2A_HR_008, P2A_HR_010, P2A_HR_011, P2A_HR_013, P2A_HR_014, P2A_HR_015, P2A_HR_018, P2A_HR_019, P2A_HR_021, P2A_HR_022, P2A_HR_023, P2A_HR_024, P2A_HR_025, P2A_HR_026, P2A_HR_027, P2A_HR_028, P2A_HR_029, P2A_HR_030, P2A_HR_033, P2A_HR_034, P2A_HR_036, P2A_HR_037, P2A_HR_038, P2A_HR_040, P2A_HR_041, P2A_HR_042, P2A_HR_043, P2A_HR_044, P2A_HR_045, P2A_HR_049, P2A_HR_060, P2A_HR_066, P2A_HR_075, P2A_HR_077 |
| E3_to_E6 | DEGRADATION | 4 | P2A_HR_047, P2A_HR_051, P2A_HR_053, P2A_HR_057 |
| E3_to_E6 | SUSTAINED_INCORRECT | 15 | P2A_HR_009, P2A_HR_012, P2A_HR_031, P2A_HR_039, P2A_HR_046, P2A_HR_052, P2A_HR_054, P2A_HR_056, P2A_HR_061, P2A_HR_063, P2A_HR_065, P2A_HR_070, P2A_HR_071, P2A_HR_072, P2A_HR_074 |
| E3_to_E6 | SUSTAINED_CORRECT | 17 | P2A_HR_016, P2A_HR_017, P2A_HR_020, P2A_HR_032, P2A_HR_035, P2A_HR_048, P2A_HR_050, P2A_HR_055, P2A_HR_058, P2A_HR_059, P2A_HR_062, P2A_HR_064, P2A_HR_067, P2A_HR_068, P2A_HR_069, P2A_HR_073, P2A_HR_076 |
| E3_to_E6 | UNRESOLVED_HUMAN_LABEL | 0 | NONE |
| E6_to_E9 | RAW_RECOVERY | 4 | P2A_HR_046, P2A_HR_047, P2A_HR_061, P2A_HR_063 |
| E6_to_E9 | DEGRADATION | 3 | P2A_HR_005, P2A_HR_034, P2A_HR_041 |
| E6_to_E9 | SUSTAINED_INCORRECT | 15 | P2A_HR_009, P2A_HR_012, P2A_HR_031, P2A_HR_039, P2A_HR_051, P2A_HR_052, P2A_HR_053, P2A_HR_054, P2A_HR_056, P2A_HR_057, P2A_HR_065, P2A_HR_070, P2A_HR_071, P2A_HR_072, P2A_HR_074 |
| E6_to_E9 | SUSTAINED_CORRECT | 55 | P2A_HR_001, P2A_HR_002, P2A_HR_003, P2A_HR_004, P2A_HR_006, P2A_HR_007, P2A_HR_008, P2A_HR_010, P2A_HR_011, P2A_HR_013, P2A_HR_014, P2A_HR_015, P2A_HR_016, P2A_HR_017, P2A_HR_018, P2A_HR_019, P2A_HR_020, P2A_HR_021, P2A_HR_022, P2A_HR_023, P2A_HR_024, P2A_HR_025, P2A_HR_026, P2A_HR_027, P2A_HR_028, P2A_HR_029, P2A_HR_030, P2A_HR_032, P2A_HR_033, P2A_HR_035, P2A_HR_036, P2A_HR_037, P2A_HR_038, P2A_HR_040, P2A_HR_042, P2A_HR_043, P2A_HR_044, P2A_HR_045, P2A_HR_048, P2A_HR_049, P2A_HR_050, P2A_HR_055, P2A_HR_058, P2A_HR_059, P2A_HR_060, P2A_HR_062, P2A_HR_064, P2A_HR_066, P2A_HR_067, P2A_HR_068, P2A_HR_069, P2A_HR_073, P2A_HR_075, P2A_HR_076, P2A_HR_077 |
| E6_to_E9 | UNRESOLVED_HUMAN_LABEL | 0 | NONE |

Among the selected 77 cases sent for human review, 41 exhibited a human-adjudicated E3-to-E6 wrong-to-correct transition. This is a diagnostic count within the selected packet, not a Top-6 population recovery rate.

## E3-to-E6 causal taxonomy

| Category | Count | IDs |
|---|---:|---|
| STRONG_EVIDENCE_ATTRIBUTABLE | 34 | P2A_HR_001, P2A_HR_002, P2A_HR_003, P2A_HR_004, P2A_HR_005, P2A_HR_006, P2A_HR_007, P2A_HR_008, P2A_HR_011, P2A_HR_013, P2A_HR_014, P2A_HR_015, P2A_HR_018, P2A_HR_019, P2A_HR_021, P2A_HR_022, P2A_HR_024, P2A_HR_025, P2A_HR_026, P2A_HR_028, P2A_HR_029, P2A_HR_030, P2A_HR_033, P2A_HR_034, P2A_HR_036, P2A_HR_037, P2A_HR_038, P2A_HR_041, P2A_HR_042, P2A_HR_043, P2A_HR_044, P2A_HR_049, P2A_HR_066, P2A_HR_077 |
| GENERATION_REASONING_RECOVERY | 1 | P2A_HR_045 |
| MIXED_OR_AMBIGUOUS | 3 | P2A_HR_010, P2A_HR_027, P2A_HR_040 |
| REFERENCE_INVALID_OR_UNCLEAR_RECOVERY | 3 | P2A_HR_023, P2A_HR_060, P2A_HR_075 |

Definitions are applied only to raw E3=NO/E6=YES transitions:

- STRONG_EVIDENCE_ATTRIBUTABLE: reference YES, E3 sufficient NO, E6 useful YES.
- GENERATION_REASONING_RECOVERY: reference YES, E3 sufficient YES, E6 useful NO.
- MIXED_OR_AMBIGUOUS: other reference-YES raw recoveries.
- REFERENCE_INVALID_OR_UNCLEAR_RECOVERY: reference NO/UNCLEAR raw recoveries; excluded from primary attribution.

Strong counts are within the reference-valid selected reviewed cases; they are not estimates over the 224-query population.

## E6-to-E9 recovery diagnostics

| Reference stratum | Diagnostic | Count | IDs |
|---|---|---:|---|
| all_selected | raw_recovery | 4 | P2A_HR_046, P2A_HR_047, P2A_HR_061, P2A_HR_063 |
| all_selected | E9_added_evidence_useful_YES | 1 | P2A_HR_046 |
| all_selected | NON_EVIDENCE_ATTRIBUTABLE_RECOVERY | 3 | P2A_HR_047, P2A_HR_061, P2A_HR_063 |
| all_selected | E9_added_evidence_useful_UNCLEAR | 0 | NONE |
| reference_valid_primary | raw_recovery | 4 | P2A_HR_046, P2A_HR_047, P2A_HR_061, P2A_HR_063 |
| reference_valid_primary | E9_added_evidence_useful_YES | 1 | P2A_HR_046 |
| reference_valid_primary | NON_EVIDENCE_ATTRIBUTABLE_RECOVERY | 3 | P2A_HR_047, P2A_HR_061, P2A_HR_063 |
| reference_valid_primary | E9_added_evidence_useful_UNCLEAR | 0 | NONE |
| reference_NO | raw_recovery | 0 | NONE |
| reference_NO | E9_added_evidence_useful_YES | 0 | NONE |
| reference_NO | NON_EVIDENCE_ATTRIBUTABLE_RECOVERY | 0 | NONE |
| reference_NO | E9_added_evidence_useful_UNCLEAR | 0 | NONE |
| reference_UNCLEAR | raw_recovery | 0 | NONE |
| reference_UNCLEAR | E9_added_evidence_useful_YES | 0 | NONE |
| reference_UNCLEAR | NON_EVIDENCE_ATTRIBUTABLE_RECOVERY | 0 | NONE |
| reference_UNCLEAR | E9_added_evidence_useful_UNCLEAR | 0 | NONE |

The frozen schema has no `human_e6_evidence_sufficient`. No general strong evidence-attributable E6-to-E9 recovery rate is claimed. E9 useful=YES alone does not show that E6 lacked necessary evidence. E9 useful=NO is described as NON_EVIDENCE_ATTRIBUTABLE_RECOVERY and is not automatically called pure generation/reasoning recovery.

HR046 must NOT be described as a strong evidence-attributable E6-to-E9 recovery. The audit finding says E6 evidence was already sufficient, and is provenance rather than an official frozen field.

Cautious interpretation for individual E6-to-E9 recoveries:

- P2A_HR_046: reference=YES, E9 useful=YES. No official human_e6_evidence_sufficient field; no general strong E6-to-E9 attribution claim is supported. Useful E9 additions alone do not establish that necessary evidence was missing at E6. HR046 must NOT be described as a strong evidence-attributable E6-to-E9 recovery. The audit finding says E6 evidence was already sufficient, and is provenance rather than an official frozen field.
- P2A_HR_047: reference=YES, E9 useful=NO. No official human_e6_evidence_sufficient field; no general strong E6-to-E9 attribution claim is supported. Use NON_EVIDENCE_ATTRIBUTABLE_RECOVERY; do not automatically label this pure generation/reasoning recovery.
- P2A_HR_061: reference=YES, E9 useful=NO. No official human_e6_evidence_sufficient field; no general strong E6-to-E9 attribution claim is supported. Use NON_EVIDENCE_ATTRIBUTABLE_RECOVERY; do not automatically label this pure generation/reasoning recovery.
- P2A_HR_063: reference=YES, E9 useful=NO. No official human_e6_evidence_sufficient field; no general strong E6-to-E9 attribution claim is supported. Use NON_EVIDENCE_ATTRIBUTABLE_RECOVERY; do not automatically label this pure generation/reasoning recovery.

## Other logically interesting combinations

| Combination | Count | IDs |
|---|---:|---|
| E3_wrong_and_E3_sufficient_YES | 7 | P2A_HR_010, P2A_HR_027, P2A_HR_031, P2A_HR_040, P2A_HR_045, P2A_HR_046, P2A_HR_075 |
| E3_correct_and_E3_sufficient_NO | 1 | P2A_HR_053 |
| E3_correct_to_E6_wrong | 4 | P2A_HR_047, P2A_HR_051, P2A_HR_053, P2A_HR_057 |
| E6_correct_to_E9_wrong | 3 | P2A_HR_005, P2A_HR_034, P2A_HR_041 |

## Reference-stratified diagnostics

| Reference | Cases | E3 YES/NO/UNCLEAR | E6 YES/NO/UNCLEAR | E9 YES/NO/UNCLEAR | E3-to-E6 recovery/degradation | E6-to-E9 recovery/degradation |
|---|---:|---|---|---|---|---|
| YES | 70 | 21 / 49 / 0 | 55 / 15 / 0 | 56 / 14 / 0 | 38 / 4 | 4 / 3 |
| NO | 4 | 0 / 4 / 0 | 3 / 1 / 0 | 3 / 1 / 0 | 3 / 0 | 0 / 0 |
| UNCLEAR | 3 | 0 / 3 / 0 | 0 / 3 / 0 | 0 / 3 / 0 | 0 / 0 | 0 / 0 |

## Automatic judge quality diagnostic

JUDGE_ERROR_UNAVAILABLE is unavailable, never False. Agreement does not establish annotation validity.

| Stage | Comparable | Agreements | Disagreements | Unavailable | False-to-YES | True-to-NO | Available auto / human UNCLEAR |
|---|---:|---:|---:|---:|---:|---:|---:|
| E3 | 73 | 59 | 14 | 4 | 11 | 3 | 0 |
| E6 | 74 | 55 | 19 | 3 | 13 | 6 | 0 |
| E9 | 74 | 54 | 20 | 3 | 11 | 9 | 0 |

Correction IDs and unavailable final human labels:

- E3 False-to-YES: P2A_HR_016, P2A_HR_017, P2A_HR_020, P2A_HR_032, P2A_HR_035, P2A_HR_047, P2A_HR_048, P2A_HR_062, P2A_HR_064, P2A_HR_067, P2A_HR_068
- E3 True-to-NO: P2A_HR_052, P2A_HR_054, P2A_HR_056
- E3 unavailable: P2A_HR_071=NO, P2A_HR_073=YES, P2A_HR_076=YES, P2A_HR_077=NO
- E6 False-to-YES: P2A_HR_050, P2A_HR_055, P2A_HR_058, P2A_HR_059, P2A_HR_060, P2A_HR_062, P2A_HR_064, P2A_HR_066, P2A_HR_067, P2A_HR_068, P2A_HR_073, P2A_HR_075, P2A_HR_076
- E6 True-to-NO: P2A_HR_009, P2A_HR_012, P2A_HR_031, P2A_HR_039, P2A_HR_046, P2A_HR_047
- E6 unavailable: P2A_HR_070=NO, P2A_HR_072=NO, P2A_HR_074=NO
- E9 False-to-YES: P2A_HR_016, P2A_HR_017, P2A_HR_020, P2A_HR_023, P2A_HR_025, P2A_HR_035, P2A_HR_046, P2A_HR_050, P2A_HR_055, P2A_HR_069, P2A_HR_073
- E9 True-to-NO: P2A_HR_005, P2A_HR_009, P2A_HR_012, P2A_HR_034, P2A_HR_039, P2A_HR_052, P2A_HR_056, P2A_HR_065, P2A_HR_070
- E9 unavailable: P2A_HR_074=NO, P2A_HR_075=YES, P2A_HR_076=YES

## Integrity and reproducibility

The script validates hash, schema, enums, complete human fields and unique review/query IDs before computing any statistics. It recomputes every transition from human labels and reads no other annotation file. JSON contains IDs for every group. No hard-coded outcome counts or provisional judgments are used. Both reports are deterministic for the same frozen bytes, input path and configured hash.

Frozen CSV, official batch CSVs and RESEARCH_NOTES are not modified. No Phase 2B work is performed.
