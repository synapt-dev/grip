"""Performance gates: measurement harness for overlay vs git baseline comparisons."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class PerfGateResult:
    gate: str
    baseline_seconds: float
    candidate_seconds: float
    ratio: float
    threshold: float
    passed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "baseline_seconds": self.baseline_seconds,
            "candidate_seconds": self.candidate_seconds,
            "ratio": self.ratio,
            "threshold": self.threshold,
            "passed": self.passed,
        }


class PerfGateFailure(Exception):
    def __init__(self, gate: str, ratio: float, threshold: float) -> None:
        super().__init__(
            f"Perf gate '{gate}' failed: ratio {ratio:.2f} exceeds threshold {threshold:.2f}"
        )
        self.gate = gate
        self.ratio = ratio
        self.threshold = threshold


def evaluate_activate_perf_gate(
    activate_op: Callable[[], None] | list[float],
    git_checkout_op: Callable[[], None] | list[float],
    samples: int,
    max_ratio: float,
    clock: Callable[[], float] | None = None,
) -> PerfGateResult:
    candidate = _collect_samples(activate_op, samples, clock)
    baseline = _collect_samples(git_checkout_op, samples, clock)

    candidate_median = statistics.median(candidate)
    baseline_median = statistics.median(baseline)
    ratio = candidate_median / baseline_median

    return PerfGateResult(
        gate="activate_vs_git_checkout_single_file",
        baseline_seconds=baseline_median,
        candidate_seconds=candidate_median,
        ratio=ratio,
        threshold=max_ratio,
        passed=ratio < max_ratio,
    )


def evaluate_status_diff_perf_gates(
    overlay_status_op: Callable[[], None] | list[float],
    git_status_op: Callable[[], None] | list[float],
    overlay_diff_op: Callable[[], None] | list[float],
    git_diff_op: Callable[[], None] | list[float],
    samples: int,
    max_ratio: float,
    clock: Callable[[], float] | None = None,
) -> dict[str, PerfGateResult]:
    status_candidate = _collect_samples(overlay_status_op, samples, clock)
    status_baseline = _collect_samples(git_status_op, samples, clock)
    status_c = statistics.median(status_candidate)
    status_b = statistics.median(status_baseline)
    status_ratio = status_c / status_b

    diff_candidate = _collect_samples(overlay_diff_op, samples, clock)
    diff_baseline = _collect_samples(git_diff_op, samples, clock)
    diff_c = statistics.median(diff_candidate)
    diff_b = statistics.median(diff_baseline)
    diff_ratio = diff_c / diff_b

    return {
        "status_vs_git_status": PerfGateResult(
            gate="status_vs_git_status",
            baseline_seconds=status_b,
            candidate_seconds=status_c,
            ratio=status_ratio,
            threshold=max_ratio,
            passed=status_ratio < max_ratio,
        ),
        "diff_vs_git_diff": PerfGateResult(
            gate="diff_vs_git_diff",
            baseline_seconds=diff_b,
            candidate_seconds=diff_c,
            ratio=diff_ratio,
            threshold=max_ratio,
            passed=diff_ratio < max_ratio,
        ),
    }


def assert_perf_gates(results: list[PerfGateResult]) -> None:
    for result in results:
        if not result.passed:
            raise PerfGateFailure(
                gate=result.gate,
                ratio=result.ratio,
                threshold=result.threshold,
            )


def _collect_samples(
    op: Callable[[], None] | list[float],
    count: int,
    clock: Callable[[], float] | None,
) -> list[float]:
    if isinstance(op, list):
        return op

    samples: list[float] = []
    for _ in range(count):
        start = clock()
        op()
        end = clock()
        samples.append(end - start)
    return samples
