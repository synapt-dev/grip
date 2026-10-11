"""Full mode: a pin no remote holds is carried by a LAYER, a git bundle stored in the member's record.

A layer holds the commits a pin has that the member's upstream lacks, as one occurrence of the repeated `layers` field
of the record. `store commit` writes it where it used to refuse the member; `materialize` fetches it into the clone and
checks the pin out; `store check` accepts a layered pin only when the last layer's head is the pin.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli import member_field_tree as mft
from tests.test_store_full_mode_carrier import _advance, _full_store, _gitlink, _member_head
from tests.test_store_git_native_smoke import configure_identity, git, gr2


def _record(root: Path, member: str) -> dict:
    return mft.read(root, f"{_gitlink(root, member)}^{{tree}}")


def _layered_store(tmp_path: Path) -> tuple[Path, str]:
    """A full-mode store whose alpha pin is on no remote, committed, and that pin."""
    root = _full_store(tmp_path)
    pin = _advance(root, "alpha", push=False)
    done = gr2(root, "commit", "-m", "alpha only local")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    return root, pin


def _fresh_clone_without_the_pin(tmp_path: Path, root: Path, pin: str) -> Path:
    """A second workspace clone whose alpha checkout cannot have the pin from anywhere but the layer."""
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    git(fresh, "clone", "--no-local", url, "alpha")
    # THE PRECONDITION THIS ROW RESTS ON: neither the remote nor the fresh checkout holds the pin, so a green
    # materialize can only have got it from the layer.
    assert git(fresh / "alpha", "cat-file", "-t", pin, check=False).returncode != 0
    return fresh


def test_a_pin_on_no_remote_is_recorded_with_one_layer_and_no_covering_ref(tmp_path):
    root, pin = _layered_store(tmp_path)

    record = _record(root, "alpha")

    assert record["pin"] == pin == _member_head(root, "alpha")
    assert len(record["layers"]) == 1 and record["layers"][0].startswith(mft.BUNDLE_HEADERS)
    assert record["covered_by"] == ""
    assert "layers" not in (root / "grip.toml").read_text(encoding="utf-8")


def test_a_covered_pin_carries_no_layer(tmp_path):
    """The control for the row above: the same store, a pushed pin, writes no layer."""
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=True)
    assert gr2(root, "commit", "-m", "alpha pushed").returncode == 0

    record = _record(root, "alpha")

    assert record["layers"] == [] and record["covered_by"] == "refs/heads/main"


def test_materialize_rebuilds_a_pin_no_remote_has_from_the_layer(tmp_path):
    root, pin = _layered_store(tmp_path)
    fresh = _fresh_clone_without_the_pin(tmp_path, root, pin)

    made = gr2(fresh, "materialize")

    assert made.returncode == 0, f"{made.stdout}\n{made.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin
    assert (fresh / "alpha" / "alpha-0.txt").read_text() == "moved\n"


def test_an_unchanged_layered_member_keeps_its_field_commit(tmp_path):
    root, _ = _layered_store(tmp_path)
    before = _gitlink(root, "alpha")
    _advance(root, "beta", push=True)

    assert gr2(root, "commit", "-m", "beta moved").returncode == 0

    assert _gitlink(root, "alpha") == before


def test_check_accepts_a_layered_pin_and_names_the_state(tmp_path):
    root, _ = _layered_store(tmp_path)

    checked = gr2(root, "check", "--json")

    assert checked.returncode == 0, f"{checked.stdout}\n{checked.stderr}"
    states = {m["name"]: m["state"] for m in json.loads(checked.stdout)["members"]}
    assert states == {"alpha": "layer", "beta": "upstream"}


def _record_with_layers(root: Path, layers: list[bytes], pin: str | None = None) -> Path:
    """The same store with alpha's record rewritten to carry `layers`, as any pusher could."""
    pin = pin or _member_head(root, "alpha")
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    forged = mft.commit(root, dict(kind=mft.KIND_REPO, pin=pin, url=url, ref="main", layers=layers), "forged record")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{forged},alpha")
    tree = git(root, "write-tree").stdout.strip()
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    made = git(root, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit-tree", tree,
               "-p", head, "-p", forged, "-m", "carry the forged record").stdout.strip()
    git(root, "update-ref", "HEAD", made)
    return root


