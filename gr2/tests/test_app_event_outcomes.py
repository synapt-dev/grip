from __future__ import annotations

from pathlib import Path

import pytest
from gr2.python_cli import app as app_module
from gr2.python_cli import events
from gr2.python_cli.events import EventEmitError


@pytest.mark.parametrize(
    "operation,event_type",
    [
        ("create", "lane.created"),
        ("enter", "lane.entered"),
        ("exit", "lane.exited"),
        ("lease_acquire", "lease.acquired"),
        ("lease_release", "lease.released"),
    ],
)
def test_sink_failure_cannot_replace_lane_or_lease_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    event_type: str,
) -> None:
    completed: list[str] = []
    monkeypatch.setattr(app_module, "_exit", lambda _result: completed.append(operation))
    monkeypatch.setattr(app_module, "_materialize_lane_repos", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module, "_run_lane_stage", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_module, "stash_if_dirty", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app_module.failures, "unresolved_lane_failure", lambda *_args: None)
    for name in (
        "create_lane",
        "enter_lane",
        "exit_lane",
        "acquire_lane_lease",
        "release_lane_lease",
    ):
        monkeypatch.setattr(app_module.lane_proto, name, lambda _args: 0)
    monkeypatch.setattr(
        app_module.lane_proto,
        "load_lane_doc",
        lambda *_args: {"type": "feature", "repos": []},
    )
    monkeypatch.setattr(
        app_module.lane_proto,
        "load_current_lane_doc",
        lambda *_args: {"current": {"lane_name": "feat/test"}},
    )
    monkeypatch.setattr(
        events,
        "emit",
        lambda **kwargs: (
            (_ for _ in ()).throw(EventEmitError("sink unavailable"))
            if kwargs["event_type"].value == event_type
            else None
        ),
    )

    if operation == "create":
        # a lane is made from a workspace spec; lane create refuses a root that has none
        (tmp_path / ".grip").mkdir()
        (tmp_path / ".grip" / "workspace_spec.toml").write_text(
            'schema_version = 1\nname = "t"\n\n[[repos]]\nname = "app"\npath = "repos/app"\n'
            'url = "https://example.invalid/app.git"\n\n[[units]]\nname = "atlas"\n'
            'path = "agents/atlas/home"\nrepos = ["app"]\n'
        )
        app_module.lane_create(
            tmp_path, "atlas", "feat/test", "app", "dev", "feature", "manual", [], False, None
        )
    elif operation == "enter":
        app_module.lane_enter(tmp_path, "atlas", "feat/test", "agent:atlas", False, False, False)
    elif operation == "exit":
        app_module.lane_exit(tmp_path, "atlas", "agent:atlas", False, False, False)
    elif operation == "lease_acquire":
        app_module.lane_lease_acquire(
            tmp_path, "atlas", "feat/test", "agent:atlas", "edit", 900, False
        )
    else:
        app_module.lane_lease_release(tmp_path, "atlas", "feat/test", "agent:atlas")

    assert completed == [operation]
