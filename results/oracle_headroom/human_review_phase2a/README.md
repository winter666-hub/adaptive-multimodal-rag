# Phase 2A human review packet

이 packet은 이후 사람이 직접 검토하기 위한 자료이며 human judgment를 자동 생성하지 않았습니다. CSV를 annotation 기록용으로 사용하고, Markdown companion으로 전체 evidence와 답변을 읽으세요. 전체 결과는 아직 human-verified final이 아닙니다.

## 대상과 순서

Canonical transition 69건과 raw judge incomplete 8건을 query_id로 합쳐 중복 제거했습니다. 각 query는 정확히 한 batch에만 있으며 최대 12건씩입니다. review_index.csv는 review ID, priority, batch와 누락 상태를 연결합니다.

Priority 1: E3 wrong→E6 correct; 2: E3 correct→E6 wrong; 3: E6 wrong→E9 correct; 4: E6 correct→E9 wrong; 5: judge incomplete. 여러 조건에 해당하면 가장 높은 우선순위를 사용합니다. 동순위는 query_id 순입니다. Automatic transition은 사람이 검토해야 할 후보이며 정답 판정이 아닙니다. 이 선택 표본으로 전체 224건 또는 전체 benchmark accuracy를 추정하지 마세요.

## Evidence와 답변

E3는 기존 Top-3, E6는 E3에 최대 3개 추가, E9는 E6에 최대 3개 추가한 textual evidence입니다. 모든 chunk text와 답변은 생략 없이 저장했습니다. E6/E9에는 새로 추가된 chunk만 별도로 표시하며 전체 순서와 각 state의 unique page 목록도 CSV에 기록했습니다.

E9 answer/automatic label은 canonical evaluation을 사용합니다. E6/E9 입력이 동일하면 E6 answer와 judgment를 E9에 재사용했습니다. canonical_e9_judgment_source와 e6_e9_input_identical을 확인하세요. 독립 생성된 원래 E9 답변은 CSV의 raw_E9_generated_answer에 별도로 보존했으며 이것을 canonical depth-effect 비교와 섞지 마세요. Raw missing state는 raw_incomplete_states에 표시하고, unavailable 자동 label은 빈칸으로 둡니다.

## 판정 원칙

1. Automatic judge와 benchmark gold를 정답으로 가정하지 않습니다.
2. 실제 document evidence를 최우선으로 봅니다.
3. Answer correctness는 질문이 요구한 내용을 실질적으로 충족하는지 판단합니다.
4. 표현이 reference와 달라도 source-supported하고 질문을 충분히 답하면 YES 가능합니다.
5. Reference/gold가 source와 충돌하면 human_reference_valid = NO입니다.
6. E3/E6/E9 answer를 각각 독립적으로 판단합니다.
7. 추가 evidence가 들어왔다는 이유만으로 E6/E9를 correct로 판단하지 않습니다.
8. GT page가 evidence에 포함됐다는 이유만으로 evidence sufficient라고 판단하지 않습니다.
9. human_e6_added_evidence_useful은 E3에 없던 새 evidence가 답을 개선하거나 질문 해결에 실질적으로 기여했는지를 봅니다.
10. human_e9_added_evidence_useful도 E6에 없던 새 evidence에 같은 기준을 적용합니다.
11. Source만으로 판단이 확실하지 않으면 UNCLEAR입니다.

## Annotation 허용값

| Field | Allowed values |
|---|---|
| human_e3_correct / human_e6_correct / human_e9_correct | YES / NO / UNCLEAR |
| human_reference_valid | YES / NO / UNCLEAR |
| human_e3_evidence_sufficient | YES / NO / UNCLEAR |
| human_e6_added_evidence_useful | YES / NO / UNCLEAR |
| human_e9_added_evidence_useful | YES / NO / UNCLEAR |
| human_confidence | HIGH / MEDIUM / LOW |
| human_notes | 자유 서술 |

모든 human annotation 필드는 빈칸입니다. 동일 treatment의 반복 답변이라도 자동 판정을 복사해 human 필드를 채우지 않습니다. PDF 직접 확인이 필요하거나 extraction/source 해석이 불확실하면 notes에 기록하세요.

## PDF와 GT text 확인

pdf_manifest.csv는 각 review_id/query_id의 document ID, local PDF 경로, GT pages, 전체 evidence pages를 연결합니다. PDF는 복사하지 않았습니다. 페이지 번호는 기존 GT 값을 그대로 사용한 physical 1-based page이며 offset을 적용하지 않았습니다.

GT text는 로컬 PDF에서 pypdf로 추출했습니다. EXTRACTED_UNVERIFIED는 텍스트가 추출되었다는 뜻이며 completeness/reading order/table/figure 정확성을 보장하지 않습니다. EMPTY_TEXT, EXTRACTION_ERROR, PDF_READ_ERROR, PAGE_OUT_OF_RANGE, POSSIBLY_INCOMPLETE 또는 EXTRACTED_WITH_PDF_WARNINGS는 PDF 직접 확인 대상으로 표시합니다. Sparse text(<40 characters) 또는 replacement characters 비율 >1%도 보수적으로 표시합니다. PDF 구조 경고는 해당 사례의 GT text note와 packet_manifest.json에 보존합니다. OCR, 텍스트 보완, 추정 생성은 수행하지 않았습니다. 추출 텍스트가 있어도 질문이 표/그림에 의존하면 원본 PDF를 확인하세요. Full evidence packet은 PDF 추출 텍스트를 자동 판정에 사용하지 않습니다.

## 파일

- review_batch_01.csv 등: annotation과 전체 사례 데이터
- review_batch_01.md 등: 읽기 쉬운 전체 사례 companion
- review_index.csv: batch/priority/selection/누락 상태 목록
- pdf_manifest.csv: PDF lookup
- packet_manifest.json: 검증 결과, 보호 파일 및 PDF SHA-256, extraction 방식

검증 결과:

```json
{
  "unique_queries": 77,
  "transition_queries_included": 69,
  "incomplete_queries_included": 8,
  "overlap_queries": 0,
  "batch_counts": [
    12,
    12,
    12,
    12,
    12,
    12,
    5
  ],
  "unique_pdf_documents": 70,
  "gt_text_requires_pdf_check_queries": 2,
  "gt_text_missing_queries": 1,
  "pdf_warning_queries": 1,
  "gt_page_extraction_status_counts": {
    "EXTRACTED_UNVERIFIED": 109,
    "EMPTY_TEXT": 1,
    "EXTRACTED_WITH_PDF_WARNINGS": 2
  },
  "priority_counts": {
    "1": 49,
    "2": 10,
    "3": 9,
    "4": 1,
    "5": 8
  },
  "missing_queries": 0,
  "human_annotation_fields_empty": true,
  "every_query_in_exactly_one_batch": true,
  "protected_sources_unchanged": true,
  "human_judgments_generated": false
}
```
