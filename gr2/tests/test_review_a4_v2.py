"""Inferred review selection and supplied options have executable refusal witnesses."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401
from tests.test_review_a4 import _flat


def _invoke(*args: str):
    return make_cli_runner().invoke(app, list(args))


def _refs(ws: Path) -> str:
    return _git_out(ws, "for-each-ref", "--format=%(refname) %(objectname)")


def _init(ws: Path) -> None:
    code, out = _cli("store", "init", str(ws))
    assert code == 0, out
    print(f"SUBJECT root={ws} alpha={_git_out(ws / 'alpha', 'rev-parse', 'HEAD')}")


@pytest.mark.parametrize("flag,value", [
    ("title", "MY TITLE"), ("title", ""), ("body", "MY BODY"), ("body", ""),
    ("path", "other-path"), ("ref", "refs/heads/other"), ("ref", "refs/heads/dev"),
])
def test_typed_inferred_flag_refuses(two_member_ws: Path, flag: str, value: str) -> None:
    ws = two_member_ws
    _init(ws)
    _unpushed_head(ws)
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws), f"--{flag}", value)
    assert result.exit_code == 2, result.output
    message = _flat(result.stderr)
    assert f"--{flag}" in message and "explicit row" in message and "--rows-json" in message
    assert result.stdout == "" and "Traceback" not in result.stderr
    assert _refs(ws) == before


def test_all_typed_inferred_flags_refuse(two_member_ws: Path) -> None:
    ws = two_member_ws
    _init(ws)
    _unpushed_head(ws)
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws), "--title", "X", "--body", "Y",
                     "--path", "Z", "--ref", "refs/heads/dev")
    assert result.exit_code == 2
    message = _flat(result.stderr)
    assert all(f"--{flag}" in message for flag in ("title", "body", "path", "ref"))
    assert result.stdout == "" and _refs(ws) == before


@pytest.mark.parametrize("flag,value", [
    ("title", "MY TITLE"), ("body", "MY BODY"), ("path", "custom-path"), ("ref", "refs/heads/main"),
])
def test_explicit_row_consumes_typed_flag(two_member_ws: Path, flag: str, value: str) -> None:
    ws = two_member_ws
    _init(ws)
    remote, base, head = _unpushed_head(ws)
    args = list(_bind_args(ws, remote, base, head))
    # Replace an existing option rather than rely on duplicate-option parsing.
    if f"--{flag}" in args:
        args[args.index(f"--{flag}") + 1] = value
    else:
        args.extend([f"--{flag}", value])
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    gr_id = result.stdout.strip()
    assert gr_id.startswith("gr:")
    shown = _invoke("review", "show", str(ws), gr_id, "--json")
    assert shown.exit_code == 0, shown.output
    row = json.loads(shown.stdout)["members"][0]
    if flag == "ref":
        # The bind validates the target ref live, but does not store ref in its repo fields.
        before = _refs(ws)
        args[args.index("--ref") + 1] = "refs/heads/missing"
        refused = _invoke(*args)
        assert refused.exit_code != 0 and "base_not_live_head" in refused.stderr
        assert _refs(ws) == before
    else:
        assert row[flag].rstrip("\n") == value


@pytest.mark.parametrize("source_flag", ["--source", "--from-range"])
def test_source_without_row_with_title_refuses(two_member_ws: Path, source_flag: str) -> None:
    ws = two_member_ws
    _init(ws)
    before = _refs(ws)
    source = ws / "alpha" if source_flag == "--source" else ws / "range.patch"
    if source_flag == "--from-range":
        source.write_text("")
    result = _invoke("review", "bind", str(ws), source_flag, str(source), "--title", "X")
    assert result.exit_code == 2
    message = _flat(result.stderr)
    assert "--repo" in message and "--remote" in message and "--base" in message and "--head" in message
    assert "required without --rows-json" in message
    assert result.stdout == "" and _refs(ws) == before


def _assert_only_bind_stdout(result) -> str:
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("gr:") and len(lines[0]) == 43
    return lines[0]


def test_ahead_names_at_pin_on_stderr_and_stdout_only_id(two_member_ws: Path) -> None:
    ws = two_member_ws
    _init(ws)
    _, base, head = _unpushed_head(ws)
    assert _git(ws / "alpha", "merge-base", "--is-ancestor", base, head).returncode == 0
    result = _invoke("review", "bind", str(ws))
    gr_id = _assert_only_bind_stdout(result)
    assert "beta (checkout is at the workspace's pin)" in result.stderr
    assert "gr2: pin alpha " in result.stderr
    shown = _invoke("review", "show", str(ws), gr_id, "--json")
    [row] = json.loads(shown.stdout)["members"]
    assert (row["key"], row["base"], row["head"]) == ("alpha", base, head)
    verified = _invoke("review", "verify", str(ws), gr_id, "--json")
    assert verified.exit_code == 0 and json.loads(verified.stdout)["tree_matches"] is True


def _orphan(checkout: Path) -> str:
    _git(checkout, "checkout", "--orphan", "unrelated")
    _git(checkout, "rm", "-rf", ".")
    (checkout / "other.txt").write_text("other history\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "unrelated")
    return _git_out(checkout, "rev-parse", "HEAD")


@pytest.mark.parametrize("kind", ["orphan", "behind"])
def test_non_descendant_refuses(two_member_ws: Path, kind: str) -> None:
    ws = two_member_ws
    if kind == "behind":
        # Record a pin that has a parent, and leave the checkout behind that pin.
        clone = ws / "alpha"
        (clone / "next.txt").write_text("next\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-m", "next")
        _git(clone, "push", "origin", "main")
    _init(ws)
    pin = _git_out(ws / "alpha", "rev-parse", "HEAD")
    if kind == "orphan":
        head = _orphan(ws / "alpha")
    else:
        _git(ws / "alpha", "checkout", "HEAD^")
        head = _git_out(ws / "alpha", "rev-parse", "HEAD")
    print(f"SUBJECT kind={kind} pin={pin} head={head}")
    assert _git(ws / "alpha", "merge-base", "--is-ancestor", pin, head, check=False).returncode == 1
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws))
    assert result.exit_code == 2, result.output
    message = _flat(result.stderr)
    assert "nothing to pin" in message and "alpha (checkout does not descend from the workspace's pin)" in message
    assert result.stdout == "" and _refs(ws) == before


def test_ancestry_error_not_selected(two_member_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = two_member_ws
    _init(ws)
    _unpushed_head(ws)
    calls = []
    def broken(cwd, ancestor, descendant, **kwargs):
        calls.append((cwd, ancestor, descendant))
        return subprocess.CompletedProcess(["git", "merge-base", "--is-ancestor"], 128, "", "ancestry unavailable")
    monkeypatch.setattr(gitops, "ancestry_proc", broken)
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws))
    assert len(calls) == 1
    assert result.exit_code == 2, result.output
    assert "alpha (ancestry could not be read)" in _flat(result.stderr)
    assert result.stdout == "" and _refs(ws) == before


def test_mixed_names_orphan_on_stderr_and_stdout_only_id(two_member_ws: Path) -> None:
    ws = two_member_ws
    _init(ws)
    _, base, head = _unpushed_head(ws)
    beta_pin = _git_out(ws / "beta", "rev-parse", "HEAD")
    beta_head = _orphan(ws / "beta")
    assert _git(ws / "beta", "merge-base", "--is-ancestor", beta_pin, beta_head, check=False).returncode == 1
    result = _invoke("review", "bind", str(ws))
    gr_id = _assert_only_bind_stdout(result)
    assert "beta (checkout does not descend from the workspace's pin)" in result.stderr
    shown = _invoke("review", "show", str(ws), gr_id, "--json")
    [row] = json.loads(shown.stdout)["members"]
    assert (row["key"], row["base"], row["head"]) == ("alpha", base, head)


def _beta_ahead(ws: Path) -> str:
    beta = ws / "beta"
    (beta / "beta-change.txt").write_text("beta under review\n")
    _git(beta, "add", ".")
    _git(beta, "commit", "-q", "-m", "beta's half of the change")
    return _git_out(beta, "rev-parse", "HEAD")


@pytest.mark.parametrize("broken_member", [None, "beta"])
def test_unreadable_ancestry_refuses_the_whole_set(two_member_ws: Path, monkeypatch: pytest.MonkeyPatch,
                                                   broken_member: str | None) -> None:
    """A changed member whose ancestry cannot be read refuses the pin, naming it, and mints no id.

    Both members carry a change. The control (nothing broken) binds both rows. With beta's ancestry unreadable,
    the old path bound alpha alone and printed an id for a partial set, which a later merge landed rc 0."""
    ws = two_member_ws
    _init(ws)
    _, a_base, a_head = _unpushed_head(ws)
    b_head = _beta_ahead(ws)
    original = gitops.ancestry_proc
    calls = []

    def broken(cwd, ancestor, descendant, **kwargs):
        if broken_member and Path(cwd).name == broken_member:
            calls.append(Path(cwd).name)
            return subprocess.CompletedProcess(["git", "merge-base", "--is-ancestor"], 128, "",
                                               "fatal: Not a valid commit name")
        return original(cwd, ancestor, descendant, **kwargs)

    monkeypatch.setattr(gitops, "ancestry_proc", broken)
    before = _refs(ws)
    result = _invoke("review", "bind", str(ws))
    print(f"SUBJECT broken={broken_member} rc={result.exit_code} stdout={result.stdout!r}")
    if broken_member is None:
        gr_id = _assert_only_bind_stdout(result)
        rows = json.loads(_invoke("review", "show", str(ws), gr_id, "--json").stdout)["members"]
        assert sorted((r["key"], r["head"]) for r in rows) == [("alpha", a_head), ("beta", b_head)]
        return
    assert calls == ["beta"]
    assert result.exit_code == 2, result.output
    message = _flat(result.stderr)
    assert "beta (ancestry could not be read" in message and "no review id was minted" in message
    assert "gr2: pin alpha" not in result.stderr
    assert result.stdout == "" and _refs(ws) == before


def test_choice_refused_default_is_immutable() -> None:
    """A NamedTuple default is one object shared by every instance, so `refused` defaults to an immutable tuple:
    appending to one Choice's default must not be possible, let alone reach another Choice."""
    from gr2.python_cli.review_members import Choice

    first, second = Choice([], []), Choice([], [])
    assert first.refused == () and second.refused == ()
    with pytest.raises(AttributeError):
        first.refused.append("x (leaked)")  # type: ignore[attr-defined]
    assert second.refused == ()
