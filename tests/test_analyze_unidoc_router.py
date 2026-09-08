from scripts.analyze_unidoc_router import (
    TEXT_ONLY,
    VISUAL_REQUIRED,
    adaptive_path_analysis,
    compare_routers,
    confusion_metrics,
    group_analysis,
    parse_bool,
)


def row(
    query_id: str,
    expected: str,
    predicted: str,
    *,
    answer_type: str = "image_only",
    domain: str = "finance",
    used_llm_router: bool = True,
) -> dict[str, object]:
    return {
        "query_id": query_id,
        "question": "question",
        "answer_type": answer_type,
        "domain": domain,
        "expected_route": expected,
        "predicted_route": predicted,
        "route_correct": expected == predicted,
        "routing_latency_ms": 1.0,
        "used_llm_router": used_llm_router,
    }


def test_confusion_grouping() -> None:
    rows = [
        row("1", VISUAL_REQUIRED, VISUAL_REQUIRED),
        row("2", TEXT_ONLY, TEXT_ONLY),
        row("3", TEXT_ONLY, VISUAL_REQUIRED),
        row("4", VISUAL_REQUIRED, TEXT_ONLY),
    ]
    metrics = confusion_metrics(rows)
    assert (metrics["tp"], metrics["tn"], metrics["fp"], metrics["fn"]) == (1, 1, 1, 1)
    assert metrics["accuracy"] == 0.5
    assert metrics["balanced_accuracy"] == 0.5


def test_answer_type_fn_rate() -> None:
    rows = [
        row("1", VISUAL_REQUIRED, VISUAL_REQUIRED, answer_type="image_only"),
        row("2", VISUAL_REQUIRED, TEXT_ONLY, answer_type="image_only"),
    ]
    group = group_analysis(rows, "answer_type", "adaptive")[0]
    assert group["answer_type"] == "image_only"
    assert group["fn"] == 1
    assert group["fn_rate"] == 0.5


def test_domain_grouping() -> None:
    rows = [
        row("1", VISUAL_REQUIRED, VISUAL_REQUIRED, domain="finance"),
        row("2", TEXT_ONLY, VISUAL_REQUIRED, domain="legal"),
    ]
    groups = group_analysis(rows, "domain", "llm")
    assert [group["domain"] for group in groups] == ["finance", "legal"]
    assert groups[0]["visual_recall"] == 1.0
    assert groups[1]["fp"] == 1


def test_adaptive_shortcut_and_fallback_split() -> None:
    rows = [
        row("1", VISUAL_REQUIRED, VISUAL_REQUIRED, used_llm_router=False),
        row("2", TEXT_ONLY, VISUAL_REQUIRED, used_llm_router=False),
        row("3", VISUAL_REQUIRED, TEXT_ONLY, used_llm_router=True),
    ]
    groups = {group["group"]: group for group in adaptive_path_analysis(rows)}
    assert groups["rule_shortcut"]["count"] == 2
    assert groups["rule_shortcut"]["fp"] == 1
    assert groups["llm_fallback"]["fn"] == 1


def test_router_join_and_disagreement() -> None:
    llm = [
        row("1", VISUAL_REQUIRED, TEXT_ONLY),
        row("2", TEXT_ONLY, TEXT_ONLY),
    ]
    adaptive = [
        row("1", VISUAL_REQUIRED, VISUAL_REQUIRED, used_llm_router=False),
        row("2", TEXT_ONLY, TEXT_ONLY),
    ]
    counts, disagreements = compare_routers(llm, adaptive)
    assert counts["same_prediction"] == 1
    assert counts["different_prediction"] == 1
    assert counts["adaptive_only_correct"] == 1
    assert counts["both_correct"] == 1
    assert [item["query_id"] for item in disagreements] == ["1"]


def test_boolean_parsing() -> None:
    assert parse_bool("True") is True
    assert parse_bool("false") is False
    assert parse_bool("1") is True
    assert parse_bool("0") is False
    assert parse_bool("") is None
