"""On a native root the verbs that read the workspace spec take the store's record of a member's pin (grip.toml's pin,
else the root's committed gitlink), not the spec's own copy, which goes stale at the first advance.

The stale root here is built by gr2's own verbs: store init, store commit, workspace init (which writes the spec with
the pins of that moment), then the remotes advance, sync run moves the checkouts and store commit records the new
pins in grip.toml and the gitlinks. The spec still names the old ones. Before this change `apply` and `workspace
materialize` recloned a missing member at the spec's OLD pin with exit 0, and `review pin` refused with PinConflict.

Written by the author of the change. Each row names what turns it red:
  * W1 apply, W2 workspace materialize    -> the resolver returns the spec's pin first (both reach spec_apply clone_repo)
  * W3 a unit's missing member            -> only the converge_unit_repos site reads repo_spec["pin"] again
  * W4 review pin with no rows            -> load_members raises PinConflict again
  * W5 working grip.toml deleted         -> the root commit is not consulted when the working file is gone
  * W5n deleted, spec moved the path     -> the committed lookup does not fall back to the member's name
  * W5nb as W5n, committed pin a branch    -> the gitlink is read at the caller's path, not the committed entry's
  * W5c spec-only root (control)          -> the resolver ignores the spec's pin when no grip.toml was ever committed
  * W6 committed grip.toml pin is a branch -> the committed-gitlink fallback is dropped
  * W7 sync run reclones a missing member -> only the syncops clone_shared_repo site reads repo_spec["pin"] again
  * W8 working grip.toml set back to P1   -> the resolver reads the working grip.toml instead of HEAD's
  * W9 working grip.toml swaps two names  -> the member is matched by name before path
  * W10 full mode, committed pin a branch -> the gitlink is taken as the pin without reading its field record
  * W11 working grip.toml drops the member -> a member missing from the working file is not looked up in HEAD's
  * W7b sync run, file gone, branch pin   -> the committed entry's record skips the gitlink
  * W12 full store, working mode line cut -> the listed member's gitlink is read in the WORKING file's mode
  * W13 plain store, working mode line add -> the same, the other way
  * M  the matrix, one row per cell       -> see its docstring; every cell must give P2
"""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.spec_apply import render_unit_toml, workspace_spec_path
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

runner = CliRunner()


def _cli(*args: str):
    return runner.invoke(app, [*args], catch_exceptions=False)


def _advance(seed: Path, label: str) -> str:
    (seed / "f").write_text(f"{label}\n")
    git(seed, "commit", "-qam", label)
    git(seed, "push", "-q", "origin", "main")
    return git(seed, "rev-parse", "HEAD")


