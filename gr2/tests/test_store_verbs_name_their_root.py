"""Every store verb names the workspace it acts on with `-C/--root`, from any directory.

THE DEFECT, measured on grip dev 312930c6: `store init` and `store migrate-reviews` took the root as an
argument; commit, check, push, status, materialize, migrate, log, diff, checkout (and the hidden
snapshot alias) read only the current directory. A walk run from outside a workspace could not name
one, and from inside workspace B a caller who meant workspace A acted on B with exit 0.

THE SPECIFIED SHAPE: one form for the group, the `--root/-C` option the repo's other verbs share. With
it the verb acts on that root wherever it is run from; without it the verb reads the current
directory, as before.

Two workspaces with DIFFERENT members (A holds alpha, B holds beta) make every row's witness tell them
apart: a verb that acted on the wrong one reports the wrong member, commit or path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_store_git_native_smoke import git, gr2, make_member, run


def _workspace(tmp_path: Path, name: str, member: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    remote, _ = make_member(tmp_path, member)
    run(root, "git", "clone", str(remote), member)
    gr2(root, "init")
    gr2(root, "commit", "-m", f"first {name}")
    return root


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    a = _workspace(tmp_path, "A", "alpha")
    b = _workspace(tmp_path, "B", "beta")
    outside = tmp_path / "outside"
    outside.mkdir()
    return a, b, outside


def _head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def _names(payload: dict) -> list[str]:
    return sorted(m["name"] for m in payload["members"])


# verb -> (words after the verb, witness(result, A, tmp_path) that holds only if the verb acted on A)
def _json(result) -> dict:
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


VERBS = {
    "commit": (["-m", "again", "--json"], lambda r, a: _json(r)["root_commit"] == _head(a)),
    "snapshot": (["-m", "again", "--json"], lambda r, a: _json(r)["root_commit"] == _head(a)),
    "check": (["--json"], lambda r, a: _names(_json(r)) == ["alpha"]),
    "status": (["--json"], lambda r, a: _names(_json(r)) == ["alpha"]),
    "materialize": (["--json"], lambda r, a: _names(_json(r)) == ["alpha"]),
    "log": (["--json"], lambda r, a: [e["message"].strip() for e in _json(r)["entries"]] == ["first A"]),
    "diff": (["HEAD", "HEAD", "--json"], lambda r, a: _names(_json(r)) == ["alpha"]),
    "checkout": (["HEAD", "--json"], lambda r, a: _json(r)["root_commit"] == _head(a)),
    "push": (["--json"], lambda r, a: _json(r)["root_commit"] == _head(a)),
    "migrate-reviews": (["--json"], lambda r, a: Path(_json(r)["receipt"]).is_relative_to(a)),
}


def _prepare(verb: str, a: Path, tmp_path: Path) -> list[str]:
    """Per-verb setup on A only; returns extra words."""
    if verb == "push":
        origin = tmp_path / "A-root.git"
        run(tmp_path, "git", "init", "--bare", str(origin))
        return ["--to", str(origin)]
    return []


@pytest.mark.parametrize("verb", sorted(VERBS))
@pytest.mark.parametrize("where", ["outside", "inside-B"])
@pytest.mark.parametrize("form", ["-C", "--root"])
def test_a_store_verb_acts_on_the_named_root_from_anywhere(verb, where, form, pair, tmp_path) -> None:
    a, b, outside = pair
    words, witness = VERBS[verb]
    extra = _prepare(verb, a, tmp_path)
    b_head = _head(b)
    cwd = outside if where == "outside" else b
    result = gr2(cwd, verb, form, str(a), *words, *extra, check=False)
    assert witness(result, a), f"{verb} from {where} did not act on A:\n{result.stdout}\n{result.stderr}"
    assert _head(b) == b_head


def test_migrate_acts_on_the_named_root(pair) -> None:
    """migrate's witness is its refusal: A carries an alpha store beside its native one, so a migrate that
    read A refuses to overwrite; one that read the cwd finds no alpha store at all."""
    a, b, outside = pair
    (a / ".grip" / ".git").mkdir(parents=True)
    for cwd in (outside, b):
        result = gr2(cwd, "migrate", "-C", str(a), "--dry-run", check=False)
        assert result.returncode == 4, result.stderr
        assert "native store already exists" in result.stderr, result.stderr


def test_init_names_its_root_with_the_option_and_refuses_it_twice(pair, tmp_path) -> None:
    a, b, outside = pair
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    remote, _ = make_member(tmp_path, "gamma")
    run(fresh, "git", "clone", str(remote), "gamma")
    result = gr2(b, "init", "-C", str(fresh), "--json", check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == str(fresh.resolve())
    assert (fresh / "grip.toml").is_file()
    twice = gr2(outside, "init", str(fresh), "-C", str(a), check=False)
    assert twice.returncode == 2, twice.stderr
    assert "twice" in twice.stderr


def test_migrate_reviews_refuses_its_root_twice(pair) -> None:
    a, b, outside = pair
    twice = gr2(outside, "migrate-reviews", str(b), "-C", str(a), check=False)
    assert twice.returncode == 2, twice.stderr
    assert "twice" in twice.stderr


@pytest.mark.parametrize("verb", sorted(set(VERBS) - {"migrate-reviews"}))
def test_no_root_from_inside_a_workspace_is_unchanged(verb, pair, tmp_path) -> None:
    a, b, outside = pair
    words, witness = VERBS[verb]
    result = gr2(a, verb, *words, *_prepare(verb, a, tmp_path), check=False)
    assert witness(result, a), f"{result.stdout}\n{result.stderr}"


def test_migrate_reviews_still_needs_a_root(pair) -> None:
    """Its root stays required, as before: the bare call from inside a workspace is a usage error."""
    a, b, outside = pair
    result = gr2(a, "migrate-reviews", check=False)
    assert result.returncode == 2, result.stderr


# Full mode (grip dev 5c251185) gave `store push` four options. Each row drives one of them with the root
# named by -C/--root, so the new paths read A too: --allow-rewind names alpha, a member only A holds, and
# --recurse=none reports A's member as the one left unchecked.
PUSH_OPTIONS = {
    "--snapshot": (["--snapshot"], lambda p, a: p["snapshot"] == _head(a) and p["root_commit"] == _head(a)),
    "--atomic": (["--atomic"], lambda p, a: p["root_commit"] == _head(a)),
    "--recurse": (["--recurse", "none"], lambda p, a: p["unchecked"] == ["alpha"] and p["root_commit"] == _head(a)),
    "--allow-rewind": (["--allow-rewind", "alpha"], lambda p, a: p["allow_rewind"] == ["alpha"]
                       and p["root_commit"] == _head(a)),
}


@pytest.mark.parametrize("option", sorted(PUSH_OPTIONS))
@pytest.mark.parametrize("where", ["outside", "inside-B"])
@pytest.mark.parametrize("form", ["-C", "--root"])
def test_store_push_full_mode_options_act_on_the_named_root(option, where, form, pair, tmp_path) -> None:
    a, b, outside = pair
    words, witness = PUSH_OPTIONS[option]
    if option == "--snapshot":
        # A pushed member commit the root has not recorded yet, so the snapshot has a pin to record.
        (a / "alpha" / "moved.txt").write_text("moved\n")
        run(a / "alpha", "git", "add", "moved.txt")
        run(a / "alpha", "git", "commit", "-q", "-m", "moved")
        run(a / "alpha", "git", "push", "-q", "origin", "HEAD")
    b_head = _head(b)
    cwd = outside if where == "outside" else b
    result = gr2(cwd, "push", form, str(a), *words, *_prepare("push", a, tmp_path), "--json", check=False)
    payload = _json(result)
    assert witness(payload, a), f"push {option} from {where} did not act on A:\n{result.stdout}\n{result.stderr}"
    assert _head(b) == b_head


# THE ADVICE CARRIES THE ROOT. A refusal or a finding that names a verb to run is followed from where the verb was
# run; advice that dropped the -C acted on the current directory, B, with exit 0. Each row takes the printed
# command, runs it as printed from inside B, and holds only if it acted on A.
def _advised(text: str, pattern: str) -> list[str]:
    import re
    import shlex

    found = re.search(pattern, text)
    assert found, f"no advice matching {pattern!r} in:\n{text}"
    words = shlex.split(found.group(1))
    assert words[:2] == ["gr2", "store"], words
    return words[2:]


def _behind_its_pin(a: Path) -> str:
    """A's alpha committed at a pushed pin, then reset one behind it; returns the pin."""
    (a / "alpha" / "moved.txt").write_text("moved\n")
    run(a / "alpha", "git", "add", "moved.txt")
    run(a / "alpha", "git", "commit", "-q", "-m", "moved")
    run(a / "alpha", "git", "push", "-q", "origin", "HEAD")
    gr2(a, "commit", "-m", "pin moved")
    pin = git(a / "alpha", "rev-parse", "HEAD").stdout.strip()
    run(a / "alpha", "git", "reset", "-q", "--hard", "HEAD~1")
    return pin


