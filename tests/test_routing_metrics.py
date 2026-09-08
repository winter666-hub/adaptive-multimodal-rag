from furiosa_rag.routing_metrics import compute_routing_metrics


def test_binary_routing_metrics() -> None:
    rows = [
        {"expected_route": "VISUAL_REQUIRED", "predicted_route": "VISUAL_REQUIRED"},
        {"expected_route": "TEXT_ONLY", "predicted_route": "TEXT_ONLY"},
        {"expected_route": "TEXT_ONLY", "predicted_route": "VISUAL_REQUIRED"},
        {"expected_route": "VISUAL_REQUIRED", "predicted_route": "TEXT_ONLY"},
        {"expected_route": "VISUAL_REQUIRED", "predicted_route": "", "error": "timeout"},
    ]
    metrics = compute_routing_metrics(rows)
    assert (metrics["tp"], metrics["tn"], metrics["fp"], metrics["fn"]) == (1, 1, 1, 1)
    assert metrics["accuracy"] == 0.5
    assert metrics["visual_precision"] == 0.5
    assert metrics["visual_recall"] == 0.5
    assert metrics["visual_f1"] == 0.5
    assert metrics["text_recall"] == 0.5
    assert metrics["balanced_accuracy"] == 0.5
    assert metrics["evaluated_count"] == 4
    assert metrics["error_count"] == 1
    assert metrics["unevaluated_count"] == 1


def test_imbalanced_accuracy_differs_from_balanced_accuracy() -> None:
    rows = [
        {"expected_route": "TEXT_ONLY", "predicted_route": "VISUAL_REQUIRED"} for _ in range(400)
    ] + [
        {"expected_route": "VISUAL_REQUIRED", "predicted_route": "VISUAL_REQUIRED"}
        for _ in range(1200)
    ]
    metrics = compute_routing_metrics(rows)
    assert metrics["accuracy"] == 0.75
    assert metrics["visual_recall"] == 1.0
    assert metrics["text_recall"] == 0.0
    assert metrics["balanced_accuracy"] == 0.5


def test_zero_denominators_return_zero() -> None:
    metrics = compute_routing_metrics([])
    assert metrics["accuracy"] == 0.0
    assert metrics["visual_precision"] == 0.0
    assert metrics["visual_recall"] == 0.0
    assert metrics["visual_f1"] == 0.0
    assert metrics["balanced_accuracy"] == 0.0
