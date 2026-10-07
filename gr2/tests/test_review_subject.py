"""Subject resolution shares the native bind selector and record reader."""
import importlib
import json

import pytest
import typer

from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401

app_module = importlib.import_module("gr2.python_cli.app")


@pytest.fixture
def bound_subject(two_member_ws, tmp_path):
    ws = two_member_ws
    rc, output = _cli("store", "init", str(ws))
    assert rc == 0, output
    rows = []
    for name in ("alpha", "beta"):
        repo = ws / name
        base = _git_out(repo, "rev-parse", "HEAD")
        (repo / "subject.txt").write_text(name + " reviewed\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "reviewed subject")
        rows.append(dict(
            key=name, path=name, remote=_git_out(repo, "remote", "get-url", "origin"),
            base=base, head=_git_out(repo, "rev-parse", "HEAD"), ref="refs/heads/main",
            source=str(repo), title=name,
        ))
    rowfile = tmp_path / "subject-rows.json"
    rowfile.write_text(json.dumps(rows))
    rc, output = _cli("review", "bind", str(ws), "--rows-json", str(rowfile))
    assert rc == 0, output
    return ws, output.strip(), rows, rowfile


def test_subject_maps_reviewed_heads_and_preserves_explicit_target(bound_subject, capsys):
    ws, target, rows, _ = bound_subject
    expected = (target, [
        {**{key: row[key] for key in ("key", "path", "remote", "base")}, "commit": row["head"]}
        for row in rows
    ])
    assert rows[0]["head"] != rows[1]["head"]
    assert all(row["head"] != row["base"] for row in rows)
    assert app_module.resolve_review_subject(ws) == expected
    assert "the only review bind" in capsys.readouterr().err
    for explicit in (target, target[3:]):
        assert app_module.resolve_review_subject(ws, explicit) == expected
        assert "the only review bind" not in capsys.readouterr().err
    # A live checkout may move after the bind. Subject facts stay pinned to its record.
    _git(ws / "alpha", "commit", "--allow-empty", "-qm", "after review")
    assert _git_out(ws / "alpha", "rev-parse", "HEAD") != rows[0]["head"]
    assert app_module.resolve_review_subject(ws, target) == expected


@pytest.mark.parametrize("explicit", ["", "not-a-bind", "gr:not-a-bind", "gr:" + "0" * 40])
def test_invalid_explicit_subject_never_falls_back(bound_subject, capsys, explicit):
    ws, _, _, _ = bound_subject
    with pytest.raises(typer.Exit) as error:
        app_module.resolve_review_subject(ws, explicit)
    assert error.value.exit_code == 2
    diagnostic = capsys.readouterr().err
    assert "not_bound:" in diagnostic
    assert "the only review bind" not in diagnostic


def test_subject_without_bind_names_missing_input(two_member_ws):
    rc, output = _cli("store", "init", str(two_member_ws))
    assert rc == 0, output
    with pytest.raises(typer.BadParameter, match="no review bind"):
        app_module.resolve_review_subject(two_member_ws)


def test_subject_with_multiple_binds_refuses_but_explicit_still_selects(bound_subject):
    ws, target, rows, rowfile = bound_subject
    expected = app_module.resolve_review_subject(ws, target)
    rows[0]["title"] = "second subject"
    rowfile.write_text(json.dumps(rows))
    rc, second = _cli("review", "bind", str(ws), "--rows-json", str(rowfile))
    assert rc == 0 and second.strip() != target, second
    with pytest.raises(typer.BadParameter, match="2 review binds exist") as error:
        app_module.resolve_review_subject(ws)
    assert target in str(error.value) and second.strip() in str(error.value)
    assert app_module.resolve_review_subject(ws, target) == expected
