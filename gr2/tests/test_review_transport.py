"""Exact existing native review refs across ordinary independent filesystems."""
from __future__ import annotations

import json
import shlex
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
from gr2.python_cli import grip, next_steps
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
    return _handoff(tmp_path, monkeypatch)


def _handoff(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    if verb == "publish":
        return  # a bare publish is every member's own remote (tests/test_review_publish_all.py)
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
    assert result.exit_code == 2 and "not a gr2 pinned review commit" in result.output
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


def test_a_field_tree_with_a_stray_entry_is_not_published(handoff, monkeypatch):
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


def test_a_legacy_bind_whose_tree_does_not_recompute_is_not_published(handoff, monkeypatch):
    """The legacy layout keeps its own gate: received content that does not recompute is refused."""
    from tests.review_ref_helper import legacy_bind_tree, legacy_review_ref
    author, receiver, remote, _, base, head = handoff
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head, title="t", body="b")
    tree = legacy_bind_tree(author, row)
    blob = git(author, "hash-object", "-w", "--stdin", input="unexpected content\n")
    stray = git(author, "mktree", input=git(author, "ls-tree", tree) + "\n100644 blob " + blob + "\tunexpected.txt\n")
    corrupt = git(author, "commit-tree", stray, "-m", "legacy review tree with a stray entry")
    ref = legacy_review_ref(corrupt)
    git(author, "push", remote, corrupt + ":" + ref)
    before = authority(receiver)
    monkeypatch.setattr(grip, "_publish_bind", lambda *a, **k: pytest.fail("non-recomputing review reached publication"))
    result = cli(receiver, monkeypatch, "receive", "gr:" + corrupt, "--remote", remote, "--ref", ref)
    assert result.exit_code == 2 and "review_tree_mismatch" in result.output, result.output
    assert git(receiver, "for-each-ref", "--format=%(refname)", PREFIX) == ""
    assert authority(receiver) == before


def _rows(result):
    return {r["key"]: r for r in json.loads(result.stdout)["heads"]}


def test_receive_fetches_the_pinned_head_into_the_member_clone(handoff, monkeypatch):
    """A reviewer's clone gets the pinned head from the remote it received from, so the set check can read it."""
    author, receiver, remote, commit, _, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    publish(handoff, monkeypatch)
    clone = receiver / "member"
    assert subprocess.run(["git", "cat-file", "-e", head + "^{commit}"], cwd=clone).returncode != 0  # control: absent
    first = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert first.exit_code == 0, first.output
    assert _rows(first)["member"]["status"] == "fetched", first.stdout
    assert git(clone, "cat-file", "-t", head) == "commit"
    again = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert again.exit_code == 0, again.output
    assert _rows(again)["member"]["status"] == "already_present", again.stdout


def test_receive_from_a_remote_that_is_not_the_members_fetches_nothing(handoff, monkeypatch, tmp_path):
    author, receiver, remote, commit, _, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    other = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", other)
    result = cli(author, monkeypatch, "publish", "gr:" + commit, "--remote", other)
    assert result.exit_code == 0, result.output
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", other)
    assert got.exit_code == 0, got.output
    assert _rows(got)["member"]["status"] == "not_this_remote", got.stdout
    assert subprocess.run(["git", "cat-file", "-e", head + "^{commit}"], cwd=receiver / "member").returncode != 0


def test_a_head_on_another_remote_names_open_and_what_still_needs_the_head(handoff, monkeypatch, tmp_path):
    """A review published to a remote that is not the member's (a staging remote): receive says the head is not
    there, that open reads the carried range while show --diff and stamp need the head, and names the open command,
    which runs exactly as printed."""
    author, receiver, remote, commit, _, _ = handoff
    staging = tmp_path / "staging.git"
    git(tmp_path, "init", "--bare", staging)
    assert cli(author, monkeypatch, "publish", "gr:" + commit, "--remote", staging).exit_code == 0
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", staging)
    assert got.exit_code == 0, got.output
    row = _rows(got)["member"]
    assert row["status"] == "not_this_remote", row
    assert row["reason"] == f"head_not_on_this_remote: its remote is {remote}; open reads the carried range; show --diff and stamp need the head there", row
    assert row["next"] == f"gr2 review open {receiver} gr:{commit}", row
    opened = runner.invoke(app, shlex.split(row["next"])[1:])  # the named next step runs as printed
    assert opened.exit_code == 0, opened.output
    lane = Path(opened.output.split("lane-dir=")[1].split()[0])
    assert git(lane, "rev-parse", "HEAD^{tree}") == git(author / "member", "rev-parse", "HEAD^{tree}")
    assert "head_not_fetched" in cli(receiver, monkeypatch, "stamp", receiver, "gr:" + commit).output  # as the reason says



