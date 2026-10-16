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
from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member


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


def test_status_names_a_layered_pin_layer_the_word_check_uses(tmp_path):
    """A healthy layered member read `missing` beside pin == head, and two operators
    could not tell what it meant. Status and check must use one word for one member."""
    root, pin = _layered_store(tmp_path)

    status = gr2(root, "status", "--json")
    checked = gr2(root, "check", "--json")

    assert status.returncode == 0, f"{status.stdout}\n{status.stderr}"
    rows = {m["name"]: m for m in json.loads(status.stdout)["members"]}
    assert rows["alpha"]["state"] == "layer", rows["alpha"]
    assert rows["alpha"]["pin"] == rows["alpha"]["head"] == pin
    assert rows["beta"]["state"] == "upstream", "the control member must read differently"
    assert {m["name"]: m["state"] for m in json.loads(checked.stdout)["members"]}["alpha"] == rows["alpha"]["state"]


def test_status_explains_each_state_word_in_the_human_table(tmp_path):
    root, _ = _layered_store(tmp_path)

    out = gr2(root, "status").stdout
    alpha = next(line for line in out.splitlines() if line.startswith("alpha "))

    assert alpha.startswith("alpha layer ") and "the record's layers carry the pin" in alpha, alpha


def test_status_reads_missing_when_the_layers_do_not_carry_a_pin_no_remote_has(tmp_path):
    """The other side of #1207: in full mode, `layer` only when the layers carry the pin. A record
    whose layer is a real bundle of a DIFFERENT commit, for a pin no remote has, is still `missing`
    (built the way the refusal row above builds it)."""
    root, pin = _layered_store(tmp_path)
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-m", "a different commit")
    raw = _bundle_bytes(root / "alpha", "HEAD")
    git(root / "alpha", "switch", "-q", "--detach", pin)
    _record_with_layers(root, [raw], pin=pin)

    status = gr2(root, "status", "--json")

    rows = {m["name"]: m for m in json.loads(status.stdout)["members"]}
    assert rows["alpha"]["state"] == "missing", rows["alpha"]


def test_the_state_words_status_and_check_emit_are_in_their_published_schemas(tmp_path):
    """A caller validating against `JSON_SHAPES` must not meet a word the schema omits: both verbs emit `layer`."""
    import re

    from gr2.python_cli.grip_cli import JSON_SHAPES

    root, _ = _layered_store(tmp_path)
    for verb in ("status", "check"):
        emitted = {m["state"] for m in json.loads(gr2(root, verb, "--json").stdout)["members"]}
        line = next(p for p in JSON_SHAPES[f"store {verb}"] if p.startswith(".members[].state"))
        published = set(re.search(r"enum\((.*)\)", line).group(1).split(","))
        assert "layer" in emitted and emitted <= published, (verb, emitted, published)
    # push returns check's own rows (`_native_store_push` calls `_native_store_check`), so it publishes check's enum,
    # plus `unchecked`, the word `--recurse=none` gives a member whose coverage it did not ask.
    def enum(verb):
        line = next(p for p in JSON_SHAPES[f"store {verb}"] if p.startswith(".members[].state"))
        return set(re.search(r"enum\((.*)\)", line).group(1).split(","))

    assert enum("push") == enum("check") | {"unchecked"}, (enum("push"), enum("check"))


def test_status_reads_missing_for_a_layered_record_once_the_root_is_switched_out_of_full_mode(tmp_path):
    """The `full and` guard is reachable: a full-mode root whose grip.toml is switched to compat
    without a commit still has HEAD's layered record. Outside full mode a layer is not a recorded carrier, so the
    member reads `missing`, not `layer`."""
    root, _ = _layered_store(tmp_path)
    toml = root / "grip.toml"
    head, sep, rest = toml.read_text(encoding="utf-8").partition("[[members]]")
    assert head.count('mode = "full"') == 1, "the workspace mode line, above the members"
    toml.write_text(head.replace('mode = "full"', 'mode = "compat"') + sep + rest, encoding="utf-8")

    # Exit 4: outside full mode the gitlink (a field-commit) no longer equals the pin, and status says so.
    status = gr2(root, "status", "--json", check=False)

    assert status.returncode == 4, f"{status.stdout}\n{status.stderr}"
    rows = {m["name"]: m for m in json.loads(status.stdout)["members"]}
    assert rows["alpha"]["state"] == "missing", rows["alpha"]


