"""Create self-contained, unannotated Phase 2A human review batches; no API calls."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/oracle_headroom"
DEST = SOURCE / "human_review_phase2a"
STATES = ("E3", "E6", "E9")
ANNOTATIONS = ("human_e3_correct", "human_e6_correct", "human_e9_correct", "human_reference_valid",
               "human_e3_evidence_sufficient", "human_e6_added_evidence_useful",
               "human_e9_added_evidence_useful", "human_confidence", "human_notes")
FLAGS = ("E3_to_E6_wrong_to_correct", "E3_to_E6_correct_to_wrong",
         "E6_to_E9_wrong_to_correct", "E6_to_E9_correct_to_wrong")


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(name, rows):
    with (DEST / name).open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def literal(text):
    # Preserve embedded Markdown/backticks as literal text without truncation.
    fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", text)), default=3))
    return f"{fence}text\n{text}\n{fence}"


def evidence_markdown(chunks):
    if not chunks:
        return "추가 chunk 없음."
    return "\n\n".join(f"### Chunk {i}: {c['chunk_id']} / source page {c['source_page']}\n\n{literal(c['chunk_text'])}"
                        for i, c in enumerate(chunks, 1))


def case_markdown(row):
    gt_text = json.loads(row["gt_page_extracted_text"])
    gt_block = "\n\n".join(f"### GT page {r['page']} — {r['status']}\n\n{r['note']}\n\n{literal(r['text']) if r['text'] else '추출 텍스트 없음: PDF 직접 확인 필요.'}"
                             for r in gt_text)
    labels = "\n".join(f"- {s}: {row[s + '_auto_correct'] or 'UNAVAILABLE'}; {row[s + '_auto_status']}" for s in STATES)
    flags = "\n".join(f"- {k}: {row[k]}" for k in FLAGS)
    annotations = "\n".join(f"| {k} |  |" for k in ANNOTATIONS)
    return f"""# {row['review_id']} / {row['query_id']}

Priority: {row['priority']} / Cohort: {row['retrieval_cohort']} / Selection: {row['selection_reason']}

## Question

{literal(row['question'])}

## Gold / Reference

{literal(row['gold_reference'])}

## GT Pages

{row['gt_pages']}

## Document Verification

- Document: {row['document_id']}
- Dataset identifier: {row['dataset_pdf_identifier']}
- Local PDF: {row['pdf_path_or_identifier']}
- Unique E3/E6/E9 pages: {row['evidence_pages']}
- E3 pages: {row['E3_unique_source_pages']}
- E6 pages: {row['E6_unique_source_pages']}
- E9 pages: {row['E9_unique_source_pages']}
- PDF direct verification flag: {row['gt_text_requires_pdf_check']}

### GT page extracted text

{gt_block}

## E3 Evidence

Ordered IDs: {row['E3_ordered_chunk_ids']}

{evidence_markdown(json.loads(row['E3_evidence']))}

## E3 Answer

{literal(row['E3_answer'])}

## E6 Added Evidence

Added ordered IDs: {row['E6_added_chunk_ids']}

{evidence_markdown(json.loads(row['E6_added_evidence']))}

## E6 Answer

{literal(row['E6_answer'])}

## E9 Added Evidence

Added ordered IDs: {row['E9_added_chunk_ids']}

{evidence_markdown(json.loads(row['E9_added_evidence']))}

## E9 Answer

Canonical judgment source: {row['canonical_e9_judgment_source']}; E6/E9 input identical: {row['e6_e9_input_identical']}

{literal(row['E9_answer'])}

## Automatic Labels

{labels}

Raw incomplete states: {row['raw_incomplete_states']}

{flags}

{literal(row['automatic_judge_reasons'])}

## Human Annotation

| Field | Value |
|---|---|
{annotations}

