from __future__ import annotations

from gr2_overlay.perf import evaluate_tier_b_perf_gate


def test_tier_b_perf_gate_enforces_capture_and_apply_ratio_against_git_baselines() -> None:
    result = evaluate_tier_b_perf_gate(
        capture_samples_ms=[184.0, 179.0, 181.0, 183.0, 180.0],
        capture_baseline_samples_ms=[95.0, 94.0, 96.0, 95.0, 94.0],
        apply_samples_ms=[191.0, 188.0, 190.0, 189.0, 192.0],
        apply_baseline_samples_ms=[98.0, 97.0, 96.0, 99.0, 98.0],
        capture_baseline_command="git stash push --keep-index",
        apply_baseline_command="git stash apply --index",
        ratio_gate=2.0,
        sample_count=5,
    )

    assert result.status == "ok"
    assert result.capture_ratio < 2.0
    assert result.apply_ratio < 2.0
    assert result.sample_count == 5
    assert result.capture_baseline_command == "git stash push --keep-index"
    assert result.apply_baseline_command == "git stash apply --index"


def test_tier_b_perf_gate_rejects_mismatched_or_empty_sample_envelopes() -> None:
    try:
        evaluate_tier_b_perf_gate(
            capture_samples_ms=[181.0, 183.0],
            capture_baseline_samples_ms=[95.0],
            apply_samples_ms=[],
            apply_baseline_samples_ms=[97.0, 98.0],
            capture_baseline_command="git stash push --keep-index",
            apply_baseline_command="git stash apply --index",
            ratio_gate=2.0,
            sample_count=2,
        )
    except ValueError as exc:
        assert "sample_count" in str(exc)
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected mismatched and empty perf sample envelopes to be rejected")


def test_tier_b_perf_gate_fails_when_overlay_ratio_exceeds_anchor() -> None:
    result = evaluate_tier_b_perf_gate(
        capture_samples_ms=[260.0, 258.0, 261.0, 259.0, 262.0],
        capture_baseline_samples_ms=[100.0, 101.0, 99.0, 100.0, 100.0],
        apply_samples_ms=[230.0, 229.0, 231.0, 232.0, 228.0],
        apply_baseline_samples_ms=[100.0, 101.0, 99.0, 100.0, 100.0],
        capture_baseline_command="git stash push --keep-index",
        apply_baseline_command="git stash apply --index",
        ratio_gate=2.0,
        sample_count=5,
    )

    assert result.status == "degraded"
    assert result.error_code == "tier_b_perf_gate_failed"
    assert result.capture_ratio > 2.0
    assert result.apply_ratio > 2.0


def test_tier_b_perf_gate_rejects_non_positive_durations_and_baselines() -> None:
    try:
        evaluate_tier_b_perf_gate(
            capture_samples_ms=[181.0, -1.0, 182.0],
            capture_baseline_samples_ms=[95.0, 0.0, 96.0],
            apply_samples_ms=[189.0, 190.0, 191.0],
            apply_baseline_samples_ms=[97.0, 98.0, 99.0],
            capture_baseline_command="git stash push --keep-index",
            apply_baseline_command="git stash apply --index",
            ratio_gate=2.0,
            sample_count=3,
        )
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("expected non-positive perf samples to be rejected")
