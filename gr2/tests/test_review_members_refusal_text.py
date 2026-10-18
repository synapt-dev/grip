"""The refusal a bind prints when a member's ancestry cannot be read starts with git's own error, not a hint."""
import subprocess

from gr2.python_cli import review_members


def _g(w, *a, **kw):
    return subprocess.run(["git", "-C", str(w), *a], check=True, capture_output=True, text=True, **kw).stdout.strip()


def test_missing_parent_refusal_quotes_git_error(tmp_path, monkeypatch):
    work = tmp_path / "m"; work.mkdir(); _g(work, "init", "-q", "-b", "main")
    ident = ["-c", "user.name=t", "-c", "user.email=t@x"]
    _g(work, *ident, "commit", "-q", "--allow-empty", "-m", "pin")
    pin = _g(work, "rev-parse", "HEAD")
    tree = _g(work, "rev-parse", "HEAD^{tree}")
    body = f"tree {tree}\nparent {'2' * 40}\nauthor t <t@x> 1 +0000\ncommitter t <t@x> 1 +0000\n\nx\n"
    head = _g(work, "hash-object", "-t", "commit", "-w", "--stdin", input=body)
    _g(work, "update-ref", "HEAD", head)
    monkeypatch.setattr(review_members, "workspace_members", lambda root: [
        review_members.Member("m", "m", "https://example.invalid/m.git", "main", pin)])
    choice = review_members.changed_member_rows(tmp_path)
    assert choice.rows == [] and len(choice.refused) == 1, choice
    said = choice.refused[0].split("git said: ", 1)[1]
    assert not said.startswith("hint:"), said
    assert "2" * 40 in said, said
