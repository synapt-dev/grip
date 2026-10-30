"""`gr2 footprint` lists what gr2 wrote for a workspace and writes nothing itself.

  * the verb writes nothing: a snapshot of the workspace, its parent, HOME, XDG_STATE_HOME, TMPDIR, the review cache
    root and uv's cache and python dirs (all in tmp_path) is byte-equal, mtimes included, before and after the human
    and --json runs
  * on a native workspace it lists the store file and the shadow refs, and the store's exclude line as an edit row
    (found where git says the exclude file is, as its writer finds it)
  * the gr shim's state is listed by the install prefix it is keyed by (marker or user-state arm), KEEP-SHARED, and
    never under a hash of the workspace root
  * the shadow-ref prefix has one spelling: griplink's namespace is grip_cli's ref root
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from gr2.python_cli import gr_shadow, grip_cli, griplink, layout
from tests.test_one_pin_record import _cli
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)


@pytest.fixture
def native_root(tmp_path, monkeypatch):
    for var, sub in (("HOME", "home"), ("XDG_STATE_HOME", "xdg"), ("TMPDIR", "tmp"),
                     ("SYNAPT_REVIEW_CACHE_ROOT", "review-cache"), ("UV_CACHE_DIR", "uv-cache"),
                     ("UV_PYTHON_INSTALL_DIR", "uv-python")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    root = tmp_path / "ws"
    root.mkdir()
    remote, seed = tmp_path / "alpha.git", tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", remote)
    git(tmp_path, "clone", "-q", remote, seed)
    git(seed, "checkout", "-q", "-B", "main")
    (seed / "f").write_text("alpha 1\n")
    git(seed, "add", "f")
    git(seed, "commit", "-qm", "alpha 1")
    git(seed, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", f"file://{remote}", root / "alpha")
    assert _cli("store", "init", str(root)).exit_code == 0
    monkeypatch.chdir(root)
    assert _cli("store", "commit", "-m", "record").exit_code == 0
    return root


def _snapshot(base: Path) -> dict:
    seen = {}
    for dirpath, dirs, files in os.walk(base):
        for name in [""] + dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            body = Path(p).read_bytes() if os.path.isfile(p) and not os.path.islink(p) else b""
            seen[os.path.relpath(p, base)] = (st.st_mode, st.st_size, st.st_mtime_ns, body)
    return seen


def test_footprint_writes_nothing(native_root):
    tmp = native_root.parent
    before = _snapshot(tmp)
    human = _cli("footprint", str(native_root))
    as_json = _cli("footprint", "--json", str(native_root))
    after = _snapshot(tmp)
    assert human.exit_code == 0 and as_json.exit_code == 0
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert changed == [], f"footprint wrote: {changed}"


def test_footprint_lists_state_dir_and_shadow_refs(native_root):
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    rows = json.loads(out.stdout)["rows"]
    paths = [r["path"] for r in rows if r["disposition"] == "REMOVE"]
    refs = [r["ref"] for r in rows if r["kind"] == "ref" and r["store"] == str(native_root)]
    # A native store writes no `.grip/` at init or commit: the dir row appears only when the dir exists.
    assert paths.count(str(native_root / "grip.toml")) == 1
    assert paths.count(str(layout.grip_dir(native_root))) == 0
    assert len(refs) >= 1 and all(r.startswith(grip_cli.GRIP_REF_ROOT) for r in refs)
    edits = [r for r in rows if r["kind"] == "line"]
    assert [e["line"] for e in edits].count("/.grip/") == 1
    table = _cli("footprint", str(native_root)).output
    assert table.count(f"{native_root / 'grip.toml'}\n") == 1


def test_footprint_says_it_is_partial(native_root):
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    assert "complete" in doc and doc["complete"] is False
    assert "partial: not every path gr2 writes is listed yet" in _cli("footprint", str(native_root)).output.splitlines()


@pytest.mark.parametrize("arm", ["marker", "user-state"])
def test_footprint_lists_shadow_state_by_install_prefix(native_root, monkeypatch, arm):
    # sys.prefix is monkeypatched to a tmp dir; the user-state arm makes it a plain file, so the
    # marker cannot be written and _record falls to the user state.
    prefix = native_root.parent / "prefix"
    prefix.mkdir() if arm == "marker" else prefix.write_text("")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    assert gr_shadow._record(sys.prefix, "x")
    marker, state = prefix / gr_shadow.MARKER, gr_shadow._user_state(sys.prefix)
    assert (marker.exists(), state.exists()) == ((True, False) if arm == "marker" else (False, True))
    written = marker if arm == "marker" else state
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    rows = json.loads(out.stdout)["rows"]
    listed = [r["disposition"] for r in rows if r.get("path") == str(written)]
    assert listed == ["KEEP-SHARED"]
    assert not any(r.get("path") == str(gr_shadow._user_state(str(native_root))) for r in rows)


def test_footprint_lists_gr1_checkouts_as_not_gr2(native_root):
    grip_dir = layout.grip_dir(native_root)
    (grip_dir / "state").mkdir(parents=True)
    (grip_dir / "state" / "applied.toml").write_text("x" * 100)
    checkout = grip_dir / layout.GR1_CHECKOUTS_DIR / "feat-x"
    checkout.mkdir(parents=True)
    (checkout / "big.bin").write_bytes(b"\0" * 1_000_000)
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    by_path = {r["path"]: r for r in doc["rows"] if r["path"]}
    row = by_path[str(checkout)]
    assert (row["disposition"], row["written_by"], row["remove_with"]) == (
        "NOT-GR2", "gr1 gr checkout add", "gr checkout remove feat-x")
    assert by_path[str(grip_dir / "state")]["disposition"] == "REMOVE"
    assert str(grip_dir) not in by_path and str(grip_dir / layout.GR1_CHECKOUTS_DIR) not in by_path
    removed = sum(r["size"] or 0 for r in doc["rows"] if r["disposition"] == "REMOVE")
    assert doc["totals"]["remove_bytes"] == removed and removed < 1_000_000
    table = _cli("footprint", str(native_root)).output
    assert table.count(f"{checkout} (remove with: gr checkout remove feat-x)") == 1


def test_footprint_lists_a_symlinked_grip_dir_as_one_link_row(native_root, tmp_path):
    grip_dir = layout.grip_dir(native_root)
    store = tmp_path / "store"
    (store / "state").mkdir(parents=True)
    held = store / "state" / "held.bin"
    held.write_bytes(b"\0" * 1_000_000)
    grip_dir.symlink_to(store)
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    paths = [r["path"] for r in doc["rows"] if r["path"]]
    links = [r for r in doc["rows"] if r["path"] == str(grip_dir)]
    assert len(links) == 1 and links[0]["disposition"] == "REMOVE" and links[0]["kind"] == "symlink"
    assert not [p for p in paths if p.startswith(f"{grip_dir}/") or p.startswith(str(store))]
    assert doc["totals"]["remove_bytes"] < 1_000_000
    assert grip_dir.is_symlink() and store.is_dir() and held.stat().st_size == 1_000_000


def test_footprint_refuses_a_plain_directory(tmp_path):
    out = _cli("footprint", str(tmp_path))
    assert out.exit_code == 2


def test_shadow_ref_prefix_is_one_spelling():
    assert griplink.NS == grip_cli.GRIP_REF_ROOT.rstrip("/")


def test_footprint_prints_no_lane_dir_line(native_root):
    """footprint learns the `<ws>.review` spelling from a pure helper: no `review open` announcement on stderr,
    and no refusal when a directory with the old probe's name is there."""
    review = native_root.parent / f"{native_root.name}.review"
    (review / ("g" * 8)).mkdir(parents=True)
    out = _cli("footprint", "--json", str(native_root))
    assert out.exit_code == 0, out.output
    assert "lane-dir=" not in out.output
    rows = json.loads(out.stdout)["rows"]
    assert [r["disposition"] for r in rows if r.get("path") == str(review)] == ["REMOVE"]
