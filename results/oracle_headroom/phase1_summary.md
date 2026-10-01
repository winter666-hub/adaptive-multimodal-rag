# Oracle Headroom Phase 1 — partial / blocked



## A. Reproducibility and actual ACK configuration

Fresh Top-3 reproduction: NOT RUN. Saved-artifact consistency only:

```json

{
  "forced_text": {
    "rank1_chunk": 1600,
    "rank1_page": 1600,
    "ordered_top3_chunks": 1600,
    "ordered_top3_pages": 1600,
    "top3_page_set": 1600,
    "mismatch_query_count": 0,
    "mismatch_samples": []
  },
  "forced_vision": {
    "rank1_chunk": 1600,
    "rank1_page": 1600,
    "ordered_top3_chunks": 1600,
    "ordered_top3_pages": 1600,
    "top3_page_set": 1600,
    "mismatch_query_count": 0,
    "mismatch_samples": []
  },
  "retrieval_aware": {
    "rank1_chunk": 1600,
    "rank1_page": 1600,
    "ordered_top3_chunks": 1600,
    "ordered_top3_pages": 1600,
    "top3_page_set": 1600,
    "mismatch_query_count": 0,
    "mismatch_samples": []
  }
}

```

All 4,800 FT/FV/RA checkpoint records have initial retrieval top_k=3 and rerank top_n=3.

The generic RagConfig default top_k=10 does not describe this experiment; benchmark CLI defaults and persisted fingerprints are 3.

Therefore the unchanged candidate pool cannot provide reranked Top-9. No Top-9 file is produced, and no deeper cohort is imputed.

Query set: benchmarks/unidoc.jsonl, 1,600 unique original queries, 1,028 per-row source PDFs under datasets/unidoc; no corpus-wide retrieval.

Query/source_pdf fields and GT page sets agree across all three checkpoints and analysis_master. analysis_master sorts GT pages: 129 list-order differences, zero GT-set differences. Two historical dataset byte hashes occur (machine migration); current bytes match the later RA hash. Query semantics checked row by row.

Models: furiosa-ai/Qwen3-Embedding-8B; furiosa-ai/Qwen3-Reranker-8B.

Preprocessing: local PDFs; pypdf extract_text + strip, PyMuPDF get_text('text') fallback, no OCR. Existing cached chunks are reused for this artifact audit; no PDFs are re-extracted.

Chunking: page-preserving 700 whitespace words, overlap 100 words; oversized inputs split at 7,600 UTF-8 bytes. Empty pages produce no chunks.

Retrieval: original question embedding, NumPy float32 cosine similarity, np.argsort(scores)[::-1], top_k=3, no deduplication.

Reranking: /rerank payload model/query/documents/top_n=3; no temperature or other inference parameters set by client. No reranker call in this audit.

Top-k convention: ranked chunks, duplicate source pages retained; unique chunk IDs, no page/text deduplication. One query has only two saved chunks.

Source pages: one-based physical PDF pages (enumeration starts at 1). GT: numeric dataset *_page_N image filename indices copied without +/-1 offset into expected_pages. ACK compares these directly; this audit preserves that mapping, without claiming every GT annotation is correct.

All saved Top-3 texts match their document caches. All 1,600 referenced PDFs and cache paths exist; PDF bytes were not rehashed against the school manifest in this audit.

Evidence: src/furiosa_rag/cli/benchmark_e2e.py, pipeline.py, retrieval.py, reranker.py, document.py, chunking.py; scripts/prepare_unidoc.py; persisted checkpoint fingerprint_config.

## B. Retrieval depth structural reachability

Page-any-hit within ranked chunks: Hit@1=1115/1600 (69.6875%); Hit@3=1376/1600 (86.0000%). Matches historical 69.6875% / 86.0%.

Hit@6, Hit@9 and incremental recovery: UNMEASURED. MISS_AT_3_HIT_AT_6, MISS_AT_6_HIT_AT_9, MISS_AT_9 counts: UNMEASURED.

HIT_AT_3=1376; MISS_AT_3_DEPTH_UNMEASURED=224; HIT_AT_3_NOT_AT_1=261 (overlapping flag).

Unique-page-depth Hit@3/6/9: not computed, because only 2–3 chunks were saved; this is not a ranking of 3/6/9 unique pages. Unique-page Hit@1 equals chunk Hit@1.