def test_the_open_line_for_a_head_on_another_remote_quotes_a_workspace_with_a_space():
    """The printed open line splits back to the workspace path when the path has a space in it."""
    line = next_steps._open_from_range("/x y/ws", "gr:abc")
    assert shlex.split(line) == ["gr2", "review", "open", "/x y/ws", "gr:abc"], line

def test_a_head_the_remote_does_not_serve_is_named_with_the_command_to_run(handoff, monkeypatch):
    """The author never pushed the head: receive still binds the review, and names the head it could not fetch and
    the exact command that fetches it once the author pushes."""
    _, receiver, remote, commit, _, head = handoff
    publish(handoff, monkeypatch)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0, got.output
    row = _rows(got)["member"]
    assert row["status"] == "not_fetched" and row["reason"].startswith("head_fetch_refused"), row
    assert row["next"] == f"git -C {receiver / 'member'} fetch -- {remote} {head}", row
    assert git(receiver, "rev-parse", PREFIX + commit) == commit  # the bind is published all the same


def _set_check(root, monkeypatch, commit):
    monkeypatch.chdir(root)
    return runner.invoke(app, ["check", "set", str(root), "gr:" + commit, "--", "true"])


def test_a_received_review_passes_the_set_check_with_no_hand_fetch(handoff, monkeypatch):
    """The reviewer's flow end to end: receive, then the set check, with no git command in between."""
    author, receiver, remote, commit, _, _ = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    publish(handoff, monkeypatch)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0, got.output
    checked = _set_check(receiver, monkeypatch, commit)
    assert checked.exit_code == 0, checked.output


def test_a_set_check_on_a_clone_without_the_head_names_the_fetch(handoff, monkeypatch):
    _, receiver, remote, commit, _, head = handoff
    publish(handoff, monkeypatch)  # the author never pushed the head, so receive cannot fetch it
    assert cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote).exit_code == 0
    checked = _set_check(receiver, monkeypatch, commit)
    assert checked.exit_code != 0, checked.output
    assert "head_not_in_member_clone: member " + head in checked.output, checked.output
    assert f"git -C {receiver / 'member'} fetch -- {remote} {head}" in checked.output, checked.output


def test_a_head_a_force_push_left_unadvertised_is_named_with_the_command_to_run(handoff, monkeypatch):
    """The head was pushed, then the branch was force-pushed back, so no ref on the remote points at it any more.
    Whether a server serves a want for such a sha depends on the server (a local bare remote with this git does;
    a server with uploadpack.allowAnySHA1InWant off may not), so the receipt must agree with the clone either way:
    fetched means the head is there, not_fetched means it is not and names the command that fetches it."""
    author, receiver, remote, commit, base, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    git(author / "member", "push", "--force", remote, f"{base}:refs/heads/feat")
    assert head not in git(receiver, "ls-remote", remote)  # control: no ref advertises the head
    publish(handoff, monkeypatch)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0, got.output
    row = _rows(got)["member"]
    present = subprocess.run(["git", "cat-file", "-e", head + "^{commit}"], cwd=receiver / "member").returncode == 0
    if row["status"] == "fetched":
        assert present, row
    else:
        assert row["status"] == "not_fetched" and row["reason"].startswith("head_fetch_refused"), row
        assert not present and row["next"] == f"git -C {receiver / 'member'} fetch -- {remote} {head}", row


def test_the_printed_fetch_runs_as_printed_from_a_path_with_a_space(tmp_path, monkeypatch):
    """The author never pushed, so receive names the fetch; the author pushes, and the printed line, run verbatim
    by a shell, brings the head into the clone. Unquoted, the space split the path and git exited 128."""
    handoff = _handoff(tmp_path / "with space", monkeypatch)
    author, receiver, remote, commit, _, head = handoff
    publish(handoff, monkeypatch)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0, got.output
    row = _rows(got)["member"]
    assert row["status"] == "not_fetched", row
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    ran = subprocess.run(["sh", "-c", row["next"]], capture_output=True, text=True)
    assert ran.returncode == 0, (row["next"], ran.stderr)
    assert git(receiver / "member", "cat-file", "-t", head) == "commit"