"""


def main():
    if DEST.exists():
        raise SystemExit("Review packet destination exists; refusing overwrite")
    protected = {str(p.relative_to(ROOT)): sha(p) for p in SOURCE.rglob("*") if p.is_file()}
    cleanup_manifest = json.loads((SOURCE / "phase2a_cleanup_manifest.json").read_text(encoding="utf-8"))
    protected.update(cleanup_manifest["protected_sha256"])
    for path, digest in protected.items():
        assert sha(ROOT / path) == digest, path
    queue = {r["query_id"]: r for r in read(SOURCE / "text_expansion_human_review_queue_final.csv")}
    canonical = {r["query_id"]: r for r in read(SOURCE / "text_expansion_canonical_evaluation_final.csv")}
    incomplete = {q for q, r in canonical.items() if r["raw_three_state_complete"] == "False"}
    assert len(queue) == 69 and len(incomplete) == 8 and len(canonical) == 224
    targets = set(queue) | incomplete
    master = {r["query_id"]: r for r in read(ROOT / "results/error_analysis/analysis_master.csv")}
    cohorts = {r["query_id"]: r for r in read(SOURCE / "retrieval_depth_cohorts.csv")}
    pool = {}
    for r in read(SOURCE / "expanded_reranked_candidates.csv"):
        pool.setdefault(r["query_id"], {})[r["chunk_id"]] = r
    def checkpoint(name):
        return [json.loads(x) for x in (SOURCE / name).read_text(encoding="utf-8").splitlines()]
    generations = {(r["query_id"], r["evidence_state"]): r for r in checkpoint("phase2a_generation.checkpoint.jsonl")}
    judgments = {(r["query_id"], r["evidence_state"]): r for r in [*checkpoint("phase2a_judgments.checkpoint.jsonl"), *checkpoint("phase2a_cleanup_judgments.checkpoint.jsonl")]}
    documents, pdf_paths = {}, {}
    for q in targets:
        path = (ROOT / "datasets/unidoc" / master[q]["source_pdf"]).resolve()
        assert path == Path(master[q]["source_pdf_path"]).resolve()
        pdf_paths[q] = path
        documents.setdefault(path, set()).update(json.loads(master[q]["expected_pages"]))
    gt_text, pdf_hashes = {}, {}
    class PdfWarningCollector(logging.Handler):
        def emit(self, record):
            self.messages.append(record.getMessage())
    collector = PdfWarningCollector()
    collector.messages = []
    pdf_logger = logging.getLogger("pypdf")
    pdf_logger.addHandler(collector)
    extraction_warnings = {}
    for path, pages in sorted(documents.items()):
        collector.messages = []
        pdf_hashes[str(path)] = sha(path) if path.exists() else "UNAVAILABLE"
        try:
            reader = PdfReader(path)
            for page in sorted(pages):
                result = {"page": page, "status": "", "text": "", "note": "", "requires_pdf_check": False}
                try:
                    text = reader.pages[page - 1].extract_text().strip() if 1 <= page <= len(reader.pages) else ""
                    if not 1 <= page <= len(reader.pages):
                        result.update(status="PAGE_OUT_OF_RANGE", note="GT page is outside physical PDF page range.", requires_pdf_check=True)
                    elif not text:
                        result.update(status="EMPTY_TEXT", note="No extractable text. No OCR or invented replacement performed.", requires_pdf_check=True)
                    elif len(text) < 40 or text.count("\ufffd") / len(text) > .01:
                        result.update(status="POSSIBLY_INCOMPLETE", text=text, note="Sparse text or replacement characters; verify PDF directly.", requires_pdf_check=True)
                    else:
                        result.update(status="EXTRACTED_UNVERIFIED", text=text, note="pypdf physical page text; reading order, tables, figures, and extraction completeness unverified.")
                except Exception as exc:
                    result.update(status="EXTRACTION_ERROR", note=f"{type(exc).__name__}: {exc}", requires_pdf_check=True)
                gt_text[path, page] = result
        except Exception as exc:
            for page in sorted(pages):
                gt_text[path, page] = {"page": page, "status": "PDF_READ_ERROR", "text": "", "note": f"{type(exc).__name__}: {exc}", "requires_pdf_check": True}
        if collector.messages:
            extraction_warnings[str(path)] = list(collector.messages)
            for page in pages:
                result = gt_text[path, page]
                if result["status"] == "EXTRACTED_UNVERIFIED":
                    result["status"] = "EXTRACTED_WITH_PDF_WARNINGS"
                result["requires_pdf_check"] = True
                result["note"] += " PDF structural/extraction warnings: " + " | ".join(collector.messages)
    pdf_logger.removeHandler(collector)
    rows = []
    for q in targets:
        c, m = canonical[q], master[q]
        labels = {s: c[f"{s}_canonical_correct"] for s in STATES}
        flags = {}
        for a, b in (("E3", "E6"), ("E6", "E9")):
            flags[f"{a}_to_{b}_wrong_to_correct"] = labels[a] == "False" and labels[b] == "True"
            flags[f"{a}_to_{b}_correct_to_wrong"] = labels[a] == "True" and labels[b] == "False"
        assert (q in queue) == any(flags.values())
        if q in queue:
            assert all(flags[k] == (queue[q][k] == "True") for k in FLAGS)
        priority = next((i for i, flag in enumerate(FLAGS, 1) if flags[flag]), 5)
        original = [{"chunk_id": m[f"reranked_chunk_id_{i}"], "source_page": int(m[f"reranked_page_{i}"]), "chunk_text": m[f"reranked_chunk_text_{i}"]} for i in (1, 2, 3)]
        lookup = {**pool[q], **{r["chunk_id"]: r for r in original}}
        evidence = {s: [{"chunk_id": lookup[x]["chunk_id"], "source_page": int(lookup[x]["source_page"]), "chunk_text": lookup[x]["chunk_text"]} for x in json.loads(cohorts[q][f"{s}_ranked_chunks"])] for s in STATES}
        assert evidence["E6"][:len(evidence["E3"])] == evidence["E3"]
        assert evidence["E9"][:len(evidence["E6"])] == evidence["E6"]
        added = {"E3": evidence["E3"], "E6": evidence["E6"][len(evidence["E3"]):], "E9": evidence["E9"][len(evidence["E6"]):]}
        page_text = [gt_text[pdf_paths[q], p] for p in sorted(json.loads(m["expected_pages"]))]
        sources = {s: "E6" if s == "E9" and c["e6_e9_input_identical"] == "True" else s for s in STATES}
        raw_missing = [s for s in STATES if (q, s) not in judgments]
        row = {"review_id": "", "query_id": q, "priority": priority, "retrieval_cohort": c["cohort"],
               "selection_reason": ";".join(x for x, yes in (("AUTOMATIC_TRANSITION", q in queue), ("JUDGE_INCOMPLETE", q in incomplete)) if yes),
               "question": m["question"], "gold_reference": m["gold_answer"], "gt_pages": m["expected_pages"],
               "document_id": m["document_id"], "dataset_pdf_identifier": m["source_pdf"],
               "pdf_path_or_identifier": str(pdf_paths[q]), "pdf_exists": pdf_paths[q].exists(),
               "evidence_pages": dump(sorted({r["source_page"] for r in evidence["E9"]})),
               **{f"{s}_unique_source_pages": dump(sorted({r["source_page"] for r in evidence[s]})) for s in STATES},
               "E3_ordered_chunk_ids": dump([r["chunk_id"] for r in evidence["E3"]]),
               "E6_ordered_chunk_ids": dump([r["chunk_id"] for r in evidence["E6"]]),
               "E9_ordered_chunk_ids": dump([r["chunk_id"] for r in evidence["E9"]]),
               "E3_evidence": dump(evidence["E3"]), "E6_added_evidence": dump(added["E6"]), "E9_added_evidence": dump(added["E9"]),
               "E6_added_chunk_ids": dump([r["chunk_id"] for r in added["E6"]]), "E9_added_chunk_ids": dump([r["chunk_id"] for r in added["E9"]]),
               **{f"{s}_answer": generations[q, sources[s]]["answer"] for s in STATES},
               "raw_E9_generated_answer": generations[q, "E9"]["answer"],
               **{f"{s}_auto_correct": labels[s] for s in STATES},
               **{f"{s}_auto_status": "OK" if labels[s] else "JUDGE_ERROR_UNAVAILABLE" for s in STATES},
               "raw_incomplete_states": dump(raw_missing),
               "automatic_judge_reasons": dump({s: judgments.get((q, sources[s]), {}).get("judge_reason", "") for s in STATES}),
               **flags, "e6_e9_input_identical": c["e6_e9_input_identical"], "canonical_e9_judgment_source": c["canonical_e9_judgment_source"],
               "gt_page_extracted_text": dump(page_text), "gt_text_requires_pdf_check": any(r["requires_pdf_check"] for r in page_text),
               **{k: "" for k in ANNOTATIONS}}
        rows.append(row)
    rows.sort(key=lambda r: (r["priority"], r["query_id"]))
    for i, r in enumerate(rows, 1):
        r["review_id"] = f"P2A_HR_{i:03d}"
    assert {r["query_id"] for r in rows} == targets and len(rows) == len(targets)
    assert all(r[k] == "" for r in rows for k in ANNOTATIONS)
    DEST.mkdir()
    batches = [rows[i:i+12] for i in range(0, len(rows), 12)]
    for i, batch in enumerate(batches, 1):
        write_csv(f"review_batch_{i:02d}.csv", batch)
        with (DEST / f"review_batch_{i:02d}.md").open("x", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(case_markdown(r) for r in batch))
    write_csv("pdf_manifest.csv", [{k: r[k] for k in ("review_id", "query_id", "document_id", "pdf_path_or_identifier", "gt_pages", "evidence_pages")} for r in rows])
    write_csv("review_index.csv", [{"batch": f"review_batch_{i:02d}", **{k: r[k] for k in ("review_id", "query_id", "priority", "selection_reason", "retrieval_cohort", "raw_incomplete_states", "gt_text_requires_pdf_check")}} for i, batch in enumerate(batches, 1) for r in batch])
    observed = [r for i in range(1, len(batches)+1) for r in read(DEST / f"review_batch_{i:02d}.csv")]
    assert Counter(r["query_id"] for r in observed) == Counter({q: 1 for q in targets})
    assert all(len(read(DEST / f"review_batch_{i:02d}.csv")) <= 12 for i in range(1, len(batches)+1))
    assert all(r[k] == "" for r in observed for k in ANNOTATIONS)
    assert all(str(expected[k]) == actual[k] for expected, actual in zip(rows, observed) for k in expected)
    for path, digest in protected.items():
        assert sha(ROOT / path) == digest, path
    summary = {"unique_queries": len(rows), "transition_queries_included": len(set(queue) & targets),
               "incomplete_queries_included": len(incomplete & targets), "overlap_queries": len(set(queue) & incomplete),
               "batch_counts": [len(b) for b in batches], "unique_pdf_documents": len(documents),
               "gt_text_requires_pdf_check_queries": sum(r["gt_text_requires_pdf_check"] for r in rows),
               "gt_text_missing_queries": sum(any(not p["text"] for p in json.loads(r["gt_page_extracted_text"])) for r in rows),
               "pdf_warning_queries": sum(r["pdf_path_or_identifier"] in extraction_warnings for r in rows),
               "gt_page_extraction_status_counts": dict(Counter(r["status"] for r in gt_text.values())),
               "priority_counts": dict(Counter(r["priority"] for r in rows)), "missing_queries": 0,
               "human_annotation_fields_empty": True, "every_query_in_exactly_one_batch": True,
               "protected_sources_unchanged": True, "human_judgments_generated": False}
    with (DEST / "packet_manifest.json").open("x", encoding="utf-8") as f:
        json.dump({"summary": summary, "protected_source_sha256": protected, "pdf_sha256": pdf_hashes,
                   "pdf_extraction_warnings": extraction_warnings,
                   "extraction": "pypdf extract_text().strip(); physical 1-based GT pages, no offset, no OCR; sparse <40 characters or replacement-character ratio >1% flagged; completeness otherwise unverified"}, f, indent=2)
    readme = """# Phase 2A human review packet

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

"""
    with (DEST / "README.md").open("x", encoding="utf-8") as f:
        f.write(readme + "검증 결과:\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
