# Phase 1B — Expanded Retrieval Oracle

## A. Setup
Original retrieval top_k=3, reranker top_n=3. Expanded requested top_k=30 and top_n=30; all actual retrieved candidates are reranked and retained (at least Top-20 when 20 chunks exist).
Target: only the 224 original GT-MISS@3 queries, 181 documents. The 1,376 initial-hit queries are not expanded; their cumulative reachability remains true, while their E6/E9 text sizes are not estimated.
Same original questions, per-row corpus, one-based physical PDF source pages, dataset expected_pages mapping (no offset), page-any-hit criterion, cached pypdf/PyMuPDF text and 700-word/100-overlap chunking, UTF-8 byte split, Qwen3-Embedding-8B, float32 NumPy cosine retriever, Qwen3-Reranker-8B /rerank scoring. No rewriting, OCR, alternative search, or page deduplication.
Verified all target PDF hashes through original cache keys; reused existing embeddings/chunks without rebuilding. API models verified before run. No API top_k/top_n fallback was applied.
E3 is original saved ACK evidence in original order. Remove original chunk IDs from expanded pool, stable-sort remaining candidates by descending rerank score, append NEW1..3 for E6 and NEW1..6 for E9. New reranked Top-3 is not substituted for E3 and differences are not reproduction failures.
Short documents: 119/224 have fewer than 20 chunks and 204/224 have fewer than 30 chunks, so actual search count=min(30, document chunks), as in the existing retriever. E6 incomplete=7; E9 incomplete=25. No padding/repeated chunks. Cumulative states are nested (subset-or-equal); strict E3⊂E6⊂E9 is impossible for 12 exhausted documents. E6/E9 are capacity labels where incomplete. Counts remain page-any-hit over all available cumulative chunks.
## B. Structural recovery
| State | Count / 1600 | Percent |
| --- | ---: | ---: |
| Original Hit@3 | 1376 | 86.0000% |
| Cumulative Hit@6 | 1549 | 96.8125% |
| Cumulative Hit@9 | 1572 | 98.2500% |

