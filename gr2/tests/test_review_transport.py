"""Exact existing native review refs across ordinary independent filesystems."""
from __future__ import annotations

import json
import subprocess

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer uses the installed Click
    from click.utils import strip_ansi

from tests.review_ref_helper import REVIEW_REF_PREFIX
from gr2.python_cli import grip
from gr2.python_cli.app import app

runner = CliRunner()
PREFIX = REVIEW_REF_PREFIX


def git(root, *args, input=None):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)], cwd=root, capture_output=True, text=True, input=input)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def cli(root, monkeypatch, *args):
    monkeypatch.chdir(root)
    return runner.invoke(app, ["review", *map(str, args)])


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(tmp_path, "init", "--bare", remote)
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("base\n")
    git(seed, "add", "payload.txt")
    git(seed, "commit", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "push", remote, "main")
    author, receiver = tmp_path / "author", tmp_path / "receiver"
    for root in (author, receiver):
        root.mkdir()
        git(root, "clone", "--no-local", "--branch", "main", remote, "member")
        init = runner.invoke(app, ["store", "init", str(root)])
        assert init.exit_code == 0, init.output
    member = author / "member"
    (member / "payload.txt").write_text("reviewed\n")
    git(member, "add", "payload.txt")
    git(member, "commit", "-m", "reviewed")
    head = git(member, "rev-parse", "HEAD")
    bound = cli(author, monkeypatch, "bind", "--repo", "member", "--remote", remote, "--base", base, "--head", head, "--ref", "refs/heads/main", "--source", member)
    assert bound.exit_code == 0, bound.output
    commit = bound.stdout.strip().splitlines()[-1][3:]
    return author, receiver, remote, commit, base, head