def test_the_set_check_fetch_command_round_trips_through_a_shell_from_a_path_with_a_space(tmp_path, monkeypatch):
    handoff = _handoff(tmp_path / "with space", monkeypatch)
    _, receiver, remote, commit, _, head = handoff
    publish(handoff, monkeypatch)
    assert cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote).exit_code == 0
    checked = _set_check(receiver, monkeypatch, commit)
    assert checked.exit_code != 0, checked.output
    printed = strip_ansi(checked.output).split("`")[1]
    assert shlex.split(printed) == ["git", "-C", str(receiver / "member"), "fetch", "--", str(remote), head], printed


def test_a_member_remote_with_a_nul_refuses_that_member_and_fetches_the_other(handoff, monkeypatch):
    """A record whose member remote carries a NUL: that member is a refused row naming why, the other member's head
    is still fetched, and the receipt is printed. Before, Path(remote).resolve() raised ValueError out of receive."""
    author, receiver, remote, commit, _, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    publish(handoff, monkeypatch)
    shown = grip.show_review_commit

    def with_nul_member(*args, **kwargs):
        out = shown(*args, **kwargs)
        out["members"] = [{**out["members"][0], "key": "bad", "path": "bad", "remote": "/x\0y"}, *out["members"]]
        return out

    monkeypatch.setattr(grip, "show_review_commit", with_nul_member)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0 and "Traceback" not in got.output, got.output
    rows = _rows(got)
    assert rows["bad"]["status"] == "refused", rows
    assert rows["bad"]["reason"] == "member_remote_control_character: '/x\\x00y'", rows
    assert rows["member"]["status"] == "fetched", rows
    assert git(receiver / "member", "cat-file", "-t", head) == "commit"


def test_a_member_path_with_a_nul_refuses_that_member_and_fetches_the_other(handoff, monkeypatch):
    """The same crash one field over: a member PATH carrying a NUL is a refused row naming the path, never a ValueError
    from resolving it, and the other member's head is still fetched."""
    author, receiver, remote, commit, _, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    publish(handoff, monkeypatch)
    shown = grip.show_review_commit

    def with_nul_path(*args, **kwargs):
        out = shown(*args, **kwargs)
        out["members"] = [{**out["members"][0], "key": "bad", "path": "b\0ad"}, *out["members"]]
        return out

    monkeypatch.setattr(grip, "show_review_commit", with_nul_path)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0 and "Traceback" not in got.output, got.output
    rows = _rows(got)
    assert rows["bad"]["status"] == "refused", rows
    assert rows["bad"]["reason"] == "member_path_control_character: 'b\\x00ad'", rows
    assert rows["member"]["status"] == "fetched", rows
    assert git(receiver / "member", "cat-file", "-t", head) == "commit"


@pytest.mark.parametrize("bad", ["/x\0y", "/x\ny", "/x\x1fy", "/x\x7fy", "https://github.com/o/a\0b"])
def test_a_control_character_in_the_receive_remote_is_refused_before_any_git_call(handoff, monkeypatch, bad):
    _, receiver, _, commit, _, _ = handoff

    def no_git(*args, **kwargs):
        raise AssertionError(f"git ran: {args}")

    monkeypatch.setattr(grip, "git", no_git)
    monkeypatch.setattr(grip, "_bind_git", no_git)
    with pytest.raises(grip.GripCorruptError, match=r"\Areview_remote_control_character: "):
        grip.receive_review_commit(receiver, "gr:" + commit, bad)


@pytest.mark.parametrize("bad", ["git@github.com:o/a\0.git", "https://github.com/o/a\x01b", "/x\0y", "/x\ty",
                                 "/x\x7fy", "ssh://git@github.com/o/a\x1b.git"])
@pytest.mark.parametrize("allow_local", [False, True])
def test_canonical_source_identity_refuses_a_control_character(bad, allow_local):
    """Never an identity with a NUL in it (the GitHub patterns took one), never a ValueError from the filesystem."""
    from gr2.python_cli.review import ReviewError, canonical_source_identity
    with pytest.raises(ReviewError, match=r"\Aremote_control_character: "):
        canonical_source_identity(bad, allow_local=allow_local)


