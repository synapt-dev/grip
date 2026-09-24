from __future__ import annotations

from pathlib import Path

import pytest
from gr2.python_cli import clone_exec, events, spec_apply
from gr2.python_cli.events import EventEmitError
from gr2.python_cli.spec_apply import PlanOperation


@pytest.mark.parametrize("victim", ["workspace.file_projected", "workspace.materialized"])
def test_sink_failure_cannot_replace_materialization_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    victim: str,
) -> None:
    repo_root = tmp_path / "repos" / "app"
    spec = {"repos": [{"name": "app", "path": "repos/app", "url": "file:///app"}]}
    operation = PlanOperation(
        kind="clone_repo",
        subject="app",
        target_path=str(repo_root),
        reason="missing",
        details={},
    )
    monkeypatch.setattr(spec_apply, "build_plan", lambda _root: (spec, [operation]))
    # apply_plan routes both clone sites through clone_exec.clone_and_pin,
    # imported function-locally because clone_exec imports spec_apply at module
    # scope -- so the seam to patch is the DEFINING module, not this one.
    monkeypatch.setattr(clone_exec, "clone_and_pin", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        spec_apply,
        "_run_materialize_hooks",
        lambda *_args, **_kwargs: {
            "projected_files": [{"kind": "copy", "src": "source", "dest": "destination"}]
        },
    )
    real_emit = events.emit

    def fail_victim(**kwargs):
        if kwargs["event_type"].value == victim:
            raise EventEmitError("sink unavailable")
        return real_emit(**kwargs)

    monkeypatch.setattr(events, "emit", fail_victim)
    result = spec_apply.apply_plan(tmp_path, yes=True)

    assert result["operation_count"] == 1
    assert result["applied"] == [f"cloned repo 'app' into {repo_root}"]
