"""`store migrate` checks each pin against the branch the member is on, not the literal main.

THE DEFECT, read on grip dev 9e4ecb56 and measured by the first run of this file: `store init`
records the branch each member is on (`_infer_member_branch`), but `_native_store_migrate`
checks each alpha pin with `merge-base --is-ancestor <pin> origin/main` and refuses with
"pin ... is not on origin/main; push it first". On a workspace whose members are namespaced
branches of one url (`core/main`, `web/main`) that refuses a pin the very next step, init,
would accept, so a workspace that `store init` + `store commit` handles cannot be migrated.

THE RULE: the coverage check uses the member's own upstream, the same value init would record,
and the refusal names it.

THE CONTROLS, which keep this from passing on a change that simply drops the check:
  * a plain `main` member still migrates, with the same receipt shape as before;
  * a pin that is NOT on the member's upstream still refuses (rc 3) and the refusal names the
    member's branch, not `origin/main`.

Fixtures are real git with bare local origins and no network. Every branch of the shared remote
carries its OWN commit (the lesson of the init witness: a shared commit makes the defect
invisible). The alpha store is made the way the existing migrate test makes it, with
`grip_init` and `grip_snapshot`.
"""
from __future__ import annotations

import json
from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.test_store_git_native_smoke import configure_identity, git, gr2
from tests.test_store_init_branch_inference import _clone, _fork_member, _root, _shared_remote


def _alpha(root: Path, *names: str) -> str:
    grip_mod.grip_init(root)
    return grip_mod.grip_snapshot(
        root, {name: root / name for name in names}, message="alpha snapshot"
    )


def test_a_namespaced_branch_workspace_migrates(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ("core/main", "web/main"))
    root = _root(tmp_path)
    core = _clone(root, remote, "demo-core", "core/main")
    web = _clone(root, remote, "demo-web", "web/main")
    alpha_head = _alpha(root, "demo-core", "demo-web")
    core_head = git(core, "rev-parse", "HEAD").stdout.strip()
    web_head = git(web, "rev-parse", "HEAD").stdout.strip()
    assert core_head != web_head, "fixture: the two branches must differ or this proves nothing"

    migrated = gr2(root, "migrate", "--json", check=False)

    assert migrated.returncode == 0, f"{migrated.stdout}\n{migrated.stderr}"
    receipt = json.loads(migrated.stdout)
    assert receipt["status"] == "migrated" and receipt["alpha_head"] == alpha_head
    assert receipt["members"] == {"demo-core": core_head, "demo-web": web_head}
    tree = git(root, "ls-tree", "HEAD").stdout
    assert f"160000 commit {core_head}\tdemo-core" in tree
    assert f"160000 commit {web_head}\tdemo-web" in tree
    assert gr2(root, "check", check=False).returncode == 0


def test_control_a_plain_main_member_migrates_as_before(tmp_path: Path) -> None:
    """THE CONTROL: without it, "skip the coverage check" would pass the row above."""
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    plain = _clone(root, remote, "plain", "main")
    _alpha(root, "plain")

    migrated = gr2(root, "migrate", "--json", check=False)

    assert migrated.returncode == 0, f"{migrated.stdout}\n{migrated.stderr}"
    assert json.loads(migrated.stdout)["members"] == {"plain": git(plain, "rev-parse", "HEAD").stdout.strip()}


def test_a_pin_not_on_the_members_branch_still_refuses_and_names_that_branch(tmp_path: Path) -> None:
    """THE SECOND CONTROL: the check must still be a check. A commit made on `core/main` and not
    pushed is not covered, and the refusal says which branch is missing it."""
    remote = _shared_remote(tmp_path, ("core/main",))
    root = _root(tmp_path)
    core = _clone(root, remote, "demo-core", "core/main")
    (core / "unpushed.txt").write_text("local only\n")
    git(core, "add", ".")
    git(core, "commit", "-m", "local only")
    _alpha(root, "demo-core")

    refused = gr2(root, "migrate", "--json", check=False)

    assert refused.returncode == 3, f"{refused.stdout}\n{refused.stderr}"
    assert "origin/core/main" in refused.stderr and "push it first" in refused.stderr, refused.stderr
    assert "origin/main" not in refused.stderr.replace("origin/core/main", ""), "the refusal must name the member's branch"
    assert not (root / ".git").exists(), "a refused migrate must leave no native store behind"


def test_a_fork_shaped_member_migrates_against_origin_like_init_records_it(tmp_path: Path) -> None:
    """A member whose branch tracks `upstream/main` records `origin/main` at init (the only
    remote a fresh clone has), so migrate must check the pin against that same value and not
    against the raw tracking ref."""
    root = _root(tmp_path)
    member = _fork_member(tmp_path, root)
    _alpha(root, "forked")

    migrated = gr2(root, "migrate", "--json", check=False)

    assert migrated.returncode == 0, f"{migrated.stdout}\n{migrated.stderr}"
    assert json.loads(migrated.stdout)["members"] == {"forked": git(member, "rev-parse", "HEAD").stdout.strip()}
