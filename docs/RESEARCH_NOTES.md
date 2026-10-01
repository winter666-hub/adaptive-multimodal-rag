# Adaptive Multimodal RAG — Research Notes

**Owner:** Sangpil Han  
**Repository target:** `adaptive-multimodal-rag/docs/RESEARCH_NOTES.md`  
**Last updated:** 2026-09-30  
**Status:** 72-case post-ACK human review complete; follow-up direction comparison pending  
**Rule:** 이 문서는 연구의 **single source of truth**로 사용한다. AI 채팅의 아이디어보다 이 문서의 `FACT / INTERPRETATION / HYPOTHESIS / DECISION / TODO` 구분을 우선한다.

---

## 0. Current Research State

### Current Stage

- ACK 2026 학부생 논문 제출 완료.
- 현재 단계는 **post-submission failure analysis 완료 후 후속 연구 방향 비교**.
- 후속 연구 주제는 아직 확정하지 않음.
- 새로운 model inference, Top-k Vision, multi-page inference, iterative retrieval, Agent loop 실험은 현재 보류.
- 기존 1,600-query 결과에서 RA 오답 582개를 분할하고 균형 표본 72개를 human review함.

### Current Research Question

> **현재 확보된 evidence가 답변에 충분하지 않을 때, 시스템은 제한된 inference budget 안에서 다음에 어떤 evidence 또는 processing action을 선택해야 하는가?**

### Candidate Directions — 아직 미확정

1. Multi-page / Visual Evidence Selection
2. Adaptive Evidence Acquisition
3. Agentic Action Selection

**Status:** `COMPARISON IN PROGRESS`

### Next Decision Gate

기존 1,600-query 결과의 대표 failure case 72건에 대한 human review를 완료했다. 다음은 관찰된 failure와 action을 바탕으로 후속 연구 후보를 비교하는 단계다.

- 현재 Top-1 page를 visual하게 확인하면 되는가?
- 다른 retrieved page를 확인해야 하는가?
- 현재 candidate set 밖에서 추가 retrieval이 필요한가?
- 여러 page를 함께 사용해야 하는가?
- 이미 적절한 evidence를 얻었고 reasoning / reading / generation 개선이 필요한가?

**후속 방법론은 아직 확정하지 않았다.**

---

# 1. Research Trajectory

현재 의도하는 장기 흐름은 다음과 같다.

```text
ACK 2026
Selective Vision Routing
"Should I invoke Vision?"
        ↓
Possible Follow-up
Adaptive Evidence Acquisition
"What evidence should I acquire next?"
        ↓
Long-term Direction
Tool / Action Selection
"What action or tool should I execute next?"
        ↓
LLM-based Agents / Web Agents / Adaptive Decision-Making
```

이 trajectory는 아직 최종 확정된 연구계획이 아니라, **ACK failure analysis가 지지하는지 검증 중인 working trajectory**다.

---

# 2. ACK 2026 — Established Results

## 2.1 Problem

멀티모달 Document QA에서 모든 query에 Vision inference를 적용하면 불필요한 계산 비용과 latency가 발생한다. 반대로 text-only processing만 사용하면 실제 시각 정보가 필요한 query에서 성능이 떨어질 수 있다.

ACK 연구는 retrieval과 reranking 이후 확보된 textual evidence를 이용하여 **추가 Vision inference 자체가 필요한지** 선택적으로 판단하는 문제를 다뤘다.

### Important Scope

ACK의 핵심 문제는:

> **어떤 page를 retrieval할 것인가?** 가 아니라  
> **현재 textual evidence를 보고 추가 Vision inference가 필요한가?**

이다.

---

## 2.2 Pipeline

```text
Document / Question
        ↓
Retrieval
        ↓
Reranking
        ↓
Top-3 reranked textual evidence
        ↓
Evidence Sufficiency Judgment
        ↓
┌──────────────────┬────────────────────┐
│ TEXT_ONLY        │ VISUAL_REQUIRED    │
│                  │                    │
│ Text evidence    │ Top-1 source page  │
│ → Answer         │ → VLM              │
│                  │ → Text + Visual    │
│                  │ → Answer           │
└──────────────────┴────────────────────┘
```

### Router Role

Router는 evidence를 생성하지 않는다.

Router의 역할은:

```text
TEXT_ONLY
vs.
VISUAL_REQUIRED
```

를 선택하는 것이다.

VISUAL_REQUIRED에서는 reranking 결과의 최상위 source가 속한 page **1개만** visual evidence로 사용한다.

---

## 2.3 Main Results — UniDoc-Bench 1,600 Queries

