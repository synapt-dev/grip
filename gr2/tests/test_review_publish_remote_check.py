"""`review publish --remote` refusals print what the bare form prints: the refused: line, then a next or a check."""
from __future__ import annotations

import json
import shlex

import pytest

from gr2.python_cli.next_steps import PUBLISH_REFUSED_CHECK
from tests.test_review_remote_safety import (CRED, FOREIGN, PREFIX, TOKEN, bind, git, grip, review, reviews, world,  # noqa: F401
                                             world_env)

LOCAL_CHECK = ("check: the member's recorded remote is a path on this host, so its review stays here; publish to a local "
               "destination instead: gr2 review publish <workspace> <review> --remote <absolute path>, or pin again "
               "from a clone whose remote is a network URL (https or ssh) on another host")
SSH = "ssh://git@example.invalid/o/member.git"


def _receipt(result) -> dict:
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith("{")]
    return json.loads(line)


def _refused_off_host(world, monkeypatch, remote):
    made = bind(world, monkeypatch, remote)
    assert made.exit_code == 0, made.output
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    return commit, review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", FOREIGN)


def test_remote_form_prints_the_local_path_check_plainly(world, monkeypatch):
    commit, off_host = _refused_off_host(world, monkeypatch, "../remote.git")
    assert off_host.exit_code == 2, off_host.output
    lines = off_host.output.splitlines()
    assert ("refused: local_path_remote: member: a host-local path or alias stays on this host; publish to a local "
            "destination (nothing left this host)") in lines, off_host.output
    assert lines.count(LOCAL_CHECK) == 1, off_host.output
    assert LOCAL_CHECK.count("gr2 review publish <workspace> <review> --remote <absolute path>") == 1
    assert not [ln for ln in lines if ln.startswith("next: ")], off_host.output


def test_remote_form_uses_the_tables_for_every_refusal(world, monkeypatch):
    monkeypatch.setattr(grip, "_refuse_remote_credentials", lambda key, remote: None, raising=False)
    made = bind(world, monkeypatch, CRED)
    assert made.exit_code == 0, made.output
    monkeypatch.undo()
    world_env(world, monkeypatch)
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    refused = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", world["dest"])
    assert refused.exit_code == 2, refused.output
    assert refused.output.splitlines().count(PUBLISH_REFUSED_CHECK["remote_credentials"]) == 1, refused.output
    assert TOKEN not in refused.output


def test_remote_form_receipt_carries_the_check(world, monkeypatch):
    commit, off_host = _refused_off_host(world, monkeypatch, "../remote.git")
    receipt = _receipt(off_host)
    assert receipt["next"] is None and receipt["check"] == LOCAL_CHECK, receipt
    assert receipt["id"] == "gr:" + commit and receipt["exit"] == 2, receipt


@pytest.mark.parametrize("spelling", ["relative", "dot-relative", "file-url", "absolute", "ssh"])
def test_each_host_local_spelling_checks_and_its_advice_runs(world, monkeypatch, spelling):
    remote = {"relative": "../remote.git", "dot-relative": "./../remote.git", "file-url": f"file://{world['remote']}",
              "absolute": str(world["remote"]), "ssh": SSH}[spelling]
    if spelling == "ssh":
        # No ssh host is reachable from a test: the ssh URL is rewritten to the fixture remote.
        git(world["author"], "config", "--global", "--add", f"url.file://{world['remote']}.insteadOf", SSH)
    commit, off_host = _refused_off_host(world, monkeypatch, remote)
    if spelling == "ssh":
        assert off_host.exit_code == 0, off_host.output
        assert "local_path_remote" not in off_host.output, off_host.output
        assert git(world["dest"], "rev-parse", PREFIX + commit) == commit
        return
    assert off_host.exit_code == 2, off_host.output
    assert off_host.output.splitlines().count(LOCAL_CHECK) == 1, off_host.output
    assert git(world["dest"], "for-each-ref") == ""
    advice = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", world["dest"])
    assert advice.exit_code == 0, advice.output
    assert git(world["dest"], "rev-parse", PREFIX + commit) == commit


def test_remote_form_next_reruns_with_the_destination(world, monkeypatch, tmp_path):
    # unresolved_merge is the one lambda-next code the --remote preflight (_member_preflight(for_push=False)) raises.
    made = bind(world, monkeypatch, str(world["remote"]))
    assert made.exit_code == 0, made.output
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    dest = tmp_path / "a dest.git"
    git(tmp_path, "init", "-q", "--bare", dest)
    (world["member"] / ".git" / "MERGE_HEAD").write_text(world["head"] + "\n")
    refused = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", dest)
    assert refused.exit_code == 2, refused.output
    (nxt,) = [ln for ln in refused.output.splitlines() if ln.startswith("next: ")]
    assert nxt.count(f"gr2 review publish {world['author']} gr:{commit} --remote {shlex.quote(str(dest))}   # ") == 1, nxt
    assert _receipt(refused)["next"] == nxt[len("next: "):]
