# Phase 2A Final Freeze integrity report

Status: PASS. Canonical freeze completed; final statistics have NOT been executed.
Created (Asia/Seoul): 2026-10-02T17:47:47+09:00

Scope: the selected 77-case human-review packet. This freeze is not a population result for all 224 Phase 2A queries.

## Frozen artifact

- File: `results/oracle_headroom/phase2a_final_freeze/phase2a_human_annotations_frozen.csv`
- SHA-256: `683b0a9cb42a5a487bfff2b0b5b1210fa761ab7857ff0d4f2ae060646e0d7e71`
- Rows / unique review IDs / unique query IDs: 77 / 77 / 77
- Deterministic order: ascending `review_id`, P2A_HR_001 through P2A_HR_077.
- Schema: 53 original columns, including all 9 human fields; all source cells preserved.
- Serialization: UTF-8 with BOM, comma delimiter, LF line terminator, minimal CSV quoting.
- Independent reconstruction from the seven source batches matches frozen bytes exactly.

## Integrity checks

| Check | Result |
|---|---|
| Batch schemas and index/PDF-manifest coverage | PASS |
| Duplicate review_id / query_id | 0 / 0 |
| Human-field blanks / invalid enums | 0 / 0 |
| Human annotation changes | 0 |
| Non-human cell changes | 0 |
| Protected file changes | 0 of 1432 |
| Protected file set | Unchanged |
| Deterministic reconstruction and CSV round-trip | PASS |
| Final statistics executed | NO |

## Preserved adjudication and provenance

- P2A_HR_052, P2A_HR_054 and P2A_HR_056 remain `human_reference_valid=UNCLEAR` without re-adjudication.
- Future primary reference-valid evidence-attribution analysis includes only frozen rows with `human_reference_valid == 'YES'`. The three UNCLEAR cases are excluded; reference-NO cases also fail that predicate.
- P2A_HR_077 direct visual verification is completed: `2332258.pdf`, physical/source page 12, actual Figure 7.
- Participants: First Responder, Investigator, Other, Prosecution, Defense, Court.
- Use cases: Collect, Authenticate, Examine, Analyse, Report.
- HR077 confirmed existing annotations remain E3=NO, E6=YES, reference=YES, E3 sufficient=NO, E6 useful=YES. The user-confirmed `STRONG_EVIDENCE_ATTRIBUTABLE` classification is retained in audit provenance.
- No official batch CSV was modified. The annotation change log explicitly records `changes=0`.
- The provenance entry records completed prior PDF verification and the user confirmation; no new case review was performed.

## Source hashes

| Source | SHA-256 |
|---|---|
| `results/oracle_headroom/human_review_phase2a/review_batch_01.csv` | `5c5d39b8611a2a5dd9802bde0d98b2fd1f6684c9558b19f30a33f5736a70cbdd` |
| `results/oracle_headroom/human_review_phase2a/review_batch_02.csv` | `ec04df2061d856099d7c74f080772450b40b763022f5b12bdf5d3481e18622bb` |
| `results/oracle_headroom/human_review_phase2a/review_batch_03.csv` | `3e11b07081b15be3a27c041b14d96ce979bfa68063e923eed36720f0092c5a42` |
| `results/oracle_headroom/human_review_phase2a/review_batch_04.csv` | `aebbf4f904a4d9d1599ab690cae9401d0ade80a59fa94629008731ddb5f5c636` |
| `results/oracle_headroom/human_review_phase2a/review_batch_05.csv` | `974df9554ef8de3e9256ec50295d69e3a44e3f654616803547516e82e7600d55` |
| `results/oracle_headroom/human_review_phase2a/review_batch_06.csv` | `e25f7af2527c447f40fa910e6df413491bd0c7ba806fffa8f71305ad8cb68ed7` |
| `results/oracle_headroom/human_review_phase2a/review_batch_07.csv` | `08f65ce8fb80fa6d111618823877702a01f2bdf2613e749b1120b8c69286c0a3` |

The manifest records the frozen artifact hash, every batch input hash, audit/coverage source hashes, and the protected-file hash ledger.

## Statistics gate

Stop here. No accuracies, transition totals, attribution totals, recovery rates or other final research statistics were calculated or saved.
A subsequent explicitly requested statistics run must verify the manifest SHA-256 and calculate its results only from the canonical frozen CSV. It must not use handwritten totals or hard-coded outcome counts.
`docs/RESEARCH_NOTES.md`, official batch CSV/Markdown, other research outputs, scripts/tests and blind-review artifacts remain unchanged. No git add, commit or push was executed.
