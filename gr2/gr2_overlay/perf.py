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


@dataclass
class TierBPerfGateResult:
    status: str
    error_code: str
    capture_ratio: float
    apply_ratio: float
    sample_count: int
    capture_baseline_command: str
    apply_baseline_command: str


def evaluate_tier_b_perf_gate(
    *,
    capture_samples_ms: list[float],
    capture_baseline_samples_ms: list[float],
    apply_samples_ms: list[float],
    apply_baseline_samples_ms: list[float],
    capture_baseline_command: str,
    apply_baseline_command: str,
    ratio_gate: float,
    sample_count: int,
) -> TierBPerfGateResult:
    all_lists = [
        capture_samples_ms,
        capture_baseline_samples_ms,
        apply_samples_ms,
        apply_baseline_samples_ms,
    ]
    if any(not s for s in all_lists):
        raise ValueError(f"sample_count {sample_count}: all sample lists must be non-empty")
    if any(len(s) != sample_count for s in all_lists):
        raise ValueError(
            f"sample_count {sample_count}: all sample lists must match, "
            f"got lengths {[len(s) for s in all_lists]}"
        )
    for samples in all_lists:
        for v in samples:
            if v <= 0:
                raise ValueError(f"All durations must be positive, got {v}")

    capture_ratio = statistics.median(capture_samples_ms) / statistics.median(
        capture_baseline_samples_ms
    )
    apply_ratio = statistics.median(apply_samples_ms) / statistics.median(apply_baseline_samples_ms)

    if capture_ratio >= ratio_gate or apply_ratio >= ratio_gate:
        status = "degraded"
        error_code = "tier_b_perf_gate_failed"
    else:
        status = "ok"
        error_code = ""

    return TierBPerfGateResult(
        status=status,
        error_code=error_code,
        capture_ratio=capture_ratio,
        apply_ratio=apply_ratio,
        sample_count=sample_count,
        capture_baseline_command=capture_baseline_command,
        apply_baseline_command=apply_baseline_command,
    )


@dataclass
class CrossRepoPerfGateResult:
    status: str
    error_code: str
    apply_ratio: float
    sample_count: int
    repo_count: int
    baseline_command: str


def evaluate_cross_repo_perf_gate(
    *,
    apply_samples_ms: list[float],
    baseline_samples_ms: list[float],
    baseline_command: str,
    ratio_gate: float,
    sample_count: int,
    repo_count: int,
) -> CrossRepoPerfGateResult:
    if repo_count <= 0:
        raise ValueError(f"repo_count must be positive, got {repo_count}")
    if not baseline_command:
        raise ValueError("baseline_command must not be empty")

    all_lists = [apply_samples_ms, baseline_samples_ms]
    if any(not s for s in all_lists):
        raise ValueError(f"sample_count {sample_count}: all sample lists must be non-empty")
    if any(len(s) != sample_count for s in all_lists):
        raise ValueError(
            f"sample_count {sample_count}: all sample lists must match, "
            f"got lengths {[len(s) for s in all_lists]}"
        )
    for samples in all_lists:
        for v in samples:
            if v <= 0:
                raise ValueError(f"All durations must be positive, got {v}")

    apply_ratio = statistics.median(apply_samples_ms) / statistics.median(baseline_samples_ms)

    if apply_ratio >= ratio_gate:
        status = "degraded"
        error_code = "cross_repo_perf_gate_failed"
    else:
        status = "ok"
        error_code = ""

    return CrossRepoPerfGateResult(
        status=status,
        error_code=error_code,
        apply_ratio=apply_ratio,
        sample_count=sample_count,
        repo_count=repo_count,
        baseline_command=baseline_command,
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