## C. Text evidence volume proxy

Mean Top-3 raw chunk characters: 7344.7944. Sum of chunk lengths; overlap and duplicate pages retained, headers/separators excluded.

Top-3→6 and Top-6→9 character/token deltas: UNMEASURED. Tokens: not computed; no model tokenizer loaded. These are context-size proxies, not monetary inference cost.

The baseline reranks only three retrieved candidates. Deeper evidence here would require expanding the initial retrieval candidate count and reranking the larger pool; it cannot be described as merely exposing already ranked evidence.

## D. Visual escalation candidates (existing outcomes only)

```json

{
  "GT_IN_TOP3_NOT_TOP1": 261,
  "AND_FORCED_VISION_INCORRECT": 161,
  "AND_RA_INCORRECT": 157,
  "AND_BOTH_INCORRECT": 144,
  "AND_RA_TEXT_ONLY": 120
}

```

Correctness columns are existing answer-judge labels from analysis_master, not route-correctness flags. No new judge or inference ran. FALSE labels are counted explicitly; missing values are not interpreted as incorrect.

## E. Human-review qualitative consistency

All 72 reviewed rows joined; 49 valid YES cases used for the following tables. NO/UNCLEAR remain in the output but are excluded from valid counts.

Log-state-balanced diagnostic sample: no population prevalence inference.

| Valid human action | Available depth cohort | Count |

| --- | --- | ---: |

| ANSWER_CURRENT_EVIDENCE | HIT_AT_3 | 3 |

| ANSWER_CURRENT_EVIDENCE | MISS_AT_3_DEPTH_UNMEASURED | 1 |

| INSPECT_CURRENT_PAGE_VISUALLY | HIT_AT_3 | 4 |

| INSPECT_OTHER_RETRIEVED_PAGE | HIT_AT_3 | 9 |

| REASON_OR_GENERATE_BETTER | HIT_AT_3 | 10 |

| RETRIEVE_MORE | HIT_AT_3 | 6 |

| RETRIEVE_MORE | MISS_AT_3_DEPTH_UNMEASURED | 16 |



| Valid primary failure | Available depth cohort | Count |

| --- | --- | ---: |

| REASONING_GENERATION | HIT_AT_3 | 10 |

| RETRIEVAL | HIT_AT_3 | 6 |

| RETRIEVAL | MISS_AT_3_DEPTH_UNMEASURED | 16 |

| ROUTING | HIT_AT_3 | 10 |

| ROUTING | MISS_AT_3_DEPTH_UNMEASURED | 1 |

| VISUAL_PAGE_SELECTION | HIT_AT_3 | 6 |



Human action/depth disagreement is not automatically annotation error: GT pages may omit sufficient pages, and multi-page sufficiency is not measured by page-any-hit.

RETRIEVE_MORE despite HIT_AT_3: HR010, HR028, HR032, HR045, HR068, HR072. For example HR045 has GT [7,8,9,10] but human sufficient pages 8,9; hitting any GT page does not prove both required pages were acquired. HR072 has GT [8,9,10] but human sufficient page 9.

## F. Limitations / Phase 2 blockers

- GT page reachability != answer correctness.

- GT page != necessarily sufficient page.

- GT page != necessarily visual-required page.

- Top-k expansion != guaranteed quality improvement.

- Counterfactual action outcomes have not been measured; utility controller effects have not been validated.

- Resolve the requirement conflict: preserving initial candidate count 3 precludes Top-9. An explicitly revised experiment must permit a larger initial pool, retain original queries/models/preprocessing, and distinguish this intervention from exposing existing ranks.

- A larger pool can reorder Top-3. Independently compare new ranks to saved ACK evidence and apply a documented stop criterion; never force old candidates into new ranks.

- Check fresh reproduction, candidate availability (including short documents), PDF/cache provenance and tokenizer availability before full depth/cost analysis.

- Human review includes 21 NO and 2 UNCLEAR judge-validity cases; keep these separate from clean counterfactual comparisons. Human sufficient-page annotations can differ from benchmark GT.

No controller, VLM, answer generation, answer judge, query rewrite, or new retrieval method was executed.

No claim of accuracy improvement, baseline superiority, controller necessity, or novelty is supported by this partial audit.