def publish(handoff, monkeypatch):
    author, _, remote, commit, _, _ = handoff
    result = cli(author, monkeypatch, "publish", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == "gr:" + commit


def authority(root):
    files = [".git/HEAD", ".git/index", ".git/config", "grip.toml", ".gitinclude", ".gitignore", "member/payload.txt"]
    values = {n: (root / n).read_bytes() if (root / n).exists() else None for n in files}
    values["state"] = {str(p.relative_to(root)): p.read_bytes() for p in (root / ".grip").rglob("*") if p.is_file()}
    return values


def test_cli_handoff_idempotence_and_reconstruction(handoff, monkeypatch, tmp_path):
    author, receiver, remote, commit, base, head = handoff
    before_author, before_receiver = authority(author), authority(receiver)
    publish(handoff, monkeypatch)
    for _ in range(2):
        result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ref"] == PREFIX + commit
        assert git(receiver, "rev-parse", PREFIX + commit) == commit
    assert authority(author) == before_author
    assert authority(receiver) == before_receiver
    assert git(receiver, "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__review_transfers__/") == ""
    shown = cli(receiver, monkeypatch, "show", "gr:" + commit, "--json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["members"][0]["head"] == head
    verified = cli(receiver, monkeypatch, "verify", "gr:" + commit, "--json")
    assert verified.exit_code == 0 and json.loads(verified.stdout)["tree_matches"] is True
    lane = tmp_path / "lane"
    opened = cli(receiver, monkeypatch, "open", "gr:" + commit, "--lane-dir", lane, "--repo", "member", "--json")
    assert opened.exit_code == 0, opened.output
    assert git(lane, "rev-parse", "HEAD^{tree}") == git(author / "member", "rev-parse", "HEAD^{tree}")
    assert (lane / "payload.txt").read_bytes() == (author / "member" / "payload.txt").read_bytes()
    assert not (receiver / ".git/objects/info/alternates").exists()
    assert not (lane / ".git/objects/info/alternates").exists()


@pytest.mark.parametrize("verb", ["publish", "receive"])
@pytest.mark.parametrize("value,extra,reason", [
    ("gr:not-hex", [], "expected_review_id_must_be_full"),
    ("gr:abcd", [], "expected_review_id_must_be_full"),
    ("gr:" + "a" * 40, ["--ref", "refs/heads/main"], "review_ref_identity_mismatch"),
])
def test_invalid_explicit_transport_never_falls_back(handoff, monkeypatch, verb, value, extra, reason):
    author, receiver, remote, _, _, _ = handoff
    def no_transport(*args, **kwargs):
        pytest.fail("invalid explicit input reached transport")
    monkeypatch.setattr(grip, "git", no_transport)
    result = cli(receiver if verb == "receive" else author, monkeypatch, verb, value, "--remote", remote, *extra)
    assert result.exit_code == 2 and reason in result.output


def required_input_display(result, styled, *tokens):
    display = strip_ansi(result.output)
    assert (display != result.output) is styled, result.output
    assert result.exit_code == 2, result.output
    assert all(token in display.lower() for token in tokens), result.output
    return display


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
@pytest.mark.parametrize("verb", ["publish", "receive"])
def test_required_expectation_and_remote_are_not_inferred(handoff, monkeypatch, tmp_path, verb, styled):
    from typer import rich_utils
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", styled)
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 120)
    monkeypatch.delenv("NO_COLOR", raising=False)
    author, _, remote, commit, _, _ = handoff
    calls = []
    def no_owner(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("missing required input reached transport owner")
    monkeypatch.setattr(grip, "publish_review_commit", no_owner)
    monkeypatch.setattr(grip, "receive_review_commit", no_owner)
    monkeypatch.chdir(author)
    missing_id = runner.invoke(app, ["review", verb, "--remote", str(remote)], color=True)
    (tmp_path / "missing-id.raw.txt").write_text(missing_id.output)
    display = required_input_display(missing_id, styled, "missing argument", "commit")
    (tmp_path / "missing-id.display.txt").write_text(display)
    assert calls == []
    missing_remote = runner.invoke(app, ["review", verb, "gr:" + commit], color=True)
    (tmp_path / "missing-remote.raw.txt").write_text(missing_remote.output)
    display = required_input_display(missing_remote, styled, "missing option", "--remote")
    (tmp_path / "missing-remote.display.txt").write_text(display)
    assert calls == []


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
@pytest.mark.parametrize("message,tokens,removed", [
    ("Missing option '--remote'", ("missing option", "--remote"), "--remote"),
    ("Missing argument 'commit'", ("missing argument", "commit"), "commit"),
])
def test_required_input_display_retains_semantics(styled, message, tokens, removed):
    def rendered(text):
        return "\x1b[31m" + text.replace("--remote", "-\x1b[0m\x1b[31m-remote") + "\x1b[0m" if styled else text
    positive = SimpleNamespace(exit_code=2, output=rendered(message))
    assert required_input_display(positive, styled, *tokens) == message
    assert message.count(removed) == 1
    negative = SimpleNamespace(exit_code=2, output=rendered(message.replace(removed, "")))
    with pytest.raises(AssertionError):
        required_input_display(negative, styled, *tokens)


def test_wrong_full_id_refuses_before_fetch(handoff, monkeypatch):
    _, receiver, remote, commit, _, _ = handoff
    publish(handoff, monkeypatch)
    wrong = "0" * 40
    git(remote, "update-ref", PREFIX + wrong, commit)
    original = grip.git
    calls = []
    def counted(cwd, *args, **kwargs):
        calls.append(args)
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(grip, "git", counted)
    result = cli(receiver, monkeypatch, "receive", "gr:" + wrong, "--remote", remote)
    assert result.exit_code == 2 and "remote_review_id_mismatch" in result.output
    assert not any(args[0] == "fetch" for args in calls)
    assert git(receiver, "for-each-ref", "--format=%(refname)", PREFIX) == ""


def root_owned_wrong_target(root, commit):
    # store init can leave HEAD unborn. Make a distinct ordinary commit in
    # this root's own object database, not the unpushed member HEAD.
    tree = git(root, "mktree", input="")
    target = git(root, "commit-tree", tree, "-m", "ordinary root object")
    assert target != commit and git(root, "cat-file", "-t", target) == "commit"
    return target


def test_local_wrong_target_refuses_before_decode(handoff, monkeypatch):
    author, _, _, commit, _, _ = handoff
    target = root_owned_wrong_target(author, commit)
    git(author, "update-ref", PREFIX + commit, target)
    def no_decode(*args, **kwargs):
        pytest.fail("mismatched ref reached decoder")
    original_verify = grip._verify_review_commit_in_store
    monkeypatch.setattr(grip, "_verify_review_commit_in_store", no_decode)
    result = cli(author, monkeypatch, "verify", "gr:" + commit)
    assert result.exit_code == 2 and "review_ref_target_mismatch" in result.output
    monkeypatch.setattr(grip, "_verify_review_commit_in_store", original_verify)
    git(author, "update-ref", PREFIX + commit, commit)
    assert grip.verify_review_commit(author, commit)["tree_matches"] is True


def test_changed_fetch_to_tag_does_not_accept_peeled_commit(handoff, monkeypatch):
    _, receiver, remote, commit, _, _ = handoff
    publish(handoff, monkeypatch)
    git(remote, "tag", "-a", "review-alias", "-m", "alias", commit)
    tag = git(remote, "rev-parse", "refs/tags/review-alias")
    assert tag != commit and git(remote, "rev-parse", "refs/tags/review-alias^{commit}") == commit
    original = grip.git
    def changed(cwd, *args, **kwargs):
        if args[0] == "fetch":
            git(remote, "update-ref", PREFIX + commit, tag)
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(grip, "git", changed)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 2 and "fetched_review_id_mismatch" in result.output and tag in result.output
    assert git(receiver, "for-each-ref", "--format=%(refname)", PREFIX) == ""
    assert git(receiver, "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__review_transfers__/") == ""
    git(remote, "update-ref", PREFIX + commit, commit)
    monkeypatch.setattr(grip, "git", original)
    accepted = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert accepted.exit_code == 0, accepted.output


def test_receive_does_not_replace_conflicting_canonical_ref(handoff, monkeypatch):
    _, receiver, remote, commit, base, _ = handoff
    publish(handoff, monkeypatch)
    git(receiver, "fetch", "--no-write-fetch-head", remote, "refs/heads/main")
    git(receiver, "update-ref", PREFIX + commit, base)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 2 and "review_ref_target_mismatch" in result.output
    assert git(receiver, "rev-parse", PREFIX + commit) == base


def test_cleanup_launch_error_preserves_primary(handoff, monkeypatch):
    _, receiver, remote, commit, _, _ = handoff
    publish(handoff, monkeypatch)
    original_git, original_bind = grip.git, grip._bind_git
    def fail_fetch(cwd, *args, **kwargs):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 7, "", "primary fetch failure")
        return original_git(cwd, *args, **kwargs)
    def fail_cleanup(cwd, *args):
        if args[0] == "rev-parse" and "__review_transfers__" in args[-1]:
            raise OSError("secondary cleanup failure")
        return original_bind(cwd, *args)
    monkeypatch.setattr(grip, "git", fail_fetch)
    monkeypatch.setattr(grip, "_bind_git", fail_cleanup)
    with pytest.raises(grip.GripCorruptError, match="review_fetch_unconfirmed.*primary fetch failure") as error:
        grip.receive_review_commit(receiver, commit, str(remote))
    assert any("secondary cleanup failure" in note for note in error.value.__notes__)


def test_corrupt_content_is_not_bound_or_allocated(handoff, monkeypatch):
    author, receiver, remote, _, _, _ = handoff
    tree = git(author, "mktree", input="")
    wrong_kind = git(author, "commit-tree", tree, "-m", "ordinary not review")
    git(author, "push", remote, wrong_kind + ":" + PREFIX + wrong_kind)
    original = authority(receiver)
    def no_publish(*args, **kwargs):
        pytest.fail("wrong kind reached canonical publication")
    monkeypatch.setattr(grip, "_publish_bind", no_publish)
    result = cli(receiver, monkeypatch, "receive", "gr:" + wrong_kind, "--remote", remote)
    assert result.exit_code == 2 and "not a gr2 review bind commit" in result.output
    assert git(receiver, "for-each-ref", "--format=%(refname)", PREFIX) == ""
    assert authority(receiver) == original


def test_listing_rejects_wrong_canonical_target(handoff):
    author, _, _, commit, _, _ = handoff
    target = root_owned_wrong_target(author, commit)
    git(author, "update-ref", PREFIX + commit, target)
    with pytest.raises(grip.GripCorruptError, match="review_ref_target_mismatch"):
        grip.list_review_binds(author)
    git(author, "update-ref", PREFIX + commit, commit)
    assert any(row[0] == commit for row in grip.list_review_binds(author))


def test_recomputed_tree_mismatch_is_not_published(handoff, monkeypatch):
    author, receiver, remote, commit, _, _ = handoff
    blob = git(author, "hash-object", "-w", "--stdin", input="unexpected content\n")
    entries = git(author, "ls-tree", commit) + "\n100644 blob " + blob + "\tunexpected.txt\n"
    tree = git(author, "mktree", input=entries)
    corrupt = git(author, "commit-tree", tree, "-m", "changed review tree")
    git(author, "push", remote, corrupt + ":" + PREFIX + corrupt)
    before = authority(receiver)
    def no_publish(*args, **kwargs):
        pytest.fail("non-recomputing review reached canonical publication")
    monkeypatch.setattr(grip, "_publish_bind", no_publish)
    result = cli(receiver, monkeypatch, "receive", "gr:" + corrupt, "--remote", remote)
    # A field tree is verified as written: the extra entry is refused by name before anything
    # recomputes (a legacy-layout record reports review_tree_mismatch for the same drift).
    assert result.exit_code == 2 and "unexpected.txt" in result.output, result.output
    assert git(receiver, "for-each-ref", "--format=%(refname)", PREFIX) == ""
    assert authority(receiver) == before


def test_listing_rejects_short_alias_but_explicit_abbreviation_still_reads(handoff):
    author, _, _, commit, _, _ = handoff
    short = commit[:12]
    target = root_owned_wrong_target(author, commit)
    git(author, "update-ref", PREFIX + short, target)
    with pytest.raises(grip.GripCorruptError, match="review_ref_identity_mismatch"):
        grip.list_review_binds(author)
    git(author, "update-ref", "-d", PREFIX + short, target)
    assert grip.show_review_commit(author, short)["id"] == "gr:" + commit
    assert any(row[0] == commit for row in grip.list_review_binds(author))
