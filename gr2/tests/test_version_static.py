"""Witness: gr2's distribution version is OWNED by the Python package and is
decoupled from the Rust crate.

gr1 (the Rust crate) and gr2 (the Python distribution) version independently:
gr1 releases as crates.io/GitHub ``v1.x.y``; gr2 releases as PyPI ``gr2/v2.x.y``.
The version is a static literal in gr2/pyproject.toml — no in-tree build backend
deriving it from Cargo.toml, no generated VERSION file. These tests pin that the
decouple is real: the number is static, it is NOT equal to the crate version, and
the old Cargo-generator machinery is gone.

No third-party imports (packaging) — the gr2 test extra is pytest + ruff only, so
version comparison is done on the plain strings.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_GR2_ROOT = Path(__file__).resolve().parents[1]  # gr2/ (the packaging root)
_REPO_ROOT = _GR2_ROOT.parent
_PYPROJECT = tomllib.loads((_GR2_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

_EXPECTED = "2.0.0a1"


def _cargo_version() -> str:
    cargo = tomllib.loads((_REPO_ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    return cargo["package"]["version"]


def test_version_is_a_static_literal_not_dynamic() -> None:
    """The version is a static string under [project], not declared dynamic and not
    sourced from a VERSION file. A bump edits this literal; nothing regenerates it."""
    project = _PYPROJECT["project"]
    assert "version" in project, "static [project].version is required"
    assert isinstance(project["version"], str) and project["version"], project.get("version")
    assert "version" not in project.get("dynamic", []), "version must not be dynamic"
    assert "dynamic" not in _PYPROJECT.get("tool", {}).get("setuptools", {}), (
        "the [tool.setuptools.dynamic] version generator must be gone"
    )


def test_build_backend_is_stock_setuptools() -> None:
    """The in-tree Cargo-reading backend is replaced by stock setuptools, so there
    is no build-time step that could re-derive the version from the crate."""
    assert _PYPROJECT["build-system"]["build-backend"] == "setuptools.build_meta"
    assert "backend-path" not in _PYPROJECT["build-system"]


def test_gr2_version_is_decoupled_from_the_crate() -> None:
    """The load-bearing property: the gr2 distribution version is INDEPENDENT of the
    Rust crate's version. gr2 is on the 2.x line, gr1 on 1.x, and they move
    separately. If a future edit re-pinned gr2 to the crate this reddens."""
    gr2_v = _PYPROJECT["project"]["version"]
    crate_v = _cargo_version()
    assert gr2_v != crate_v, (gr2_v, crate_v)
    assert gr2_v.split(".", 1)[0] == "2", f"gr2 is the 2.x line, got {gr2_v}"
    assert crate_v.split(".", 1)[0] == "1", f"gr1 crate is the 1.x line, got {crate_v}"


def test_version_is_the_expected_prerelease() -> None:
    """gr2 opens at the 2.0.0a1 pre-release."""
    assert _PYPROJECT["project"]["version"] == _EXPECTED


def test_gr2_version_option_reads_metadata() -> None:
    """`gr2 --version` prints the distribution version from importlib.metadata and
    exits 0 — the runtime read the pyproject comment promises, and the same single
    source of truth as the wheel. Asserted against the metadata value (not a literal),
    so it tracks whatever is installed rather than pinning to a number here."""
    import importlib.metadata

    from typer.testing import CliRunner

    from python_cli.app import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == importlib.metadata.version("gitgrip")


def test_overlay_module_carries_no_version_literal() -> None:
    """gr2/overlay/__init__.py must not hardcode a version literal — a third number
    that silently drifts from the distribution version (it read 0.1.0 while the dist
    was 2.0.0a1). The version lives in the package metadata alone. Checked at the
    assignment level (a docstring may name the attribute) and at runtime."""
    import ast

    overlay_init = _GR2_ROOT / "gr2" / "overlay" / "__init__.py"
    tree = ast.parse(overlay_init.read_text(encoding="utf-8"))
    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "__version__" not in assigned, "no __version__ assignment in the overlay module"

    import gr2.overlay

    assert not hasattr(gr2.overlay, "__version__"), "the overlay module exposes no __version__"


def test_cargo_version_generator_machinery_is_gone() -> None:
    """The in-tree backend, its helper, the generated VERSION file, and the sdist
    MANIFEST that carried it are all removed; only their absence keeps the version
    from being re-derived from the crate."""
    assert not (_GR2_ROOT / "_build").exists(), "gr2/_build (the Cargo generator) must be gone"
    assert not (_GR2_ROOT / "VERSION").exists(), "the generated VERSION file must be gone"
    assert not (_GR2_ROOT / "MANIFEST.in").exists(), "MANIFEST.in only carried VERSION/_build"
