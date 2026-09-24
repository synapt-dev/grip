"""Mutation locks for the event/outcome classification in grip#844."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest
from gr2.python_cli.events import EventEmitError, EventType, emit_after_outcome

ROOT = Path(__file__).parents[1] / "python_cli"


def _calls(path: Path) -> list[tuple[str, str, str | None, bool]]:
    tree = ast.parse(path.read_text())
    rows: list[tuple[str, str, str | None, bool]] = []
    functions = (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    for function in functions:
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Name) or call.func.id not in {
                "emit",
                "emit_after_outcome",
                "_emit_sync_event",
            }:
                continue
            event: str | None = None
            if (
                call.func.id == "_emit_sync_event"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Dict)
            ):
                for key, value in zip(call.args[1].keys, call.args[1].values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "type"
                        and isinstance(value, ast.Constant)
                    ):
                        event = str(value.value)
                    elif (
                        isinstance(key, ast.Constant)
                        and key.value == "type"
                        and isinstance(value, ast.IfExp)
                        and isinstance(value.body, ast.Constant)
                        and isinstance(value.orelse, ast.Constant)
                    ):
                        event = f"{value.body.value}|{value.orelse.value}"
            else:
                for keyword in call.keywords:
                    if keyword.arg == "event_type" and isinstance(keyword.value, ast.Attribute):
                        event = keyword.value.attr
            after = any(
                keyword.arg == "after_outcome"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in call.keywords
            )
            rows.append((function.name, call.func.id, event, after))
    return rows


def test_every_direct_site_has_the_classified_policy() -> None:
    actual: Counter[tuple[str, str, str | None, bool]] = Counter()
    for name in ("hooks.py", "spec_apply.py", "execops.py", "failures.py", "pr.py", "app.py"):
        actual.update(_calls(ROOT / name))

    expected = Counter(
        {
            ("run_lifecycle_stage", "emit", "HOOK_SKIPPED", False): 1,
            ("run_lifecycle_stage", "emit", "HOOK_STARTED", False): 1,
            ("run_lifecycle_stage", "emit_after_outcome", "HOOK_COMPLETED", False): 1,
            ("run_lifecycle_stage", "emit_after_outcome", "HOOK_FAILED", False): 1,
            ("_emit_projected", "emit_after_outcome", "WORKSPACE_FILE_PROJECTED", False): 1,
            ("apply_plan", "emit_after_outcome", "WORKSPACE_MATERIALIZED", False): 1,
            ("run_exec", "emit_after_outcome", "EXEC_STARTED", False): 1,
            ("run_exec", "emit_after_outcome", "EXEC_COMPLETED", False): 1,
            ("run_exec", "emit_after_outcome", "EXEC_FAILED", False): 1,
            ("resolve_failure_marker", "emit_after_outcome", "FAILURE_RESOLVED", False): 1,
            ("create_pr_group", "emit_after_outcome", "PR_CREATED", False): 1,
            ("merge_pr_group", "emit_after_outcome", "PR_MERGED", False): 1,
            ("_record_merge_failure", "emit", "PR_MERGE_FAILED", False): 1,
            ("check_pr_group_status", "emit", "PR_STATUS_CHANGED", False): 1,
            ("check_pr_group_status", "emit", "PR_CHECKS_FAILED", False): 1,
            ("check_pr_group_status", "emit", "PR_CHECKS_PASSED", False): 1,
            ("record_pr_review", "emit", "PR_REVIEW_SUBMITTED", False): 1,
            ("lane_create", "emit_after_outcome", "LANE_CREATED", False): 1,
            ("lane_enter", "emit_after_outcome", "LANE_ENTERED", False): 1,
            ("lane_exit", "emit_after_outcome", "LANE_EXITED", False): 1,
            ("lane_lease_acquire", "emit_after_outcome", "LEASE_ACQUIRED", False): 1,
            ("lane_lease_release", "emit_after_outcome", "LEASE_RELEASED", False): 1,
        }
    )
    assert actual == expected


def test_every_dynamic_sync_site_has_the_classified_policy() -> None:
    rows = [row for row in _calls(ROOT / "syncops.py") if row[1] == "_emit_sync_event"]
    actual = Counter((function, event, after) for function, _call, event, after in rows)
    expected = Counter(
        {
            ("_execute_operation", "sync.cache_seeded|sync.cache_refreshed", True): 1,
            ("_execute_operation", "sync.repo_updated", True): 2,
            ("_execute_operation", "sync.repo_fetched", True): 1,
            ("_execute_operation", "sync.repo_skipped", True): 2,
            ("run_sync", "sync.conflict", False): 3,
            ("run_sync", "sync.completed", False): 2,
            ("run_sync", "sync.started", False): 1,
            ("run_sync", "sync.completed", True): 1,
        }
    )
    assert actual == expected


def test_after_outcome_reports_sink_failure_without_replacing_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gr2.python_cli import events

    monkeypatch.setattr(
        events,
        "emit",
        lambda **_kwargs: (_ for _ in ()).throw(EventEmitError("sink unavailable")),
    )
    assert (
        emit_after_outcome(
            EventType.EXEC_COMPLETED,
            tmp_path,
            "system",
            "workspace",
            {"status": "success"},
        )
        is None
    )
    assert "could not record exec.completed" in capsys.readouterr().err


def test_after_outcome_does_not_swallow_non_sink_defects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gr2.python_cli import events

    monkeypatch.setattr(events, "emit", lambda **_kwargs: (_ for _ in ()).throw(ValueError("bug")))
    with pytest.raises(ValueError, match="bug"):
        emit_after_outcome(EventType.EXEC_COMPLETED, tmp_path, "system", "workspace", {})
