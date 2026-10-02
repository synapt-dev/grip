"""The built-in Python plugin, and the plan phase that drives plugins (step 2 of the lane-graph slice).

Two jobs.

1. CONFORMANCE. ``review_run._derive_member_order`` stays where it is in this slice and is what the lane run
   still calls. Its derivation rows (``test_review_run_dependency_order.py``) are re-run here through the plugin
   path (``ecosystems_python`` + ``lane_graph.plan_lane``) on the SAME fixtures, and the two answers are compared.
   They must be identical for every lane dev can order. They differ in exactly one place, on purpose: dev REFUSES a
   dependency cycle between members and the plan phase PLANS it as one group (design note, section 5). Both halves
   of that divergence are pinned below, so it cannot drift in either direction unnoticed.
2. THE PROTOCOL. The plan phase validates every answer with ``check_answer``, the one validator an external
   executable's answer will also go through; a plugin failure refuses the lane and never falls back to marker order.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from gr2.python_cli import ecosystems_python as ep
from gr2.python_cli import lane_graph as lg
from gr2.python_cli import review_run as rr
from gr2.python_cli.lane_graph import LaneRefused, plan_lane

from tests.test_review_run_dependency_order import _pp, _pyproject_dirs

PY = {"python": ep.call}


def plugin_order(lane: Path, keys: list[str]) -> list[str]:
    plan = plan_lane(lane, keys, PY)
    members = {u.id: u.member for u in plan.units}
    return [members[u] for g in plan.groups for u in g.units]


# --- conformance: the same fixtures, both implementations -------------------------------------------------

ORDERABLE = {
    "no pyproject at all": ({"b": None, "a": None, "c": None}, ["b", "a", "c"]),
    "independent members keep marker order": ({"zeta": _pp("zeta-lib", ["requests"]), "alpha": _pp("alpha-lib")}, ["zeta", "alpha"]),
    "ties follow marker order": (
        {"a": _pp("a-lib", ["c-lib"]), "b": _pp("b-lib", ["c-lib"]), "c": _pp("c-lib")},
        ["a", "b", "c"],
    ),
    "a chain in the wrong marker order": (
        {"a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib", ["c-lib"]), "c": _pp("c-lib")},
        ["a", "b", "c"],
    ),
    "pep503 dotted": ({"a": _pp("a-lib", ["core.lib>=1.0"]), "b": _pp("Core_Lib")}, ["a", "b"]),
    "pep503 underscore": ({"a": _pp("a-lib", ["Core_Lib"]), "b": _pp("Core_Lib")}, ["a", "b"]),
    "pep503 extra and marker": ({"a": _pp("a-lib", ["CORE-LIB[extra]>=1; python_version >= '3.11'"]), "b": _pp("Core_Lib")}, ["a", "b"]),
    "pep503 direct reference": ({"a": _pp("a-lib", ["core-lib @ file:///somewhere"]), "b": _pp("Core_Lib")}, ["a", "b"]),
    "outside the lane and self": ({"a": _pp("a-lib", ["a-lib", "pytest>=7", "numpy"]), "b": _pp("b-lib")}, ["a", "b"]),
    "optional dependencies are not edges": ({"a": _pp("a-lib", optional={"dev": ["b-lib"]}), "b": _pp("b-lib")}, ["a", "b"]),
    "unreadable pyproject": ({"a": "this is [not toml", "b": _pp("b-lib", ["a-lib"])}, ["a", "b"]),
    "project is not a table": ({"a": "project = 5\n", "b": _pp("b")}, ["a", "b"]),
    "project is an array of tables": ({"a": "[[project]]\nname = 'a'\n", "b": _pp("b")}, ["a", "b"]),
    "dependencies is an int": ({"a": "[project]\nname = 'a'\ndependencies = 5\n", "b": _pp("b")}, ["a", "b"]),
    "dependencies is a string": ({"a": "[project]\nname = 'a'\ndependencies = 'b'\n", "b": _pp("b")}, ["a", "b"]),
    "dependencies with non-string entries": ({"a": "[project]\nname = 'a'\ndependencies = [5, {x = 1}]\n", "b": _pp("b")}, ["a", "b"]),
    "a non-string entry is not stringified": ({"a": "[project]\nname = 'a'\ndependencies = [5]\n", "b": _pp("5")}, ["a", "b"]),
    "no name but dependencies still orders after them": (
        {"a": "[project]\ndependencies = ['b-lib']\n", "b": _pp("b-lib")},
        ["a", "b"],
    ),
}


@pytest.mark.parametrize("label", list(ORDERABLE))
def test_the_plugin_path_orders_every_lane_dev_can_order_exactly_as_dev_does(tmp_path: Path, label: str) -> None:
    spec, keys = ORDERABLE[label]
    lane = _pyproject_dirs(tmp_path, spec)
    assert plugin_order(lane, keys) == rr._derive_member_order(lane, keys)


def test_the_orderable_fixtures_are_not_all_the_trivial_order(tmp_path: Path) -> None:
    """A conformance table where every answer is the marker order would pass an implementation that ignores
    edges. Count the fixtures whose dev answer is NOT the marker order."""
    moved = 0
    for label, (spec, keys) in ORDERABLE.items():
        lane = _pyproject_dirs(tmp_path / label.replace(" ", "_"), spec)
        moved += rr._derive_member_order(lane, keys) != keys
    assert moved >= 6


def test_two_members_claiming_one_distribution_are_refused_by_name_in_both(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("same-lib"), "b": _pp("Same_Lib"), "c": _pp("c-lib")})
    with pytest.raises(rr.ReviewRunRefused) as dev:
        rr._derive_member_order(lane, ["a", "b", "c"])
    with pytest.raises(LaneRefused) as new:
        plan_lane(lane, ["a", "b", "c"], PY)
    assert dev.value.code == new.value.code == "member_name_clash"
    for d in (dev.value.detail, new.value.detail):
        assert "'a'" in d and "'b'" in d and "--order" in d


def test_a_cycle_is_where_the_two_differ_dev_refuses_and_the_plan_phase_makes_one_group(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {
        "a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib", ["c-lib"]), "c": _pp("c-lib", ["a-lib"]), "d": _pp("d-lib"),
    })
    keys = ["a", "b", "c", "d"]
    with pytest.raises(rr.ReviewRunRefused) as dev:
        rr._derive_member_order(lane, keys)
    assert dev.value.code == "dependency_cycle"
    plan = plan_lane(lane, keys, PY)
    assert [list(g.units) for g in plan.groups] == [["python:a-lib", "python:b-lib", "python:c-lib"], ["python:d-lib"]]
    assert plan.methods == {1: {"method": "one-invocation", "note": plan.methods[1]["note"]}}
    assert "pip install -e" in plan.methods[1]["note"]
    loops, truncated = plan.loops[1]
    assert loops == (("python:a-lib", "python:b-lib", "python:c-lib"),) and not truncated
    assert not any("d-lib" in "".join(c) for c in loops)  # the member outside the loop is not named in it


def test_the_printed_plan_for_a_python_cycle_carries_the_plugins_answer(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"atl-pa": _pp("atl-pa", ["atl-pb"]), "atl-pb": _pp("atl-pb", ["atl-pa"])})
    text = lg.format_plan(plan_lane(lane, ["atl-pa", "atl-pb"], PY))
    assert "    plan_group: ok, method one-invocation" in text
    assert text.index("plan_group: ok") < text.index("loop: ")


def test_an_unclaimed_member_keeps_its_marker_position_among_claimed_ones(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"x": None, "a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib")})
    assert plugin_order(lane, ["x", "a", "b"]) == ["x", "b", "a"]


def test_the_edge_via_names_the_member_manifest_and_section(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"web": _pp("web-app", ["core-lib"]), "core": _pp("core-lib")})
    plan = plan_lane(lane, ["web", "core"], PY)
    assert [e.via for e in plan.edges] == ["web/pyproject.toml [project].dependencies"]


# --- the protocol ------------------------------------------------------------------------------------------


def stub(describe=None, plan_group=None, log=None):
    def call(name, request):
        if log is not None:
            log.append(name)
        return (describe if name == "describe" else plan_group)(request)

    return call


def units_answer(*units):
    return {"protocol": 1, "ok": True, "units": [dict(u) for u in units]}


def u(uid, key, *deps):
    return {"id": uid, "dir": f"/lane/{key}", "edges": [{"to": d, "kind": "install", "via": f"{key}/Cargo.toml"} for d in deps]}


@pytest.mark.parametrize("answer,code", [
    ("not an object", "plugin_failure"),
    ({"ok": True, "units": []}, "plugin_failure"),
    ({"protocol": 2, "ok": True, "units": []}, "plugin_protocol_mismatch"),
    ({"protocol": 1, "units": []}, "plugin_failure"),
    ({"protocol": 1, "ok": False}, "plugin_failure"),
    ({"protocol": 1, "ok": True}, "plugin_failure"),
    ({"protocol": 1, "ok": True, "units": [{"id": "x:y"}]}, "plugin_failure"),
    ({"protocol": 1, "ok": True, "units": [{"id": "x:y", "dir": "d", "edges": [{"to": "x:z", "kind": "dev", "via": "v"}]}]}, "plugin_failure"),
])
def test_an_invalid_answer_refuses_the_lane_naming_the_plugin_and_the_call(tmp_path: Path, answer, code) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib"), "b": _pp("b-lib")})
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b"], {"odd": stub(describe=lambda r: answer)})
    assert exc.value.code == code
    assert "'odd'" in exc.value.detail


def test_a_plugin_that_refuses_a_member_refuses_the_lane_and_does_not_fall_back_to_marker_order(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib", ["b-lib"]), "b": _pp("b-lib")})
    refusing = stub(describe=lambda r: {"protocol": 1, "ok": False, "reason": "cannot read manifest", "detail": "x"})
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b"], {"odd": refusing})
    assert exc.value.code == "plugin_failure" and "cannot read manifest" in exc.value.detail


def test_unknown_fields_are_ignored_in_both_directions(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": _pp("a-lib")})
    seen = {}

    def describe(request):
        seen.update(request)
        ans = ep.describe(request)
        ans["a_field_from_the_future"] = [1, 2]
        for unit in ans["units"]:
            unit["pool"] = "heavy"
        return ans

    plan = plan_lane(lane, ["a"], {"python": stub(describe=describe)})
    assert [g.units for g in plan.groups] == [("python:a-lib",)]
    assert seen["protocol"] == 1 and set(seen) == {"protocol", "key", "dir"}


def test_plan_group_is_asked_exactly_once_and_only_for_the_cyclic_group(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {
        "p": _pp("p-lib", ["q-lib"]), "q": _pp("q-lib", ["p-lib"]), "solo": _pp("solo-lib"),
    })
    log: list[str] = []
    plan_lane(lane, ["p", "q", "solo"], {"python": stub(describe=ep.describe, plan_group=ep.plan_group, log=log)})
    assert log.count("plan_group") == 1 and log.count("describe") == 3


def test_a_plugin_that_cannot_plan_a_group_refuses_the_lane_naming_the_group_and_the_loops(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"p": _pp("p-lib", ["q-lib"]), "q": _pp("q-lib", ["p-lib"])})
    no = stub(
        describe=ep.describe,
        plan_group=lambda r: {"protocol": 1, "ok": False, "reason": "cycle in normal dependencies", "detail": "cargo says so"},
    )
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["p", "q"], {"python": no})
    d = exc.value.detail
    assert exc.value.code == "group_unplannable"
    assert "python:p-lib" in d and "cycle in normal dependencies" in d and "cargo says so" in d
    assert "p/pyproject.toml [project].dependencies" in d  # the loop is printed hop by hop with its via


def test_a_group_that_mixes_ecosystems_is_refused_because_no_single_plugin_can_install_it(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": None, "b": None})
    one = stub(describe=lambda r: units_answer(u("one:a", "a", "two:b")) if r["key"] == "a" else units_answer())
    two = stub(describe=lambda r: units_answer(u("two:b", "b", "one:a")) if r["key"] == "b" else units_answer())
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b"], {"one": one, "two": two})
    assert exc.value.code == "group_unplannable" and "mixes ecosystems" in exc.value.detail


def test_the_same_name_in_two_ecosystems_is_not_a_clash(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": None, "b": None})
    one = stub(describe=lambda r: units_answer(u("npm:core", "a")) if r["key"] == "a" else units_answer())
    two = stub(describe=lambda r: units_answer(u("cargo:core", "b")) if r["key"] == "b" else units_answer())
    plan = plan_lane(lane, ["a", "b"], {"one": one, "two": two})
    assert [g.units for g in plan.groups] == [("npm:core",), ("cargo:core",)]


def test_one_member_that_two_plugins_both_claim_yields_two_units(tmp_path: Path) -> None:
    lane = _pyproject_dirs(tmp_path, {"a": None})
    one = stub(describe=lambda r: units_answer(u("cargo:a", "a")))
    two = stub(describe=lambda r: units_answer(u("npm:a", "a")))
    plan = plan_lane(lane, ["a"], {"one": one, "two": two})
    assert {x.id for x in plan.units} == {"cargo:a", "npm:a"} and {x.member for x in plan.units} == {"a"}
