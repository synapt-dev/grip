"""`gr2 review update <rid>`: bring a pinned review up to its moved target, then pin again.

Real git, bare local remotes, no network. The author workspace has a space in its name, so every printed line is
run exactly as printed (shlex), the way an operator pastes it.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import approvals, check_records, merge_gate
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX

runner = CliRunner()


def git(root, *args, check=True):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0 or not check, p.stderr
    return p.stdout.strip()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "Fixture"), ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
                 ("GIT_COMMITTER_NAME", "Fixture"), ("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
                 ("GIT_CONFIG_NOSYSTEM", "1"), ("GIT_TERMINAL_PROMPT", "0")):
        monkeypatch.setenv(k, v)


def build(tmp_path, monkeypatch, names):
    """Members pinned as ONE review (each with its source), feature pushed, review published on every remote."""
    author = tmp_path / "au thor"
    author.mkdir()
    w = {"author": author, "members": {}, "tmp": tmp_path}
    for name in names:
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "--bare", "-b", "main", remote)
        seed.mkdir()
        git(seed, "init", "-b", "main")
        (seed / "payload.txt").write_text(f"{name} base\n")
        (seed / "shared.txt").write_text("one\n")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "base")
        git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        w["members"][name] = {"remote": remote, "base": git(seed, "rev-parse", "HEAD"), "repo": author / name}
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    rows = []
    for name, m in w["members"].items():
        git(m["repo"], "checkout", "-b", "feat")
        (m["repo"] / "payload.txt").write_text(f"{name} reviewed\n")
        git(m["repo"], "commit", "-am", "reviewed")
        m["head"] = git(m["repo"], "rev-parse", "HEAD")
        rows.append({"key": name, "path": name, "remote": str(m["remote"]), "base": m["base"], "head": m["head"],
                     "ref": "refs/heads/main", "source": str(m["repo"])})
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "pin", str(author), "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    w["review"] = made.stdout.strip().splitlines()[-1]
    for m in w["members"].values():
        git(m["repo"], "push", m["remote"], "feat")
        out = runner.invoke(app, ["review", "publish", str(author), w["review"], "--remote", str(m["remote"])])
        assert out.exit_code == 0, out.output
    return w


def move_main(w, name, path="unrelated.txt", text="someone else\n"):
    m = w["members"][name]
    other = w["tmp"] / f"other-{name}-{path}"
    git(w["tmp"], "clone", "--no-local", "--branch", "main", m["remote"], other)
    (other / path).write_text(text)
    git(other, "add", path)
    git(other, "commit", "-m", "main moved")
    git(other, "push", "origin", "main")
    return git(m["remote"], "rev-parse", "main")


def update(w, *extra, ws=True):
    argv = ["review", "update", *([str(w["author"])] if ws else []), w["review"], *extra]
    return runner.invoke(app, argv)


def receipt(result):
    return json.loads(result.stdout)


def binds(w):
    return git(w["author"], "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).splitlines()


def parents(repo, rev="HEAD"):
    return git(repo, "rev-list", "--parents", "-n", "1", rev).split()[1:]


@pytest.fixture
def one(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch, ["alpha"])


@pytest.fixture
def two(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch, ["alpha", "beta"])


def test_u1_a_clean_update_merges_the_moved_tip_keeping_the_reviewed_head(one):
    tip = move_main(one, "alpha")
    out = update(one)
    assert out.exit_code == 0, out.output
    repo = one["members"]["alpha"]["repo"]
    assert parents(repo) == [one["members"]["alpha"]["head"], tip], "a merge, with the reviewed head as first parent"
    assert receipt(out)["new_review"].startswith("gr:")


def test_u2_the_new_pin_has_the_live_tip_as_base_and_the_old_pin_is_unchanged(one):
    tip = move_main(one, "alpha")
    out = update(one)
    assert out.exit_code == 0, out.output
    new = runner.invoke(app, ["review", "show", str(one["author"]), receipt(out)["new_review"], "--json"])
    row = json.loads(new.stdout)["members"][0]
    assert (row["base"], row["head"]) == (tip, git(one["members"]["alpha"]["repo"], "rev-parse", "HEAD"))
    old = json.loads(runner.invoke(app, ["review", "show", str(one["author"]), one["review"], "--json"]).stdout)["members"][0]
    assert (old["base"], old["head"]) == (one["members"]["alpha"]["base"], one["members"]["alpha"]["head"])


def run_printed(line, cwd):
    """Run one printed `next:` line exactly as printed; gr2 lines through the CLI, git lines through git."""
    argv = shlex.split(line.removeprefix("next: "))
    if argv[0] == "gr2":
        return runner.invoke(app, argv[1:]).exit_code
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True).returncode


def test_u3_p1_the_printed_next_lines_run_as_printed_and_the_review_then_lands(one):
    move_main(one, "alpha")
    out = update(one)
    assert out.exit_code == 0, out.output
    nexts = [l for l in out.stderr.splitlines() if l.startswith("next: ")]
    assert [shlex.split(l)[1:3] for l in nexts] == [["gr2", "review"]], nexts
    for line in nexts:
        assert run_printed(line, one["tmp"]) == 0, line
    m, new = one["members"]["alpha"], receipt(out)["new_review"]
    head = git(m["repo"], "rev-parse", "HEAD")
    check_records.run_check(m["repo"], str(m["remote"]), head, "test", [sys.executable, "-c", "pass"])
    landed = runner.invoke(app, ["review", "merge", str(one["author"]), new, "--approvals", "0"])
    assert landed.exit_code == 0, landed.output
    assert parents(m["remote"], "main") == [receipt(out)["members"][0]["base"], head]


def conflict(w, name):
    move_main(w, name, "shared.txt", "theirs\n")
    repo = w["members"][name]["repo"]
    (repo / "shared.txt").write_text("ours\n")
    git(repo, "commit", "-am", "ours")
    return repo


@pytest.fixture
def conflicted(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    repo = w["members"]["alpha"]["repo"]
    (repo / "shared.txt").write_text("ours\n")
    git(repo, "commit", "-am", "ours")
    w["members"]["alpha"]["head"] = git(repo, "rev-parse", "HEAD")
    rows = [{"key": "alpha", "path": "alpha", "remote": str(w["members"]["alpha"]["remote"]),
             "base": w["members"]["alpha"]["base"], "head": w["members"]["alpha"]["head"],
             "ref": "refs/heads/main", "source": str(repo)}]
    (tmp_path / "rows2.json").write_text(json.dumps(rows))
    w["review"] = runner.invoke(app, ["review", "pin", str(w["author"]), "--rows-json",
                                      str(tmp_path / "rows2.json")]).stdout.strip().splitlines()[-1]
    move_main(w, "alpha", "shared.txt", "theirs\n")
    return w


def test_u4_a_conflict_exits_3_names_exactly_the_unmerged_paths_and_pins_nothing(conflicted):
    before = binds(conflicted)
    out = update(conflicted)
    assert out.exit_code == 3, out.output
    assert receipt(out)["members"][0]["conflicts"] == ["shared.txt"]
    assert "  shared.txt" in out.stderr.splitlines()
    assert binds(conflicted) == before, "no review may be pinned while a member is conflicted"


def test_u5_the_receipt_reports_the_conflict_state_and_no_new_review(conflicted):
    out = update(conflicted)
    r = receipt(out)
    assert (r["members"][0]["state"], r["new_review"], r["exit"]) == ("conflict", None, 3), r


def test_u6_resolve_commit_and_rerun_pins_again(conflicted):
    assert update(conflicted).exit_code == 3
    repo = conflicted["members"]["alpha"]["repo"]
    (repo / "shared.txt").write_text("both\n")
    git(repo, "commit", "-am", "resolved")
    out = update(conflicted)
    assert out.exit_code == 0, out.output
    r = receipt(out)
    assert r["members"][0]["state"] == "up_to_date" and r["new_review"], r
    assert len(parents(repo)) == 2, "the rerun must not merge a second time"


def test_u7_one_conflicted_member_pins_nobody_and_the_clean_merge_is_kept(two):
    move_main(two, "alpha")
    repo = conflict(two, "beta")
    before = binds(two)
    out = update(two)
    assert out.exit_code == 3, out.output
    assert binds(two) == before and receipt(out)["new_review"] is None
    assert len(parents(two["members"]["alpha"]["repo"])) == 2, "the clean member's merge stays in its clone"
    assert receipt(out)["members"][1]["conflicts"] == ["shared.txt"] and repo.exists()


def test_u8_publishing_while_a_member_is_mid_merge_refuses_from_git(conflicted):
    assert update(conflicted).exit_code == 3
    m = conflicted["members"]["alpha"]
    out = runner.invoke(app, ["review", "publish", str(conflicted["author"]), conflicted["review"],
                              "--remote", str(m["remote"])])
    assert out.exit_code == 2, out.output
    assert "unresolved_merge" in out.stderr and "shared.txt" in out.stderr


def test_u9_merge_base_moved_names_update_and_that_line_runs_as_printed(one):
    move_main(one, "alpha")
    m = one["members"]["alpha"]
    check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", [sys.executable, "-c", "pass"])
    code, rec = merge_gate.review_merge(one["author"], one["review"], into="main", feature="feat",
                                        required_approvals=0)
    row = rec["members"][0]
    assert code == merge_gate.EXIT_REFUSED and row["refused"].startswith("base_moved"), rec
    assert "gr2 review update" in row["next"], row["next"]
    line = "next: gr2 " + row["next"].split("gr2 ", 1)[1]
    assert run_printed(line, one["tmp"]) == 0, line
    assert len(parents(m["repo"])) == 2


def test_u10_a_stamp_on_a_moved_base_names_update_and_the_review(one):
    move_main(one, "alpha")
    m = one["members"]["alpha"]
    with pytest.raises(approvals.ApprovalRefused) as exc:
        approvals._refuse_moved_base(one["author"], "alpha", str(m["remote"]), m["base"], one["review"])
    assert "gr2 review update" in exc.value.next_step and one["review"] in exc.value.next_step, exc.value.next_step


def test_u11_update_fetches_from_the_rows_remote_not_the_clones_origin(one, tmp_path):
    m = one["members"]["alpha"]
    mirror = tmp_path / "stale-mirror.git"
    git(tmp_path, "clone", "--bare", m["remote"], mirror)
    git(m["repo"], "remote", "set-url", "origin", mirror)
    tip = move_main(one, "alpha")
    out = update(one)
    assert out.exit_code == 0, out.output
    assert parents(m["repo"])[1] == tip and receipt(out)["new_review"], out.output


def test_u12_a_dirty_member_refuses_before_any_member_is_merged(two):
    move_main(two, "alpha")
    move_main(two, "beta")
    alpha, beta = two["members"]["alpha"]["repo"], two["members"]["beta"]["repo"]
    head = git(alpha, "rev-parse", "HEAD")
    (beta / "payload.txt").write_text("half done\n")
    out = update(two)
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("member_dirty: beta: payload.txt"), receipt(out)
    assert git(alpha, "rev-parse", "HEAD") == head, "nothing may be merged anywhere"



def test_u15_an_untracked_file_where_main_adds_one_refuses_before_any_member_is_merged(two):
    move_main(two, "alpha")
    move_main(two, "beta", path="new.txt")
    alpha, beta = two["members"]["alpha"]["repo"], two["members"]["beta"]["repo"]
    head = git(alpha, "rev-parse", "HEAD")
    (beta / "new.txt").write_text("mine, never added\n")
    (beta / "elsewhere.txt").write_text("untracked at a path main does not touch\n")
    out = update(two)
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("member_untracked_in_the_way: beta: new.txt"), receipt(out)
    assert git(alpha, "rev-parse", "HEAD") == head, "nothing may be merged anywhere"
    assert not receipt(out)["new_review"]


def test_u28_an_ignored_file_where_main_adds_a_tracked_one_refuses_before_any_member_is_merged(two):
    """Git's merge overwrites an ignored file the target adds as tracked, and an undo deletes it. Either way the
    user's file is gone, so it is in the way exactly as an untracked one is."""
    move_main(two, "alpha", path="local.env", text="from main\n")
    move_main(two, "beta")
    alpha = two["members"]["alpha"]["repo"]
    with open(alpha / ".git" / "info" / "exclude", "a") as f:
        f.write("local.env\n")
    mine = b"LOCAL=user bytes, never added\r\n"
    (alpha / "local.env").write_bytes(mine)
    assert git(alpha, "status", "--porcelain") == "", "local.env must be ignored, not merely untracked"
    heads = {n: git(m["repo"], "rev-parse", "HEAD") for n, m in two["members"].items()}
    out = update(two)
    assert (alpha / "local.env").exists() and (alpha / "local.env").read_bytes() == mine, "the user's file is kept"
    assert {n: git(m["repo"], "rev-parse", "HEAD") for n, m in two["members"].items()} == heads, "no merge anywhere"
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("member_untracked_in_the_way: alpha: local.env"), receipt(out)
    assert not receipt(out)["new_review"]


