from __future__ import annotations

import pytest

from gr2_overlay.perf import (
    PerfGateFailure,
    PerfGateResult,
    assert_perf_gates,
    evaluate_activate_perf_gate,
    evaluate_status_diff_perf_gates,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def now(self) -> float:
        return self.current

    def op(self, elapsed: float):
        def _run() -> None:
            self.current += elapsed

        return _run


def test_activate_perf_gate_passes_when_within_two_x_git_checkout_anchor() -> None:
    clock = FakeClock()

    result = evaluate_activate_perf_gate(
        activate_op=clock.op(0.18),
        git_checkout_op=clock.op(0.10),
        samples=5,
        max_ratio=2.0,
        clock=clock.now,
    )

    assert result.gate == "activate_vs_git_checkout_single_file"
    assert result.passed is True
    assert result.threshold == 2.0
    assert result.ratio == pytest.approx(1.8)
    assert result.candidate_seconds == pytest.approx(0.18)
    assert result.baseline_seconds == pytest.approx(0.10)


def test_activate_perf_gate_fails_when_overlay_apply_exceeds_two_x_anchor() -> None:
    clock = FakeClock()

    result = evaluate_activate_perf_gate(
        activate_op=clock.op(0.26),
        git_checkout_op=clock.op(0.10),
        samples=5,
        max_ratio=2.0,
        clock=clock.now,
    )

    assert result.passed is False
    assert result.ratio == pytest.approx(2.6)

    with pytest.raises(PerfGateFailure) as exc:
        assert_perf_gates([result])

    assert exc.value.gate == "activate_vs_git_checkout_single_file"
    assert exc.value.ratio == pytest.approx(2.6)
    assert exc.value.threshold == 2.0


def test_status_and_diff_perf_gates_compare_against_git_baselines() -> None:
    clock = FakeClock()

    gates = evaluate_status_diff_perf_gates(
        overlay_status_op=clock.op(0.08),
        git_status_op=clock.op(0.05),
        overlay_diff_op=clock.op(0.09),
        git_diff_op=clock.op(0.06),
        samples=5,
        max_ratio=2.0,
        clock=clock.now,
    )

    status_gate = gates["status_vs_git_status"]
    diff_gate = gates["diff_vs_git_diff"]

    assert status_gate.passed is True
    assert status_gate.ratio == pytest.approx(1.6)
    assert diff_gate.passed is True
    assert diff_gate.ratio == pytest.approx(1.5)


def test_perf_gates_use_median_sample_to_reduce_single_run_noise() -> None:
    baseline_samples = [0.10, 0.10, 0.10, 0.10, 0.10]
    candidate_samples = [0.15, 0.16, 0.45, 0.15, 0.16]

    result = evaluate_activate_perf_gate(
        activate_op=candidate_samples,
        git_checkout_op=baseline_samples,
        samples=5,
        max_ratio=2.0,
    )

    assert result.baseline_seconds == pytest.approx(0.10)
    assert result.candidate_seconds == pytest.approx(0.16)
    assert result.ratio == pytest.approx(1.6)
    assert result.passed is True


def test_perf_gate_result_is_machine_readable_for_ceremony_regressions() -> None:
    result = PerfGateResult(
        gate="activate_vs_git_checkout_single_file",
        baseline_seconds=0.10,
        candidate_seconds=0.18,
        ratio=1.8,
        threshold=2.0,
        passed=True,
    )

    assert result.to_json() == {
        "gate": "activate_vs_git_checkout_single_file",
        "baseline_seconds": 0.10,
        "candidate_seconds": 0.18,
        "ratio": 1.8,
        "threshold": 2.0,
        "passed": True,
    }
