# Phase 2B Stage 1 strong alternative-page repeat confirmation

Seven selected strong-case repeat confirmations, no population recovery rate

Original 14-case adjudication is immutable; automatic judge is diagnostic only. Repeat source correctness is independently assessed against established source facts and the original source PDF page. P2B_S1_006 remains UNCLEAR and was not repeated.

| ID | Query | PDF | Page | Automatic correctness | Repeat source correct | Stability |
|---|---|---|---:|---|---|---|
| P2B_S1_001 | unidoc_commerce_manufacturing_0002 | 6109033 | 7 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_003 | unidoc_commerce_manufacturing_0035 | 6532134 | 6 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_009 | unidoc_crm_0151 | 4898395 | 28 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_013 | unidoc_energy_0111 | 2497225 | 3 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_015 | unidoc_energy_0193 | 1511581 | 4 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_022 | unidoc_legal_0061 | 2502771 | 5 | True | YES | STABLE_STRONG_RECOVERY |
| P2B_S1_023 | unidoc_legal_0086 | 3134069 | 2 | True | YES | STABLE_STRONG_RECOVERY |

## Source-check evidence

- P2B_S1_001 (physical pages 7): Repeat answer is text-identical to the source-adjudicated first alternative answer. The original 6109033.pdf physical page 7 Figure 5 was visually inspected: A. Borsig advertisement and machinery illustrations support industrial equipment, steam engines/boilers and locomotive-related machinery. The requested product identification remains supported; original adjudication is unchanged.
- P2B_S1_003 (physical pages 6): Original 6532134.pdf physical page 6 Figure 3 was visually inspected. Its Shipping panel lists DHL uninsured 3.90 euros, Hermes 4.00 euros, DHL insured 6.00 euros, and DHL parcel station 3.50 euros. All four repeated option/cost pairs are exact; minor paraphrasing relative to the first answer does not change the requested facts. The selected parcel-station radio button is also visible.
- P2B_S1_009 (physical pages 28): Repeat answer is text-identical to the adjudicated first alternative answer. Original 4898395.pdf physical page 28 was visually inspected: the lower-left certification mark explicitly reads INVESTOR IN PEOPLE. The repeated named certification is correct.
- P2B_S1_013 (physical pages 3): Original 2497225.pdf physical page 3 Figure 1 was visually inspected. The Spar Platform contains permanent ballast at the bottom and Variable ballast directly above it; the repeat preserves the established section/type/order facts. Its ancillary dashed-line description was also present in the adjudicated original answer; the visible dashed blue line is above the ballast regions rather than their interface and is not needed for the requested placement relation. Core source correctness is retained, with this minor visual-description limitation explicitly recorded; original adjudication is not altered.
- P2B_S1_015 (physical pages 4): Original 1511581.pdf physical page 4 was visually inspected. The photograph shows an individual manually operating a mechanical press/tool with dark cylindrical briquettes collected in a tray amid outdoor vegetation. The repeated answer describes localized labor-intensive briquette fabrication and community productive-energy activity, consistent with the established source fact, photograph and supplied Top-3 context; paraphrasing does not reverse the process.
- P2B_S1_022 (physical pages 5): Repeat answer is text-identical to the source-adjudicated first alternative answer. Original 2502771.pdf physical page 5 was visually inspected: the footer award badge says EGR Operator Virtual Awards 2020, Winner, Safer gambling operator of the year, Entain. The repeated accolade is correct; omission of Virtual in the event wording does not change its identity.
- P2B_S1_023 (physical pages 2): Repeat answer is text-identical to the adjudicated first alternative answer. Original 3134069.pdf physical page 2 Figure 2 was visually inspected: Snapchat is 35 percent and YouTube 32 percent in the use-most-often column. The repeated 35-percent preference claim and comparison remain source-supported.

## Calls and provenance

| Stage | Attempted | Transport succeeded | Failed |
|---|---:|---:|---:|
| VLM | 7 | 7 | 0 |
| FINAL | 7 | 7 | 0 |
| JUDGE | 7 | 7 | 0 |

All repeat VLM request bodies and rendered image hashes match first-run alternatives; original question/ordered Top-3 and model/configuration remain fixed. No retrieval, reranking, rank1, text control or another page was run. New raw receipts and state checkpoints are append-only under alt_repeat_provenance/.

STABLE IDs: ['P2B_S1_001', 'P2B_S1_003', 'P2B_S1_009', 'P2B_S1_013', 'P2B_S1_015', 'P2B_S1_022', 'P2B_S1_023']
UNSTABLE IDs: []
UNCLEAR IDs: []
Stable distinct PDFs: 7; pre-specified >=2 distinct-PDF GO criterion satisfied: True.
This reports the gate only. Stage 2 was NOT started. No population recovery rate is calculated.
Protected original files unchanged: 14427; includes source-verification CSV, original pilot outputs, design audit, Phase 2A and RESEARCH_NOTES.