def test_u16_a_fetch_failure_on_a_later_member_refuses_before_any_member_is_merged(two):
    move_main(two, "alpha")
    alpha, beta = two["members"]["alpha"], two["members"]["beta"]
    head = git(alpha["repo"], "rev-parse", "HEAD")
    beta["remote"].rename(beta["remote"].with_name("beta-gone.git"))
    out = update(two, "--into", "main")  # named, so the fetch is the first step to reach beta's remote
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("fetch_failed: beta:"), receipt(out)
    assert git(alpha["repo"], "rev-parse", "HEAD") == head, "nothing may be merged anywhere"

def test_u18_a_member_already_mid_merge_refuses_unresolved_merge_before_any_member_is_merged(two):
    # The read is publish's own preflight (grip._member_preflight), shared, not a second copy in update.
    move_main(two, "alpha")
    beta = conflict(two, "beta")
    git(beta, "fetch", "-q", str(two["members"]["beta"]["remote"]), "main")
    git(beta, "merge", "--no-edit", "FETCH_HEAD", check=False)
    alpha = two["members"]["alpha"]["repo"]
    head, before = git(alpha, "rev-parse", "HEAD"), binds(two)
    out = update(two)
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"] == "unresolved_merge: beta: shared.txt; resolve or abort the merge in this clone"
    ws = two["author"].resolve()  # the fixture path holds a space, so the line must be shell-quoted to run as printed
    assert (f"next: resolve in {shlex.quote(str(beta.resolve()))}, git add, git commit (or git merge --abort), then run "
            f"gr2 review update {shlex.quote(str(ws))} {receipt(out)['review']}") in out.stderr, out.stderr
    assert git(alpha, "rev-parse", "HEAD") == head, "nothing may be merged anywhere"
    assert binds(two) == before and receipt(out)["new_review"] is None


