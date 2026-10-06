# Phase 2B Stage 1 pilot automatic diagnostic summary

Selected 24-query diagnostic pilot only; no population recovery rate

Source verification is PENDING. Automatic-positive cases are not confirmed recoveries. No GO/STOP decision or expansion is made.

| API stage | Attempted | Transport succeeded | Transport failed |
|---|---:|---:|---:|
| VLM | 96 | 96 | 0 |
| FINAL | 120 | 120 | 0 |
| JUDGE | 126 | 126 | 0 |

Candidate pages: 72; PDFs: 24.
Fresh rank1 wrong: 21; automatic-positive queries / review queue: 14.

## Automatic-positive cases

- P2B_S1_001 / unidoc_commerce_manufacturing_0002: rank1 page 8 wrong; automatic-correct alternatives [7]; rank1 repeat correct=False.
- P2B_S1_003 / unidoc_commerce_manufacturing_0035: rank1 page 2 wrong; automatic-correct alternatives [6]; rank1 repeat correct=False.
- P2B_S1_006 / unidoc_construction_0189: rank1 page 4 wrong; automatic-correct alternatives [5]; rank1 repeat correct=False.
- P2B_S1_009 / unidoc_crm_0151: rank1 page 26 wrong; automatic-correct alternatives [28]; rank1 repeat correct=False.
- P2B_S1_010 / unidoc_education_0043: rank1 page 1 wrong; automatic-correct alternatives [3]; rank1 repeat correct=False.
- P2B_S1_012 / unidoc_education_0163: rank1 page 13 wrong; automatic-correct alternatives [11]; rank1 repeat correct=False.
- P2B_S1_013 / unidoc_energy_0111: rank1 page 1 wrong; automatic-correct alternatives [3]; rank1 repeat correct=False.
- P2B_S1_014 / unidoc_energy_0150: rank1 page 7 wrong; automatic-correct alternatives [8]; rank1 repeat correct=False.
- P2B_S1_015 / unidoc_energy_0193: rank1 page 6 wrong; automatic-correct alternatives [4]; rank1 repeat correct=False.
- P2B_S1_017 / unidoc_finance_0108: rank1 page 16 wrong; automatic-correct alternatives [19]; rank1 repeat correct=False.
- P2B_S1_021 / unidoc_healthcare_0138: rank1 page 5 wrong; automatic-correct alternatives [4]; rank1 repeat correct=False.
- P2B_S1_022 / unidoc_legal_0061: rank1 page 4 wrong; automatic-correct alternatives [5]; rank1 repeat correct=False.
- P2B_S1_023 / unidoc_legal_0086: rank1 page 6 wrong; automatic-correct alternatives [2]; rank1 repeat correct=False.
- P2B_S1_024 / unidoc_legal_0154: rank1 page 5 wrong; automatic-correct alternatives [6]; rank1 repeat correct=False.

## Controls

| Comparison | Comparable | Label agreements | Label changes | Unavailable | Answer changes |
|---|---:|---:|---:|---:|---:|
| historical_FV_vs_fresh_rank1 | 23 | 21 | 2 | 1 | 12 |
| fresh_rank1_vs_repeat | 23 | 23 | 0 | 1 | 7 |
| historical_FT_vs_fresh_TEXT_ONLY | 22 | 22 | 0 | 2 | 8 |

## Selected fixed sample

| Review ID | Query ID | PDF ID | Candidate pages |
|---|---|---|---|
| P2B_S1_001 | unidoc_commerce_manufacturing_0002 | 6109033 | [8, 6, 7] |
| P2B_S1_002 | unidoc_commerce_manufacturing_0031 | 2762994 | [18, 17, 16] |
| P2B_S1_003 | unidoc_commerce_manufacturing_0035 | 6532134 | [2, 3, 6] |
| P2B_S1_004 | unidoc_construction_0068 | 4228468 | [8, 7, 6] |
| P2B_S1_005 | unidoc_construction_0161 | 3147496 | [15, 14, 17] |
| P2B_S1_006 | unidoc_construction_0189 | 5254543 | [4, 5, 6] |
| P2B_S1_007 | unidoc_crm_0041 | 7453427 | [2, 1, 4] |
| P2B_S1_008 | unidoc_crm_0131 | 3492507 | [7, 5, 1] |
| P2B_S1_009 | unidoc_crm_0151 | 4898395 | [26, 13, 28] |
| P2B_S1_010 | unidoc_education_0043 | 0931214 | [1, 4, 3] |
| P2B_S1_011 | unidoc_education_0119 | 2352105 | [14, 12, 2] |
| P2B_S1_012 | unidoc_education_0163 | 7909471 | [13, 14, 11] |
| P2B_S1_013 | unidoc_energy_0111 | 2497225 | [1, 3, 2] |
| P2B_S1_014 | unidoc_energy_0150 | 3629103 | [7, 8, 2] |
| P2B_S1_015 | unidoc_energy_0193 | 1511581 | [6, 5, 4] |
| P2B_S1_016 | unidoc_finance_0049 | 6941304 | [21, 19, 18] |
| P2B_S1_017 | unidoc_finance_0108 | 5703797 | [16, 19, 14] |
| P2B_S1_018 | unidoc_finance_0129 | 0070518 | [1, 2, 3] |
| P2B_S1_019 | unidoc_healthcare_0101 | 5825109 | [5, 6, 3] |
| P2B_S1_020 | unidoc_healthcare_0108 | 0779019 | [1, 5, 4] |
| P2B_S1_021 | unidoc_healthcare_0138 | 2520596 | [5, 4, 3] |
| P2B_S1_022 | unidoc_legal_0061 | 2502771 | [4, 5, 3] |
| P2B_S1_023 | unidoc_legal_0086 | 3134069 | [6, 2, 1] |
| P2B_S1_024 | unidoc_legal_0154 | 3274043 | [5, 6, 11] |

Manifest and original question/chunks/order/PDF were frozen before inference. No retrieval or reranking occurred. Every raw request/response attempt is retained in raw_calls.jsonl, with full response, reconstructed image reference/hash and parameters; per-state checkpoints are append-only.
Visual failures retain the explicit ACK text fallback while remaining unavailable for visual-event analysis. Judge failures remain unavailable, not incorrect.
Source-verification fields are blank. Relevant source PDFs and rendered candidate pages accompany the queue; no speculative adjudication was performed.
Protected file hashes unchanged: 14342. Phase 2A, human annotations, design audit and RESEARCH_NOTES untouched. No Stage 1B/Stage 2/router/population recovery rate.