def test_a_layer_that_is_not_a_git_bundle_is_refused_at_the_read(tmp_path):
    root = _record_with_layers(_full_store(tmp_path), [b"not a bundle"])
    git(tmp_path, "clone", str(root), "victim")

    made = gr2(tmp_path / "victim", "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode != 0 and "is not a git bundle" in out and "Traceback" not in out, out


def test_a_layer_that_does_not_carry_the_pin_is_refused_by_name(tmp_path):
    """A real bundle, but of an older commit than the pin the record names: `check` and `materialize` both say so."""
    root, pin = _layered_store(tmp_path)
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-m", "a different commit")
    raw = _bundle_bytes(root / "alpha", "HEAD")
    git(root / "alpha", "switch", "-q", "--detach", pin)
    forged = _record_with_layers(root, [raw], pin=pin)

    checked = gr2(forged, "check", check=False)

    assert checked.returncode == 3 and "full_mode_layer_does_not_carry_pin" in f"{checked.stdout}{checked.stderr}", (
        checked.stdout, checked.stderr)


def _bundle_bytes(repo: Path, ref: str) -> bytes:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), "bundle", "create", "-", ref, "--not", "origin/main"],
                          capture_output=True, check=True).stdout


def test_a_root_with_a_layered_member_can_be_pushed_and_a_clone_of_it_materializes_the_pin(tmp_path):
    """The point of a layer: the workspace remote carries what the member's remote does not, so push, clone and
    materialize reconstruct the pin from the two remotes with nothing else."""
    root, pin = _layered_store(tmp_path)
    bare = tmp_path / "root.git"
    git(tmp_path, "init", "--bare", str(bare))

    pushed = git(root, "push", str(bare), "HEAD:refs/heads/main", check=False)
    assert pushed.returncode == 0, f"{pushed.stdout}\n{pushed.stderr}"
    git(tmp_path, "clone", "--branch", "main", str(bare), "second")
    second = tmp_path / "second"
    configure_identity(second)
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    git(second, "clone", "--no-local", url, "alpha")
    assert git(second / "alpha", "cat-file", "-t", pin, check=False).returncode != 0   # the precondition

    made = gr2(second, "materialize")

    assert made.returncode == 0, f"{made.stdout}\n{made.stderr}"
    assert git(second / "alpha", "rev-parse", "HEAD").stdout.strip() == pin


def test_a_push_whose_layer_does_not_carry_the_pin_is_refused_by_the_hook(tmp_path):
    root, pin = _layered_store(tmp_path)
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-m", "a different commit")
    raw = _bundle_bytes(root / "alpha", "HEAD")
    git(root / "alpha", "switch", "-q", "--detach", pin)
    forged = _record_with_layers(root, [raw], pin=pin)
    bare = tmp_path / "root.git"
    git(tmp_path, "init", "--bare", str(bare))

    pushed = git(forged, "push", str(bare), "HEAD:refs/heads/main", check=False)

    assert pushed.returncode != 0 and "full_mode_layer_does_not_carry_pin" in f"{pushed.stdout}{pushed.stderr}", (
        pushed.stdout, pushed.stderr)