def test_u13_stdout_is_one_json_receipt_with_no_flag_and_the_human_lines_go_to_stderr(one):
    move_main(one, "alpha")
    out = update(one)
    assert out.exit_code == 0, out.output
    assert len(out.stdout.strip().splitlines()) == 1 and receipt(out)["exit"] == 0
    assert "next: " in out.stderr and "next: " not in out.stdout


def test_u14_run_from_a_member_subdirectory_resolves_the_same_workspace(one, monkeypatch):
    move_main(one, "alpha")
    sub = one["members"]["alpha"]["repo"] / "deep"
    sub.mkdir()
    monkeypatch.chdir(sub)
    out = update(one, ws=False)
    assert out.exit_code == 0, out.output
    r = receipt(out)
    assert r["review"] == one["review"] and r["new_review"] and r["members"][0]["state"] == "merged", r


def test_u17_a_bare_update_from_the_workspace_resolves_the_one_pinned_review(one):
    tip = move_main(one, "alpha")
    out = runner.invoke(app, ["review", "update"])  # cwd is the workspace; no root, no review id
    assert out.exit_code == 0, out.output
    r = receipt(out)
    assert r["review"] == one["review"] and r["new_review"], r
    assert parents(one["members"]["alpha"]["repo"]) == [one["members"]["alpha"]["head"], tip]


