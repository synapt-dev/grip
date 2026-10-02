"""Rows for the lane graph (step 1): units, edges, groups, order and the loop printer.

Every row drives the real ``build_plan`` and ``format_plan`` over hand-built inputs, and each is written
so that a plausible wrong implementation fails it: the keys in the first rows sort the WRONG way round,
so an implementation that ignores edges and falls back to marker order cannot pass by luck.
"""
from __future__ import annotations

import itertools

import pytest

from gr2.python_cli import lane_graph as lg
from gr2.python_cli.lane_graph import Edge, GraphError, Unit, build_plan, format_loop, format_plan


def U(name: str, member: str | None = None) -> Unit:
    return Unit(id=f"python:{name}", member=member or name, dir=f"/lane/{member or name}")


def E(src: str, dst: str, kind: str = "install") -> Edge:
    return Edge(src=f"python:{src}", dst=f"python:{dst}", kind=kind, via=f"{src}/pyproject.toml [project].dependencies")


def order(plan):
    return [list(g.units) for g in plan.groups]


def ids(*names):
    return [f"python:{n}" for n in names]


# --- order -------------------------------------------------------------------------------------------


def test_a_dependency_installs_before_its_dependent_even_when_the_keys_sort_the_other_way():
    # marker order is a-web, b-core; web needs core, so core must go first
    plan = build_plan([U("demo-web", "a-web"), U("demo-core", "b-core")], [E("demo-web", "demo-core")])
    assert order(plan) == [ids("demo-core"), ids("demo-web")]
    assert not plan.cyclic_groups


def test_a_lane_with_no_edges_keeps_marker_order_exactly():
    plan = build_plan([U("zeta"), U("alpha"), U("mid")], [])
    assert order(plan) == [ids("zeta"), ids("alpha"), ids("mid")]


def test_a_reversed_chain_comes_out_dependencies_first():
    # marker order d, c, b, a; a needs b needs c needs d: install order is d, c, b, a
    names = ["d", "c", "b", "a"]
    plan = build_plan([U(n) for n in names], [E("a", "b"), E("b", "c"), E("c", "d")])
    assert order(plan) == [ids(n) for n in names]
    # and the same chain with the marker order reversed must reverse the answer, not repeat it
    plan2 = build_plan([U(n) for n in reversed(names)], [E("a", "b"), E("b", "c"), E("c", "d")])
    assert order(plan2) == [ids(n) for n in names]


def test_ties_among_ready_groups_follow_marker_order():
    # x and y are independent; both are needed by z. marker order is y, x, z: y goes before x.
    plan = build_plan([U("y"), U("x"), U("z")], [E("z", "x"), E("z", "y")])
    assert order(plan) == [ids("y"), ids("x"), ids("z")]
    plan2 = build_plan([U("x"), U("y"), U("z")], [E("z", "x"), E("z", "y")])
    assert order(plan2) == [ids("x"), ids("y"), ids("z")]


def test_a_group_that_becomes_ready_later_still_goes_ahead_of_a_later_marker_one_already_waiting():
    # marker order p, q, r. p needs q; r is free. q goes first (only q and r are ready, q is earlier); then p
    # becomes ready and is EARLIER than r in marker order, so p goes before r: q, p, r (not q, r, p).
    plan = build_plan([U("p"), U("q"), U("r")], [E("p", "q")])
    assert order(plan) == [ids("q"), ids("p"), ids("r")]


def test_a_later_member_with_no_dependencies_does_not_jump_ahead_of_an_earlier_ready_one():
    # b needs c; a is free and earlier than c in marker order. a, then c (b waits), then b.
    plan = build_plan([U("a"), U("b"), U("c")], [E("b", "c")])
    assert order(plan) == [ids("a"), ids("c"), ids("b")]


def test_a_dependency_outside_the_lane_makes_no_edge_and_no_error():
    plan = build_plan([U("a"), U("b")], [E("a", "not-in-the-lane")])
    assert order(plan) == [ids("a"), ids("b")]
    assert plan.edges == ()


def test_a_self_dependency_orders_nothing_and_is_not_a_cycle():
    plan = build_plan([U("a")], [E("a", "a")])
    assert order(plan) == [ids("a")]
    assert not plan.cyclic_groups
    assert plan.edges == ()


def test_test_edges_never_order_installs_and_never_form_a_group():
    # a test edge each way is a loop that does not matter: two groups of one, in marker order
    plan = build_plan([U("a"), U("b")], [E("a", "b", "test"), E("b", "a", "test")])
    assert order(plan) == [ids("a"), ids("b")]
    assert not plan.cyclic_groups
    assert len(plan.edges) == 2  # they are kept: downstream selection uses them


def test_a_test_edge_does_not_reorder_when_the_install_edges_say_otherwise():
    plan = build_plan([U("a"), U("b")], [E("b", "a", "install"), E("a", "b", "test")])
    assert order(plan) == [ids("a"), ids("b")]


# --- groups ------------------------------------------------------------------------------------------


def test_two_units_that_need_each_other_are_one_group_of_two():
    plan = build_plan([U("atl-pa"), U("atl-pb")], [E("atl-pa", "atl-pb"), E("atl-pb", "atl-pa")])
    assert order(plan) == [ids("atl-pa", "atl-pb")]
    assert plan.groups[0].cyclic
    loops, truncated = plan.loops[1]
    assert not truncated
    assert loops == (tuple(ids("atl-pa", "atl-pb")),)


def test_a_group_is_ordered_among_the_rest_as_one_node():
    # core <- (p, q cycle) <- app; marker order puts app first
    plan = build_plan(
        [U("app"), U("p"), U("q"), U("core")],
        [E("app", "p"), E("p", "q"), E("q", "p"), E("q", "core")],
    )
    assert order(plan) == [ids("core"), ids("p", "q"), ids("app")]
    assert [g.cyclic for g in plan.groups] == [False, True, False]


