"""Which members of a lane are changed, downstream and upstream: a pure function over the plan.

Written by the author of `select_roles` (not by an independent reader). Each row names what must make it go red:
  * reverse reachability stops after one hop      -> the transitive row
  * test edges are not followed in reverse        -> the test-edge row
  * test edges are followed forward for upstream  -> the test-edge row, upstream half
  * the downstream member's own needs are skipped -> the second-order upstream row
  * a member in both sets is not downstream       -> the both-ways row
  * an unrelated member gets a role               -> the unrelated row
"""
from __future__ import annotations

from gr2.python_cli.lane_graph import INSTALL, TEST, Edge, Unit, build_plan, select_roles


def _plan(members: list[str], edges: list[tuple[str, str, str]]):
    units = [Unit(id=f"python:{m}", member=m, dir=f"/lane/{m}") for m in members]
    return build_plan(units, [Edge(src=f"python:{a}", dst=f"python:{b}", kind=k, via="test") for a, b, k in edges])


def test_downstream_is_transitive_and_upstream_covers_the_downstream_members_own_needs() -> None:
    """C changed. B needs C, D needs B (downstream, two hops). C needs U (upstream). B needs U2 (upstream through a
    DOWNSTREAM member: without it B cannot be installed). E touches nothing. Goes red if the closure stops after one
    hop, if the downstream members' needs are skipped, or if E gets a role."""
    plan = _plan(
        ["c", "b", "d", "u", "u2", "e"],
        [("b", "c", INSTALL), ("d", "b", INSTALL), ("c", "u", INSTALL), ("b", "u2", INSTALL)],
    )
    roles = select_roles(plan, {"c"})
    assert roles.changed == ("c",)
    assert roles.downstream == ("b", "d")
    assert roles.upstream == ("u", "u2")
    assert "e" not in roles.changed + roles.downstream + roles.upstream


def test_a_test_edge_makes_a_member_downstream_but_never_upstream() -> None:
    """T has a dev-dependency on the changed C: its tests exercise C, so it is downstream. C's own test edge to X
    installs nothing X is needed for, so X is NOT upstream. Goes red if test edges are ignored in reverse, or
    followed forward."""
    plan = _plan(["c", "t", "x"], [("t", "c", TEST), ("c", "x", TEST)])
    roles = select_roles(plan, {"c"})
    assert roles.downstream == ("t",) and roles.upstream == ()


def test_a_member_reached_both_ways_is_downstream_not_upstream() -> None:
    """B needs C (downstream) and C needs B's sibling S that needs B: B is reached forward from C and in reverse.
    A member that is tested is downstream. Goes red if the both-ways member is reported upstream."""
    plan = _plan(["c", "b", "s"], [("b", "c", INSTALL), ("c", "s", INSTALL), ("s", "b", INSTALL)])
    roles = select_roles(plan, {"c"})
    assert roles.downstream == ("b", "s") and roles.upstream == ()


def test_two_changed_members_and_roles_follow_marker_order() -> None:
    plan = _plan(["z", "a", "m"], [("m", "a", INSTALL), ("m", "z", INSTALL)])
    roles = select_roles(plan, {"a", "z"})
    assert roles.changed == ("z", "a") and roles.downstream == ("m",), "marker order, not alphabetical"


def test_a_lane_whose_members_are_all_changed_has_no_other_roles() -> None:
    plan = _plan(["a", "b"], [("a", "b", INSTALL)])
    roles = select_roles(plan, {"a", "b"})
    assert roles.upstream == () and roles.downstream == ()
