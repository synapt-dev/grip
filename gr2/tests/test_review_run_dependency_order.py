"""`review run` on a multi-member lane derives the install order from what each member DECLARES.

Before this, the order was the lane marker's row order (sorted by member key at bind) unless
`--order` named one, so a member that depends on another installed correctly only when its key
happened to sort after the key of the member it needs. uv installs a workspace's members into one
`.venv` in dependency order derived from each member's declared dependencies; this does the same,
keeps `--order` as the explicit override, and refuses a cycle by name.

Each row names what must make it go red:
  * the derivation is not called           -> witness, receipt-source rows
  * the derivation runs BEFORE integrity   -> the tamper-precedence row
  * the tie-break is not the marker order  -> the stability rows
  * names are compared raw, not PEP 503    -> the normalisation rows
  * a cycle is broken silently             -> the cycle rows
  * `--order` stops overriding             -> the override row
  * optional-dependencies become edges     -> the optional-dependencies row
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from gr2.python_cli import review_run as rr

from tests.test_review_run_multi_repo import (
    CORE_SRC,
    CORE_TEST,
    WEB_SRC,
    _git,
    _member,
    _web_test,
)


def _declare(lane: Path, key: str, name: str, deps: list[str]) -> str:
    """Give the member at `<lane>/<key>` a pyproject declaring distribution `name` and `deps`, fold
    it into the member's one commit, and return the new head tree."""
    repo = lane / key
    listing = ", ".join(json.dumps(d) for d in deps)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = {json.dumps(name)}\nversion = "0"\ndependencies = [{listing}]\n'
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    return _git(repo, "rev-parse", "HEAD^{tree}")


def _witness_lane(tmp_path: Path, *, web_deps: list[str] | None = None,
                  core_deps: list[str] | None = None) -> Path:
    """The card's witness: `a-web` (distribution web-app, needs core-lib) sorts FIRST by key, and
    `z-core` (distribution core-lib) sorts last. The marker order is therefore [a-web, z-core]."""
    lane = tmp_path / "lane"
    lane.mkdir()
    web_tree = _member(lane, "a-web", "demo_web", WEB_SRC, _web_test(lane / "z-core"))
    web_tree = _declare(lane, "a-web", "web-app", ["core-lib>=1"] if web_deps is None else web_deps)
    core_tree = _member(lane, "z-core", "demo_core", CORE_SRC, CORE_TEST)
    core_tree = _declare(lane, "z-core", "core-lib", core_deps or [])
    marker = {
        "kind": rr._MARKER_KIND,
        "gr_commit": "cafef00d" * 5,
        "repos": [
            {"key": "a-web", "bound_head": "a" * 40, "bound_head_tree": web_tree,
             "reconstructed_tree": web_tree, "tree_match": True},
            {"key": "z-core", "bound_head": "b" * 40, "bound_head_tree": core_tree,
             "reconstructed_tree": core_tree, "tree_match": True},
        ],
    }
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n")
    return lane


def _pyproject_dirs(tmp_path: Path, spec: dict[str, str | None]) -> Path:
    """A lane directory holding one member directory per key, each with the given pyproject text
    (None = no pyproject at all). No git, no venv: for rows that call the derivation directly."""
    lane = tmp_path / "lane"
    for key, text in spec.items():
        (lane / key).mkdir(parents=True)
        if text is not None:
            (lane / key / "pyproject.toml").write_text(text)
    return lane


def _pp(name: str, deps: list[str] | None = None, optional: dict[str, list[str]] | None = None) -> str:
    out = f'[project]\nname = {json.dumps(name)}\nversion = "0"\n'
    out += "dependencies = [" + ", ".join(json.dumps(d) for d in (deps or [])) + "]\n"
    for extra, reqs in (optional or {}).items():
        out += f"[project.optional-dependencies]\n{extra} = [" + ", ".join(json.dumps(r) for r in reqs) + "]\n"
    return out


# ------------------------------------------------------------------ the card's witness (venv rows)


def test_a_member_that_sorts_first_but_depends_on_a_later_one_still_installs_second(tmp_path: Path) -> None:
    """THE WITNESS. a-web needs core-lib, which z-core provides, and a-web sorts first by key. With the
    old marker order a-web installs first and its test cannot import the reviewed core; derived, z-core
    installs first and the lane is green. Goes red if the derivation is not called."""
    lane = _witness_lane(tmp_path)
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green", receipt
    assert receipt["order"] == ["z-core", "a-web"]
    assert receipt["order_source"] == "dependencies"
    assert [m["key"] for m in receipt["members"]] == ["z-core", "a-web"]


