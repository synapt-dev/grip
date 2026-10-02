"""The built-in Python ecosystem plugin: ``describe`` and ``plan_group``, in process, in the protocol's own shapes.

This is dev's install-order derivation (``review_run._derive_member_order``) re-expressed as a plugin. It does not
copy the reading of a ``pyproject.toml``: it CALLS ``review_run._member_distribution``, so there is one reader.
It speaks exactly the JSON an external ``grip-ecosystem-<name>`` executable speaks (a request dict in, an answer
dict out, both carrying ``"protocol": 1``) and goes through the same validator, so the built-in is not special.

Units are named ``python:<PEP 503 distribution name>``. A member with a ``[project]`` that declares dependencies
but no name still has edges (dev ordered it after what it needs), so it answers a unit named ``python:?<key>``;
``?`` cannot occur in a normalised name, so nothing can depend on it by accident. A member with no readable
``[project]`` answers no unit, which means "not mine", and keeps its marker-order place.
"""

from __future__ import annotations

from pathlib import Path

from .lane_graph import PROTOCOL

NAME = "python"


def call(name: str, request: dict) -> dict:
    """The plugin entry point the plan phase calls: ``call("describe" | "plan_group", request) -> answer``."""
    if name == "describe":
        return describe(request)
    if name == "plan_group":
        return plan_group(request)
    return {"protocol": PROTOCOL, "ok": False, "reason": f"unknown call {name!r}"}


def describe(request: dict) -> dict:
    from .review_run import _member_distribution  # lazy: review_run will import the lane graph in a later step

    key, mdir = request["key"], request["dir"]
    dist, required = _member_distribution(Path(mdir))
    if dist is None and not required:
        return {"protocol": PROTOCOL, "ok": True, "units": []}
    uid = f"python:{dist}" if dist is not None else f"python:?{key}"
    via = f"{key}/pyproject.toml [project].dependencies"
    edges = [{"to": f"python:{r}", "kind": "install", "via": via} for r in required]
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "units": [{"id": uid, "dir": mdir, "edges": edges, "executes_member_code": False}],
    }


def plan_group(request: dict) -> dict:
    """Python can install any group in one pip invocation: pip resolves the in-group requirement cycle and the
    members' external requirements together (measured in the design note, section 2)."""
    dirs = " ".join(f"-e {u['dir']}" for u in request["units"])
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "method": "one-invocation",
        "note": f"one `pip install {dirs}` covers the group",
    }
