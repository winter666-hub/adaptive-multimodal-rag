from __future__ import annotations

import json
import socket
from copy import deepcopy

import pytest

from furiosa_rag.cli.analyze_route_utility import main
from furiosa_rag.route_gt_audit import alignment_outcome, write_csv
from furiosa_rag.route_utility import (
    build_utility_cases,
    efficiency_statistics,
    format_summary,
    load_csv,
    paired_correctness_rows,
    paired_correctness_statistics,
    summarize_utility,
)


@pytest.fixture
def inputs():
    # Deliberately asymmetric recalls: vision 2/3, text 1/2, overall decisive 3/5.
    specs = [
        (False, True, "VISUAL_REQUIRED", "text_only"),
        (False, True, "VISUAL_REQUIRED", "image_only"),
        (False, True, "TEXT_ONLY", "text_only"),
        (True, False, "TEXT_ONLY", "table_required"),
        (True, False, "VISUAL_REQUIRED", "image_plus_text_as_answer"),
        (True, True, "TEXT_ONLY", "text_only"),
        (True, True, "VISUAL_REQUIRED", "image_only"),
        (False, False, "TEXT_ONLY", "table_required"),
        (False, False, "VISUAL_REQUIRED", "image_plus_text_as_answer"),
    ]
    alignment, runs, judged = [], [], []
    for i, (text, vision, route, answer_type) in enumerate(specs):
        alignment.append(
            {
                "query_id": f"q{i}",
                "answer_type": answer_type,
                "text_correct": str(text),
                "vision_correct": str(vision),
                "outcome": alignment_outcome(text, vision),
                "text_e2e_latency_ms": "200",
                "vision_e2e_latency_ms": "300",
            }
        )
        runs.append(
            {
                "id": f"q{i}",
                "query_id": f"q{i}",
                "answer_type": answer_type,
                "predicted_route": route,
                "route": route,
                "strategy": "retrieval_aware_adaptive",
                "total_latency_ms": str(100 * (i + 1)),
                "routing_latency_ms": str(10 * (i + 1)),
                "retrieval_page_hit_3": "False" if i == 7 else "True",
                "page_hit_1": "False" if i == 8 else "",
            }
        )
        # RA answer correctness is deliberately independent of oracle route utility.
        judged.append({"query_id": f"q{i}", "judge_correct": str(i % 2 == 0)})
    return alignment, runs, judged


def test_decisions_and_regret(inputs):
    cases = build_utility_cases(*inputs)
    assert [row["route_utility_correct"] for row in cases] == [
        True,
        True,
        False,
        True,
        False,
        True,
        True,
        None,
        None,
    ]
    assert [row["id"] for row in cases if row["avoidable_regret"]] == ["q2", "q4"]
    assert [row["optimal_route"] for row in cases[5:]] == ["EITHER", "EITHER", "NEITHER", "NEITHER"]
    assert cases[2]["retrieval_aware_correct"] is True
    assert cases[3]["retrieval_aware_correct"] is False


def test_metrics_and_nondecisive_denominators(inputs):
    cases = build_utility_cases(*inputs)
    overall = summarize_utility(cases)[0]
    assert overall["count"] == 9
    assert overall["decisive_count"] == 5
    assert overall["decisive_route_accuracy"] == pytest.approx(3 / 5)
    assert overall["vision_selection_recall"] == pytest.approx(2 / 3)
    assert overall["text_selection_recall"] == 0.5
    assert overall["decisive_vision_precision"] == pytest.approx(2 / 3)
    assert overall["decisive_vision_f1"] == pytest.approx(2 / 3)
    assert overall["decisive_balanced_accuracy"] == pytest.approx(7 / 12)
    assert [
        overall[key]
        for key in (
            "optimal_visual_predicted_visual_count",
            "optimal_visual_predicted_text_count",
            "optimal_text_predicted_text_count",
            "optimal_text_predicted_visual_count",
        )
    ] == [2, 1, 1, 1]
    assert overall["avoidable_regret_count"] == 2
    assert overall["avoidable_regret_rate_overall"] == pytest.approx(2 / 9)
    assert overall["avoidable_regret_rate_decisive"] == 0.4
    assert overall["missed_needed_vision_count"] == overall["wrongly_used_vision_count"] == 1
    assert overall["ra_correctness"] == pytest.approx(5 / 9)
    assert overall["ra_avg_e2e_latency_ms"] == 500
    assert overall["ra_avg_routing_latency_ms"] == 50
    decisive_only = summarize_utility(cases[:5])[0]
    assert decisive_only["decisive_route_accuracy"] == overall["decisive_route_accuracy"]