| Method | Accuracy | Vision Call Rate | Mean E2E Latency |
|---|---:|---:|---:|
| Forced Text | 58.25% | 0.00% | 5.740 s |
| Forced Vision | 64.00% | 100.00% | 10.299 s |
| Retrieval-Aware | 63.63% | 46.81% | 8.268 s |

### Retrieval-Aware vs. Forced Vision

- Vision calls: **53.19% 감소**
- Mean end-to-end latency: **19.72% 감소**
- Accuracy difference: **-0.38 percentage points**

### Statistical Result Reported in ACK

- McNemar test: `p = 0.679`
- Paired bootstrap 95% CI for RA − FV accuracy difference:
  `-1.88%p ~ +1.06%p`

---

## 2.4 What ACK Actually Demonstrated

### FACT — Supported

Retrieval 이후 확보된 textual evidence를 이용해 추가 Vision inference의 필요성을 선택적으로 판단하면, Forced Vision의 accuracy를 대부분 유지하면서 Vision call과 latency를 줄일 수 있었다.

### NOT Demonstrated

ACK 결과만으로 아래를 증명했다고 말하면 안 된다.

- Optimal visual evidence page selection
- Multi-page reasoning의 필요성
- Query별 optimal visual page count
- Adaptive retrieval depth
- Missing evidence type prediction
- General tool/action selection
- Agentic planning의 우수성

---

# 3. Known Limitations from ACK

## L1. Top-1 Visual Evidence Bottleneck

VISUAL_REQUIRED 이후 reranking 최상위 source page 하나만 VLM에 입력한다.

따라서:

```text
relevant page가 Top-2 / Top-3에 존재
        ↓
Top-1은 다른 page
        ↓
Vision은 잘못된 page만 확인
```

하는 failure가 가능하다.

---

## L2. Binary Action Space

현재 Router의 action은 두 개뿐이다.

```text
TEXT_ONLY
VISUAL_REQUIRED
```

하지만 실제 evidence insufficiency는 서로 다른 원인일 수 있다.

예:

- 현재 page는 맞지만 visual inspection이 필요함
- 다른 retrieved page를 봐야 함
- retrieval 범위를 늘려야 함
- 여러 page를 결합해야 함
- 이미 evidence가 충분하고 reasoning/generation이 문제임

현재 Router는 이 차이를 표현하지 못한다.

---

## L3. Evidence Sufficiency Judgment

ACK Router는 top-3 reranked textual evidence를 보고 binary sufficiency 판단을 수행한다.

현재 결과는 Vision benefit이 있는 모든 query를 완벽하게 포착하지 못한다.

---

# 4. Post-ACK Error Analysis — Data State

## 4.1 Analysis Files

현재 생성된 핵심 분석 파일:

```text
results/error_analysis/analysis_master.csv
results/error_analysis/human_review_queue.csv
results/error_analysis/human_review_sample_72.csv
results/error_analysis/human_review_progress.csv
results/error_analysis/human_review_summary.md
results/error_analysis/human_review_summary_by_type.csv
results/error_analysis/human_review_state_matrix.csv
```

### analysis_master.csv

- Rows: **1,600**
- Query-level FT / FV / RA 결과를 모두 연결
- Retrieval/reranking candidates
- GT evidence pages
- Router decision
- Selected visual page
- Judge result
- Latency
- Failure candidate flags 포함

### human_review_queue.csv

- Rows: **498 unique queries**
- 자동 failure candidate 중 human inspection 대상
- human annotation column 포함

### Human review artifacts

- `human_review_sample_72.csv`: six log states에서 각 12건씩 선정한 고정 표본.
- `human_review_progress.csv`: **72/72** human annotation 완료.
- `human_review_summary.md`, `human_review_summary_by_type.csv`, `human_review_state_matrix.csv`: 완료된 annotation의 기계적 집계.

---

# 5. Verified Aggregate Facts from `analysis_master.csv`

## 5.1 Answer Correctness

| Strategy | Correct | Wrong |
|---|---:|---:|
| Forced Text | 932 | 668 |
| Forced Vision | 1,024 | 576 |
| Retrieval-Aware | 1,018 | 582 |

## 5.2 Retrieval-Aware Route Counts

| Route | Count |
|---|---:|
| TEXT_ONLY | 851 |
| VISUAL_REQUIRED | 749 |

## 5.3 Evidence Statistics

| Metric | Count | Rate |
|---|---:|---:|
| Reranked GT Hit@1 | 1,115 / 1,600 | 69.69% |
| GT Hit@3 | 1,376 / 1,600 | 86.00% |
| GT absent from Top-3 | 224 / 1,600 | 14.00% |
| RA visual selected page ∈ GT | 462 / 749 | 61.68% |
| RA visual selected page ∉ GT | 287 / 749 | 38.32% |