def test_materialize_refuses_by_name_when_the_layer_does_not_carry_the_pin(tmp_path):
    root, pin = _layered_store(tmp_path)
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-m", "a different commit")
    raw = _bundle_bytes(root / "alpha", "HEAD")
    git(root / "alpha", "switch", "-q", "--detach", pin)
    forged = _record_with_layers(root, [raw], pin=pin)
    git(tmp_path, "clone", str(forged), "victim")
    victim = tmp_path / "victim"
    configure_identity(victim)
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    git(victim, "clone", "--no-local", url, "alpha")

    made = gr2(victim, "materialize", check=False)

    assert made.returncode == 3 and "full_mode_layer_does_not_carry_pin" in f"{made.stdout}{made.stderr}", (
        made.stdout, made.stderr)


def test_two_layer_lists_with_the_same_bytes_joined_are_measured_separately_by_the_hook(tmp_path):
    """[A, B] and [A+B] are different records with the same joined bytes. The first (its last layer carries the pin) is
    allowed and the second (its only layer's header names another commit) must not be skipped as a repeat of it."""
    root, pin = _layered_store(tmp_path)
    good = _record(root, "alpha")["layers"][0]
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-m", "a different commit")
    other = _bundle_bytes(root / "alpha", "HEAD")
    git(root / "alpha", "switch", "-q", "--detach", pin)
    _record_with_layers(root, [other, good], pin=pin)
    forged = _record_with_layers(root, [other + good], pin=pin)
    bare = tmp_path / "root.git"
    git(tmp_path, "init", "--bare", str(bare))

    assert gr2(forged, "check", check=False).returncode == 3   # the precondition: the second record IS refused alone
    pushed = git(forged, "push", str(bare), "HEAD:refs/heads/main", check=False)

    assert pushed.returncode != 0 and "full_mode_layer_does_not_carry_pin" in f"{pushed.stdout}{pushed.stderr}", (
        pushed.stdout, pushed.stderr)


def test_a_layer_over_the_size_bound_is_refused_at_the_read_and_one_at_the_bound_is_not(tmp_path):
    root = _full_store(tmp_path)
    header = b"# v2 git bundle\n"
    pin = _member_head(root, "alpha")
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()

    def read(size: int) -> dict:
        rec = dict(kind=mft.KIND_REPO, pin=pin, url=url, ref="main", layers=[header + b"x" * (size - len(header))])
        return mft.read(root, f"{mft.commit(root, rec, 'r')}^{{tree}}")

    assert len(read(mft.MAX_LAYER_BYTES)["layers"][0]) == mft.MAX_LAYER_BYTES     # the bound itself is allowed
    with pytest.raises(mft.FieldTreeError, match="over the"):
        read(mft.MAX_LAYER_BYTES + 1)


def test_a_layer_over_the_size_bound_is_refused_at_the_write_by_name_and_no_root_commit_is_made(tmp_path):
    """Commit and check must agree: a record `check` and push would refuse is never written."""
    import os

    root = _full_store(tmp_path)
    before = git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "alpha" / "big.bin").write_bytes(os.urandom(mft.MAX_LAYER_BYTES + 1024 * 1024))
    git(root / "alpha", "add", "big.bin")
    git(root / "alpha", "commit", "-m", "a large unpushed commit")

    refused = gr2(root, "commit", "-m", "too large", check=False)

    out = f"{refused.stdout}{refused.stderr}"
    assert refused.returncode == 3 and "full_mode_layer_too_large" in out, (refused.returncode, out[-300:])
    assert git(root, "rev-parse", "HEAD").stdout.strip() == before   # no root commit was made


def test_a_record_with_an_oversize_layer_is_refused_without_advising_unshallow(tmp_path):
    """The unshallow advice is for a record this clone does not hold; an oversize layer is held and refused."""
    root = _full_store(tmp_path)
    layer = b"# v2 git bundle\n" + b"x" * mft.MAX_LAYER_BYTES   # one byte over the bound once the header is counted
    forged = _record_with_layers(root, [layer])

    checked = gr2(forged, "check", check=False)

    out = f"{checked.stdout}{checked.stderr}"
    assert checked.returncode == 4 and "over the" in out and "unshallow" not in out, (checked.returncode, out[-300:])