def test_alignment_groups_and_page_hit_availability(inputs):
    summaries = {row["group"]: row for row in summarize_utility(build_utility_cases(*inputs))}
    both = summaries["BOTH_CORRECT"]
    assert both["count"] == 2
    assert both["ra_text_only_count"] == both["ra_visual_required_count"] == 1
    assert both["ra_text_only_rate"] == both["ra_visual_required_rate"] == 0.5
    assert both["unnecessary_vision_count"] == 1
    assert both["unnecessary_vision_rate"] == 0.5
    assert both["decisive_count"] == 0
    assert both["decisive_route_accuracy"] is None
    wrong = summaries["BOTH_WRONG"]
    assert wrong["count"] == 2
    assert wrong["ra_correctness"] == 0.5
    assert wrong["retrieval_page_hit_3_available_count"] == 2
    assert wrong["retrieval_page_hit_3_rate"] == 0.5
    assert wrong["visual_page_hit_1_available_count"] == 1
    assert wrong["visual_page_hit_1_rate"] == 0.0
    assert wrong["decisive_route_accuracy"] is None


def test_answer_type_is_only_a_grouping_key(inputs):
    summaries = {row["group"]: row for row in summarize_utility(build_utility_cases(*inputs))}
    expected = {
        "text_only": (2, 0.5, 2, 0.5, 0, None),
        "image_only": (1, 1.0, 1, 1.0, 0, None),
        "table_required": (1, 1.0, 0, None, 1, 1.0),
        "image_plus_text_as_answer": (1, 0.0, 0, None, 1, 0.0),
    }
    for answer_type, values in expected.items():
        row = summaries[answer_type]
        assert (
            tuple(
                row[key]
                for key in (
                    "decisive_count",
                    "decisive_route_accuracy",
                    "vision_needed_count",
                    "vision_selection_recall",
                    "text_only_better_count",
                    "text_selection_recall",
                )
            )
            == values
        )


def test_paired_correctness_statistics_are_row_level_and_deterministic(inputs):
    cases = build_utility_cases(*inputs)
    paired = paired_correctness_rows(cases)
    stats = paired_correctness_statistics(cases, bootstrap_seed=42, bootstrap_samples=1_000)

    assert len(paired) == 9
    assert stats["both_correct"] == 3
    assert stats["fv_only_correct"] == 2
    assert stats["ra_only_correct"] == 2
    assert stats["both_incorrect"] == 2
    assert stats["ra_minus_fv_difference"] == 0
    assert stats["mcnemar_exact_binomial_p"] == 1.0
    assert stats["mcnemar_continuity_corrected_p"] == 1.0
    assert paired_correctness_statistics(
        cases, bootstrap_seed=42, bootstrap_samples=1_000
    )["bootstrap_percentile_ci"] == stats["bootstrap_percentile_ci"]


def test_efficiency_statistics(inputs):
    stats = efficiency_statistics(build_utility_cases(*inputs))
    assert stats["forced_vision_correct_count"] == 5
    assert stats["retrieval_aware_correct_count"] == 5
    assert stats["retrieval_aware_vision_calls"] == 5
    assert stats["vision_calls_saved"] == 4
    assert stats["forced_vision_avg_e2e_latency_ms"] == 300
    assert stats["retrieval_aware_avg_e2e_latency_ms"] == 500
    assert stats["vision_calls_saved_per_fewer_correct_answer"] is None