### Important Terminology

`Hit@3`는 **top-3 reranked chunks의 source page들 중 GT page가 포함되는지**를 의미한다.

항상 3개의 unique page를 의미하지 않는다.

---

# 6. Existing Failure Candidate Flags

## 6.1 Routing Failure Candidate

Condition:

```text
RA route = TEXT_ONLY
AND Forced Text = wrong
AND Forced Vision = correct
```

Count:

**67 queries**

### Interpretation

이 집합은 현재 실행에서 Vision action이 도움이 됐을 가능성이 높은 counterfactual candidate다.

### Warning

Forced Vision success를 곧바로 "이 query는 본질적으로 Vision-required"라고 정의하면 안 된다.

---

## 6.2 Evidence Selection Failure Candidate

Condition:

```text
RA route = VISUAL_REQUIRED
AND GT page exists in Top-3 candidate set
AND selected_page ∉ GT
```

Count:

**141 queries**

그중 RA answer wrong:

**113 queries**

### Interpretation

candidate set 안에 annotated GT page가 있었지만 Top-1 visual page selection 때문에 그 page를 사용하지 않은 경우다.

### Warning

GT page를 선택했다면 반드시 정답이 됐을 것이라고 단정할 수는 없다.

---

## 6.3 Retrieval Failure Candidate

Condition:

```text
Top-3 candidate page set ∩ expected_pages = ∅
```

Count:

**224 queries**

그중 RA answer wrong:

**172 queries**

### Important Scope

현재 benchmark는 `per_row_pdf` 방식이다.

따라서 여기서 retrieval failure는:

> 여러 문서 중 잘못된 문서를 고른 실패

가 아니라,

> **주어진 source PDF 내부에서 annotated GT page/chunk를 Top-3 candidate 안에 올리지 못한 실패**

를 의미한다.

---

## 6.4 Evidence-Hit-but-Wrong Candidate

Condition:

```text
RA route = VISUAL_REQUIRED
AND selected_page ∈ GT
AND RA answer = wrong
```

Count:

**151 queries**

### Warning

이를 자동으로 `reasoning failure`라고 부르면 안 된다.

가능한 원인은:

- VLM reading error
- 필요한 chunk/context 부족
- 복수 evidence 필요
- answer generation error
- judge error / overly strict judgment
- annotated GT page가 충분한 evidence를 실제로 포함하지 않는 경우

등이 있다.

---

## 6.5 Multi-GT Review Candidate

Unique `expected_pages >= 2`:

**1,009 queries**

### Critical Warning

```text
multiple expected_pages
≠
multiple pages are jointly required
```

여러 GT page가 대체 가능한 evidence일 수도 있다.

따라서 multi-page necessity는 human inspection 없이 확정하지 않는다.

---

# 7. RA Wrong Queries — Mutually Exclusive Log States

Retrieval-Aware가 틀린 query는 총 **582개**다.

현재 route 및 GT 위치를 기준으로 다음 6개 state로 배타적으로 나눌 수 있다.

| State | Count | % of RA Wrong |
|---|---:|---:|
| V: selected GT page but wrong | 151 | 25.9% |
| V: GT absent from Top-3 | 123 | 21.1% |
| V: GT in Top-3 but wrong page selected | 113 | 19.4% |
| T: GT at reranked Top-1 but wrong | 102 | 17.5% |
| T: GT absent from Top-3 | 49 | 8.4% |
| T: GT only at Top-2/3 | 44 | 7.6% |
| **Total** | **582** | **100%** |

이 표는 **log state**를 나타내며 실제 causal failure type을 확정한 것이 아니다.

---

# 8. Important New Observation

## FACT

RA wrong query는 하나의 failure mechanism으로 설명되지 않는다.

실제 로그에는 다음 상황들이 동시에 존재한다.

1. TEXT_ONLY로 갔지만 Vision이 유용했을 가능성이 있는 경우
2. VISUAL_REQUIRED로 갔지만 다른 candidate page를 선택해야 했을 가능성이 있는 경우
3. GT page가 current Top-3 candidate set에 없는 경우
4. GT page를 실제 visual input으로 사용했는데도 틀린 경우

## INTERPRETATION

현재 evidence가 불충분한 경우 항상 같은 recovery action을 수행하는 구조보다, **failure state에 따라 다른 evidence/action을 선택해야 할 가능성**이 있다.

## NOT YET A DECISION

이 관찰만으로 Adaptive Evidence Acquisition 또는 Agentic Action Selection을 최종 연구주제로 확정하지 않는다.

72-case human review에서 여러 best next action이 확인되었다. 이것만으로 특정 후속 방법론이나 action 선택 정책의 효과가 증명된 것은 아니다.

