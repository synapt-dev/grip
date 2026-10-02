"""The lane's dependency graph: units, edges, groups, and the plan built from them.

A *unit* is one (member, ecosystem) pair named ``<ecosystem>:<name>``. An *edge*
says one unit needs another, and is either ``install`` (needed to install, build
or run; these and only these order installs) or ``test`` (needed only to run the
unit's own tests; these never order installs). Every edge carries ``via``, the
manifest file and section it came from, so a diagnostic can point at a line.

``build_plan`` takes the lane's units in MARKER ORDER and the edges the plugins
reported, and answers three things before anything installs: which units form a
group (a strongly connected component of the ``install`` edges; a unit with no
cycle is a group of one), in what order the groups go (topological and STABLE:
among ready groups the one whose first member is earliest in marker order goes
first, so a lane with no edges keeps marker order exactly), and, for a cyclic
group, every elementary loop hop by hop (capped, then the complete edge list).

This module holds the pure part: no plugin is run and nothing is installed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator

PROTOCOL = 1  # the plugin protocol's major version; every request and every answer carries it

INSTALL = "install"
TEST = "test"
EDGE_KINDS = (INSTALL, TEST)

# How many elementary loops a refused group prints before the complete edge list
# takes over. A default, not a promise: a group of n mutually dependent units has
# 1, 5, 20, 84, 409, 2365, 16064 loops for n = 2..8, so every loop cannot be listed.
LOOP_CAP = 50


@dataclass(frozen=True)
class Unit:
    id: str  # "<ecosystem>:<name>", the name normalised by the plugin
    member: str  # the lane member (repo) this unit belongs to
    dir: str  # where the unit's manifest lives


@dataclass(frozen=True)
class Edge:
    src: str  # the unit that needs ...
    dst: str  # ... this one
    kind: str  # "install" or "test"
    via: str  # "<manifest file> [<section>]", for diagnostics


@dataclass(frozen=True)
class Group:
    index: int  # 1-based position in the order
    units: tuple[str, ...]  # unit ids, in marker order
    cyclic: bool


@dataclass(frozen=True)
class Plan:
    units: tuple[Unit, ...]  # marker order
    edges: tuple[Edge, ...]  # resolved: both ends in the lane, no self edge
    groups: tuple[Group, ...]  # in install order
    loops: dict = field(default_factory=dict)  # group index -> (tuple of loops, truncated)
    methods: dict = field(default_factory=dict)  # cyclic group index -> the plugin's plan_group answer

    @property
    def cyclic_groups(self) -> tuple[Group, ...]:
        return tuple(g for g in self.groups if g.cyclic)


class LaneRefused(Exception):
    """The lane cannot be planned, and the plan phase says so before anything installs. ``code`` is one of
    ``plugin_failure``, ``plugin_protocol_mismatch``, ``member_name_clash``, ``group_unplannable``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class GraphError(ValueError):
    """The input is not a graph this module can plan (a duplicate unit id, an unknown edge kind)."""