# C1 (U+0085 NEL, U+009B one-byte CSI), bidi (U+202E), zero-width (U+200B), line separator (U+2028): each passed the
# C0+DEL check, and U+202E / U+009B landed inside an identity, a display spoof wherever it is printed raw.
_UNSAFE = ["\x9b", "\u202e", "\u200b", "\u2028", "\x85", "\u200c", "\u200d"]
_LEGIT = ["ünï", "日本", "\U0001f600"]


def _member_row(handoff, monkeypatch, member_remote):
    author, receiver, remote, commit, _, head = handoff
    git(author / "member", "push", remote, "HEAD:refs/heads/feat")
    publish(handoff, monkeypatch)
    shown = grip.show_review_commit

    def with_member(*args, **kwargs):
        out = shown(*args, **kwargs)
        out["members"] = [{**out["members"][0], "key": "odd", "path": "odd", "remote": member_remote},
                          *out["members"]]
        return out

    monkeypatch.setattr(grip, "show_review_commit", with_member)
    got = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert got.exit_code == 0 and "Traceback" not in got.output, got.output
    rows = _rows(got)
    assert rows["member"]["status"] == "fetched", rows
    return rows["odd"]


@pytest.mark.parametrize("ch", _UNSAFE)
def test_a_member_remote_with_unsafe_text_is_a_refused_row(handoff, monkeypatch, ch):
    bad = f"/x{ch}y"
    row = _member_row(handoff, monkeypatch, bad)
    assert row["status"] == "refused" and row["reason"] == f"member_remote_control_character: {bad!r}", row


@pytest.mark.parametrize("ch", _LEGIT)
def test_a_member_remote_with_non_ascii_letters_is_not_refused(handoff, monkeypatch, ch):
    row = _member_row(handoff, monkeypatch, f"/x{ch}y")
    assert row["status"] != "refused", row


@pytest.mark.parametrize("ch", _UNSAFE)
@pytest.mark.parametrize("shape", ["/x{}y", "https://github.com/o/a{}b"])
def test_unsafe_text_in_the_receive_remote_is_refused_before_any_git_call(handoff, monkeypatch, ch, shape):
    _, receiver, _, commit, _, _ = handoff

    def no_git(*args, **kwargs):
        raise AssertionError(f"git ran: {args}")

    monkeypatch.setattr(grip, "git", no_git)
    monkeypatch.setattr(grip, "_bind_git", no_git)
    with pytest.raises(grip.GripCorruptError, match=r"\Areview_remote_control_character: "):
        grip.receive_review_commit(receiver, "gr:" + commit, shape.format(ch))


@pytest.mark.parametrize("ch", _LEGIT)
def test_a_receive_remote_with_non_ascii_letters_is_not_refused(tmp_path, ch):
    path = tmp_path / f"r{ch}"
    path.mkdir()
    assert grip._review_transport_remote(str(path)).endswith(f"r{ch}")


@pytest.mark.parametrize("ch", _UNSAFE)
@pytest.mark.parametrize("shape", ["git@github.com:o/a{}.git", "https://github.com/o/a{}b", "/x{}y",
                                   "ssh://git@github.com/{}o/a.git"])
@pytest.mark.parametrize("allow_local", [False, True])
def test_canonical_source_identity_refuses_unsafe_text(ch, shape, allow_local):
    from gr2.python_cli.review import ReviewError, canonical_source_identity
    with pytest.raises(ReviewError, match=r"\Aremote_control_character: "):
        canonical_source_identity(shape.format(ch), allow_local=allow_local)


@pytest.mark.parametrize("ch", _LEGIT)
def test_canonical_source_identity_keeps_non_ascii_letters(ch):
    from gr2.python_cli.review import canonical_source_identity
    assert canonical_source_identity(f"https://github.com/o/a{ch}") == f"https://github.com/o/a{ch}".lower()
    assert canonical_source_identity(f"/x{ch}y", allow_local=True).endswith(f"/x{ch}y")


@pytest.mark.parametrize("url", ["\x1fgit@github.com:o/a.git", "git@github.com:o/a.git\x1f", "git@github.com:o/a.git\x85",
                                 "git@github.com:o/a.git\t", "https://github.com/o/a\n", "https://github.com/o/a\u2028"])
@pytest.mark.parametrize("allow_local", [False, True])
def test_canonical_source_identity_checks_the_raw_input(url, allow_local):
    """The check reads the input before any strip: str.strip() drops \\t, U+0085 and U+001C..U+001F, so a stripped
    check would turn these into https://github.com/o/a while grip refuses the same raw remote."""
    from gr2.python_cli.review import ReviewError, canonical_source_identity
    with pytest.raises(ReviewError, match=r"\Aremote_control_character: "):
        canonical_source_identity(url, allow_local=allow_local)


