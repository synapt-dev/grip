from __future__ import annotations

from gr2_overlay.perf import evaluate_cross_repo_perf_gate


def test_cross_repo_perf_gate_passes_when_apply_stays_within_git_baseline_budget() -> None:
    result = evaluate_cross_repo_perf_gate(
        apply_samples_ms=[98.0, 101.0, 100.0],
        baseline_samples_ms=[50.0, 50.0, 49.0],
        baseline_command="git checkout -- app/settings.toml docs/COMPOSE.md",
        ratio_gate=2.1,
        sample_count=3,
        repo_count=2,
    )

    assert result.status == "ok"
    assert result.error_code == ""
    assert result.repo_count == 2
    assert result.sample_count == 3
    assert result.baseline_command == "git checkout -- app/settings.toml docs/COMPOSE.md"
    assert result.apply_ratio < 2.1


def test_cross_repo_perf_gate_fails_when_apply_ratio_exceeds_budget() -> None:
    result = evaluate_cross_repo_perf_gate(
        apply_samples_ms=[130.0, 132.0, 131.0],
        baseline_samples_ms=[50.0, 50.0, 50.0],
        baseline_command="git checkout -- app/settings.toml docs/COMPOSE.md",
        ratio_gate=2.0,
        sample_count=3,
        repo_count=2,
    )

    assert result.status == "degraded"
    assert result.error_code == "cross_repo_perf_gate_failed"
    assert result.apply_ratio > 2.0


def test_cross_repo_perf_gate_rejects_invalid_or_gameable_sample_envelopes() -> None:
    invalid_cases = [
        {
            "apply_samples_ms": [],
            "baseline_samples_ms": [50.0, 50.0],
            "baseline_command": "git checkout -- app/settings.toml docs/COMPOSE.md",
            "ratio_gate": 2.0,
            "sample_count": 2,
            "repo_count": 2,
        },
        {
            "apply_samples_ms": [100.0],
            "baseline_samples_ms": [50.0, 50.0],
            "baseline_command": "git checkout -- app/settings.toml docs/COMPOSE.md",
            "ratio_gate": 2.0,
            "sample_count": 2,
            "repo_count": 2,
        },
        {
            "apply_samples_ms": [100.0, -1.0],
            "baseline_samples_ms": [50.0, 50.0],
            "baseline_command": "git checkout -- app/settings.toml docs/COMPOSE.md",
            "ratio_gate": 2.0,
            "sample_count": 2,
            "repo_count": 2,
        },
        {
            "apply_samples_ms": [100.0, 101.0],
            "baseline_samples_ms": [50.0, 50.0],
            "baseline_command": "",
            "ratio_gate": 2.0,
            "sample_count": 2,
            "repo_count": 2,
        },
        {
            "apply_samples_ms": [100.0, 101.0],
            "baseline_samples_ms": [50.0, 50.0],
            "baseline_command": "git checkout -- app/settings.toml docs/COMPOSE.md",
            "ratio_gate": 2.0,
            "sample_count": 2,
            "repo_count": 0,
        },
    ]

    for case in invalid_cases:
        try:
            evaluate_cross_repo_perf_gate(**case)
        except ValueError:
            continue
        raise AssertionError(f"expected invalid perf envelope to be rejected: {case}")