@pytest.mark.parametrize("source", [0, 1, 2])
def test_duplicate_id_error(inputs, source):
    inputs[source].append(deepcopy(inputs[source][0]))
    with pytest.raises(ValueError, match="duplicate ID.*q0"):
        build_utility_cases(*inputs)


@pytest.mark.parametrize("source", [0, 1, 2])
def test_missing_id_value_error(inputs, source):
    inputs[source][0]["query_id"] = " "
    with pytest.raises(ValueError, match="missing ID"):
        build_utility_cases(*inputs)


@pytest.mark.parametrize("source", [0, 1, 2])
def test_missing_id_row_error(inputs, source):
    inputs[source].pop()
    with pytest.raises(ValueError, match="ID mismatch.*q8"):
        build_utility_cases(*inputs)


@pytest.mark.parametrize("source", [0, 1, 2])
def test_empty_input_error(inputs, source):
    inputs[source].clear()
    with pytest.raises(ValueError, match="empty input"):
        build_utility_cases(*inputs)


def test_row_order_independent_join_and_id_alias(inputs):
    expected = build_utility_cases(*inputs)
    inputs[0].reverse()
    inputs[1][:] = inputs[1][3:] + inputs[1][:3]
    inputs[2].reverse()
    for row in inputs[1]:
        del row["query_id"]
    assert build_utility_cases(*inputs) == expected


@pytest.mark.parametrize("value", [None, "", "null", "NaN", "N/A"])
def test_optional_page_hit_null(inputs, value):
    for row in inputs[1]:
        row["page_hit_1"] = value
        row["retrieval_page_hit_3"] = value
    summaries = summarize_utility(build_utility_cases(*inputs))
    for key in ("visual_page_hit_1", "retrieval_page_hit_3"):
        assert summaries[0][f"{key}_available_count"] == 0
        assert summaries[0][f"{key}_rate"] is None
    assert "N/A (0/0 available rows)" in format_summary(summaries)


def test_optional_page_hit_columns_absent_and_explicit_visual_alias(inputs):
    for row in inputs[1]:
        del row["page_hit_1"], row["retrieval_page_hit_3"]
    inputs[1][8]["visual_page_hit_1"] = "True"
    summary = summarize_utility(build_utility_cases(*inputs))[0]
    assert summary["retrieval_page_hit_3_rate"] is None
    assert summary["visual_page_hit_1_rate"] == 1.0
    assert summary["visual_page_hit_1_available_count"] == 1


@pytest.mark.parametrize(
    ("source", "field", "value", "message"),
    [
        (0, "outcome", "BOTH_CORRECT", "alignment outcome"),
        (0, "text_correct", "unknown", "expected boolean"),
        (0, "answer_type", "unknown", "invalid answer_type"),
        (1, "id", "different", "conflicting id/query_id"),
        (1, "route", "TEXT_ONLY", "conflicting route"),
        (1, "predicted_route", "unknown", "conflicting route"),
        (1, "strategy", "forced_text", "unexpected strategy"),
        (1, "answer_type", "image_only", "answer_type mismatch"),
        (1, "total_latency_ms", "nan", "finite nonnegative latency"),
        (1, "routing_latency_ms", "-1", "finite nonnegative latency"),
        (1, "routing_latency_ms", "", "finite nonnegative latency"),
        (1, "page_hit_1", "unknown", "expected boolean"),
        (1, "error", "inference failed", "error for ID"),
        (2, "error", "judge failed", "error for ID"),
        (2, "judge_correct", "", "expected boolean"),
        (2, "predicted_route", "TEXT_ONLY", "judged route mismatch"),
    ],
)
def test_invalid_or_inconsistent_data_error(inputs, source, field, value, message):
    inputs[source][0][field] = value
    with pytest.raises(ValueError, match=message):
        build_utility_cases(*inputs)


def test_judged_answer_must_match_run(inputs):
    inputs[1][0]["answer"] = "one answer"
    inputs[2][0]["answer"] = "different answer"
    with pytest.raises(ValueError, match="judged answer mismatch"):
        build_utility_cases(*inputs)


