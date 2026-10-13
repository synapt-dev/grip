"""NP-Bench's live contract migration (arXiv 2610.07261, Section 6.2), cut into members, run through the declared-set gate.

The producer renames the money field (`amount` -> `amountCents`); a consumer that still reads `amount` merges cleanly
and is broken. Each member's `field.txt` names the field it produces (producer) or reads (everyone else) on its first
line. The joint acceptance check passes only when every member it is handed reads what the producer produces.

Rows: A the producer alone, no set record; B the set check over new producer + unadapted consumer; C consumer adapted;
D a third consumer, `ui`, that is NOT in the declared set (the paper's scope-leakage question, one level up); D' the
same `ui` declared into the set.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, merge_gate, set_check
from gr2.python_cli.app import app
from tests.test_declared_sets import _refs, declare, redeclare, store_commit
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

runner = CliRunner()
PASS = [sys.executable, "-c", "pass"]
# The joint acceptance check: every member in the lane reads the field the producer produces.
ACCEPT = [sys.executable, "-c", (
    "import json, os, pathlib, sys\n"
    "m = {k: pathlib.Path(v) for k, v in json.loads(os.environ['GR2_SET_MEMBERS']).items()}\n"
    "first = lambda k: (m[k] / 'field.txt').read_text().splitlines()[0]\n"
    "made = first('producer')\n"
    "bad = sorted(k for k in m if k != 'producer' and first(k) != made)\n"
    "print('produces', made, 'stale readers', bad)\n"
    "sys.exit(1 if bad else 0)\n")]


def _world(tmp_path, monkeypatch, consumer_adapted, in_set):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    author = tmp_path / "author"
    author.mkdir()
    feat_field = {"producer": "amountCents", "consumer": "amountCents" if consumer_adapted else "amount", "ui": "amount"}
    members = {}
    for name in ("producer", "consumer", "ui"):
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "--bare", "-b", "main", remote)
        seed.mkdir()
        git(seed, "init", "-b", "main")
        (seed / "field.txt").write_text("amount\n")
        git(seed, "add", "field.txt")
        git(seed, "commit", "-m", "base")
        base = git(seed, "rev-parse", "HEAD")
        git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        members[name] = dict(repo=author / name, remote=remote, base=base)
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    monkeypatch.chdir(author)
    for name, m in members.items():
        repo = m["repo"]
        git(repo, "checkout", "-b", "feat")
        (repo / "field.txt").write_text(f"{feat_field[name]}\n# {name} reviewed\n")
        git(repo, "add", "field.txt")
        git(repo, "commit", "-m", "reviewed")
        m["head"] = git(repo, "rev-parse", "HEAD")
        before = _refs(author)
        made = runner.invoke(app, ["review", "bind", "--repo", name, "--remote", str(m["remote"]), "--base", m["base"],
                                   "--head", m["head"], "--ref", "refs/heads/main", "--source", str(repo)])
        assert made.exit_code == 0, made.output
        (ref,) = _refs(author) - before
        m["review"] = "gr:" + ref.rsplit("/", 1)[1]
        assert runner.invoke(app, ["review", "publish", m["review"], "--remote", str(m["remote"])]).exit_code == 0
        git(repo, "push", "origin", "feat")
        check_records.run_check(repo, str(m["remote"]), m["head"], "test", PASS)   # each member passes ITS OWN check
    text = (author / "grip.toml").read_text().replace('ref = "main"', 'ref = "feat"').replace('upstream = "origin/main"', 'upstream = "origin/feat"')
    (author / "grip.toml").write_text(text)
    declare(author, "money", in_set)
    root = store_commit(author, "the change, every pin")
    return dict(author=author, members=members, root=root)


def merge(w, name):
    return merge_gate.review_merge(w["author"], w["members"][name]["review"], into="main", feature="feat")


def integrated_readers_broken(w):
    """What the paper's post-integration acceptance check sees over ALL members at main: stale readers of the field."""
    field = {n: git(m["remote"], "show", "main:field.txt").splitlines()[0] for n, m in w["members"].items()}
    return sorted(n for n in field if n != "producer" and field[n] != field["producer"])


def test_row_a_the_producer_alone_with_no_set_record_is_refused(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=False, in_set=["producer", "consumer"])
    code, receipt = merge(w, "producer")
    print("ROW A", code, receipt["members"][0].get("refused"))
    assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_absent"), receipt


def test_row_b_the_joint_check_goes_red_on_the_stale_consumer_and_both_merges_refuse(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=False, in_set=["producer", "consumer"])
    out = set_check.run_declared_set_check(w["author"], "money", w["root"], ACCEPT)
    print("ROW B check", out["result"], out["output_tail"].strip())
    assert out["result"] == "fail"
    for name in ("producer", "consumer"):
        code, receipt = merge(w, name)
        print("ROW B merge", name, code, receipt["members"][0].get("refused"))
        assert code == merge_gate.EXIT_REFUSED and receipt["members"][0]["refused"].startswith("declared_set_fail"), receipt


def test_row_c_d_adapted_consumer_merges_and_ui_outside_the_set_is_left_broken(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    out = set_check.run_declared_set_check(w["author"], "money", w["root"], ACCEPT)
    print("ROW C check", out["result"], out["output_tail"].strip())
    assert out["result"] == "pass"
    for name in ("producer", "consumer", "ui"):
        code, receipt = merge(w, name)
        print("ROW C/D merge", name, code, receipt["members"][0].get("refused"), receipt["members"][0].get("declared_set"))
        assert code == merge_gate.EXIT_MERGED, (name, receipt)
    broken = integrated_readers_broken(w)
    print("ROW D integrated stale readers after every merge", broken)
    assert broken == ["ui"]   # every gate green, and the integrated workspace still has a consumer on the old field


def test_row_d_prime_ui_declared_into_the_set_is_caught(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer", "ui"])
    out = set_check.run_declared_set_check(w["author"], "money", w["root"], ACCEPT)
    print("ROW D' check", out["result"], out["output_tail"].strip())
    assert out["result"] == "fail"
    code, receipt = merge(w, "producer")
    print("ROW D' merge producer", code, receipt["members"][0].get("refused"))
    assert code == merge_gate.EXIT_REFUSED
