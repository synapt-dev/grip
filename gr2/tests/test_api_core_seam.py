"""The core/plugin seam inside gr2: the generic core imports nothing from gr2.

The design rule: do not extract the dumper to its own repo now; cut the seam
INSIDE gr2 first (no new repo, no new dependency), and make the boundary a TEST
rather than a convention.

A CONVENTION DRIFTS, so this walks the core's own import graph instead: parse
the module, collect every module it imports AT ANY DEPTH, and assert none of
them is gr2. Depth matters and is the reason this is a walk rather than a scan
of the top of the file -- in the single-file layout the app, the JSON_SHAPES
table and the LAYOUT table were all imported INSIDE function bodies, so a
module-level-only reading reports a clean seam for precisely the file the seam
exists to split.

THE CONTROL IS THE SECOND TEST, and it is what makes the first one able to
fail: the same walker, run against the plugin beside the core, must FIND gr2
imports. A walker returning "no gr2" for both files would report a clean seam
while proving nothing about the walker, and it would have no symptom -- the
instrument-that-cannot-fail class, where a working instrument and a blind one
print exactly the same thing.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
CORE = GR2 / "gr2" / "api_core.py"
PLUGIN = GR2 / "scripts" / "dump_api.py"

# What the core may not reach for. ``gr2`` covers ``from gr2.python_cli import
# grip_cli``; the bare names cover the same modules reached by another spelling
# inside the distribution (``import grip_cli``, ``from layout import LAYOUT``),
# which a package-prefix-only test would let through. ``python_cli`` is listed
# on its own because it is a top-level name in this tree.
FORBIDDEN_ROOTS = frozenset(
    {"gr2", "python_cli", "grip_cli", "layout", "exit_codes", "prototypes", "gr2_overlay"}
)


def _imported_roots(path: Path) -> set[str]:
    """The top-level root of every module imported anywhere in ``path``.

    Relative imports are reported as a run of dots rather than dropped: a
    ``from . import sibling`` inside the core would be a gr2 dependency wearing
    no name, and a walker that silently skipped it would be the blind half of
    the control below.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("." * node.level)
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _load(name: str, path: Path):
    """Load a module by path -- both live outside any installed package.

    The module is registered in ``sys.modules`` BEFORE it executes, because
    ``dataclasses`` resolves a string annotation by looking the defining module
    up there: without the registration, ``@dataclass`` on the core's ``Kind``
    raises ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    from inside the standard library. Measured -- the four failures that
    prompted this line were all that error, and the seam itself was already
    correct.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _core():
    """The core through the SAME import the plugin uses.

    Not a path load: loading the file twice under two names gives two module
    objects with identical-looking functions, and an identity assertion between
    them fails for a reason that has nothing to do with the seam. Importing it
    by its real name also asserts a packaging fact worth having -- that
    ``gr2/api_core.py`` is importable as ``gr2.api_core`` -- which a path load
    would not notice the loss of.
    """
    return importlib.import_module("gr2.api_core")


def _plugin():
    return _load("_gr2_dump_api_seam", PLUGIN)


def test_the_walker_finds_gr2_in_the_plugin() -> None:
    """CONTROL: the walker can fail. Run it where gr2 IS imported."""
    roots = _imported_roots(PLUGIN)
    print(f"control: {PLUGIN.name} roots = {sorted(roots)}")
    assert "gr2" in roots, (
        f"the seam walker found no gr2 import in {PLUGIN.name}, which imports "
        "gr2 throughout. The walker is blind, so its verdict on the core is "
        "worth nothing until this passes. Fix the walker first."
    )


def test_the_core_imports_nothing_from_gr2() -> None:
    assert CORE.exists(), f"{CORE} does not exist -- the seam has not been cut"
    roots = _imported_roots(CORE)
    print(f"subject: {CORE.name} roots = {sorted(roots)}")
    hit = sorted(roots & FORBIDDEN_ROOTS)
    assert not hit, (
        f"{CORE.name} imports {hit}. The core must stay gr2-agnostic so it can "
        "be extracted to its own repo after Show HN: it knows the line format, "
        "the Typer walker and the marker lookup, and the consumer registers the "
        "kinds and supplies the tables."
    )


def test_the_plugin_uses_the_core_rather_than_a_copy_of_it() -> None:
    """One implementation, not two. Identity, not equality of output."""
    core, plugin = _core(), _plugin()
    assert plugin._walk is core.walk, "the plugin kept its own walker"
    assert plugin._label is core.label, "the plugin kept its own label rule"
    assert plugin._line is core.line, "the plugin kept its own line format"
    assert plugin.COLUMN == core.COLUMN, "the column width has two homes"


def test_the_core_alone_renders_the_plugin_s_surface() -> None:
    """The registered kinds are enough: the core needs nothing else from gr2."""
    core, plugin = _core(), _plugin()
    through_core = core.render(plugin._registry(), plugin._markers())
    assert through_core.strip(), "the core rendered nothing; an empty dump must never read as correct"
    assert through_core == plugin.render(), (
        "the core and the plugin disagree on the same registry -- the seam is "
        "not a single implementation"
    )


def test_the_core_renders_a_registry_built_outside_gr2() -> None:
    """No gr2 in scope at all: a consumer registers kinds and gets the format."""
    core = _core()
    registry = core.Registry()
    registry.register("verb", lambda: [("verb", "demo run", False, None)], core.plain_marker("verb"))
    registry.register(
        "exit",
        lambda: [("exit", "demo 0 ok", False, None)],
        core.plain_marker("exit"),
    )
    out = core.render(registry, {("exit", "demo 0 ok"): "may-change"})
    lines = out.splitlines()
    assert lines == [
        f"{'exit':<6}{'demo 0 ok':<64}may-change",
        f"{'verb':<6}{'demo run':<64}stable",
    ], f"unexpected core format: {lines!r}"
    assert out.endswith("\n"), "the dump must be terminated by exactly one newline"


def test_an_unregistered_kind_is_refused() -> None:
    """A kind nobody registered has no marker rule; guessing 'stable' for it
    would publish a promise nobody made."""
    core = _core()
    registry = core.Registry()
    registry.register("verb", lambda: [("verb", "demo run", False, None)], core.plain_marker("verb"))
    try:
        registry.marker("exit", "demo 0 ok", {})
    except KeyError:
        return
    raise AssertionError("an unregistered kind resolved a marker instead of refusing")