---

# 9. Follow-up Research Candidates

## 9.1 Multi-page / Visual Evidence Selection

### Core Question

> VISUAL_REQUIRED가 결정된 이후, 어떤 page 또는 page set을 visual evidence로 선택해야 하는가?

### Supporting Evidence

- VISUAL_REQUIRED + GT in Top-3 + wrong page selected + RA wrong:
  **113 queries**
- 균형 human-review 표본에서 primary `VISUAL_PAGE_SELECTION` 6건, secondary 5건이 기록되었다. 이 수치는 113건의 발생 비율이 아니다.

### Strength

- ACK의 Top-1 limitation과 가장 직접적으로 연결됨.
- 기존 pipeline 재사용성이 높음.
- 비교적 명확한 experimental setup 구성 가능.

### Missing Evidence

- Top-2/3 GT page를 선택하면 실제 정답이 회복되는가?
- 하나의 다른 page만 보면 충분한가?
- 여러 page를 동시에 봐야 하는가?

### Status

`OPEN — HUMAN REVIEW COMPLETE; ANSWER RECOVERY NOT TESTED`

---

## 9.2 Adaptive Evidence Acquisition

### Core Question

> 현재 evidence가 부족하다면 어떤 additional evidence를 얻어야 하는가?

Potential actions:

```text
INSPECT_CURRENT_PAGE_VISUALLY
INSPECT_OTHER_RETRIEVED_PAGE
RETRIEVE_MORE
USE_MULTIPLE_PAGES
ANSWER
```

### Supporting Evidence

현재 RA failure가 routing / page selection / retrieval candidate miss / evidence-hit-but-wrong으로 분리된다.

완료된 72-case human review에서 `human_judge_valid=YES` 49건의 best next action은 `RETRIEVE_MORE` 22, `REASON_OR_GENERATE_BETTER` 10, `INSPECT_OTHER_RETRIEVED_PAGE` 9, `INSPECT_CURRENT_PAGE_VISUALLY` 4, `ANSWER_CURRENT_EVIDENCE` 4건으로 기록되었다.

### Working Interpretation

단일 `TEXT_ONLY vs VISUAL_REQUIRED` binary decision을 넘어, evidence insufficiency의 종류에 따라 다음 evidence acquisition action을 선택하는 문제가 자연스럽게 발생할 가능성이 있다.

### Missing Evidence

- 현재 evidence state만으로 best next action을 안정적으로 예측할 수 있는지
- 추가 evidence acquisition이 실제 answer recovery로 이어지는지
- cost-aware stopping rule이 필요한지

### Status

`OPEN — CURRENTLY STRONG WORKING HYPOTHESIS, NOT SELECTED`

---

## 9.3 Agentic Action Selection

### Core Question

> 현재 evidence state에서 answer quality와 inference cost를 고려할 때 어떤 action/tool을 실행해야 하는가?

Possible generalized actions:

```text
ANSWER
INSPECT_VISUAL
INSPECT_OTHER_PAGE
RETRIEVE_MORE
RERANK
REFORMULATE
STOP
```

### Strength

장기 관심 분야인:

- LLM-based Agents
- Tool Use
- Adaptive Decision-Making
- Web Agents

와 자연스럽게 연결될 수 있다.

### Risk

현재 evidence에서 너무 빨리 general Agent framework로 확장하면:

- 실제 ACK failure와 연결이 약해질 수 있음
- action space가 불필요하게 커질 수 있음
- 연구 범위와 evaluation complexity가 증가함
- "Agent"라는 이름만 붙인 확장으로 보일 위험이 있음

### Status

`LONG-TERM DIRECTION — DO NOT IMPLEMENT YET`

---

# 10. Human Review Plan

## 2026-09-30 — Human Review Sample Construction

### FACT

Created:

- `results/error_analysis/human_review_sample_72.csv`
- `scripts/build_human_review_sample.py`

The 582 Retrieval-Aware incorrect queries were partitioned into six
mutually exclusive log states with:

- overlap: 0
- unclassified: 0

Sample design:

- 12 queries per state
- 72 queries total
- no duplicate query IDs
- no duplicate document IDs in the sample
- sampling is reproducible

State populations:

- V_SELECTED_GT_WRONG: 151
- V_GT_ABSENT_TOP3: 123
- V_GT_IN_TOP3_WRONG_PAGE: 113
- T_GT_TOP1_WRONG: 102
- T_GT_ABSENT_TOP3: 49
- T_GT_ONLY_TOP2_3: 44

### INTERPRETATION

The 72-query sample is designed to compare semantic failure mechanisms
across log states.