def test_two_separate_cycles_are_two_groups_not_one():
    plan = build_plan(
        [U("a"), U("b"), U("c"), U("d")],
        [E("a", "b"), E("b", "a"), E("c", "d"), E("d", "c")],
    )
    assert order(plan) == [ids("a", "b"), ids("c", "d")]


def test_a_three_cycle_with_a_chord_is_one_group_and_lists_both_loops():
    plan = build_plan(
        [U("a"), U("b"), U("c")],
        [E("a", "b"), E("b", "c"), E("c", "a"), E("b", "a")],
    )
    assert order(plan) == [ids("a", "b", "c")]
    loops, _ = plan.loops[1]
    assert sorted(loops) == sorted([tuple(ids("a", "b", "c")), tuple(ids("a", "b"))])


def test_a_long_chain_does_not_hit_the_recursion_limit():
    n = 3000
    units = [U(f"n{i}") for i in range(n)]
    edges = [E(f"n{i}", f"n{i + 1}") for i in range(n - 1)]
    plan = build_plan(units, edges)
    assert len(plan.groups) == n
    assert order(plan)[0] == ids("n2999") and order(plan)[-1] == ids("n0")


# --- the loop printer and its cap --------------------------------------------------------------------


def complete(n):
    names = [f"u{i}" for i in range(n)]
    return [U(x) for x in names], [E(a, b) for a, b in itertools.permutations(names, 2)]


@pytest.mark.parametrize("n,count", [(2, 1), (3, 5), (4, 20), (5, 84), (6, 409)])
def test_the_loop_count_of_a_complete_group_matches_the_known_series(n, count):
    units, edges = complete(n)
    plan = build_plan(units, edges, loop_cap=10_000)
    loops, truncated = plan.loops[1]
    assert not truncated
    assert len(loops) == count
    assert len(set(loops)) == count  # each elementary loop once
    for cyc in loops:
        assert len(set(cyc)) == len(cyc)  # elementary: no unit twice


def test_every_listed_loop_is_a_real_loop_in_the_graph():
    units, edges = complete(4)
    plan = build_plan(units, edges)
    pairs = {(e.src, e.dst) for e in plan.edges}
    for cyc in plan.loops[1][0]:
        hops = list(cyc) + [cyc[0]]
        assert all((a, b) in pairs for a, b in zip(hops, hops[1:]))


def test_the_complete_graph_of_seven_prints_fifty_loops_says_more_and_lists_all_forty_two_edges():
    units, edges = complete(7)
    plan = build_plan(units, edges)
    loops, truncated = plan.loops[1]
    assert len(loops) == 50 and truncated
    text = format_plan(plan)
    assert text.count("    loop: ") == 50
    assert f"more than {lg.LOOP_CAP} loops" in text
    tail = text.split("more than 50 loops")[1]
    assert tail.count("    edge  ") == 42


def test_a_group_under_the_cap_does_not_claim_more_and_prints_no_edge_dump():
    units, edges = complete(3)
    text = format_plan(build_plan(units, edges))
    assert "more than" not in text
    assert text.count("    loop: ") == 5


def test_each_hop_of_a_loop_carries_its_via():
    plan = build_plan([U("demo-web"), U("demo-core")], [E("demo-web", "demo-core"), E("demo-core", "demo-web")])
    lines = format_loop(plan, plan.loops[1][0][0])
    assert len(lines) == 2
    assert all("pyproject.toml [project].dependencies" in ln for ln in lines)
    assert "python:demo-web -(install)-> python:demo-core" in lines[0]


def test_the_printed_plan_for_the_codelab_pair_is_exactly_this():
    plan = build_plan([U("demo-web", "a-web"), U("demo-core", "b-core")], [E("demo-web", "demo-core")])
    assert format_plan(plan) == "\n".join(
        [
            "plan: 2 units, 1 edge, 2 groups, no cycles",
            "  python:demo-web   member a-web  dir /lane/a-web",
            "  python:demo-core  member b-core  dir /lane/b-core",
            "  edge  python:demo-web -(install)-> python:demo-core   [demo-web/pyproject.toml [project].dependencies]",
            "order: group 1 [python:demo-core]  then  group 2 [python:demo-web]",
        ]
    )


def test_the_printed_plan_for_a_python_cycle_names_the_group_and_the_loop():
    plan = build_plan([U("atl-pa"), U("atl-pb")], [E("atl-pa", "atl-pb"), E("atl-pb", "atl-pa")])
    text = format_plan(plan)
    assert text.splitlines()[0] == "plan: 2 units, 2 edges, 1 group (1 cyclic)"
    assert "  group 1 [python:atl-pa, python:atl-pb]  cyclic" in text
    assert "    loop: python:atl-pa -(install)-> python:atl-pb" in text
    assert text.splitlines()[-1] == "order: group 1 [python:atl-pa, python:atl-pb]"


# --- refusals of malformed input ---------------------------------------------------------------------


def test_two_units_claiming_one_id_are_refused_by_name():
    with pytest.raises(GraphError, match="python:dup"):
        build_plan([U("dup", "one"), U("dup", "two")], [])


def test_an_unknown_edge_kind_is_refused_not_ignored():
    with pytest.raises(GraphError, match="unknown kind"):
        build_plan([U("a"), U("b")], [Edge("python:a", "python:b", "dev", "x")])


def test_the_same_input_gives_the_same_plan_every_time():
    units, edges = complete(5)
    assert format_plan(build_plan(units, edges)) == format_plan(build_plan(units, list(edges)))