def test_canonical_source_identity_still_strips_spaces():
    from gr2.python_cli.review import canonical_source_identity
    assert canonical_source_identity(" https://github.com/O/A.git ") == "https://github.com/o/a"


@pytest.mark.parametrize("ws", ["\u00a0", "\u3000", "\u2000"])
@pytest.mark.parametrize("url", ["git@github.com:O/A.git{}", "{}git@github.com:O/A.git", "{}https://github.com/O/A{}"])
@pytest.mark.parametrize("allow_local", [False, True])
def test_canonical_source_identity_strips_unicode_whitespace_to_the_plain_identity(ws, url, allow_local):
    """A space that prints as nothing is not a second identity: a trailing U+00A0 or U+3000 keys the same as the plain
    URL, and a leading one does not turn a GitHub origin into local:<cwd>/..."""
    from gr2.python_cli.review import canonical_source_identity
    assert canonical_source_identity(url.format(ws, ws), allow_local=allow_local) == "https://github.com/o/a"


@pytest.mark.parametrize("prefix, suffix", [("\x1f", ""), ("", "\x1f"), ("", "\x85")])
def test_remote_origin_url_keeps_unicode_whitespace_for_the_check(tmp_path, prefix, suffix):
    """remote_origin_url drops git's one line terminator only, so the identity check sees what git config holds."""
    import subprocess
    from gr2.python_cli.gitops import remote_origin_url
    from gr2.python_cli.review import ReviewError, canonical_source_identity
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(tmp_path), "config", "remote.origin.url", "git@github.com:o/a.git"], check=True)
    assert remote_origin_url(tmp_path) == "git@github.com:o/a.git"
    raw = f"{prefix}git@github.com:o/a.git{suffix}"
    subprocess.run(["git", "-C", str(tmp_path), "config", "remote.origin.url", raw], check=True)
    assert remote_origin_url(tmp_path) == raw
    with pytest.raises(ReviewError, match=r"\Aremote_control_character: "):
        canonical_source_identity(remote_origin_url(tmp_path), allow_local=True)


# The predicate table, owned here (not read from gitops). A person's name keeps U+200C ZWNJ and U+200D ZWJ (Persian and
# Kurdish names are written with them) and refuses every other Cf; an identifier (remote, path, ref) refuses every Cf.
# Each character sits in the middle of the text, so a check of only the first or last character does not pass.
_BIDI = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"]
_REFUSED_BY_BOTH = [*_BIDI, "\x00", "\x1b", "\x7f", "\x85", "\x9b", "\u2028", "\u2029",
                    "\u200b", "\u200e", "\u200f", "\u2060", "\ufeff", "\u00ad", "\u061c"]
_NAME_ONLY = ["\u200c", "\u200d"]
_ACCEPTED_BY_BOTH = ["ü", "日", "\U0001f600", " ", "-", "\u200a", "\u0640"]


@pytest.mark.parametrize("ch", _REFUSED_BY_BOTH)
def test_both_forms_refuse_controls_separators_and_bidi(ch):
    from gr2.python_cli.gitops import unsafe_in_identifier, unsafe_in_name
    assert unsafe_in_name(f"ab{ch}cd") is True
    assert unsafe_in_identifier(f"ab{ch}cd") is True


@pytest.mark.parametrize("ch", _NAME_ONLY)
def test_a_name_keeps_zwnj_and_zwj(ch):
    from gr2.python_cli.gitops import unsafe_in_name
    assert unsafe_in_name(f"\u0645\u0647\u0631{ch}\u0646\u0648\u0634") is False


@pytest.mark.parametrize("ch", _NAME_ONLY)
def test_an_identifier_refuses_zwnj_and_zwj(ch):
    from gr2.python_cli.gitops import unsafe_in_identifier
    assert unsafe_in_identifier(f"ab{ch}cd") is True


@pytest.mark.parametrize("ch", _ACCEPTED_BY_BOTH)
def test_both_forms_accept_letters_and_ordinary_spaces(ch):
    from gr2.python_cli.gitops import unsafe_in_identifier, unsafe_in_name
    assert unsafe_in_name(f"ab{ch}cd") is False
    assert unsafe_in_identifier(f"ab{ch}cd") is False