It must NOT be used to estimate the population prevalence of semantic
failure types because sampling is balanced across states rather than
proportional to their population sizes.

### REVIEW QUESTIONS (at sampling)

For each state:

- What actually caused the failure?
- Was the annotated GT page genuinely useful?
- Was a single page sufficient?
- Were multiple pages jointly required?
- What would have been the best next action?

### NEXT ACTION

72-case review completed. Compare the three follow-up directions before defining a new experiment.

## 2026-09-30 — Human Review Completion

### FACT

Artifacts: `results/error_analysis/human_review_progress.csv`, `human_review_summary.md`, `human_review_summary_by_type.csv`, and `human_review_state_matrix.csv` in the same directory.

- Reviewed: **72/72**; unreviewed: **0**.
- `human_judge_valid`: **YES 49**, **NO 21**, **UNCLEAR 2**. UNCLEAR rows count as reviewed.

| Primary `human_failure_type` | All 72 | Valid subset (`human_judge_valid=YES`, n=49) |
|---|---:|---:|
| RETRIEVAL | 22 | 22 |
| ROUTING | 11 | 11 |
| REASONING_GENERATION | 10 | 10 |
| VISUAL_PAGE_SELECTION | 6 | 6 |
| JUDGE_ERROR | 21 | 0 |
| UNCLEAR | 2 | 0 |

| Best next action among valid 49 | Count |
|---|---:|
| RETRIEVE_MORE | 22 |
| REASON_OR_GENERATE_BETTER | 10 |
| INSPECT_OTHER_RETRIEVED_PAGE | 9 |
| INSPECT_CURRENT_PAGE_VISUALLY | 4 |
| ANSWER_CURRENT_EVIDENCE | 4 |

`VISUAL_PAGE_SELECTION` was recorded as a primary failure in **6** cases and as a secondary failure in **5** cases. The confirmed `human_multi_page_required=YES` cases are **HR022** and **HR045**. The 21 `JUDGE_ERROR` cases are excluded from the valid system-error denominator.

These 72 cases were sampled equally, 12 from each original log state. Their percentages describe this review sample only; they must not be used as prevalence estimates for all 582 RA failures or UniDoc-Bench. Human annotations are grounded in the completed review, and no answer-recovery experiment was run.

### FACT — Observed limitations

- Some cases had enough textual evidence but took an unnecessary Vision route; others needed visual inspection of the current page.
- In some cases the useful page was already among the reranked candidates but was not inspected visually. HR025 is a primary page-selection example; HR061, HR066, and HR070 additionally show TEXT_ONLY routing with another retrieved visual page available.
- Other cases needed a page absent from Top-3, while some already had adequate evidence but failed in reasoning, structured extraction, or answer completion.
- The review marked 21 cases as evaluation or benchmark/gold problems. Only HR022 and HR045 were confirmed as jointly requiring multiple pages.

### INTERPRETATION

The observed actions span answering from current evidence, inspecting the current page, inspecting another retrieved page, retrieving more, and improving reasoning or generation. This suggests a broader evidence-related action question than the ACK binary route alone, but does not establish an effective policy. The Top-1 visual page constraint was a bottleneck in some reviewed cases. The two confirmed multi-page cases do not by themselves establish multi-page reasoning as the dominant follow-up problem.

### DECISION

The 72-case PDF review is complete. Do not keep adding PDFs solely for this error analysis. No follow-up direction has been selected.

### TODO

Compare Multi-page / Visual Evidence Selection, Adaptive Evidence Acquisition, and Agentic Action Selection before defining a minimum research question, architecture, baseline, and evaluation. Do not implement a new method yet.

## Objective

전체 498개 queue를 전수 검토하는 것이 목적이 아니다.

목표는:

> **각 log state가 실제로 어떤 semantic failure와 best next action을 의미하는지 확인하는 것**

이다.

## Completed First Sample

RA wrong 582개를 다음 6개 state로 나누고 각 state에서 12개씩 균형 표본을 뽑았다.

| Group | Sample |
|---|---:|
| V + selected GT + wrong | 12 |
| V + GT absent Top-3 | 12 |
| V + GT in Top-3 but wrong page | 12 |
| T + GT Top-1 + wrong | 12 |
| T + GT absent Top-3 | 12 |
| T + GT only Top-2/3 | 12 |
| **Total** | **72** |

Sampling 시 가능한 한:

- `domain`
- `question_type`
- `expected_page_count`
- multi-GT 여부

가 한쪽으로 치우치지 않도록 한다.

---

# 11. Human Annotation Schema

각 sample에 다음을 annotation한다.

## Required Fields

### `human_judge_valid`

```text
YES
NO
UNCLEAR
```

질문:

> LLM judge가 해당 answer를 실제로 잘못 판정했는가?