def build_plan(units: list[Unit], edges: list[Edge], loop_cap: int = LOOP_CAP) -> Plan:
    ids = [u.id for u in units]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise GraphError(f"two units claim the same id: {', '.join(dup)}")
    pos = {u.id: i for i, u in enumerate(units)}
    resolved: list[Edge] = []
    for e in edges:
        if e.kind not in EDGE_KINDS:
            raise GraphError(f"edge {e.src} -> {e.dst} has unknown kind {e.kind!r} (expected one of {EDGE_KINDS})")
        # An id nobody in the lane claims is external and ignored; a self edge orders nothing.
        if e.src in pos and e.dst in pos and e.src != e.dst:
            resolved.append(e)
    # De-duplicate exact repeats; keep first-seen order so output is deterministic.
    seen: set[Edge] = set()
    resolved = [e for e in resolved if not (e in seen or seen.add(e))]

    succ: dict[str, list[str]] = {i: [] for i in ids}  # install edges only: src needs dst
    for e in resolved:
        if e.kind == INSTALL and e.dst not in succ[e.src]:
            succ[e.src].append(e.dst)
    for i in ids:
        succ[i].sort(key=pos.__getitem__)

    comps = _tarjan(ids, succ)
    comp_of = {u: ci for ci, comp in enumerate(comps) for u in comp}
    members_sorted = [tuple(sorted(c, key=pos.__getitem__)) for c in comps]
    first = [pos[m[0]] for m in members_sorted]

    # Condensation: group A must come after group B when a unit of A needs a unit of B.
    needs: list[set[int]] = [set() for _ in comps]
    needed_by: list[set[int]] = [set() for _ in comps]
    for e in resolved:
        if e.kind != INSTALL:
            continue
        a, b = comp_of[e.src], comp_of[e.dst]
        if a != b:
            needs[a].add(b)
            needed_by[b].add(a)

    order: list[int] = []
    remaining = {ci: len(needs[ci]) for ci in range(len(comps))}
    ready = sorted((ci for ci, n in remaining.items() if n == 0), key=first.__getitem__)
    while ready:
        ci = ready.pop(0)  # earliest first-member in marker order
        order.append(ci)
        for later in needed_by[ci]:
            remaining[later] -= 1
            if remaining[later] == 0:
                ready.append(later)
        ready.sort(key=first.__getitem__)
    if len(order) != len(comps):  # cannot happen over a condensation; a loud failure beats a silent short plan
        raise GraphError("internal error: the condensation of the install graph has a cycle")

    groups: list[Group] = []
    loops: dict = {}
    for n, ci in enumerate(order, start=1):
        members = members_sorted[ci]
        cyclic = len(members) > 1  # self edges were dropped, so a singleton is never cyclic
        groups.append(Group(index=n, units=members, cyclic=cyclic))
        if cyclic:
            found = list(_elementary_cycles(members, succ, pos, loop_cap + 1))
            loops[n] = (tuple(tuple(c) for c in found[:loop_cap]), len(found) > loop_cap)
    return Plan(units=tuple(units), edges=tuple(resolved), groups=tuple(groups), loops=loops)


