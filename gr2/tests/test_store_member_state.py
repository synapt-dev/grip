"""``store snapshot`` and ``store checkout`` must read MEMBER state, not the
root's.

Measured 2026-09-24 on the adopted-superproject flow: after a
plain clone, adoption and materialization, `store snapshot` recorded the ROOT's
HEAD as every member's head (rc 0), and with the root carrying its own
untracked .grip/ and agents/ -- the default state after adoption -- it refused
with "Dirty repos detected: example1, jabberwocky" for members that are clean.
Both answers come from git resolving the EMPTY placeholder at the declared path
to the enclosing root. The dirty check and the head record are the two checks;
the same repo map feeds `store checkout`, which would act on the placeholder
paths the same way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_repo_path_read_through import _adopted_superproject, _workspace_with_declared_repo


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _adopted(tmp_path: Path, *, root_clean: bool) -> tuple[Path, dict[str, str]]:
    plain, pins = _adopted_superproject(tmp_path)
    rc, out = _cli("workspace", "init", str(plain), "--from-superproject")
    assert rc == 0, out
    rc, out = _cli("workspace", "materialize", str(plain), "--yes")
    assert rc == 0, out
    if root_clean:
        # the measured clean-root shape: the root's own .grip/ and agents/ are
        # excluded, so git status at the root is clean
        excl = plain / ".git" / "info" / "exclude"
        excl.write_text(".grip/\nagents/\n")
    return plain, pins


def test_snapshot_records_the_members_pins_not_the_root_head(tmp_path: Path) -> None:
    """THE WITNESS. On the unfixed code every member's head is the ROOT's."""
    plain, pins = _adopted(tmp_path, root_clean=True)
    rc, out = _cli("store", "snapshot", str(plain), "-m", "clean-root")
    assert rc == 0, out
    index = json.loads((plain / ".grip" / "snapshots" / "index.json").read_text())
    states = index[-1]["repo_states"]
    assert set(states) == set(pins), f"got {sorted(states)}"
    for name, want in pins.items():
        got = states[name]["head"]
        assert got == want, f"{name} recorded {got[:12]}, the root pins {want[:12]}"
        assert states[name]["head_state"] in {"attached", "detached"}, states[name]


def test_snapshot_succeeds_when_the_root_carries_untracked_workspace_files(
    tmp_path: Path,
) -> None:
    """THE SECOND WITNESS. With the root NOT clean (its own .grip/ and agents/
    untracked, the default), the unfixed code refused with 'Dirty repos
    detected' for members that are clean."""
    plain, pins = _adopted(tmp_path, root_clean=False)
    rc, out = _cli("store", "snapshot", str(plain), "-m", "dirty-root")
    assert rc == 0, f"the members are clean; the root's own state is not theirs: {out}"
    index = json.loads((plain / ".grip" / "snapshots" / "index.json").read_text())
    states = index[-1]["repo_states"]
    for name, want in pins.items():
        assert states[name]["head"] == want, f"{name} recorded {states[name]['head'][:12]}"


def test_snapshot_on_a_plain_workspace_still_records_its_heads(tmp_path: Path) -> None:
    """THE CONTROL. A workspace whose members are real checkouts snapshots
    exactly as before: heads recorded, nothing else changed."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="checkout")
    rc, out = _cli("store", "snapshot", str(root), "-m", "plain control")
    assert rc == 0, out
    index = json.loads((root / ".grip" / "snapshots" / "index.json").read_text())
    states = index[-1]["repo_states"]
    want = _run_head(root / "repos" / "m")
    assert states["m"]["head"] == want, f"the control member's head must be its own: {states['m']}"


def test_checkout_restores_member_heads_and_leaves_the_root(tmp_path: Path) -> None:
    """THE COVERAGE WITNESS. `store checkout` reads the same map: it must act
    on the members (through the unit copy) and never move the ROOT repository."""
    plain, pins = _adopted(tmp_path, root_clean=False)
    rc, out = _cli("store", "snapshot", str(plain), "-m", "before")
    assert rc == 0, out
    snapshot_id = json.loads((plain / ".grip" / "snapshots" / "index.json").read_text())[-1]["id"]
    snapshot_sha = _run_head(plain)
    # move one member's HEAD away from the pin (an empty commit on the
    # detached checkout), then restore it; the pin is often the root commit,
    # so HEAD~1 does not exist to detach to
    member = plain / "agents" / "default" / "home" / "diverging"
    # the commit carries an explicit identity: a CI runner has no git user,
    # and the verb dies there with "empty ident name" before the restore it
    # enables even runs
    subprocess.run(
        ["git", "-C", str(member), "-c", "user.name=test", "-c", "user.email=t@e.invalid",
         "commit", "-q", "--allow-empty", "-m", "moved"],
        check=True,
    )
    moved = _run_head(member)
    assert moved != pins["diverging"], "precondition: the member moved"
    rc, out = _cli("store", "checkout", str(plain), snapshot_id)
    assert rc == 0, out
    assert _run_head(member) == pins["diverging"], "the member is restored to its recorded head"
    assert _run_head(plain) == snapshot_sha, "the ROOT repository must not move"


def test_snapshot_with_a_member_path_missing_records_it_empty(tmp_path: Path) -> None:
    """THE MISSING-MEMBER WITNESS. A member whose declared path does not exist
    is nothing to read, not a conflict: the snapshot succeeds and records it
    empty while the present member records its pin. Before the short-circuit
    the head-record path raised FileNotFoundError from inside the verb."""
    plain, pins = _adopted(tmp_path, root_clean=False)
    # the declared path is the NAME-keyed path at the workspace root, which
    # materialize leaves as an empty placeholder -- exactly the path git
    # resolves to the enclosing root before this range
    shutil.rmtree(plain / "diverging")
    rc, out = _cli("store", "snapshot", str(plain), "-m", "one-member-missing")
    assert rc == 0, out
    index = json.loads((plain / ".grip" / "snapshots" / "index.json").read_text())
    states = index[-1]["repo_states"]
    assert states["diverging"]["head"] is None, states["diverging"]
    assert states["diverging"]["is_empty"] is True, states["diverging"]
    assert states["converging"]["head"] == pins["converging"], states["converging"]


def _run_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()