---

### `human_failure_type`

권장 값:

```text
ROUTING
VISUAL_PAGE_SELECTION
RETRIEVAL
MULTI_PAGE
VISUAL_READING
REASONING_GENERATION
JUDGE_ERROR
OTHER
UNCLEAR
```

---

### `human_gt_page_useful`

```text
YES
NO
UNCLEAR
```

---

### `human_single_page_sufficient`

```text
YES
NO
UNCLEAR
```

---

### `human_multi_page_required`

```text
YES
NO
UNCLEAR
```

---

### `human_best_next_action`

권장 값:

```text
ANSWER_CURRENT_EVIDENCE
INSPECT_CURRENT_PAGE_VISUALLY
INSPECT_OTHER_RETRIEVED_PAGE
RETRIEVE_MORE
USE_MULTIPLE_PAGES
REASON_OR_GENERATE_BETTER
OTHER
UNCLEAR
```

### Important Distinction

아래 두 action을 반드시 분리한다.

```text
INSPECT_CURRENT_PAGE_VISUALLY
INSPECT_OTHER_RETRIEVED_PAGE
```

첫 번째는 **page retrieval/selection 자체는 맞지만 modality가 부족한 경우**이고,
두 번째는 **현재 visual page selection이 잘못된 경우**이기 때문이다.

---

### `human_confidence`

```text
HIGH
MEDIUM
LOW
```

---

### `human_notes`

1~3문장으로 판단 근거를 남긴다.

---

# 12. Interpretation Guardrails

향후 분석에서 아래 오류를 피한다.

## G1

```text
expected_pages >= 2
```

만으로 multi-page-required라고 하지 않는다.

## G2

```text
selected_page ∈ GT
AND answer wrong
```

만으로 reasoning failure라고 하지 않는다.

## G3

Forced Vision이 맞았다는 이유만으로 query를 ground-truth `VISUAL_REQUIRED`라고 정의하지 않는다.

## G4

GT가 Top-3 candidate에 없다는 사실만으로 PDF 전체에 유효 evidence가 없다고 말하지 않는다.

## G5

Top-3는 reranked **chunks** 기준이며 항상 3 unique pages가 아니다.

## G6

LLM judge score를 human ground-truth correctness와 동일시하지 않는다.

## G7

Human review 전에:

- Multi-page가 dominant bottleneck이다.
- Adaptive Evidence Acquisition이 최종 답이다.
- Agentic action selection이 필요하다.

라고 확정하지 않는다.

---

# 13. Hypothesis Registry

## H1 — Visual Page Selection Bottleneck

**Hypothesis**

VISUAL_REQUIRED query의 상당한 failure는 candidate set 안의 useful page를 적절히 선택하지 못해서 발생한다.

**Existing Support**

- Evidence-selection failure candidate: 141
- 그중 RA wrong: 113
- 완료된 균형 human-review 표본에서 primary `VISUAL_PAGE_SELECTION` 6건, secondary 5건. 이는 582건 전체에서의 발생률이 아니다.

**What Would Strengthen H1**

Human review에서 `INSPECT_OTHER_RETRIEVED_PAGE`가 자주 선택되고,
해당 다른 page가 실제 answer evidence를 제공함이 확인됨.

**What Would Weaken H1**

GT page가 있어도 실제로 답변에 충분하지 않거나,
다른 page selection보다 reasoning / retrieval expansion이 더 자주 필요함.

**Status**

`OPEN`

---

## H2 — Adaptive Evidence Acquisition

**Hypothesis**

Evidence insufficiency는 하나의 binary Vision decision이 아니라,
서로 다른 evidence acquisition action을 요구하는 여러 상태로 구성된다.

**Existing Support**

RA wrong query가:

- current page visual issue
- other page selection issue
- candidate retrieval miss
- evidence-hit-but-wrong

등의 서로 다른 log state로 분리됨.

완료된 human review의 valid 49건에서는 다섯 종류의 best next action이 기록되었다. 이 관찰은 action 선택 정책의 성능을 검증한 결과는 아니다.

**Possible Formulation — Still Hypothetical**

현재 evidence가 불충분할 때 `ANSWER`, `INSPECT_CURRENT_PAGE`, `INSPECT_OTHER_RETRIEVED_PAGE`, `RETRIEVE_MORE` 중 다음 행동을 선택하고 evidence sufficiency를 다시 판단하는 iterative 구조가 적절할 수 있다.

**What Would Strengthen H2**

Human review에서 `best_next_action`이 안정적으로 여러 종류로 분리되고,
각 action 선택 근거가 current evidence state에서 식별 가능함.

**What Would Weaken H2**

