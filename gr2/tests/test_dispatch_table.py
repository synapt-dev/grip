"""The dispatch table: `gr2 review stamp` builds that verb alone, and says and exits what the full CLI does.

Each row runs the console entry (dispatch.main, as the installed gr2 script calls it) and the full tree
(app.main) in a subprocess on the same inputs and compares stdout, stderr and exit code.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from gr2.python_cli import dispatch
from tests.test_review_approvals import git, world  # noqa: F401  (fixture)

TABLE_CMD = [sys.executable, "-c", "import sys; sys.argv[0] = 'gr2'; from gr2.python_cli.dispatch import main; main()"]
FULL_CMD = [sys.executable, "-c", "import sys; sys.argv[0] = 'gr2'; from gr2.python_cli.app import main; main()"]


def _run(cmd, argv, cwd=None):
    p = subprocess.run([*cmd, *argv], capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL,
                       env=None, timeout=120)
    return p.returncode, p.stdout, p.stderr


def _same(argv, cwd=None):
    table, full = _run(TABLE_CMD, argv, cwd), _run(FULL_CMD, argv, cwd)
    assert table == full, (table, full)
    return table


@pytest.mark.parametrize("argv", [
    ["review", "stamp"],                                  # usage error: missing WORKSPACE_ROOT
    ["review", "stamp", "{tmp}/absent"],                  # no such workspace
    ["review", "stamp", "{tmp}", "gr:" + "0" * 40],       # not a workspace, explicit id
    ["review", "stamp", "{tmp}", "a", "b"],               # extra argument
    ["review", "stamp", "--bogus", "{tmp}"],              # unknown option
])
def test_stamp_refusals_through_the_table_equal_the_full_cli(tmp_path, argv):
    code, _, _ = _same([a.format(tmp=tmp_path) for a in argv], cwd=tmp_path)
    assert code != 0


def test_stamp_self_approval_refusal_and_a_stamp_through_the_table(world):  # noqa: F811
    ws = str(world["workspace"])
    git(world["workspace"], "config", "user.name", "Author A")
    code, out, err = _same(["review", "stamp", ws], cwd=ws)
    assert (code, out) == (2, "") and "self_approval" in err, err
    git(world["workspace"], "config", "user.name", "Reviewer B")
    code, out, err = _run(TABLE_CMD, ["review", "stamp", ws], cwd=ws)
    assert code == 0, err
    assert "Reviewer B" in out and "Traceback" not in err
    table = json.loads(out)
    # A stamp appends a fresh link (its own tip and hash), so the full CLI's next stamp is compared by shape.
    git(world["workspace"], "config", "user.name", "Reviewer C")
    code, out, err = _run(FULL_CMD, ["review", "stamp", ws], cwd=ws)
    assert (code, err) == (0, ""), err
    full = json.loads(out)
    assert (sorted(table), table["review_id"], table["count"]) == (sorted(full), full["review_id"], 1), (table, full)
    assert [link["approver"] for link in full["links"]] == ["Reviewer B", "Reviewer C"], full


@pytest.mark.parametrize("argv", [
    ["--help"], ["-h"], [], ["review", "--help"], ["review", "stamp", "--help"], ["review", "stamp", "x", "-h"],
    ["nosuch"], ["review", "nosuch"], ["nosuch", "stamp"], ["--version"], ["help", "-a"],
])
def test_help_and_unknown_verbs_are_the_full_cli(tmp_path, argv):
    _same(argv, cwd=tmp_path)


def test_top_help_still_lists_every_verb(tmp_path):
    _, out, _ = _run(TABLE_CMD, ["--help"], cwd=tmp_path)
    # CI forces color, so rich wraps each verb in ANSI codes; compare the plain text.
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    for verb in ("lane", "review", "check", "pr", "sync", "store"):
        assert f" {verb} " in out, out


def test_stamp_through_the_table_imports_only_its_verb(tmp_path):
    """The table's point: `review stamp` never imports app.py, and loads far fewer gr2 modules.

    On every way of naming the root: none (inferred from a cwd inside a workspace, the common
    call), an absent path, and an existing directory. The root walk lives outside app.py."""
    probe = ("import json, sys; sys.argv = json.loads(sys.argv[1])\n"
             "from gr2.python_cli import dispatch\n"
             "try:\n    dispatch.main()\nexcept SystemExit:\n    pass\n"
             "mods = [m for m in sys.modules if m.startswith('gr2.')]\n"
             "print('gr2.python_cli.app' in sys.modules, len(mods))")
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text("")
    inside = ws / "sub"
    inside.mkdir()

    def run(argv, cwd):
        p = subprocess.run([sys.executable, "-c", probe, json.dumps(argv)], cwd=cwd, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=120)
        loaded, count = p.stdout.split()
        return loaded, int(count)

    stamp = {
        "inferred": run(["gr2", "review", "stamp"], inside),
        "absent": run(["gr2", "review", "stamp", str(tmp_path / "nonexistent-path")], tmp_path),
        "existing": run(["gr2", "review", "stamp", str(tmp_path)], tmp_path),
    }
    full_app, full_n = run(["gr2", "nosuch", "stamp", str(tmp_path)], tmp_path)
    assert {k: v[0] for k, v in stamp.items()} == dict.fromkeys(stamp, "False"), stamp
    assert full_app == "True", (full_app, full_n)
    assert stamp["existing"][1] * 2 < full_n, (stamp, full_n)
    assert all(n < full_n for _, n in stamp.values()), (stamp, full_n)


def test_route_is_exact_on_the_first_two_words():
    assert dispatch.route(["review", "stamp", "x"]) is dispatch.TABLE[("review", "stamp")]
    for argv in (["review"], ["stamp", "review"], ["--json", "review", "stamp"], ["review", "stamp", "--help"]):
        assert dispatch.route(argv) is None, argv