def test_u19_a_pin_refused_after_members_merged_restores_them_and_exits_2(two, monkeypatch):
    """The members merged, then the pin was refused: each merge is undone, so nothing
    moved and the exit is 2; the receipt names the clones it restored."""
    from gr2.python_cli import grip

    move_main(two, "alpha")
    move_main(two, "beta")

    def refuse(*args, **kwargs):
        raise grip.GripReviewRefused("pin_refused_in_test", "a pin", "refused")

    monkeypatch.setattr(grip, "create_review_bind_commit", refuse)
    out = update(two)
    r = receipt(out)
    assert out.exit_code == 2, out.output
    assert r["refused"].startswith("pin_refused") and r["merged"] == [] and r["restored"] == ["alpha", "beta"], r
    assert "partial:" not in out.stderr and "restored:" in out.stderr and "alpha, beta" in out.stderr, out.stderr
    for m in two["members"].values():
        assert git(m["repo"], "rev-parse", "HEAD") == m["head"], "the merge must be undone"



def test_u26_a_member_whose_reset_fails_stays_merged_and_the_exit_is_4(two, monkeypatch):
    """Both members merged, then the pin was refused, and beta cannot be reset (its index.lock is taken after
    its merge, before the restore). Alpha is restored; beta stays in merged, so the exit is 4, not 2."""
    from gr2.python_cli import grip

    move_main(two, "alpha")
    move_main(two, "beta")
    lock = two["members"]["beta"]["repo"] / ".git" / "index.lock"

    def refuse(*args, **kwargs):
        lock.write_text("")
        raise grip.GripReviewRefused("pin_refused_in_test", "a pin", "refused")

    monkeypatch.setattr(grip, "create_review_bind_commit", refuse)
    out = update(two)
    lock.unlink()
    r = receipt(out)
    alpha, beta = two["members"]["alpha"], two["members"]["beta"]
    assert git(alpha["repo"], "rev-parse", "HEAD") == alpha["head"], "alpha is restored"
    assert git(beta["repo"], "rev-parse", "HEAD") != beta["head"], "beta keeps its merge"
    assert r["refused"].startswith("pin_refused") and r["merged"] == ["beta"] and r["restored"] == ["alpha"], r
    assert out.exit_code == 4, out.output