def _build_stale(tmp_path, monkeypatch, full: bool = False):
    """A native root whose spec pins alpha and beta at P1 while grip.toml and the gitlinks record P2. With ``full`` the
    root is switched to full mode after workspace init (which refuses a full-mode root), before the advance, so the
    advance is recorded as gitlinks with a sidecar under .grip/fields/."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    root, seeds = tmp_path / "ws", {}
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", remote)
        git(tmp_path, "clone", "-q", remote, seed)
        git(seed, "checkout", "-q", "-B", "main")
        (seed / "f").write_text(f"{name} 1\n")
        git(seed, "add", "f")
        git(seed, "commit", "-qm", f"{name} 1")
        git(seed, "push", "-q", "origin", "main")
        git(tmp_path, "clone", "-q", f"file://{remote}", root / name)
        seeds[name] = seed
    assert _cli("store", "init", str(root)).exit_code == 0
    monkeypatch.chdir(root)
    assert _cli("store", "commit", "-m", "record").exit_code == 0
    assert _cli("workspace", "init", str(root)).exit_code == 0
    p1 = git(root / "alpha", "rev-parse", "HEAD")
    if full:
        text = (root / "grip.toml").read_text()
        assert "mode" not in tomllib.loads(text)   # the top-level key; a member table's own `mode` is another key
        (root / "grip.toml").write_text('mode = "full"\n' + text)
        assert tomllib.loads((root / "grip.toml").read_text())["mode"] == "full"
    p2 = {name: _advance(seed, f"{name} 2") for name, seed in seeds.items()}
    assert _cli("sync", "run", str(root)).exit_code == 0
    assert _cli("store", "commit", "-m", "advance").exit_code == 0
    spec = workspace_spec_path(root).read_text()
    assert p1 in spec and p2["alpha"] not in spec, "the fixture must leave the spec's pin behind"
    entry = git(root, "ls-tree", "HEAD", "alpha").split()[2]
    if full:
        assert tomllib.loads(git(root, "show", "HEAD:grip.toml")).get("mode") == "full"
        assert entry == p2["alpha"], "in full mode the gitlink holds the member's own commit"
        assert git(root, "ls-tree", "-d", "--name-only", "HEAD", ".grip/fields/alpha") == ".grip/fields/alpha", (
            "and its fields are in the sidecar")
    else:
        assert entry == p2["alpha"]
    return {"root": root, "p1": p1, "p2": p2["alpha"]}


@pytest.fixture
def stale(tmp_path, monkeypatch):
    return _build_stale(tmp_path, monkeypatch)


@pytest.fixture
def stale_full(tmp_path, monkeypatch):
    return _build_stale(tmp_path, monkeypatch, full=True)


def _alpha_head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


@pytest.mark.parametrize("verb", [("apply",), ("workspace", "materialize")])
def test_a_missing_member_is_recloned_at_the_stores_record_not_the_specs_stale_pin(stale, verb):
    """W1 / W2. The two verbs reach the same clone site; both are kept because a user types either."""
    root = stale["root"]
    shutil.rmtree(root / "alpha")
    result = _cli(*verb, str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert stale["p1"][:12] in notice and stale["p2"][:12] in notice and "using the store's record" in notice


def test_a_units_missing_member_converges_at_the_stores_record(stale):
    """W3. The unit-home clone is a second site; reverting only it leaves W1 green and this red."""
    root = stale["root"]
    unit = {"name": "u1", "path": "agents/u1", "repos": ["alpha"]}
    spec_path = workspace_spec_path(root)
    spec_path.write_text(spec_path.read_text().rstrip("\n") + '\n\n[[units]]\nname = "u1"\npath = "agents/u1"\n'
                         'repos = ["alpha"]\n')
    (root / unit["path"]).mkdir(parents=True)
    (root / unit["path"] / "unit.toml").write_text(render_unit_toml(unit))
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "agents" / "u1" / "alpha") == stale["p2"]


def test_review_pin_with_no_rows_reads_the_record_and_reaches_its_own_refusal(stale):
    """W4. No PinConflict; the verb goes on to the refusal it gives a root with nothing ahead of its pins, which
    tells a conflict apart from a crash."""
    result = runner.invoke(app, ["review", "pin", str(stale["root"])])
    assert result.exit_code != 0
    assert "records pin" not in result.output and "PinConflict" not in result.output
    assert "nothing to pin" in result.output, result.output


def test_a_deleted_working_grip_toml_keeps_the_committed_record(stale):
    """W5. The working grip.toml is deleted while HEAD:grip.toml still records alpha at P2. The root is native by its
    record, so apply reclones at P2, not the spec's P1, and says so."""
    root = stale["root"]
    (root / "grip.toml").unlink()
    assert 'name = "alpha"' in git(root, "show", "HEAD:grip.toml")
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert stale["p1"][:12] in notice and stale["p2"][:12] in notice and "committed grip.toml" in notice


def test_a_deleted_working_grip_toml_finds_the_member_by_its_committed_name(stale):
    """W5n. As W5, and the spec has moved alpha to another path, so no committed entry holds that path. The member is
    matched by its committed name, which no working edit can change, and reclones at P2."""
    root = stale["root"]
    (root / "grip.toml").unlink()
    spec_path = workspace_spec_path(root)
    spec = spec_path.read_text()
    assert spec.count('path = "alpha"') == 1
    spec_path.write_text(spec.replace('path = "alpha"', 'path = "moved/alpha"'))
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "moved" / "alpha") == stale["p2"]