대부분의 failure가 사실상 하나의 action으로 해결 가능하거나,
log state와 실제 semantic failure 사이 대응이 약함.

**Status**

`OPEN — PRIORITY HYPOTHESIS TO TEST`

---

## H3 — Agentic Action Selection

**Hypothesis**

Adaptive evidence acquisition을 일반화하면,
Document QA를 evidence state에 따른 sequential tool/action selection 문제로 볼 수 있다.

**Existing Support**

현재 binary route보다 다양한 recovery action이 human review에서 관찰되었다. Retrieval, Vision, browsing, external tools를 포괄하는 일반적 action selection으로 확장 가능한지는 아직 검증되지 않았다.

**Missing Evidence**

아직 sequential multi-step interaction 자체가 필요한지 증명되지 않음.

**Status**

`LONG-TERM HYPOTHESIS`

---

# 14. Decision Log

## 2026-09-30 — Complete the 72-case human review before selecting a follow-up

### FACT

The balanced six-state review is complete: 72/72 annotated. Among the 49 cases with `human_judge_valid=YES`, the recorded best next actions fall into five categories. HR022 and HR045 are the confirmed multi-page-required cases. See Section 10 and `results/error_analysis/human_review_summary.md` for counts and sample limitations.

### DECISION

Do not add more PDF cases solely for this error analysis. Compare the three follow-up directions and define the minimum research question, architecture, and evaluation only after that comparison. Adaptive Evidence Acquisition, Multi-page / Visual Evidence Selection, and Agentic Action Selection all remain unselected.

---

## 2026-09-30 — Do not start a new follow-up experiment yet

### DECISION

새로운 Vision / multi-page / retrieval / Agent 실험을 바로 시작하지 않는다.

### Reason

현재 1,600-query 기존 결과에서 충분한 query-level failure information을 복구할 수 있으며,
먼저 failure mechanism을 확인하는 것이 후속 research question 선택에 더 중요하다.

### Revisit When

72-case human review에서 semantic failure 및 best next action 분포가 확인된 뒤.

---

## 2026-09-30 — Use a persistent Research Note

### DECISION

`docs/RESEARCH_NOTES.md`를 연구의 single source of truth로 사용한다.

### Reason

ChatGPT / Codex를 반복 사용하면서:

- 검증된 사실
- AI의 해석
- 아직 검증되지 않은 가설
- 실제 연구 결정

이 섞이는 것을 방지하기 위함.

### Rule

AI가 새 정보를 제안했다고 자동으로 이 문서를 수정하지 않는다.

```text
AI analysis
→ verification
→ human/research decision
→ Research Note update
```

순서를 유지한다.

---

# 15. Experiment Log

현재 새 후속 실험 없음.

다음 experiment ID는:

`EXP-001`

부터 시작한다.

Template:

```text
## EXP-XXX — Title

Date:

Research Question:

Hypothesis:

Why this experiment is necessary:

Input / Dataset:

Method:

Baseline:

Metrics:

Result:

Interpretation:

What this does NOT prove:

Decision:

Artifacts:

Next action:
```

---

# 16. AI / Codex Work Log

## 2026-09-30 — Repository Audit

### Task

기존 ACK 1,600-query 결과로 query-level failure analysis가 가능한지 read-only 조사.

### Important Findings

- FT/FV/RA 전체 1,600 query join 가능
- Router decision 연결 가능
- GT evidence page와 reranked candidates 비교 가능
- Judge correctness 연결 가능
- Top-3 candidate 및 selected visual page 복구 가능

### Verification

`PARTIAL → aggregate counts independently rechecked from analysis_master.csv`

---

## 2026-09-30 — Error Analysis Dataset Construction

### Files Generated

```text
results/error_analysis/analysis_master.csv
results/error_analysis/human_review_queue.csv
```

### Verification

`YES — structural sanity checks completed`

Confirmed:

```text
analysis_master rows = 1600
human_review_queue rows = 498

Forced Text correct = 932
Forced Vision correct = 1024
Retrieval-Aware correct = 1018

TEXT_ONLY = 851
VISUAL_REQUIRED = 749
```

### Important Finding

기존 4개 failure candidate flag만으로 모든 RA wrong query를 의미적으로 설명할 수는 없다.

따라서 다음 단계에서는 RA wrong 582개를 6개의 mutually exclusive **log state**로 나누어 human review를 수행한다.

---

# 17. Open Questions

