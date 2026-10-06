"""Real two-member binds, sole-target selection, and explicit-target compatibility."""
import json

import pytest

from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


@pytest.mark.parametrize("verb", ["show", "verify"])
def test_review_reader_selects_only_one_bind(two_member_ws, monkeypatch, tmp_path, verb):
    ws = two_member_ws
    rc, output = _cli("store", "init", str(ws))
    assert rc == 0, output
    nested = ws / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    rc, output = _cli("review", verb, "--json")
    assert rc == 2 and "no review bind" in output
    rows = []
    for name in ("alpha", "beta"):
        repo = ws / name
        base = _git_out(repo, "rev-parse", "HEAD")
        (repo / "change.txt").write_text(name + " reviewed\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "reviewed")
        rows.append(dict(key=name, path=name, remote=_git_out(repo, "remote", "get-url", "origin"),
                         base=base, head=_git_out(repo, "rev-parse", "HEAD"), ref="refs/heads/main",
                         source=str(repo), title=name))
    rowfile = tmp_path / "rows.json"
    rowfile.write_text(json.dumps(rows))
    rc, bind = _cli("review", "bind", str(ws), "--rows-json", str(rowfile))
    assert rc == 0, bind
    bind = bind.strip()
    rc, explicit = _cli("review", verb, str(ws), bind, "--json")
    assert rc == 0, explicit
    # Inference diagnostics are on stderr; _cli combines both streams. Use the
    # actual CliRunner streams below so stdout remains an exact JSON contract.
    from gr2.python_cli.app import app
    from tests.conftest import make_cli_runner
    for args in ([], [str(ws)], ["-C", str(ws)], [bind], ["-C", str(ws), bind]):
        result = make_cli_runner().invoke(app, ["review", verb, *args, "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == json.loads(explicit)
    for args in (["not-a-bind"], [str(ws), bind, "extra"]):
        result = make_cli_runner().invoke(app, ["review", verb, *args, "--json"])
        assert result.exit_code != 0
        assert "the only review bind" not in result.stderr
    if verb == "show":
        actual = json.loads(explicit)
        assert len(actual["members"]) == 2
        for member, row in zip(actual["members"], rows):
            assert all(member[k] == row[k] for k in ("key", "path", "remote", "base", "head"))
    else:
        assert json.loads(explicit)["tree_matches"] is True
    rows[0]["title"] = "a different bind"
    rowfile.write_text(json.dumps(rows))
    rc, second = _cli("review", "bind", str(ws), "--rows-json", str(rowfile))
    assert rc == 0 and second.strip() != bind, second
    rc, output = _cli("review", verb, "--json")
    assert rc == 2 and "2 review binds exist" in output and bind in output and second.strip() in output
    rc, output = _cli("review", verb, bind, "--json")
    assert rc == 0 and json.loads(output) == json.loads(explicit)