def test_explicit_order_still_overrides_the_derivation(tmp_path: Path) -> None:
    """`--order` is the override: the same lane, told web first, runs web first and is not green,
    and the receipt says the order was explicit. Goes red if `--order` stops overriding."""
    lane = _witness_lane(tmp_path)
    try:
        receipt = rr.run_review_lane(lane, pytest_args=["-q"], order=["a-web", "z-core"])
    except rr.ReviewRunRefused as exc:
        assert exc.member == "a-web" and exc.order == ["a-web", "z-core"]
        assert exc.order_source == "explicit"
        receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
        assert receipt["result"] == "refused"
    else:
        assert receipt["result"] != "green", receipt
    assert receipt["order"] == ["a-web", "z-core"] and receipt["order_source"] == "explicit"


def test_a_dependency_cycle_refuses_by_name_before_any_venv_exists(tmp_path: Path) -> None:
    """a-web needs core-lib and z-core needs web-app: no order exists. The refusal prints the loop and
    the way out, nothing installs, and the receipt carries the refusal. Goes red if a cycle is broken
    silently (an order is returned), or if the venv is created first."""
    lane = _witness_lane(tmp_path, core_deps=["web-app"])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "dependency_cycle"
    assert "a-web -> z-core -> a-web" in exc.value.detail and "--order" in exc.value.detail
    assert not (lane / rr._VENV_DIRNAME).exists()
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["refusal_code"] == "dependency_cycle"
    assert receipt["order"] == ["a-web", "z-core"], "the marker order: the derivation refused"
    assert receipt["order_source"] is None, "no derived order exists, so none is claimed"


def test_a_name_clash_refusal_does_not_claim_a_derived_order(tmp_path: Path) -> None:
    """Two members declaring one distribution refuse from inside the derivation, so the receipt carries the
    marker order and no source, the same as the cycle and the tree_drift refusals. Goes red if order_source is
    assigned before the derivation is called."""
    lane = _witness_lane(tmp_path)
    clash_tree = _declare(lane, "a-web", "core-lib", [])
    marker_path = lane / rr._MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["repos"][0]["bound_head_tree"] = marker["repos"][0]["reconstructed_tree"] = clash_tree
    marker_path.write_text(json.dumps(marker, indent=2) + "\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "member_name_clash"
    assert not (lane / rr._VENV_DIRNAME).exists()
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["refusal_code"] == "member_name_clash" and receipt["order"] == ["a-web", "z-core"]
    assert receipt["order_source"] is None


def test_order_flag_bypasses_the_cycle_refusal(tmp_path: Path) -> None:
    """`--order` is the explicit way out of a cycle: with it the derivation is never consulted."""
    lane = _witness_lane(tmp_path, core_deps=["web-app"])
    try:
        rr.run_review_lane(lane, pytest_args=["-q"], order=["z-core", "a-web"])
    except rr.ReviewRunRefused as exc:
        assert exc.code != "dependency_cycle", exc
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["order"] == ["z-core", "a-web"] and receipt["order_source"] == "explicit"


def test_a_tampered_pyproject_refuses_as_a_tamper_not_as_a_cycle(tmp_path: Path) -> None:
    """The order is read from each member's pyproject, so it is read only AFTER every tree passed its
    integrity check: a pyproject edited in the lane to create a cycle must refuse as tree_drift, never
    steer the order or surface as a cycle. Goes red if the derivation runs before integrity."""
    lane = _witness_lane(tmp_path)
    (lane / "z-core" / "pyproject.toml").write_text(_pp("core-lib", ["web-app"]))
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "z-core"
    assert not (lane / rr._VENV_DIRNAME).exists()


def test_a_refusal_before_the_derivation_does_not_claim_a_derived_order(tmp_path: Path) -> None:
    """order_source says how the order was chosen, so it says "dependencies" only once the derivation ran.
    A tree_drift refusal happens before it (by design), so the receipt carries the MARKER order and no
    source. Goes red if order_source is set to "dependencies" before the integrity checks."""
    lane = _witness_lane(tmp_path)
    (lane / "z-core" / "pyproject.toml").write_text(_pp("core-lib", ["web-app"]))
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.order_source is None
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["order"] == ["a-web", "z-core"], "the marker order: nothing was derived"
    assert receipt["order_source"] is None


def test_a_refusal_before_the_run_with_an_explicit_order_still_says_explicit(tmp_path: Path) -> None:
    """The control for the row above: when --order named the order, that IS how it was chosen, even
    for a refusal that comes before anything ran."""
    lane = _witness_lane(tmp_path)
    (lane / "z-core" / "pyproject.toml").write_text(_pp("core-lib", ["web-app"]))
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"], order=["z-core", "a-web"])
    assert exc.value.code == "tree_drift" and exc.value.order_source == "explicit"
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["order"] == ["z-core", "a-web"] and receipt["order_source"] == "explicit"


# ------------------------------------------------------------------ the derivation itself (no venv)


def test_members_without_any_pyproject_keep_the_marker_order(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"b": None, "a": None, "c": None})
    assert rr._derive_member_order(lane, ["b", "a", "c"]) == ["b", "a", "c"]


