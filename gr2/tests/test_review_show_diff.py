"""`gr2 review show --diff`: a reviewer reads each member's change from the pinned review's own objects.

The diff is read from the member's OBJECTS (`git diff <base> <head>` in the member clone), never from the pinned
range.patch text: that text is ambiguous (an empty commit's message can carry a whole forged stanza). Each row
compares against git run in the member clone that made the commits.
"""
from __future__ import annotations

import json

from gr2.python_cli.app import app
from tests.test_review_update import build, git, move_main, runner, _env  # noqa: F401


def _show(w, *args):
    return runner.invoke(app, ["review", "show", str(w["author"]), w["review"], *args])


def _diff(m, a, b):
    return git(m["repo"], "diff", a, b) + "\n"


def test_diff_prints_each_members_exact_diff(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha", "beta"])
    out = _show(w, "--diff")
    assert out.exit_code == 0, out.output
    for name, m in w["members"].items():
        expected = _diff(m, m["base"], m["head"])
        assert expected in out.stdout, (name, out.stdout)
    assert out.stdout.count("  diff: 1 file, +1 -1\n") == 2
    # each member's diff sits under its own block
    assert out.stdout.index("alpha: ") < out.stdout.index("+alpha reviewed") < out.stdout.index("beta: ")


def test_stat_prints_the_summary_only(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha", "beta"])
    out = _show(w, "--stat")
    assert out.exit_code == 0, out.output
    assert out.stdout.count("  diff: 1 file, +1 -1\n") == 2
    assert "diff --git" not in out.stdout and "@@" not in out.stdout


def test_member_limits_to_one_member(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha", "beta"])
    out = _show(w, "--diff", "--member", "beta")
    assert out.exit_code == 0, out.output
    assert "beta: " in out.stdout and "alpha: " not in out.stdout
    assert "+beta reviewed" in out.stdout and "alpha reviewed" not in out.stdout
    assert "(1 member)" in out.stdout.splitlines()[0]
    data = json.loads(_show(w, "--json", "--member", "beta").stdout)
    assert [m["key"] for m in data["members"]] == ["beta"]


def test_unknown_member_refuses_and_names_the_members(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha", "beta"])
    out = _show(w, "--diff", "--member", "gamma")
    assert out.exit_code == 2, out.output
    assert out.stdout == ""
    assert "gamma" in out.stderr and "alpha" in out.stderr and "beta" in out.stderr
    assert "Traceback" not in out.stderr


def test_json_carries_diff_and_diffstat(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha", "beta"])
    data = json.loads(_show(w, "--json", "--diff").stdout)
    for row in data["members"]:
        m = w["members"][row["key"]]
        assert row["diff"] == _diff(m, m["base"], m["head"])
        assert row["diffstat"] == {"files": 1, "commits": 1, "added": 1, "removed": 1}
        assert "range_patch" not in row
    plain = json.loads(_show(w, "--json").stdout)
    assert all("diff" not in r and "diffstat" not in r for r in plain["members"])
    stat = json.loads(_show(w, "--json", "--stat").stdout)
    assert all("diff" not in r and r["diffstat"]["files"] == 1 for r in stat["members"])


def test_two_commit_head_shows_the_whole_range(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    first = m["head"]
    (m["repo"] / "second.txt").write_text("added later\n")
    git(m["repo"], "add", ".")
    git(m["repo"], "commit", "-m", "second commit")
    head = git(m["repo"], "rev-parse", "HEAD")
    rows = [{"key": "alpha", "path": "alpha", "remote": str(m["remote"]), "base": m["base"], "head": head,
             "ref": "refs/heads/main", "source": str(m["repo"])}]
    (tmp_path / "rows2.json").write_text(json.dumps(rows))
    made = runner.invoke(app, ["review", "pin", str(w["author"]), "--rows-json", str(tmp_path / "rows2.json")])
    assert made.exit_code == 0, made.output
    w["review"] = made.stdout.strip().splitlines()[-1]
    out = _show(w, "--diff")
    assert out.exit_code == 0, out.output
    # the net change, one diff for the whole range
    assert _diff(m, m["base"], head) in out.stdout
    assert "  diff: 2 files, 2 commits, +2 -1\n" in out.stdout
    assert "commit 1/2" not in out.stdout
    # every file the net base..head range changes is in the printed range
    for name in git(m["repo"], "diff", "--name-only", m["base"], head).splitlines():
        assert f"diff --git a/{name} b/{name}" in out.stdout


def test_space_and_control_chars_print_safely(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    (m["repo"] / "my file.txt").write_text("spaced\n")
    (m["repo"] / "esc\x1b[31mred.txt").write_text("\x1b[2J\x1b]0;owned\x07 clear\n")
    git(m["repo"], "add", ".")
    git(m["repo"], "commit", "-m", "hostile \x1b[31mnames")
    head = git(m["repo"], "rev-parse", "HEAD")
    rows = [{"key": "alpha", "path": "alpha", "remote": str(m["remote"]), "base": m["base"], "head": head,
             "ref": "refs/heads/main", "source": str(m["repo"])}]
    (tmp_path / "rows3.json").write_text(json.dumps(rows))
    made = runner.invoke(app, ["review", "pin", str(w["author"]), "--rows-json", str(tmp_path / "rows3.json")])
    assert made.exit_code == 0, made.output
    w["review"] = made.stdout.strip().splitlines()[-1]
    out = _show(w, "--diff")
    assert out.exit_code == 0, out.output
    assert "diff --git a/my file.txt b/my file.txt\n" in out.stdout
    assert "+spaced\n" in out.stdout
    assert "\x1b" not in out.stdout and "\x07" not in out.stdout
    assert '+++ "b/esc\\033[31mred.txt"' in out.stdout
    assert "+\\x1b[2J\\x1b]0;owned\\x07 clear\n" in out.stdout
    assert "  diff: 3 files, 2 commits, +3 -1\n" in out.stdout
    # JSON keeps the bytes; json.dumps escapes them itself
    data = json.loads(_show(w, "--json", "--diff").stdout)
    assert "\x1b[2J" in data["members"][0]["diff"]


def _repin(w, tmp_path, name):
    m = w["members"][name]
    head = git(m["repo"], "rev-parse", "HEAD")
    rows = [{"key": name, "path": name, "remote": str(m["remote"]), "base": m["base"], "head": head,
             "ref": "refs/heads/main", "source": str(m["repo"])}]
    (tmp_path / "rowsp.json").write_text(json.dumps(rows))
    made = runner.invoke(app, ["review", "pin", str(w["author"]), "--rows-json", str(tmp_path / "rowsp.json")])
    assert made.exit_code == 0, made.output
    return made.stdout.strip().splitlines()[-1], head


def _json_row(w, rid):
    out = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json", "--diff"])
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)["members"][0]


# a commit MESSAGE quoting a diff is message, not diff: one real file, +1 -1, no fake.txt.
def test_message_quoting_a_diff_is_not_read_as_diff(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "payload.txt").write_text("alpha changed\n")
    msg = "subject line\n\nbody says:\ndiff --git a/fake.txt b/fake.txt\n--- a/fake.txt\n+++ b/fake.txt\n@@ -1 +1 @@\n"
    git(m["repo"], "commit", "-q", "-am", msg)
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert row["diffstat"]["files"] == 1, row["diffstat"]
    assert row["diffstat"]["added"] == 1 and row["diffstat"]["removed"] == 1, row["diffstat"]
    assert "fake.txt" not in row["diff"], row["diff"]
    assert row["diff"] == _diff(m, m["base"], head), row["diff"]


# A new file puts " create mode" after the diffstat summary; the separator still ends there.
def test_added_file_after_create_mode_line(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "new.txt").write_text("fresh\n")
    git(m["repo"], "add", "new.txt")
    git(m["repo"], "commit", "-q", "-m", "add new")
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert row["diff"] == _diff(m, m["base"], head), row["diff"]
    assert row["diffstat"]["files"] == 1 and row["diffstat"]["added"] == 1, row["diffstat"]


# a MESSAGE quoting a whole format-patch stanza ("---", diffstat, blank, diff --git) is still
# message; the diff starts after git's own (last) separator, so fake.txt never shows and files match git.
def test_message_quoting_a_format_patch_stanza_is_not_read_as_diff(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "payload.txt").write_text("alpha changed\n")
    msg = ("subject line\n\nbody quotes:\n---\n fake.txt | 1 +\n 1 file changed, 1 insertion(+)\n\n"
           "diff --git a/fake.txt b/fake.txt\n--- a/fake.txt\n+++ b/fake.txt\n@@ -0,0 +1 @@\n+FORGED\n")
    git(m["repo"], "commit", "-q", "-am", msg)
    (m["repo"] / "real.txt").write_text("real\n")
    git(m["repo"], "add", "real.txt")
    git(m["repo"], "commit", "-q", "-m", "add real")
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert "fake.txt" not in row["diff"] and "FORGED" not in row["diff"], row["diff"]
    names = git(m["repo"], "diff", "--name-only", m["base"], head).splitlines()
    assert row["diffstat"]["files"] == len(names) == 2, row["diffstat"]


# Caveat: with diff.suppressBlankEmpty an empty context line prints empty, so a hunk can hold a
# removed "--" line, space-led lines shaped like a diffstat, and an empty line right before the next file. Pin
# captures with suppressBlankEmpty off, so that empty line is " " and the last separator match is git's own.
def test_suppress_blank_empty_hunk_shaped_like_a_separator(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "a.txt").write_text("--\nx | 1 +\n1 file changed, 1 insertion(+)\n\n")
    (m["repo"] / "b.txt").write_text("b\n")
    git(m["repo"], "add", "a.txt", "b.txt")
    git(m["repo"], "commit", "-q", "-m", "seed")
    seed = git(m["repo"], "rev-parse", "HEAD")
    git(m["repo"], "push", "-q", str(m["remote"]), "HEAD:refs/heads/main")
    (m["repo"] / "a.txt").write_text("x | 1 +\n1 file changed, 1 insertion(+)\n\n")
    (m["repo"] / "b.txt").write_text("b2\n")
    git(m["repo"], "commit", "-q", "-am", "edit")
    git(m["repo"], "config", "diff.suppressBlankEmpty", "true")
    m["base"] = seed
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert "diff --git a/a.txt b/a.txt" in row["diff"], row["diff"]
    assert row["diff"] == _diff(m, seed, head), row["diff"]
    assert row["diffstat"]["files"] == 2, row["diffstat"]


_FORGED = ("empty on purpose\n\n---\n forged.txt | 1 +\n 1 file changed, 1 insertion(+)\n\n"
           "diff --git a/forged.txt b/forged.txt\nnew file mode 100644\n--- /dev/null\n+++ b/forged.txt\n"
           "@@ -0,0 +1 @@\n+FORGED\n")


# e5: an EMPTY commit whose message is a whole forged stanza. It has no separator of its own,
# so any cut of the range.patch text lands on the message's (git am --empty=keep creates forged.txt too).
def test_empty_commit_carrying_a_forged_stanza(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "payload.txt").write_text("real change\n")
    git(m["repo"], "commit", "-q", "-am", "real change")
    git(m["repo"], "commit", "-q", "--allow-empty", "-m", _FORGED)
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert "forged.txt" not in row["diff"] and "FORGED" not in row["diff"], row["diff"]
    assert row["diffstat"]["files"] == 1, row["diffstat"]
    assert row["files"] == ["payload.txt"], row["files"]
    assert row["diff"] == _diff(m, m["base"], head), row["diff"]
    out = _show(w, "--diff")
    assert out.exit_code == 0, out.output
    assert "forged" not in out.stdout and "FORGED" not in out.stdout, out.stdout


# e6: the empty forged commit alone changes nothing.
def test_empty_forged_commit_alone_changes_nothing(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    git(m["repo"], "commit", "-q", "--allow-empty", "-m", _FORGED)
    rid, head = _repin(w, tmp_path, "alpha")
    row = _json_row(w, rid)
    assert row["diffstat"]["files"] == 0 and row["diff"] == "", row
    assert row["files"] == [], row["files"]


# The member clone lacks the head: refuse by name, never fall back to the range.patch text.
def test_missing_member_objects_refuse(tmp_path, monkeypatch):
    import shutil
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    (m["repo"] / "local.txt").write_text("never pushed\n")
    git(m["repo"], "add", "local.txt")
    git(m["repo"], "commit", "-q", "-m", "local only")
    rid, head = _repin(w, tmp_path, "alpha")
    shutil.rmtree(m["repo"])
    git(w["author"], "clone", "-q", "--no-local", "--branch", "main", m["remote"], "alpha")
    out = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--diff"])
    assert out.exit_code == 2, out.output
    assert "member_objects_missing" in out.stderr and head in out.stderr, out.stderr
    assert "diff --git" not in out.stdout and "Traceback" not in out.stderr
    data = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json", "--stat"])
    assert data.exit_code == 2 and "member_objects_missing" in data.stdout, data.output


# plain `review show` (no --diff/--stat) lists `files:` from the same objects, so e5's forged
# stanza is absent there too, in text and JSON.
def test_plain_show_files_come_from_objects(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    (m["repo"] / "payload.txt").write_text("real change\n")
    git(m["repo"], "commit", "-q", "-am", "real change")
    git(m["repo"], "commit", "-q", "--allow-empty", "-m", _FORGED)
    rid, head = _repin(w, tmp_path, "alpha")
    data = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json"])
    assert data.exit_code == 0, data.output
    row = json.loads(data.stdout)["members"][0]
    assert row["files"] == ["payload.txt"], row["files"]
    assert "diff" not in row and "diffstat" not in row, row
    out = runner.invoke(app, ["review", "show", str(w["author"]), rid])
    assert out.exit_code == 0, out.output
    assert "  files: payload.txt\n" in out.stdout, out.stdout
    assert "forged" not in out.stdout and "FORGED" not in out.stdout, out.stdout


# A name git would quote (a tab, a double quote) is listed by its real name on plain show; a control
# character in a name is escaped in text, never sent raw to the terminal.
def test_plain_show_lists_quoted_names_by_their_real_name(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    git(m["repo"], "reset", "-q", "--hard", m["base"])
    names = ["tab\there.txt", 'quo"te.txt', "esc\x1b[31m.txt"]
    for name in names:
        (m["repo"] / name).write_text("x\n")
    git(m["repo"], "add", ".")
    git(m["repo"], "commit", "-q", "-m", "quoted names")
    rid, head = _repin(w, tmp_path, "alpha")
    want = git(m["repo"], "diff", "--name-only", "-z", m["base"], head).strip("\0").split("\0")
    data = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json"])
    assert data.exit_code == 0, data.output
    assert json.loads(data.stdout)["members"][0]["files"] == want == sorted(names), data.stdout
    out = runner.invoke(app, ["review", "show", str(w["author"]), rid])
    assert out.exit_code == 0, out.output
    assert "  files: esc\\x1b[31m.txt, quo\"te.txt, tab\there.txt\n" in out.stdout, out.stdout
    assert "\x1b" not in out.stdout, out.stdout


# Plain show on a clone that lacks the head still lists the member (a received review, before its fetch) but
# reads no files: `files` is null and names member_objects_missing; no fall back to range.patch.
def test_plain_show_missing_member_objects_reads_no_files(tmp_path, monkeypatch):
    import shutil
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    (m["repo"] / "local.txt").write_text("never pushed\n")
    git(m["repo"], "add", "local.txt")
    git(m["repo"], "commit", "-q", "-m", "local only")
    rid, head = _repin(w, tmp_path, "alpha")
    shutil.rmtree(m["repo"])
    git(w["author"], "clone", "-q", "--no-local", "--branch", "main", m["remote"], "alpha")
    out = runner.invoke(app, ["review", "show", str(w["author"]), rid])
    assert out.exit_code == 0, out.output
    assert "  files: (not read: " in out.stdout and "member_objects_missing" in out.stdout, out.stdout
    assert head in out.stdout and "local.txt" not in out.stdout, out.stdout
    row = json.loads(runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json"]).stdout)["members"][0]
    assert row["files"] is None and row["files_unread"] == f"member_objects_missing alpha {head}", row


# A member's change is what the head adds to the target, read from the merge-base of the pinned base (the target's
# live tip at pin) and the head: never a change the target made that the head lacks or merged in.
def _pin_at(w, tmp_path, name, base, head):
    m = w["members"][name]
    rows = [{"key": name, "path": name, "remote": str(m["remote"]), "base": base, "head": head,
             "ref": "refs/heads/main", "source": str(m["repo"])}]
    (tmp_path / "rowsb.json").write_text(json.dumps(rows))
    return runner.invoke(app, ["review", "pin", str(w["author"]), "--rows-json", str(tmp_path / "rowsb.json")])


def _mine(m):
    (m["repo"] / "mine.txt").write_text("mine\n")
    git(m["repo"], "add", "mine.txt")
    git(m["repo"], "commit", "-q", "-m", "mine")


def _only_the_members_change(w, rid, m, base, head, absent):
    names = git(m["repo"], "diff", "--name-only", f"{base}...{head}").splitlines()
    assert "mine.txt" in names and absent not in names, names
    diff = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--diff", "--member", "alpha"])
    assert diff.exit_code == 0, diff.output
    assert git(m["repo"], "diff", f"{base}...{head}") + "\n" in diff.stdout, diff.stdout
    assert absent not in diff.stdout, diff.stdout
    stat = runner.invoke(app, ["review", "show", str(w["author"]), rid, "--stat"])
    assert stat.exit_code == 0 and f"  diff: {len(names)} files" in stat.stdout, stat.output
    data = json.loads(runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json", "--stat"]).stdout)
    assert data["members"][0]["files"] == names and data["members"][0]["diffstat"]["files"] == len(names), data
    plain = json.loads(runner.invoke(app, ["review", "show", str(w["author"]), rid, "--json"]).stdout)
    assert plain["members"][0]["files"] == names, plain


def test_merged_in_target_is_not_the_members_change(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    _mine(m)
    move_main(w, "alpha", "main.txt", "main\n")
    git(m["repo"], "fetch", "-q", "origin")
    target = git(m["repo"], "rev-parse", "origin/main")
    git(m["repo"], "merge", "-q", "--no-edit", "origin/main")
    head = git(m["repo"], "rev-parse", "HEAD")
    stale = _pin_at(w, tmp_path, "alpha", m["base"], head)  # the base before main moved: refused by name
    assert stale.exit_code == 2 and "base_not_live_head" in stale.output, stale.output
    made = _pin_at(w, tmp_path, "alpha", target, head)
    assert made.exit_code == 0, made.output
    _only_the_members_change(w, made.stdout.strip().splitlines()[-1], m, target, head, "main.txt")


def test_target_moved_past_the_branch_point_is_not_the_members_change(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, ["alpha"])
    m = w["members"]["alpha"]
    _mine(m)
    head = git(m["repo"], "rev-parse", "HEAD")
    move_main(w, "alpha", "later.txt", "later\n")
    git(m["repo"], "fetch", "-q", "origin")
    target = git(m["repo"], "rev-parse", "origin/main")
    made = _pin_at(w, tmp_path, "alpha", target, head)
    assert made.exit_code == 0, made.output
    _only_the_members_change(w, made.stdout.strip().splitlines()[-1], m, target, head, "later.txt")