def test_empty_groups_and_no_predicted_vision(inputs):
    selected = [[rows[i] for i in (3, 5, 7)] for rows in inputs]
    summaries = summarize_utility(build_utility_cases(*selected))
    assert len(summaries) == 9
    overall = summaries[0]
    assert overall["decisive_route_accuracy"] == 1.0
    assert overall["vision_selection_recall"] is None
    assert overall["decisive_vision_precision"] is None
    assert overall["decisive_vision_f1"] is None
    assert overall["decisive_balanced_accuracy"] is None
    empty = next(row for row in summaries if row["group"] == "VISION_NEEDED")
    assert empty["count"] == 0
    assert empty["ra_correctness"] is None
    assert empty["ra_avg_e2e_latency_ms"] is None


def test_cli_writes_aggregates_and_optional_cases_offline(inputs, tmp_path, capsys, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("offline analysis must not access network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    paths = [tmp_path / f"input{i}.csv" for i in range(3)]
    for path, rows in zip(paths, inputs):
        write_csv(rows, path)
    snapshots = [path.read_bytes() for path in paths]
    output, cases = tmp_path / "aggregate.csv", tmp_path / "cases.csv"
    by_type = tmp_path / "by_type.csv"
    paired = tmp_path / "paired.csv"
    statistics = tmp_path / "statistics.json"
    args = [
        "--alignment",
        str(paths[0]),
        "--retrieval-aware",
        str(paths[1]),
        "--retrieval-aware-judged",
        str(paths[2]),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert not cases.exists()
    assert main(
        [
            *args,
            "--cases-output",
            str(cases),
            "--answer-type-output",
            str(by_type),
            "--paired-output",
            str(paired),
            "--statistics-output",
            str(statistics),
            "--bootstrap-samples",
            "100",
        ]
    ) == 0
    assert len(load_csv(output)) == 9
    assert len(load_csv(cases)) == 9
    assert len(load_csv(by_type)) == 4
    assert len(load_csv(paired)) == 9
    statistical_output = json.loads(statistics.read_text(encoding="utf-8"))
    assert statistical_output["paired_correctness"]["bootstrap_samples"] == 100
    assert statistical_output["efficiency"]["vision_calls_saved"] == 4
    assert load_csv(cases)[7]["route_utility_correct"] == ""
    assert [path.read_bytes() for path in paths] == snapshots
    stdout = capsys.readouterr().out
    assert "VISION_NEEDED vision-selection recall: 66.667%" in stdout
    assert "decisive_route_accuracy: 60.000% (3/5)" in stdout
    assert "runtime oracle knowledge" in stdout
    assert "BOTH_WRONG" in stdout


def test_cli_errors_before_writing_on_invalid_input(inputs, tmp_path, capsys):
    inputs[1].pop()
    paths = [tmp_path / f"input{i}.csv" for i in range(3)]
    for path, rows in zip(paths, inputs):
        write_csv(rows, path)
    output = tmp_path / "aggregate.csv"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--alignment",
                str(paths[0]),
                "--retrieval-aware",
                str(paths[1]),
                "--retrieval-aware-judged",
                str(paths[2]),
                "--output",
                str(output),
            ]
        )
    assert exc.value.code == 2
    assert "missing IDs=['q8']" in capsys.readouterr().err
    assert not output.exists()


def test_cli_rejects_input_overwrite(tmp_path, capsys):
    path = str(tmp_path / "input.csv")
    with pytest.raises(SystemExit) as exc:
        main(["--alignment", path, "--output", path])
    assert exc.value.code == 2
    assert "must not overwrite input" in capsys.readouterr().err


def test_cli_rejects_colliding_outputs(tmp_path, capsys):
    path = str(tmp_path / "output.csv")
    with pytest.raises(SystemExit) as exc:
        main(["--output", path, "--cases-output", path])
    assert exc.value.code == 2
    assert "must be distinct" in capsys.readouterr().err