def test_u20_a_target_fast_forwarded_to_the_reviewed_head_is_not_reported_current(one):
    """The work already landed: the target is now the reviewed head. The member is up to date, but its base
    moved, so the review is not current and a new pin is refused because the head is already on the remote.
    It must not exit 0 as if nothing changed."""
    m = one["members"]["alpha"]
    git(m["repo"], "push", m["remote"], "feat:main")
    before = binds(one)
    out = update(one)
    r = receipt(out)
    assert out.exit_code == 2, out.output
    assert r["refused"].startswith("pin_refused") and "head_already_on_remote" in r["refused"], r
    assert r["merged"] == [] and r["new_review"] is None, r
    assert r["members"][0]["state"] == "up_to_date" and r["members"][0]["base"] == m["head"], r
    assert binds(one) == before


def test_update_by_remote_name_writes_no_configured_ref_or_tag(tmp_path, monkeypatch):
    """A pin row may name its remote (`origin`) when the workspace resolves it; update's fetch of the target then goes
    by that name in the member clone, where a configured refspec and tagOpt=--tags would write refs and tags."""
    author, remote, seed = tmp_path / "au thor", tmp_path / "a.git", tmp_path / "seed"
    author.mkdir()
    seed.mkdir()
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    git(seed, "push", remote, "main")
    git(author, "clone", "--no-local", "--branch", "main", remote, "a")
    repo, base = author / "a", git(seed, "rev-parse", "HEAD")
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    git(author, "remote", "add", "origin", remote)
    git(repo, "checkout", "-b", "feat")
    (repo / "payload.txt").write_text("reviewed\n")
    git(repo, "commit", "-am", "reviewed")
    rows = [{"key": "a", "path": "a", "remote": "origin", "base": base, "head": git(repo, "rev-parse", "HEAD"),
             "ref": "refs/heads/main", "source": str(repo)}]
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "pin", str(author), "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    git(repo, "push", "origin", "feat")
    git(seed, "commit", "--allow-empty", "-m", "moved")
    git(seed, "push", remote, "main")
    git(remote, "tag", "t1", "main")
    git(repo, "branch", "keep", base)
    git(repo, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/main:refs/heads/keep")
    git(repo, "config", "remote.origin.tagOpt", "--tags")
    out = runner.invoke(app, ["review", "update", str(author), made.stdout.strip().splitlines()[-1]])
    assert out.exit_code == 0, out.output
    assert git(repo, "rev-parse", "keep") == base
    assert git(repo, "tag", "-l") == ""


def land_the_same_change(w, name):
    """Main takes the reviewed change by another commit (a squash): merging it into feat adds nothing."""
    return move_main(w, name, path="payload.txt", text=f"{name} reviewed\n")


def clone_state(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "write-tree"), git(repo, "status", "--porcelain"),
            git(repo, "rev-parse", "--abbrev-ref", "HEAD"))


def test_u21_a_refused_update_leaves_every_member_clone_as_it_found_it(two):
    """The pin refused range_adds_nothing after the members merged. The refusal must
    restore each clone (HEAD, index, worktree), so nothing moved and the exit says so."""
    for name in two["members"]:
        land_the_same_change(two, name)
    before = {n: clone_state(m["repo"]) for n, m in two["members"].items()}
    out = update(two)
    r = receipt(out)
    assert r["refused"].startswith("pin_refused") and "range_adds_nothing" in r["refused"], r
    assert r["new_review"] is None, r
    assert {n: clone_state(m["repo"]) for n, m in two["members"].items()} == before, "a refusal moves no clone"
    assert out.exit_code == 2 and r["merged"] == [] and r["restored"] == ["alpha", "beta"], r


def test_u22_a_second_refused_update_does_not_stack_another_merge(two):
    for name in two["members"]:
        land_the_same_change(two, name)
    heads = {n: git(m["repo"], "rev-parse", "HEAD") for n, m in two["members"].items()}
    first, second = update(two), update(two)
    assert receipt(first)["refused"] and receipt(second)["refused"], (first.output, second.output)
    for n, m in two["members"].items():
        assert git(m["repo"], "rev-parse", "HEAD") == heads[n], f"{n}: refused runs stacked a merge"
        assert len(parents(m["repo"])) == 1, n