def test_the_rewind_advice_run_as_printed_from_b_acts_on_a(pair) -> None:
    a, b, _ = pair
    pin = _behind_its_pin(a)
    refused = gr2(b, "commit", "-C", str(a), "-m", "again", check=False)
    assert refused.returncode == 3, f"{refused.stdout}\n{refused.stderr}"
    words = _advised(refused.stderr, r"run `(gr2 store materialize[^`]*)` to bring the checkout to the pin")
    result = gr2(b, *words, "--json", check=False)
    assert _names(_json(result)) == ["alpha"], f"the advice acted on B:\n{result.stdout}"
    assert git(a / "alpha", "rev-parse", "HEAD").stdout.strip() == pin


def _hooks_removed(a: Path) -> None:
    for name in ("pre-commit", "pre-push"):
        (a / ".git" / "hooks" / name).unlink()


def _hook_advice_acts_on_a(a: Path, b: Path) -> None:
    _hooks_removed(a)
    status = gr2(b, "status", "-C", str(a), check=False)
    for name in ("pre-commit", "pre-push"):
        words = _advised(status.stderr, rf"hooks: {name} is not installed; run (gr2 store materialize.*?) to install it\n")
        result = gr2(b, *words, "--json", check=False)
        assert _names(_json(result)) == ["alpha"], f"the {name} advice acted on B:\n{result.stdout}"
        assert (a / ".git" / "hooks" / name).is_file(), f"the {name} advice did not install A's hook"
        _hooks_removed(a)


def test_the_hook_advice_run_as_printed_from_b_acts_on_a(pair) -> None:
    a, b, _ = pair
    _hook_advice_acts_on_a(a, b)


def test_the_advice_quotes_a_root_with_a_space_in_it(tmp_path) -> None:
    a = _workspace(tmp_path, "A spaced", "alpha")
    b = _workspace(tmp_path, "B", "beta")
    _hook_advice_acts_on_a(a, b)
