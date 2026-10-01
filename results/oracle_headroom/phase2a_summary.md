# Phase 2A — Text Expansion Counterfactual Evaluation

## Setup / E3 reuse decision
Evaluation status=PARTIAL_JUDGE_ERRORS; primary target=224; complete paired judgments=212. If incomplete, statistics below use only complete paired cases and do not represent all 224. Missing judges are never counted as incorrect. Strict parser failures require resolution before full-cohort kill-criteria calculations.
E3 answers regenerated. Original FT query, original chunk order, and final model match, but historical generation prompt hash and full decoding config were not recorded; exact equality cannot be certified.
All states use existing TextRagPipeline._text_context / _answer_prompt / clean_internal_citations and FuriosaLlm.generate. Model=furiosa-ai/Qwen3-32B-FP8; one user message with embedded SYSTEM INSTRUCTION, no separate system role; temperature=0, max_tokens=1024, enable_thinking=False, no other decoding parameters explicitly set. Per-request prompts and source code hashes are in phase2a_run_manifest.json.
Judge is the unchanged UniDoc audit judge, not the Transformer-specific default judge. Existing judge prompt hash verified against all original records; model=Qwen3-32B-FP8, temperature=0, max_tokens=512, thinking=False, correctness>=3. Same parser and up-to-two format retries. Judge is blinded to state/cohort/latency and receives only question/reference/candidate.
Only saved Phase 1B expansion and original ACK chunks used. E3 subset-or-equal E6 subset-or-equal E9 verified; short documents remain unpadded. Each available state is generated and judged independently, even if E6=E9; numerical/service nondeterminism or judge variability can create transitions with identical inputs.
No retrieval/reranking, query rewriting, controller, or vision inference ran. Tokens not computed: local tokenizer libraries/cache unavailable.
## Primary population: 224 original GT-MISS@3 queries
Automatic judge results are first-pass paired evaluations, not ground truth and not full UniDoc-Bench accuracy.
Per-state coverage (not exact full-224 accuracy when labels are missing). Bounds assume every unjudged state incorrect versus correct; they are bounds on this automatic evaluation, not ground-truth answer accuracy:
```json
{
  "E3": {
    "judged_n": 219,
    "known_correct": 55,
    "unavailable_n": 5,
    "full224_accuracy_lower_bound_percent": 24.553571428571427,
    "full224_accuracy_upper_bound_percent": 26.785714285714285
  },
  "E6": {
    "judged_n": 218,
    "known_correct": 94,
    "unavailable_n": 6,
    "full224_accuracy_lower_bound_percent": 41.964285714285715,
    "full224_accuracy_upper_bound_percent": 44.642857142857146
  },
  "E9": {
    "judged_n": 219,
    "known_correct": 98,
    "unavailable_n": 5,
    "full224_accuracy_lower_bound_percent": 43.75,
    "full224_accuracy_upper_bound_percent": 45.982142857142854
  }
}
```
```json
{
  "n": 212,
  "correct_counts": {
    "E3": 54,
    "E6": 92,
    "E9": 95
  },
  "accuracy_percent": {
    "E3": 25.471698113207548,
    "E6": 43.39622641509434,
    "E9": 44.81132075471698
  },
  "transitions": {
    "E3_to_E6": {
      "WRONG_TO_CORRECT": 47,
      "CORRECT_TO_WRONG": 9,
      "WRONG_TO_WRONG": 111,
      "CORRECT_TO_CORRECT": 45
    },
    "E6_to_E9": {
      "WRONG_TO_CORRECT": 13,
      "CORRECT_TO_WRONG": 10,
      "WRONG_TO_WRONG": 107,
      "CORRECT_TO_CORRECT": 82
    },
    "E3_to_E9": {
      "WRONG_TO_CORRECT": 47,
      "CORRECT_TO_WRONG": 6,
      "WRONG_TO_WRONG": 111,
      "CORRECT_TO_CORRECT": 48
    }
  },
  "absolute_pp_E6_minus_E3": 17.92452830188679,
  "absolute_pp_E9_minus_E3": 19.339622641509433,
  "marginal_pp_E9_minus_E6": 1.4150943396226414,
  "E6_recovery_rate_among_E3_errors_percent": 29.746835443037973,
  "E9_recovery_rate_among_E6_errors_percent": 10.833333333333334
}
```
## Retrieval cohorts
```json
{
  "MISS_AT_3_HIT_AT_6": {
    "n": 163,
    "correct_counts": {
      "E3": 48,
      "E6": 81,
      "E9": 84
    },
    "accuracy_percent": {
      "E3": 29.447852760736197,
      "E6": 49.693251533742334,
      "E9": 51.533742331288344
    },
    "transitions": {
      "E3_to_E6": {
        "WRONG_TO_CORRECT": 40,
        "CORRECT_TO_WRONG": 7,
        "WRONG_TO_WRONG": 75,
        "CORRECT_TO_CORRECT": 41
      },
      "E6_to_E9": {
        "WRONG_TO_CORRECT": 9,
        "CORRECT_TO_WRONG": 6,
        "WRONG_TO_WRONG": 73,
        "CORRECT_TO_CORRECT": 75
      },
      "E3_to_E9": {
        "WRONG_TO_CORRECT": 42,
        "CORRECT_TO_WRONG": 6,
        "WRONG_TO_WRONG": 73,
        "CORRECT_TO_CORRECT": 42
      }
    },
    "absolute_pp_E6_minus_E3": 20.245398773006134,
    "absolute_pp_E9_minus_E3": 22.085889570552148,
    "marginal_pp_E9_minus_E6": 1.8404907975460123,
    "E6_recovery_rate_among_E3_errors_percent": 34.78260869565217,
    "E9_recovery_rate_among_E6_errors_percent": 10.975609756097562
  },
  "MISS_AT_6_HIT_AT_9": {
    "n": 22,
    "correct_counts": {
      "E3": 1,
      "E6": 6,
      "E9": 6
    },
    "accuracy_percent": {
      "E3": 4.545454545454546,
      "E6": 27.272727272727273,
      "E9": 27.272727272727273
    },
    "transitions": {
      "E3_to_E6": {
        "WRONG_TO_CORRECT": 6,
        "CORRECT_TO_WRONG": 1,
        "WRONG_TO_WRONG": 15,
        "CORRECT_TO_CORRECT": 0
      },
      "E6_to_E9": {
        "WRONG_TO_CORRECT": 3,
        "CORRECT_TO_WRONG": 3,
        "WRONG_TO_WRONG": 13,
        "CORRECT_TO_CORRECT": 3
      },
      "E3_to_E9": {
        "WRONG_TO_CORRECT": 5,
        "CORRECT_TO_WRONG": 0,
        "WRONG_TO_WRONG": 16,
        "CORRECT_TO_CORRECT": 1
      }
    },
    "absolute_pp_E6_minus_E3": 22.727272727272727,
    "absolute_pp_E9_minus_E3": 22.727272727272727,
    "marginal_pp_E9_minus_E6": 0.0,
    "E6_recovery_rate_among_E3_errors_percent": 28.571428571428573,
    "E9_recovery_rate_among_E6_errors_percent": 18.75
  },
  "MISS_AT_9": {
    "n": 27,
    "correct_counts": {
      "E3": 5,
      "E6": 5,
      "E9": 5
    },
    "accuracy_percent": {
      "E3": 18.51851851851852,
      "E6": 18.51851851851852,
      "E9": 18.51851851851852
    },
    "transitions": {
      "E3_to_E6": {
        "WRONG_TO_CORRECT": 1,
        "CORRECT_TO_WRONG": 1,
        "WRONG_TO_WRONG": 21,
        "CORRECT_TO_CORRECT": 4
      },
      "E6_to_E9": {
        "WRONG_TO_CORRECT": 1,
        "CORRECT_TO_WRONG": 1,
        "WRONG_TO_WRONG": 21,
        "CORRECT_TO_CORRECT": 4
      },
      "E3_to_E9": {
        "WRONG_TO_CORRECT": 0,
        "CORRECT_TO_WRONG": 0,
        "WRONG_TO_WRONG": 22,
        "CORRECT_TO_CORRECT": 5
      }
    },
    "absolute_pp_E6_minus_E3": 0.0,
    "absolute_pp_E9_minus_E3": 0.0,
    "marginal_pp_E9_minus_E6": 0.0,
    "E6_recovery_rate_among_E3_errors_percent": 4.545454545454546,
    "E9_recovery_rate_among_E6_errors_percent": 4.545454545454546
  }
}
```
## Human diagnostic subset
Primary available-pair transitions (separate from complete-three-state paired subset; missing judge excluded, not incorrect):
```json
{
  "E3_to_E6": {
    "n": 215,
    "counts": {
      "WRONG_TO_CORRECT": 48,
      "CORRECT_TO_WRONG": 9,
      "WRONG_TO_WRONG": 113,
      "CORRECT_TO_CORRECT": 45
    },
    "net_change_count": 39
  },
  "E6_to_E9": {
    "n": 214,
    "counts": {
      "WRONG_TO_CORRECT": 13,
      "CORRECT_TO_WRONG": 10,
      "WRONG_TO_WRONG": 108,
      "CORRECT_TO_CORRECT": 83
    },
    "net_change_count": 3
  },
  "E3_to_E9": {
    "n": 215,
    "counts": {
      "WRONG_TO_CORRECT": 48,
      "CORRECT_TO_WRONG": 7,
      "WRONG_TO_WRONG": 112,
      "CORRECT_TO_CORRECT": 48
    },
    "net_change_count": 41
  }
}
```
22 valid RETRIEVE_MORE cases reported separately, never used to estimate population prevalence. The 16 original misses overlap primary; the additional 6 initial-hit cases have E3 only. Their E6/E9 evidence was never saved in Phase 1B; this stage forbids retrieval reruns. Those 12 state rows remain explicitly UNAVAILABLE, with no fabricated answer/correctness.
```json
{
  "miss16": {
    "n": 15,
    "correct_counts": {
      "E3": 1,
      "E6": 8,
      "E9": 7
    },
    "accuracy_percent": {
      "E3": 6.666666666666667,
      "E6": 53.333333333333336,
      "E9": 46.666666666666664
    },
    "transitions": {
      "E3_to_E6": {
        "WRONG_TO_CORRECT": 7,
        "CORRECT_TO_WRONG": 0,
        "WRONG_TO_WRONG": 7,
        "CORRECT_TO_CORRECT": 1
      },
      "E6_to_E9": {
        "WRONG_TO_CORRECT": 0,
        "CORRECT_TO_WRONG": 1,
        "WRONG_TO_WRONG": 7,
        "CORRECT_TO_CORRECT": 7
      },
      "E3_to_E9": {
        "WRONG_TO_CORRECT": 6,
        "CORRECT_TO_WRONG": 0,
        "WRONG_TO_WRONG": 8,
        "CORRECT_TO_CORRECT": 1
      }
    },
    "absolute_pp_E6_minus_E3": 46.666666666666664,
    "absolute_pp_E9_minus_E3": 40.0,
    "marginal_pp_E9_minus_E6": -6.666666666666667,
    "E6_recovery_rate_among_E3_errors_percent": 50.0,
    "E9_recovery_rate_among_E6_errors_percent": 0.0
  },
  "initial_hit6": {
    "n": 6,
    "E3_judged_n": 6,
    "E3_correct_count": 0,
    "E6_E9": "UNAVAILABLE"
  }
}
```
## Human verification queue / anomalies
Queue size=67 unique queries with any E3->E6 or E6->E9 automatic binary-label change. No human verdict is filled automatically.
Identical-input transitions (potential generation/judge variability):
```json
[
  {
    "query_id": "unidoc_construction_0015",
    "pair": "E6_to_E9"
  }
]
```
## Context and latency observations
Primary-224 averages. Evidence characters sum raw chunk text; input characters include instructions/question/source headers. Tokens are blank, never estimated from characters. Generation latency is observed per-call wall time in ms, not isolated benchmark or monetary cost.
```json
{
  "E3": {
    "actual_chunk_count": 3.0,
    "actual_unique_page_count": 2.888392857142857,
    "input_character_count": 9139.30357142857,
    "evidence_character_count": 7872.785714285715,
    "generation_latency_ms": 3562.6390325898165
  },
  "E6": {
    "actual_chunk_count": 5.955357142857143,
    "actual_unique_page_count": 5.691964285714286,
    "input_character_count": 17168.647321428572,
    "evidence_character_count": 15778.794642857143,
    "generation_latency_ms": 4516.867742859176
  },
  "E9": {
    "actual_chunk_count": 8.727678571428571,
    "actual_unique_page_count": 8.209821428571429,
    "input_character_count": 24537.34375,
    "evidence_character_count": 23031.723214285714,
    "generation_latency_ms": 4898.642443751409
  }
}
```
Up to 4 concurrent generation/judge workers, shuffled state request order (seed 42). Phase 1B candidates/pairs and observed retrieval/reranking timings remain in retrieval_expansion_costs.csv, separately from these generation timings. Its 15.27s reranking average with three concurrent requests is preliminary, not a final latency claim.
## Limitations / next decisions
- GT page recovery != answer recovery; GT page does not guarantee sufficient evidence.
- Automatic judgment may be wrong; all changed labels need human verification. No human labels were inferred.
- Primary accuracy is conditional on original GT-MISS@3, not the 1,600-query population.
- Human balanced diagnostic sample cannot estimate population prevalence.
- Six initial-hit diagnostic cases lack expansion evidence; resolve saved evidence availability separately without silently rerunning retrieval.
- Same-input state changes are not attributable to added evidence; review before kill-criteria decisions.
- Exact tokenizer and controlled compute timings remain needed for final cost claims.
- No conclusion about controller effectiveness, full-benchmark superiority, adaptive routing, novelty, or kill/continue decision is made.
All original ACK, human review, research notes, and consumed Phase 1B inputs verified unchanged by SHA-256.