def test_a_committed_name_match_reads_the_gitlink_at_the_committed_path(stale):
    """W5nb. W5n with the committed pin a branch name, so the record is a gitlink. The member is matched by its
    committed name, and the gitlink is read at the committed entry's path (alpha), not at the spec's moved path, where
    the root holds nothing."""
    root = stale["root"]
    _set_alpha_pin(root, stale["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    (root / "grip.toml").unlink()
    spec_path = workspace_spec_path(root)
    spec = spec_path.read_text()
    assert spec.count('path = "alpha"') == 1
    spec_path.write_text(spec.replace('path = "alpha"', 'path = "moved/alpha"'))
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "moved" / "alpha") == stale["p2"]

def test_a_spec_only_root_keeps_the_specs_pin(tmp_path, monkeypatch):
    """W5c, the control: a root whose HEAD has never held a grip.toml (no store at all) has no store record, so the
    spec's pin is the pin. The remote has moved on, so a resolver that ignored the spec would clone the tip."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    remote, seed, root = tmp_path / "alpha.git", tmp_path / "seed-alpha", tmp_path / "ws"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", remote)
    git(tmp_path, "clone", "-q", remote, seed)
    git(seed, "checkout", "-q", "-B", "main")
    (seed / "f").write_text("alpha 1\n")
    git(seed, "add", "f")
    git(seed, "commit", "-qm", "alpha 1")
    git(seed, "push", "-q", "origin", "main")
    root.mkdir()
    git(tmp_path, "clone", "-q", f"file://{remote}", root / "alpha")
    assert _cli("workspace", "init", str(root)).exit_code == 0
    p1 = _alpha_head(root / "alpha")
    assert p1 in workspace_spec_path(root).read_text()
    assert not (root / "grip.toml").exists() and not (root / ".git").exists()
    assert _advance(seed, "alpha 2") != p1
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == p1
    assert "gr2: member" not in result.output


def _set_alpha_pin(root: Path, old: str, new: str) -> None:
    text = (root / "grip.toml").read_text()
    assert text.count(f'pin = "{old}"') == 1
    (root / "grip.toml").write_text(text.replace(f'pin = "{old}"', f'pin = "{new}"'))


def test_a_branch_name_in_grip_toml_falls_back_to_the_committed_gitlink_not_the_spec(stale):
    """W6. The committed grip.toml's pin is not a commit, so the record is the root's committed gitlink (P2), never
    the spec's stale P1. The store's pre-commit hook folds pin lines to the gitlinks, so this state is reached the way
    its docstring names, a commit under --no-verify; the gitlink stays P2 while grip.toml's pin changes."""
    root = stale["root"]
    _set_alpha_pin(root, stale["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    assert 'pin = "main"' in git(root, "show", "HEAD:grip.toml")
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]


def test_an_uncommitted_pin_set_back_is_not_the_record(stale):
    """W8. HEAD:grip.toml records P2; the working grip.toml is edited back to P1, which the spec also names. The
    working file is editable, so the record is still the committed P2, and the line names both."""
    root = stale["root"]
    _set_alpha_pin(root, stale["p2"], stale["p1"])
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert stale["p1"][:12] in notice and stale["p2"][:12] in notice and "committed grip.toml" in notice


def test_the_member_is_found_by_path_when_the_working_file_swaps_names(stale):
    """W9. The working grip.toml swaps alpha's and beta's names, paths unchanged. The member at path alpha is still
    alpha's, so it reclones at alpha's committed P2; a name-first match would take beta's entry and beta's pin."""
    root = stale["root"]
    text = (root / "grip.toml").read_text()
    assert text.count('name = "alpha"') == 1 and text.count('name = "beta"') == 1
    swapped = text.replace('name = "alpha"', 'name = "@@"').replace('name = "beta"', 'name = "alpha"')
    (root / "grip.toml").write_text(swapped.replace('name = "@@"', 'name = "beta"'))
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]


def test_sync_run_reclones_a_missing_member_at_the_stores_record(stale):
    """W7. sync run's clone of a removed member is a third reader of the spec's pin; it takes the store's record."""
    root = stale["root"]
    shutil.rmtree(root / "alpha")
    result = _cli("sync", "run", str(root))
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert stale["p1"][:12] in notice and stale["p2"][:12] in notice


def test_full_mode_reads_the_pin_through_the_field_record(stale_full):
    """W10. In full mode the gitlink is a field-commit. With the committed grip.toml pin made a branch name (under
    --no-verify, as in W6), the record is the pin the field-commit carries, P2; the bare gitlink would be no commit
    of alpha's at all."""
    root = stale_full["root"]
    _set_alpha_pin(root, stale_full["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale_full["p2"]


def _drop_alpha(root: Path) -> None:
    text = (root / "grip.toml").read_text()
    blocks = text.split("[[members]]")
    alpha = [i for i, block in enumerate(blocks) if 'name = "alpha"' in block]
    assert len(alpha) == 1 and alpha[0] > 0
    del blocks[alpha[0]]
    (root / "grip.toml").write_text("[[members]]".join(blocks))
    assert 'name = "alpha"' not in (root / "grip.toml").read_text()


def test_a_member_dropped_from_the_working_file_keeps_the_committed_record(stale):
    """W11. The working grip.toml no longer lists alpha, while HEAD:grip.toml still records it at P2. The working file
    is editable, so the commit's pin is still the record: apply reclones at P2, not the spec's P1, and says so."""
    root = stale["root"]
    _drop_alpha(root)
    assert 'name = "alpha"' in git(root, "show", "HEAD:grip.toml")
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert stale["p1"][:12] in notice and stale["p2"][:12] in notice and "committed grip.toml" in notice


@pytest.mark.parametrize("mode", ["plain", "full"])
@pytest.mark.parametrize("pin", ["sha", "branch"])
@pytest.mark.parametrize("working", ["lists", "drops", "absent"])
def test_every_cell_of_the_record_matrix_reclones_at_the_record(tmp_path, monkeypatch, working, pin, mode):
    """M, one row per cell. The record has to come out the same whatever state the working grip.toml is in, so the
    cells are the working file (lists alpha / drops alpha's block / is deleted) by the committed pin (a full commit, or
    a branch name committed under --no-verify as in W6) by the store mode (plain, or full, where the gitlink is a
    field-commit). Every cell's record is P2; the spec says P1. The named rows above cover some cells with more
    detail (lists-sha-plain W1, lists-branch-plain W6, lists-branch-full W10, drops-sha-plain W11, absent-sha-plain W5);
    these rows exist so that no cell is left to a reader's inference. Red when: the committed entry's reader
    skips the gitlink (the four `drops`/`absent` x `branch` cells), or reads the store mode from the working file
    (`absent-branch-full` only: in `drops` the working file still says full, and a full-commit pin answers before the
    mode is read)."""
    built = _build_stale(tmp_path, monkeypatch, full=mode == "full")
    root = built["root"]
    if pin == "branch":
        _set_alpha_pin(root, built["p2"], "main")
        git(root, "add", "grip.toml")
        git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
        assert 'pin = "main"' in git(root, "show", "HEAD:grip.toml")
    if working == "drops":
        _drop_alpha(root)
    elif working == "absent":
        (root / "grip.toml").unlink()
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == built["p2"], "the spec's stale P1" if _alpha_head(
        root / "alpha") == built["p1"] else _alpha_head(root / "alpha")
    notice = next(line for line in result.output.splitlines() if line.startswith("gr2: member 'alpha'"))
    assert built["p1"][:12] in notice and built["p2"][:12] in notice and "using the store's record" in notice


def test_sync_run_reads_the_committed_gitlink_when_the_working_file_is_gone(stale):
    """W7b. sync run's clone site, in the matrix cell where the record is only in the commit's gitlink: the working
    grip.toml is deleted and the committed pin is a branch name. Same resolver as apply, so this row is the RAN
    witness for the second verb, not a different path."""
    root = stale["root"]
    _set_alpha_pin(root, stale["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    (root / "grip.toml").unlink()
    shutil.rmtree(root / "alpha")
    result = _cli("sync", "run", str(root))
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]


def test_a_full_store_whose_working_file_drops_the_mode_line_reads_the_field_record(stale_full):
    """W12. The gitlink is HEAD's, so how to read it is the COMMITTED store mode. The
    working grip.toml loses its `mode = "full"` line (uncommitted); taking that file's mode would read the field-commit
    as alpha's pin, and the clone refuses."""
    root = stale_full["root"]
    _set_alpha_pin(root, stale_full["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    text = (root / "grip.toml").read_text()
    head_part, sep, rest = text.partition("[[members]]")
    assert head_part.count('mode = "full"\n') == 1, head_part
    (root / "grip.toml").write_text(head_part.replace('mode = "full"\n', "") + sep + rest)
    assert "mode" not in tomllib.loads((root / "grip.toml").read_text())
    assert tomllib.loads(git(root, "show", "HEAD:grip.toml")).get("mode") == "full"
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale_full["p2"]


def test_a_plain_store_whose_working_file_adds_a_mode_line_reads_the_gitlink(stale):
    """W13. The mirror of W12: a plain store whose working grip.toml gains an uncommitted
    `mode = "full"`. Taking that file's mode would read alpha's own commit as a field-commit, fail, and fall to the
    spec's stale P1 with exit 0."""
    root = stale["root"]
    _set_alpha_pin(root, stale["p2"], "main")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "--no-verify", "-m", "branch-name pin")
    (root / "grip.toml").write_text('mode = "full"\n' + (root / "grip.toml").read_text())
    assert "mode" not in tomllib.loads(git(root, "show", "HEAD:grip.toml"))
    shutil.rmtree(root / "alpha")
    result = _cli("apply", str(root), "--yes")
    assert result.exit_code == 0, result.output
    assert _alpha_head(root / "alpha") == stale["p2"]