- [ ] Top-3 candidate 안의 GT page를 실제로 Vision으로 보면 answer가 회복되는가?
- [ ] `TEXT_ONLY + GT Top-1 + wrong` 사례는 주로 current-page visual inspection 문제인가?
- [ ] GT absent Top-3 사례는 추가 retrieval로 실제 해결 가능한가?
- [ ] 여러 `expected_pages`가 존재하는 query 중 실제 multi-page reasoning이 필요한 비율은 어느 정도인가?
- [ ] `selected_page ∈ GT + wrong` 사례 중 judge error 비율은 어느 정도인가?
- [ ] semantic failure와 `best_next_action` 사이에 안정적인 mapping이 존재하는가?
- [ ] next action 선택에 필요한 signal을 current evidence state에서 얻을 수 있는가?
- [ ] 후속 문제를 one-step acquisition으로 충분히 정의할 수 있는가, 아니면 sequential action loop가 필요한가?

---

# 18. Next Actions

## Completed

1. RA wrong 582개를 6개 mutually exclusive log state로 분류.
2. 각 group에서 균형 있게 12개씩 뽑아 `human_review_sample_72.csv` 생성.
3. 72개에 대해 human annotation 수행.

## Must Do Next

Compare the three follow-up directions below. Do not start a new experiment or implement a new method yet.

## Follow-up Direction Comparison

다음 세 후보를 다시 비교한다.

```text
Multi-page / Visual Evidence Selection
vs.
Adaptive Evidence Acquisition
vs.
Agentic Action Selection
```

비교 기준:

- ACK와의 continuity
- 실제 failure coverage
- novelty potential
- implementation complexity
- RTX A5000 feasibility
- clean evaluation 가능성
- 대학원 면담에서 설명 가능한 research story
- 장기 LLM Agent / Tool Use 확장성

방향을 선택한 뒤에만 최소 research question, system action space, baseline, evaluation protocol, minimum experiment를 정의한다.

## Do Not Start Yet

- Top-k Vision experiment
- Multi-page VLM inference
- Iterative retrieval
- Agent loop
- RL / policy training
- 새 benchmark 추가
- 별도 신규 프로젝트 시작

---

# 19. AI Research Workflow Rules

AI에게 작업을 시킬 때 다음 순서를 유지한다.

1. `Current Research State`를 확인한다.
2. `Established Results`와 `Interpretation Guardrails`를 확인한다.
3. `Open Questions` 중 어떤 질문을 해결하는 작업인지 명시한다.
4. 새 실험이라면 반드시 explicit research question과 연결한다.
5. 기존 결과를 덮어쓰지 않는다.
6. generated artifact의 파일 경로를 기록한다.
7. 중요한 aggregate number는 기존 결과와 sanity check한다.
8. AI 해석을 FACT로 자동 승격하지 않는다.
9. meaningful work 이후 Research Note update 필요 여부를 검토한다.
10. Research Note 수정 전 변경 내용을 먼저 검토한다.

### Standard Codex Footer

향후 Codex prompt 끝에는 필요 시 다음을 붙인다.

```text
작업이 끝난 후 docs/RESEARCH_NOTES.md를 읽고,
이번 작업에서 새로 확인된 내용을 아래 네 범주로만 보고하라.

- FACT
- INTERPRETATION
- OPEN QUESTION
- PROPOSED NOTE UPDATE

내 승인 없이 RESEARCH_NOTES.md 자체는 수정하지 말 것.
기존 실험 결과를 덮어쓰지 말 것.
```

---

# 20. Update Protocol

Research Note를 업데이트할 때 날짜별 채팅 내용을 그대로 붙이지 않는다.

반영할 것은 다음뿐이다.

### FACT
실제 파일 / 실험 / 논문에서 확인된 내용.

### INTERPRETATION
FACT에서 도출한 해석. 사실과 분리해서 기록.

### HYPOTHESIS
아직 검증되지 않은 research claim.

### DECISION
실제로 연구 방향이나 실험 설계에 반영하기로 한 판단.

### TODO
다음 행동.

---

# 21. Quick Resume — 다음에 연구를 다시 시작할 때 이것만 읽기

현재 ACK 연구는:

```text
retrieval
→ reranking
→ evidence sufficiency
→ TEXT_ONLY / VISUAL_REQUIRED
```

를 통해 Forced Vision 대비 Vision call과 latency를 줄이면서 accuracy를 거의 유지했다.

현재 post-ACK 분석에서는 failure가 하나가 아니라:

```text
wrong routing
wrong visual page
GT absent from current candidates
correct GT page but still wrong
```

등의 서로 다른 log state로 나타난다.

따라서 현재 가장 중요한 질문은:

> **현재 evidence가 부족할 때 다음에 어떤 evidence/action을 선택해야 하는가?**

이다.

하지만 아직 Adaptive Evidence Acquisition 또는 Agentic Action Selection을 최종 주제로 선택하지 않았다.

**다음 행동은 새 실험이 아니라 72-case human review다.**
