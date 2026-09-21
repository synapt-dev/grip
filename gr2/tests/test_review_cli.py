"""CLI glue over the review gr-commit engine: ``gr2 review bind|open-gr|verify``.

The engine functions (create_review_bind_commit / reconstruct_review_lane /
verify_review_commit) are covered by test_review_bind_verify and
test_review_open_lane. These tests cover the NEW thin Typer verbs, and the one
property those tests cannot see: that the glue turns an engine refusal or
corruption into a clean nonzero EXIT with real error text on stderr — never a
Python traceback, and never a swallowed exit 0. Glue is exactly where a caught
exception becomes a silent success, so every adversarial case asserts BOTH the
nonzero exit AND the absence of "Traceback" in the output.

Fathom's three antagonist probes (m_b2708f8f) map here: base-not-on-remote is
probe 3 (refuse at bind), tampered-range is probe 2 (open-gr must fail loud),
verify-on-tampered is probe 1/2 (verify recomputes, does not trust the record).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import grip
from gr2.python_cli.app import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """CLI usage/error panels are Rich-rendered: under forced color (CI) Rich
    interleaves ANSI escapes through the styled text, so a flag name like
    ``--head`` is not a literal substring of the raw output even though it is
    plainly visible. Strip the escapes before asserting on the message content.
    """
    return _ANSI_RE.sub("", text)

TITLE = "feat: prove the review gr tree\n"
BODY = "body line\n"


def _env() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("PATH", ""),
    }


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=_env(), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _fixture_repo(tmp_path: Path, name: str = "r") -> tuple[str, str, str, Path]:
    """A bare remote advertising dev@BASE, plus a work clone carrying an
    unpublished HEAD (a genuine pre-push review). Returns
    (remote_url, base, head, work_dir)."""
    work = tmp_path / f"{name}-work"
    _git(tmp_path, "init", "-b", "dev", str(work))
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    remote = tmp_path / f"{name}-origin.git"
    _git(tmp_path, "clone", "--bare", "--quiet", str(work), str(remote))
    (work / "f.txt").write_text("head under review\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feat: the reviewed change")
    head = _git(work, "rev-parse", "HEAD")
    return str(remote), base, head, work


def _grip_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    grip.grip_init(ws)
    return ws


def _bind(ws: Path, remote: str, base: str, head: str, work: Path, key: str = "recall") -> str:
    result = runner.invoke(
        app,
        ["review", "bind", str(ws), "--repo", key, "--remote", remote,
         "--base", base, "--head", head, "--ref", "refs/heads/dev",
         "--path", key, "--source", str(work), "--title", TITLE, "--body", BODY],
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.strip()
    assert line.startswith("gr:"), result.output
    return line


def _tamper_blob(ws: Path, commit_ref: str, tree_path: str, content: bytes) -> str:
    """Hand-craft a tampered review artifact: replace one carried blob and commit
    the mutated tree onto a NEW commit in .grip. Returns the new gr:<sha>."""
    gd = ws / ".grip"
    sha = commit_ref[3:] if commit_ref.startswith("gr:") else commit_ref
    blob = subprocess.run(
        ["git", "-C", str(gd), "hash-object", "-w", "--stdin"],
        input=content, env=_env(), capture_output=True, check=True,
    ).stdout.decode().strip()
    index = ws / ".tamper-index"
    env = {**_env(), "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "-C", str(gd), "read-tree", sha], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(gd), "update-index", "--add", "--cacheinfo",
         f"100644,{blob},{tree_path}"],
        env=env, check=True,
    )
    new_tree = subprocess.run(
        ["git", "-C", str(gd), "write-tree"], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    new_commit = subprocess.run(
        ["git", "-C", str(gd), "commit-tree", new_tree, "-p", sha, "-m", "tamper"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    index.unlink(missing_ok=True)
    return f"gr:{new_commit}"


# --- happy path: the smallest proof, as a test -----------------------------


def test_bind_open_gr_verify_roundtrip(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)

    lane = tmp_path / "lane"
    opened = runner.invoke(
        app, ["review", "open-gr", str(ws), grc, "--repo", "recall",
               "--lane-dir", str(lane), "--enter", "--json"],
    )
    assert opened.exit_code == 0, opened.output
    assert (lane / "f.txt").read_text() == "head under review\n"
    # The assertion is on the TREE (git am mints a new head sha), so tree must match.
    assert _git(lane, "rev-parse", "HEAD^{tree}") == \
        _git(ws / ".grip", "show", f"{grc[3:]}:objects/recall/head-tree")

    verified = runner.invoke(app, ["review", "verify", str(ws), grc, "--json"])
    assert verified.exit_code == 0, verified.output
    assert '"tree_matches": true' in verified.stdout
    assert f'"observed_remote_head": "{base}"' in verified.stdout


def _multi_row(key, remote, base, head, work):
    return {"key": key, "remote": remote, "path": key, "head": head,
            "base": base, "ref": "refs/heads/dev", "title": TITLE,
            "body": BODY, "source": str(work)}


def test_open_gr_materializes_every_row_of_a_multi_row_commit(tmp_path):
    r1, b1, h1, w1 = _fixture_repo(tmp_path, "one")
    r2, b2, h2, w2 = _fixture_repo(tmp_path, "two")
    ws = _grip_ws(tmp_path)
    # A GENUINE two-row review commit. The CLI `bind` writes one row per commit,
    # so a multi-row commit is built through the engine directly -- expressing a
    # multi-row commit from the CLI surface is a named M1 follow-up. open-gr's
    # no-`--repo` path is what consumes it, and that path is what this witnesses.
    commit = grip.create_review_bind_commit(
        ws, [_multi_row("one", r1, b1, h1, w1), _multi_row("two", r2, b2, h2, w2)]
    )
    assert grip.review_row_keys(ws, commit) == ["one", "two"]  # one commit, two rows

    lane = tmp_path / "multilane"
    opened = runner.invoke(
        app, ["review", "open-gr", str(ws), f"gr:{commit}", "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 0, opened.output
    assert "one:" in opened.stdout and "two:" in opened.stdout
    assert opened.stdout.count("tree_match=True") == 2   # every bound row reconstructed
    assert (lane / "one" / "f.txt").read_text() == "head under review\n"
    assert (lane / "two" / "f.txt").read_text() == "head under review\n"


# --- Fathom probe 3: BASE NOT ON THE REMOTE, refuse at bind, cleanly -------


def test_bind_refuses_base_not_on_remote_with_clean_exit(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    # An unrelated repo's sha: valid hex, never on this remote.
    other = tmp_path / "other"
    _git(tmp_path, "init", "-b", "dev", str(other))
    (other / "x").write_text("x\n")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "unrelated")
    stranger = _git(other, "rev-parse", "HEAD")

    result = runner.invoke(
        app,
        ["review", "bind", str(ws := _grip_ws(tmp_path)), "--repo", "recall",
         "--remote", remote, "--base", stranger, "--head", head, "--ref", "refs/heads/dev",
         "--path", "recall", "--source", str(work), "--title", TITLE, "--body", BODY],
    )
    assert result.exit_code == 2, result.output
    assert "refused:" in result.output
    assert "Traceback" not in result.output  # clean error text, not a stack trace


# --- the plumbing store (.grip/.git) absent, bind must refuse cleanly and
# name the remediation -- not traceback. create_review_bind_commit calls
# _validate_grip_repo first thing, which raises GripInitError; _review_call
# (the CLI glue's one exception boundary) caught GripReviewRefused and
# GripCorruptError but not GripInitError, so this one raised straight through
# Typer as a raw Python traceback instead of the same clean nonzero exit every
# other engine refusal gets.


def test_bind_refuses_cleanly_when_the_plumbing_store_is_absent(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = tmp_path / "ws-uninitialized"
    ws.mkdir()  # a real workspace dir, deliberately never `grip init`-ed

    result = runner.invoke(
        app,
        ["review", "bind", str(ws), "--repo", "recall", "--remote", remote,
         "--base", base, "--head", head, "--ref", "refs/heads/dev",
         "--path", "recall", "--source", str(work), "--title", TITLE, "--body", BODY],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output  # clean error text, not a stack trace
    assert "grip init" in result.output  # names the remediation, not just the symptom
    assert str(ws) in result.output  # names WHERE, not just that something's missing


def test_workspace_init_leaves_a_store_ready_for_project_review(tmp_path: Path) -> None:
    """A workspace adopted through the public CLI can create a project review.

    This is the stranger path: initialize an existing workspace, make and enter
    a materialized lane, then create its project-review commit.  ``workspace
    init`` used to write only ``workspace_spec.toml``.  The later review command
    then crashed because the adjacent ``.grip`` directory was not a Git object
    store.  The initializer owns that store, so this test proves the complete
    path rather than a private call to ``grip_init``.
    """
    remote, _base, _head, _work = _fixture_repo(tmp_path, "adopted")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "adopted"
    _git(tmp_path, "clone", "--quiet", "--branch", "dev", remote, str(source))

    initialized = runner.invoke(app, ["workspace", "init", str(workspace)])
    assert initialized.exit_code == 0, initialized.output
    assert (workspace / ".grip" / ".git").is_dir()

    created = runner.invoke(
        app,
        [
            "lane", "create", str(workspace), "default", "review",
            "--repos", "adopted", "--branch", "dev",
        ],
    )
    assert created.exit_code == 0, created.output
    entered = runner.invoke(
        app,
        ["lane", "enter", str(workspace), "default", "review", "--actor", "agent:test"],
    )
    assert entered.exit_code == 0, entered.output

    project = runner.invoke(
        app, ["review", "create-project", str(workspace), "default", "review"]
    )
    assert project.exit_code == 0, project.output
    assert project.stdout.startswith("gr:")


# --- Fathom probe 2: TAMPERED CARRIED RANGE, open-gr must fail loud --------


def test_open_gr_propagates_tampered_range_loudly(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    tampered = _tamper_blob(ws, grc, "objects/recall/range.patch", b"not a valid patch\n")

    lane = tmp_path / "lane-tampered"
    opened = runner.invoke(
        app, ["review", "open-gr", str(ws), tampered, "--repo", "recall",
               "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 2, opened.output       # loud, not swallowed
    assert "refused:" in opened.output
    assert "Traceback" not in opened.output


# --- Fathom probe 1: WRONG HEAD, caught at open-gr by the head-TREE assertion.
# The bound head-tree is the only cryptographically pinned content: reconstruction
# ams the carried range and asserts the resulting tree equals objects/<key>/head-tree.
# Substituting the head-tree makes that assertion fail loud. (A tampered range,
# probe 2 above, fails the same assertion; the recorded commit-SHA and base-SHA are
# pinned instead at BIND time by the liveness refusal, not re-checked here.)


def test_open_gr_refuses_when_head_tree_is_wrong(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    # Claim a different head-tree than the range reconstructs to.
    wrong = _tamper_blob(
        ws, grc, "objects/recall/head-tree",
        b"0000000000000000000000000000000000000000\n",
    )
    lane = tmp_path / "lane-wronghead"
    opened = runner.invoke(
        app, ["review", "open-gr", str(ws), wrong, "--repo", "recall",
               "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 2, opened.output       # tree_mismatch, loud
    assert "refused:" in opened.output
    assert "Traceback" not in opened.output


# --- verify's own contract: STRUCTURAL integrity (recompute the canonical tree
# and compare ids). It catches an extraneous/missing file, a wrong schema, or a
# malformed row -- not a well-placed content swap, which verify faithfully
# re-encodes. Content trust lives at bind (liveness) and open-gr (reconstruction).


def test_verify_flags_structural_corruption_nonzero(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    # Add an extraneous blob the canonical recomputation will not reproduce.
    corrupt = _tamper_blob(ws, grc, "objects/recall/EXTRA", b"unexpected\n")

    verified = runner.invoke(app, ["review", "verify", str(ws), corrupt, "--json"])
    assert verified.exit_code != 0, verified.output   # structural drift is never green
    assert "Traceback" not in verified.output
    assert '"tree_matches": false' in verified.stdout


# --- multi-row bind from the CLI (the R2 fruit: N rows in ONE commit) -------
# `gr2 review bind --rows-json` is what lets the CLI express what previously
# only grip.create_review_bind_commit could. Exclusive with the single-row
# flags; an incomplete single-row invocation is a clean usage refusal.


def test_bind_rows_json_binds_all_rows_in_one_commit(tmp_path):
    r1, b1, h1, w1 = _fixture_repo(tmp_path, "one")
    r2, b2, h2, w2 = _fixture_repo(tmp_path, "two")
    ws = _grip_ws(tmp_path)
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([
        {"key": "one", "remote": r1, "base": b1, "head": h1, "source": str(w1),
         "title": "feat: one", "body": "b1"},
        {"key": "two", "remote": r2, "base": b2, "head": h2, "source": str(w2),
         "title": "feat: two", "body": "b2"},
    ]))

    out = runner.invoke(app, ["review", "bind", str(ws), "--rows-json", str(rows)])
    assert out.exit_code == 0, out.output
    commit = out.stdout.strip()[3:]  # strip gr:
    assert grip.review_row_keys(ws, commit) == ["one", "two"]  # ONE commit, two rows

    lane = tmp_path / "lane"
    opened = runner.invoke(
        app, ["review", "open-gr", str(ws), f"gr:{commit}", "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 0, opened.output
    assert opened.stdout.count("tree_match=True") == 2
    assert (lane / "one" / "f.txt").read_text() == "head under review\n"
    assert (lane / "two" / "f.txt").read_text() == "head under review\n"


def test_bind_rejects_rows_json_together_with_single_row_flags(tmp_path):
    r1, b1, h1, w1 = _fixture_repo(tmp_path, "one")
    ws = _grip_ws(tmp_path)
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"key": "one", "remote": r1, "base": b1, "head": h1}]))
    out = runner.invoke(
        app, ["review", "bind", str(ws), "--rows-json", str(rows), "--repo", "one"],
    )
    assert out.exit_code != 0                       # usage refusal, not a bind
    assert "exclusive" in out.output.lower()
    assert "Traceback" not in out.output


def test_bind_rejects_incomplete_single_row(tmp_path):
    r1, b1, h1, w1 = _fixture_repo(tmp_path, "one")
    ws = _grip_ws(tmp_path)
    # --head missing, no --rows-json. Pin a wide width so the required-flags
    # message never wraps mid-flag, and strip ANSI so forced color (CI) does not
    # break the substring.
    out = runner.invoke(
        app, ["review", "bind", str(ws), "--repo", "one", "--remote", r1, "--base", b1],
        env={"COLUMNS": "200"},
    )
    assert out.exit_code != 0
    assert "--head" in _plain(out.output)
    assert "Traceback" not in out.output


def test_bind_rows_json_rejects_empty_list(tmp_path):
    ws = _grip_ws(tmp_path)
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    out = runner.invoke(app, ["review", "bind", str(ws), "--rows-json", str(rows)])
    assert out.exit_code != 0
    assert "Traceback" not in out.output


# --- rows-json glue through the REAL entry point (Stromus R2, m_8b084d88) ----
# CliRunner catches these as exit 1 with empty output, hiding the traceback the
# glue used to raise. Run them through `python -m gr2.python_cli.app` so a raw
# traceback would be visible: each must be a clean BadParameter (exit != 0, no
# "Traceback").


def _run_bind_real(ws: Path, rows_json: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "gr2.python_cli.app", "review", "bind",
         str(ws), "--rows-json", str(rows_json)],
        capture_output=True, text=True,
    )


def test_bind_rows_json_malformed_json_is_clean_error(tmp_path):
    ws = _grip_ws(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{")
    r = _run_bind_real(ws, bad)
    assert r.returncode != 0
    assert "Traceback" not in (r.stdout + r.stderr)
    assert "invalid JSON" in (r.stdout + r.stderr)


def test_bind_rows_json_missing_file_is_clean_error(tmp_path):
    ws = _grip_ws(tmp_path)
    r = _run_bind_real(ws, tmp_path / "does-not-exist.json")
    assert r.returncode != 0
    assert "Traceback" not in (r.stdout + r.stderr)


def test_bind_rows_json_non_dict_entry_is_clean_error(tmp_path):
    ws = _grip_ws(tmp_path)
    f = tmp_path / "strentry.json"
    f.write_text(json.dumps(["i am a string, not an object"]))
    r = _run_bind_real(ws, f)
    assert r.returncode != 0
    assert "Traceback" not in (r.stdout + r.stderr)
    assert "must be a JSON object" in (r.stdout + r.stderr)


# --- Fathom R1 (m_eb5f6948): a JSON number in a string field must refuse, not
# traceback. _normalize_review_row checked presence, never type; a truthy int
# passed the presence gate and hit a string op downstream. Type-check every
# string field. Witnessed through the real entry point (int head, int remote).


def test_bind_rows_json_int_head_is_clean_error(tmp_path):
    ws = _grip_ws(tmp_path)
    f = tmp_path / "inthead.json"
    f.write_text(json.dumps([{"key": "one", "remote": "/x/y.git", "base": "aa", "head": 456}]))
    r = _run_bind_real(ws, f)
    assert r.returncode != 0
    assert "Traceback" not in (r.stdout + r.stderr)
    assert "must be a string" in (r.stdout + r.stderr)


def test_bind_rows_json_int_remote_is_clean_error(tmp_path):
    ws = _grip_ws(tmp_path)
    f = tmp_path / "intremote.json"
    f.write_text(json.dumps([{"key": "one", "remote": 123, "base": "aa", "head": "bb"}]))
    r = _run_bind_real(ws, f)
    assert r.returncode != 0
    assert "Traceback" not in (r.stdout + r.stderr)
    assert "must be a string" in (r.stdout + r.stderr)