```json
{
  "HIT_AT_3": 1376,
  "MISS_AT_3_HIT_AT_6": 173,
  "MISS_AT_9": 28,
  "MISS_AT_6_HIT_AT_9": 23
}
```
## C. First recovery NEW rank
```json
{
  "NEW1": 128,
  "NEW2": 28,
  "NEW3": 17,
  "NEW4": 7,
  "NEW5": 7,
  "NEW6": 9,
  "BEYOND_NEW6": 20,
  "NOT_RECOVERED_IN_EXPANDED_POOL": 8
}
```
First recovery is searched across all retained new candidates, not only NEW1..6. BEYOND_NEW6 differs from not recovered anywhere in the expanded pool.
## D. Unique-page diversity / E. Cost proxies
Means below use the 224 expanded targets only. Duplicate pages and overlapping text are retained; character counts sum raw chunk text and exclude prompt headers/separators.
```json
{
  "query_embedding_latency_ms": 22867.51576998504,
  "retrieval_latency_ms": 5.792113394168804,
  "reranking_latency_ms": 15271.115934818226,
  "expanded_candidate_count": 18.232142857142858,
  "reranker_pair_count": 18.232142857142858,
  "E3_text_chars": 7872.785714285715,
  "E6_text_chars": 15778.794642857143,
  "E9_text_chars": 23031.723214285714,
  "delta_chars_3_to_6": 7906.008928571428,
  "delta_chars_6_to_9": 7252.928571428572,
  "original_top3_unique_pages": 2.888392857142857,
  "E6_unique_pages": 5.691964285714286,
  "E9_unique_pages": 8.209821428571429
}
```
No unique-page increase counts (including candidate exhaustion):
```json
{
  "E3_to_E6": 0,
  "E6_to_E9": 15
}
```
Of the no-page-increase cases, actual added chunks but no new pages: E3->E6=0; E6->E9=3. The other 12 E6->E9 cases have no remaining chunks to add.
Completed retrieval candidates / reranker pairs total=4084. Each completed target has one query vector and one retained expanded reranker result. The first 20 queries used individual embedding requests; remaining queries used 7 requests with batch sizes [32, 32, 32, 32, 32, 32, 12]. Same original strings and embedding scoring model, same API; only transport batching changed. A checkpoint-preserving restart interrupted some in-flight requests, so these completed-work totals exclude discarded calls and must not be interpreted as total billed work.
Latency columns are observed perf_counter durations in milliseconds with up to 3 simultaneous reranker requests. Individual query embedding, batch embedding, local retrieval, and API reranking are measured separately. Batch duration is repeated as batch metadata per query and must not be summed or treated as per-query time. The mean individual embedding latency above only covers individual requests. They include concurrency/service/network variation; they are not stable isolated benchmark latency or monetary costs. No fabricated latency or baseline latency subtraction. Local transformers/tokenizers packages and cached Qwen tokenizer files are unavailable; tokens and token deltas remain blank.
## F. Human RETRIEVE_MORE cross-check
72 human-review rows joined read-only. Count only human_judge_valid=YES and human_best_next_action=RETRIEVE_MORE (22 cases). This balanced diagnostic sample cannot estimate population prevalence.
```json
{
  "HIT_AT_3": 6,
  "MISS_AT_3_HIT_AT_6": 14,
  "MISS_AT_9": 2
}
```
The 6 HIT_AT_3 cases are explicitly retained as INITIAL_HIT / not expanded, because human need for more evidence is not equivalent to GT-page absence. The remaining 16 cases have measured expansion cohorts.
| Sample | Query | Cohort | First recovery NEW rank | Human sufficient pages |
| --- | --- | --- | ---: | --- |
| HR010 | unidoc_healthcare_0052 | HIT_AT_3 |  |  |
| HR013 | unidoc_commerce_manufacturing_0120 | MISS_AT_3_HIT_AT_6 | 1 |  |
| HR014 | unidoc_construction_0114 | MISS_AT_3_HIT_AT_6 | 2 | 5 |
| HR015 | unidoc_construction_0187 | MISS_AT_9 |  | 23 |
| HR017 | unidoc_education_0082 | MISS_AT_3_HIT_AT_6 | 1 | 13 |
| HR019 | unidoc_energy_0016 | MISS_AT_3_HIT_AT_6 | 1 | 4 |
| HR020 | unidoc_energy_0060 | MISS_AT_3_HIT_AT_6 | 1 | 8 |
| HR021 | unidoc_finance_0021 | MISS_AT_9 | 7 | 13 |
| HR022 | unidoc_finance_0183 | MISS_AT_3_HIT_AT_6 | 1 | 17,18 |
| HR024 | unidoc_legal_0160 | MISS_AT_3_HIT_AT_6 | 1 | 3 |
| HR028 | unidoc_construction_0108 | HIT_AT_3 |  |  |
| HR032 | unidoc_energy_0078 | HIT_AT_3 |  |  |
| HR045 | unidoc_finance_0150 | HIT_AT_3 |  | 8,9 |
| HR049 | unidoc_commerce_manufacturing_0032 | MISS_AT_3_HIT_AT_6 | 1 | 7 |
| HR050 | unidoc_commerce_manufacturing_0051 | MISS_AT_3_HIT_AT_6 | 1 | 3 |
| HR051 | unidoc_construction_0015 | MISS_AT_3_HIT_AT_6 | 1 | 2 |
| HR052 | unidoc_construction_0075 | MISS_AT_3_HIT_AT_6 | 1 | 14 |
| HR054 | unidoc_education_0034 | MISS_AT_3_HIT_AT_6 | 1 | 16 |
| HR057 | unidoc_finance_0090 | MISS_AT_3_HIT_AT_6 | 1 | 28 |
| HR058 | unidoc_healthcare_0008 | MISS_AT_3_HIT_AT_6 | 1 | 5 |
| HR068 | unidoc_finance_0018 | HIT_AT_3 |  | 4 |
| HR072 | unidoc_legal_0168 | HIT_AT_3 |  | 9 |

## G. Limitations and next-phase blockers
- GT page recovery != answer recovery.
- GT page != necessarily sufficient evidence.
- Human RETRIEVE_MORE label != necessarily GT-page absence.
- Larger candidate pool may improve reachability without improving final QA.
- Answer-level utility must be measured separately by future counterfactual inference.
- No VLM inference, final answer generation, answer judge, controller, or novelty/baseline-superiority claim.
- Full-population expansion cost cannot be estimated from only original misses; 1,376 initial-hit queries were not expanded.
- Short-document exhaustion precludes literal 6/9-chunk states in some cases; later inference must use recorded actual context counts.
- Stable compute cost needs a controlled latency benchmark; exact token cost needs a verified model tokenizer.
- Phase 2 must specify fixed-E3 action inputs, success evaluation, sufficient-page/multi-page and judge-validity handling before measuring answer-level utility.
All protected ACK/error-analysis/review inputs and RESEARCH_NOTES.md verified unchanged by SHA-256. Phase 1 files were not overwritten.