def _tarjan(ids: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Strongly connected components, iteratively (a deep lane must not hit the recursion limit)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = 0
    for root in ids:
        if root in index:
            continue
        work: list[tuple[str, Iterator[str]]] = [(root, iter(succ[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(succ[w])))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
    return comps


def _elementary_cycles(
    members: tuple[str, ...], succ: dict[str, list[str]], pos: dict[str, int], limit: int
) -> Iterator[list[str]]:
    """Every elementary cycle of one strongly connected group, at most ``limit`` of them (Johnson's algorithm).

    A cycle is yielded once, starting at its member that is earliest in marker order, so the output is
    deterministic. Johnson's blocking keeps the work proportional to the cycles found, not to the dead
    ends a plain depth-first walk would explore before reaching the cap.
    """
    inside = set(members)
    emitted = 0
    for si, start in enumerate(members):
        allowed = set(members[si:])
        blocked: set[str] = set()
        blocked_by: dict[str, set[str]] = {m: set() for m in allowed}
        path: list[str] = []

        def unblock(u: str) -> None:
            todo = [u]
            while todo:
                x = todo.pop()
                if x in blocked:
                    blocked.discard(x)
                    todo.extend(blocked_by[x])
                    blocked_by[x].clear()

        def circuit(v: str) -> Iterator[tuple[bool, list[str] | None]]:
            found_here = False
            path.append(v)
            blocked.add(v)
            for w in succ[v]:
                if w not in allowed or w not in inside:
                    continue
                if w == start:
                    found_here = True
                    yield True, list(path)
                elif w not in blocked:
                    sub_found = False
                    for flag, cyc in circuit(w):
                        if cyc is not None:
                            yield flag, cyc
                        sub_found = sub_found or flag
                    found_here = found_here or sub_found
            if found_here:
                unblock(v)
            else:
                for w in succ[v]:
                    if w in allowed and w in inside:
                        blocked_by[w].add(v)
            path.pop()
            yield found_here, None

        for _flag, cyc in circuit(start):
            if cyc is not None:
                yield cyc
                emitted += 1
                if emitted >= limit:
                    return


def _hop(plan: Plan, src: str, dst: str) -> str:
    for e in plan.edges:
        if e.kind == INSTALL and e.src == src and e.dst == dst:
            return f"{src} -({e.kind})-> {dst}   [{e.via}]"
    return f"{src} -(install)-> {dst}"


def format_loop(plan: Plan, cycle: tuple[str, ...]) -> list[str]:
    """One loop, hop by hop, each hop with its ``via``."""
    hops = list(cycle) + [cycle[0]]
    return [_hop(plan, a, b) for a, b in zip(hops, hops[1:])]


def format_plan(plan: Plan) -> str:
    """The plan as the operator reads it, before anything installs."""
    n_units, n_edges = len(plan.units), len(plan.edges)
    n_cyc = len(plan.cyclic_groups)
    head = f"plan: {n_units} unit{'s' if n_units != 1 else ''}, {n_edges} edge{'s' if n_edges != 1 else ''}, "
    head += f"{len(plan.groups)} group{'s' if len(plan.groups) != 1 else ''}"
    head += f" ({n_cyc} cyclic)" if n_cyc else ", no cycles"
    out = [head]
    width = max((len(u.id) for u in plan.units), default=0)
    for u in plan.units:
        out.append(f"  {u.id:<{width}}  member {u.member}  dir {u.dir}")
    for e in plan.edges:
        out.append(f"  edge  {e.src} -({e.kind})-> {e.dst}   [{e.via}]")
    for g in plan.groups:
        if not g.cyclic:
            continue
        out.append(f"  group {g.index} [{', '.join(g.units)}]  cyclic")
        answer = plan.methods.get(g.index)
        if answer:
            out.append(f"    plan_group: ok, method {answer['method']}")
            if answer.get("note"):
                out.append(f"      {answer['note']}")
        found, truncated = plan.loops[g.index]
        for cycle in found:
            out.append("    loop: " + "  then  ".join(format_loop(plan, cycle)))
        if truncated:
            out.append(f"    more than {LOOP_CAP} loops; the group's complete edge list follows")
            members = set(g.units)
            for e in plan.edges:
                if e.kind == INSTALL and e.src in members and e.dst in members:
                    out.append(f"    edge  {e.src} -({e.kind})-> {e.dst}   [{e.via}]")
    out.append("order: " + "  then  ".join(f"group {g.index} [{', '.join(g.units)}]" for g in plan.groups))
    return "\n".join(out)


# --- the plugin protocol, as the plan phase uses it ------------------------------------------------------
# A plugin is a callable ``plugin(call, request) -> answer`` where call is "describe" or "plan_group" and both
# sides are plain JSON-shaped dicts carrying ``"protocol": 1``. The built-in Python plugin is such a callable
# in process; an external ``grip-ecosystem-<name>`` executable is the same callable behind a subprocess.
# ``check_answer`` is the ONE validator for both, so the built-in cannot become a special case.


def check_answer(plugin: str, call: str, answer: object) -> dict:
    def fail(why: str) -> LaneRefused:
        return LaneRefused("plugin_failure", f"plugin {plugin!r} answered {call} with {why}")

    if not isinstance(answer, dict):
        raise fail("something that is not a JSON object")
    version = answer.get("protocol")
    if version is None:
        raise fail('no "protocol" field')
    if version != PROTOCOL:
        raise LaneRefused(
            "plugin_protocol_mismatch",
            f"plugin {plugin!r} speaks protocol {version!r} and this gr2 speaks {PROTOCOL}",
        )
    ok = answer.get("ok")
    if not isinstance(ok, bool):
        raise fail('no boolean "ok"')
    if not ok:
        if not isinstance(answer.get("reason"), str) or not answer["reason"]:
            raise fail('"ok": false and no "reason"')
        return answer
    if call == "describe":
        units = answer.get("units")
        if not isinstance(units, list):
            raise fail('no "units" list')
        for u in units:
            if not (isinstance(u, dict) and isinstance(u.get("id"), str) and u["id"] and isinstance(u.get("dir"), str)):
                raise fail('a unit without a string "id" and "dir"')
            if not u["id"].startswith(f"{plugin}:"):
                raise fail(f'a unit {u["id"]!r} whose id does not start with "{plugin}:" (a unit id names its ecosystem)')
            if not isinstance(u.get("edges", []), list):
                raise fail(f'unit {u["id"]!r} with "edges" that is not a list')
            for e in u.get("edges", []):
                if not (isinstance(e, dict) and isinstance(e.get("to"), str) and e.get("kind") in EDGE_KINDS and isinstance(e.get("via"), str)):
                    raise fail(f'unit {u["id"]!r} with an edge lacking a string "to" and "via" and a kind of {EDGE_KINDS}')
    elif call == "plan_group":
        if not isinstance(answer.get("method"), str) or not answer["method"]:
            raise fail('"ok": true and no "method"')
    return answer


def plan_lane(
    lane_dir: Path, keys: list[str], plugins: dict[str, Callable[[str, dict], dict]], loop_cap: int = LOOP_CAP
) -> Plan:
    """Plan a lane: describe every member (in marker order), build the graph, group, order, and ask the plugin
    that owns each cyclic group whether it can install that group. Raises ``LaneRefused`` BEFORE anything is
    installed. A plugin failure refuses the lane; it never falls back to marker order. Marker order stands only
    for a member NO plugin claims (the lane keeps a stand-in unit ``member:<key>`` for it, so it keeps its place)."""
    units: list[Unit] = []
    edges: list[Edge] = []
    owner: dict[str, str] = {}
    unit_plugin: dict[str, str] = {}
    for key in keys:
        mdir = str(lane_dir / key)
        claimed = False
        for name, call in plugins.items():
            answer = check_answer(name, "describe", call("describe", {"protocol": PROTOCOL, "key": key, "dir": mdir}))
            if not answer["ok"]:
                raise LaneRefused(
                    "plugin_failure",
                    f"plugin {name!r} refused member {key!r}: {answer['reason']}"
                    + (f" ({answer['detail']})" if answer.get("detail") else ""),
                )
            for u in answer["units"]:
                if u["id"] in owner:
                    raise LaneRefused(
                        "member_name_clash",
                        f"members {owner[u['id']]!r} and {key!r} both claim the unit {u['id']!r}, so the install order "
                        "cannot be derived; pass --order to name it",
                    )
                owner[u["id"]] = key
                unit_plugin[u["id"]] = name
                units.append(Unit(id=u["id"], member=key, dir=u["dir"]))
                edges.extend(Edge(src=u["id"], dst=e["to"], kind=e["kind"], via=e["via"]) for e in u.get("edges", []))
                claimed = True
        if not claimed:
            units.append(Unit(id=f"member:{key}", member=key, dir=mdir))
    plan = build_plan(units, edges, loop_cap=loop_cap)
    methods: dict = {}
    for g in plan.cyclic_groups:
        owners = {unit_plugin.get(u) for u in g.units}
        loops = "; ".join(" ".join(format_loop(plan, c)) for c in plan.loops[g.index][0][:3])
        if None in owners or len(owners) != 1:
            raise LaneRefused(
                "group_unplannable",
                f"group {g.index} [{', '.join(g.units)}] mixes ecosystems or unclaimed members, so no single plugin can "
                f"install it; the loop: {loops}",
            )
        name = owners.pop()
        by_id = {u.id: u for u in plan.units}
        group_edges = [
            {"from": e.src, "to": e.dst, "kind": e.kind, "via": e.via}
            for e in plan.edges
            if e.kind == INSTALL and e.src in g.units and e.dst in g.units
        ]
        answer = check_answer(
            name,
            "plan_group",
            plugins[name](
                "plan_group",
                {"protocol": PROTOCOL, "units": [{"id": u, "dir": by_id[u].dir} for u in g.units], "edges": group_edges},
            ),
        )
        if not answer["ok"]:
            raise LaneRefused(
                "group_unplannable",
                f"plugin {name!r} cannot plan group {g.index} [{', '.join(g.units)}]: {answer['reason']}"
                + (f" ({answer['detail']})" if answer.get("detail") else "")
                + f"; the loops: {loops}",
            )
        methods[g.index] = {"method": answer["method"], "note": answer.get("note", "")}
    return replace(plan, methods=methods)