def test_status_in_a_compat_store_reads_missing_for_a_rewound_upstream_without_reading_a_record(tmp_path):
    """The `full and` guard's other job: in a compat store the gitlink names the member's own
    commit, which the root does not hold, so reading a field record there refuses. Without the guard, status
    exits 4 with advice to unshallow; with it, a pin the rewound upstream lacks reads `missing`."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        git(root, "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    before = _member_head(root, "alpha")
    pin = _advance(root, "alpha", push=True)
    gr2(root, "commit", "-m", "alpha pushed")
    git(root / "alpha", "push", "--force", "origin", f"{before}:main")   # the upstream rewound behind the pin

    status = gr2(root, "status", "--json", check=False)

    assert status.returncode == 0, f"{status.stdout}\n{status.stderr}"
    rows = {m["name"]: m for m in json.loads(status.stdout)["members"]}
    assert rows["alpha"]["pin"] == pin and rows["alpha"]["state"] == "missing", rows["alpha"]


def test_commit_says_which_member_it_carried_as_a_layer(tmp_path):
    """An operator read `store commit` as origin-covered only, saw nothing about a layer, and
    declined a task gr2 had already done. The verb names each member it carried as a layer."""
    root = _full_store(tmp_path)
    pin = _advance(root, "alpha", push=False)

    done = gr2(root, "commit", "-m", "alpha only local")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert f"alpha: pin {pin[:12]} is on no remote; carried as a layer in the record" in done.stdout, done.stdout
    assert "beta" not in done.stdout, "a covered member gets no layer line"


def test_commit_json_lists_the_layered_members(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)

    done = gr2(root, "commit", "-m", "alpha only local", "--json")

    assert json.loads(done.stdout)["layered"] == ["alpha"]


def test_commit_of_covered_pins_says_nothing_about_layers(tmp_path):
    """The control: a pushed pin writes no layer, so the verb names none."""
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=True)

    done = gr2(root, "commit", "-m", "alpha pushed", "--json")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert json.loads(done.stdout)["layered"] == []


def test_commit_help_says_what_full_mode_does_with_an_uncovered_pin(tmp_path):
    root = _full_store(tmp_path)

    text = " ".join(gr2(root, "commit", "--help").stdout.split())

    assert "origin-covered member pins" not in text
    assert "full mode" in text and "layer" in text


def test_a_later_commit_that_moves_only_another_member_still_names_the_layered_one(tmp_path):
    """`layered` is state, not delta: alpha's work is still recorded only as a layer after a commit that moved only
    beta, so the verb names alpha again (the operator's question is whether the work is recorded)."""
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    assert gr2(root, "commit", "-m", "alpha only local").returncode == 0
    _advance(root, "beta", push=True)

    done = gr2(root, "commit", "-m", "beta pushed", "--json")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert json.loads(done.stdout)["layered"] == ["alpha"]


def test_the_snapshot_alias_prints_the_layer_line_too(tmp_path):
    root = _full_store(tmp_path)
    pin = _advance(root, "alpha", push=False)

    done = gr2(root, "snapshot", "-m", "alpha only local")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert f"alpha: pin {pin[:12]} is on no remote; carried as a layer in the record" in done.stdout, done.stdout


def test_a_no_op_commit_names_no_layered_member(tmp_path):
    """Nothing recorded, nothing named: a second commit with alpha still layered and nothing changed is `unchanged`,
    and `layered` is empty, because the verb wrote no record."""
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    assert gr2(root, "commit", "-m", "alpha only local").returncode == 0

    again = gr2(root, "commit", "-m", "nothing new", "--json")

    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"
    assert json.loads(again.stdout) == {"status": "unchanged", "root_commit": json.loads(again.stdout)["root_commit"], "layered": []}