def test_members_that_do_not_depend_on_each_other_keep_the_marker_order(tmp_path: Path) -> None:
    """Stability: no edges between members means the marker order it always had. Goes red if the
    tie-break is anything but the marker order (alphabetical, reversed, hash order)."""
    lane = _pyproject_dirs(tmp_path, {"zeta": _pp("zeta-lib", ["requests"]), "alpha": _pp("alpha-lib")})
    assert rr._derive_member_order(lane, ["zeta", "alpha"]) == ["zeta", "alpha"]


def test_dependencies_come_first_and_ties_follow_the_marker_order(tmp_path: Path) -> None:
    """a and b both need c: c goes first, then a, then b (marker order among the ready)."""
    lane = _pyproject_dirs(tmp_path, {
        "a": _pp("a-lib", ["c-lib"]), "b": _pp("b-lib", ["c-lib"]), "c": _pp("c-lib"),
    })
    assert rr._derive_member_order(lane, ["a", "b", "c"]) == ["c", "a", "b"]


def test_a_chain_in_the_wrong_marker_order_is_fully_reversed(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {
        "a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib", ["c-lib"]), "c": _pp("c-lib"),
    })
    assert rr._derive_member_order(lane, ["a", "b", "c"]) == ["c", "b", "a"]


@pytest.mark.parametrize("requirement", [
    "core.lib>=1.0",
    "Core_Lib",
    "CORE-LIB[extra]>=1; python_version >= '3.11'",
    "core-lib @ file:///somewhere",
])
def test_distribution_names_compare_after_pep_503_normalisation(tmp_path: Path, requirement: str) -> None:
    """The dependency is spelled several ways and is still the member `core-lib`. Goes red if names
    are compared raw, or if the requirement is not cut down to its name."""
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib", [requirement]), "b": _pp("Core_Lib")})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["b", "a"]


def test_a_dependency_on_something_outside_the_lane_and_a_self_dependency_make_no_edge(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib", ["a-lib", "pytest>=7", "numpy"]), "b": _pp("b-lib")})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]


def test_optional_dependencies_are_not_edges(tmp_path: Path) -> None:
    """Only `[project].dependencies` orders the install: an extra is not installed unless asked for,
    and the member's own install hint decides that. Goes red if extras become edges."""
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib", optional={"dev": ["b-lib"]}), "b": _pp("b-lib")})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]


def test_two_members_claiming_one_distribution_are_refused_by_name(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("same-lib"), "b": _pp("Same_Lib"), "c": _pp("c-lib")})
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr._derive_member_order(lane, ["a", "b", "c"])
    assert exc.value.code == "member_name_clash"
    assert "'a'" in exc.value.detail and "'b'" in exc.value.detail and "--order" in exc.value.detail


def test_an_unreadable_pyproject_declares_nothing_and_does_not_crash(tmp_path: Path) -> None:
    """A member whose pyproject is not TOML is placed by the marker order alone, as before; the
    derivation never turns that into a traceback."""
    lane = _pyproject_dirs(tmp_path, {"a": "this is [not toml", "b": _pp("b-lib", ["a-lib"])})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]


def test_a_cycle_is_named_without_the_members_outside_it(tmp_path: Path) -> None:
    """a -> b -> c -> a, with d free: the loop is printed, d is not in it. Goes red if the cycle
    message names every remaining member instead of the loop."""
    lane = _pyproject_dirs(tmp_path, {
        "a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib", ["c-lib"]),
        "c": _pp("c-lib", ["a-lib"]), "d": _pp("d-lib"),
    })
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr._derive_member_order(lane, ["a", "b", "c", "d"])
    assert exc.value.code == "dependency_cycle"
    loop = exc.value.detail.split("exists: ")[1].split("; ")[0]
    assert loop == "a -> b -> c -> a"


@pytest.mark.parametrize("text", [
    "project = 5\n",
    "[[project]]\nname = 'a'\n",
    "[project]\nname = 'a'\ndependencies = 5\n",
    "[project]\nname = 'a'\ndependencies = 'b'\n",
    "[project]\nname = 'a'\ndependencies = [5, {x = 1}]\n",
], ids=["project-not-a-table", "project-array-of-tables", "dependencies-int", "dependencies-str", "dependencies-non-str-entries"])
def test_valid_toml_that_is_not_a_project_table_declares_nothing(tmp_path: Path, text: str) -> None:
    """The same promise as an unreadable file: a pyproject that parses but is not a [project] table with
    a dependency LIST of requirement strings declares no edge and does not crash. A string is not a list
    of names (it must not be walked character by character, which would hand member `b` an edge to a).
    Goes red if the isinstance guards are removed."""
    lane = _pyproject_dirs(tmp_path, {"a": text, "b": _pp("b")})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]


def test_a_non_string_dependency_entry_is_skipped_not_stringified(tmp_path: Path) -> None:
    """`dependencies = [5]` must not become the requirement "5": a member whose distribution is named `5`
    would otherwise gain an edge from a number. Goes red if the entry is passed through str()."""
    lane = _pyproject_dirs(tmp_path, {"a": "[project]\nname = 'a'\ndependencies = [5]\n", "b": _pp("5")})
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]
