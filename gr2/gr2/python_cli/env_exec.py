"""Executors for the `venv` and `editable_install` operations (S4-D).

The last two materialization handlers. With these, the A -> B -> C -> D path is
complete: contract, clones, projections, environments.

Contract: MaterializationPlan v1 environment invariant 6 and
          acceptance fruit 10/11/12.

Working, not exhaustively hardened (Layne's 2026-07-27 prototype doctrine).

INVARIANT 6 IS THE INTERESTING ONE, and it is written as a warning about
proxies: "Treat an existing venv as valid only when pyvenv.cfg is a regular file
AND the platform interpreter is present, executable, and succeeds under an
isolated bounded probe. The probe must report a resolved sys.prefix equal to the
declared venv path and a distinct sys.base_prefix. FILE PRESENCE ALONE IS NOT
VENV EVIDENCE."

That last sentence is the spec naming the mask itself. `pyvenv.cfg exists` is
the cheap check that LOOKS like venv validation, and it stays true for a venv
whose interpreter was deleted, whose base Python was upgraded out from under it,
or which was copied to a new path (the config records the OLD prefix, so every
relative path inside it is wrong). Only running the interpreter can tell.

The spec's TWO PREFIX ASSERTIONS ARE KEPT BUT CANNOT FIRE on CPython 3.11+, and
that was established by running it rather than by reading the docs:

  - pyvenv.cfg next to bin/python is LITERALLY what makes a venv, so base_prefix
    always differs from prefix -- even when the file contains garbage. A "bare
    interpreter plus a pyvenv.cfg" is a real venv by Python's own rules.
  - sys.prefix is derived from the directory you INVOKED THROUGH, not from
    anything recorded inside, so it equals the declared path even when
    bin/python symlinks to a different venv's interpreter -- and a COPIED venv
    is self-consistent at its new location.

They stay because the spec mandates them and a future Python may behave
differently; they are defence in depth, not live guards, and saying so beats
leaving them looking like protection. test_the_prefix_assertions_cannot_fire_on_
current_cpython pins the empirical finding and FAILS if CPython ever changes,
which is the signal that they have become reachable and deserve real probes.

What the probe genuinely catches today is the reachable set: an interpreter that
is missing, not executable, failing, or hanging, and a missing pyvenv.cfg. Those
are the real content of "file presence alone is not venv evidence".

FRUIT 11's `python -I` IS LOAD-BEARING, not decoration. Without isolation, an
import can be satisfied by the current working directory or an inherited
PYTHONPATH, so a passing import proves nothing about what the venv installed.
`-I` ignores both, which is what makes "resolves inside that unit clone" a real
claim.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

from .spec_apply import (
    MaterializationPlanError,
    ValidatedPlan,
    _read_canonical_workspace_spec_bytes,
    canonicalize_workspace_path,
)

# A venv probe that hangs would hang the timed path, and section 13 gives the
# whole materialization 180s. Bounded, per invariant 6's "bounded probe".
_PROBE_TIMEOUT_SECONDS = 30
_INSTALL_TIMEOUT_SECONDS = 300


class EnvExecutionError(MaterializationPlanError):
    """A validated plan's venv or editable_install operation could not run."""


@dataclasses.dataclass(frozen=True)
class _VenvBinding:
    dest_path: str
    engine: str
    python: str


@dataclasses.dataclass(frozen=True)
class _EditableBinding:
    venv_path: str
    source_path: str
    extras: tuple[str, ...]


def venv_interpreter(venv_root: Path) -> Path:
    """Platform interpreter inside a venv. Windows puts it in Scripts/."""
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _require_workspace_binding(validated: ValidatedPlan, workspace_root: Path) -> None:
    """Re-prove at USE what validation proved earlier -- see S4-B/S4-C. Local
    rather than imported so the error names THIS layer; the shared executor
    scaffolding wants extracting into one module, deferred."""
    try:
        spec_bytes = _read_canonical_workspace_spec_bytes(workspace_root)
    except MaterializationPlanError as exc:
        raise EnvExecutionError(
            f"cannot execute against {workspace_root}: its canonical WorkspaceSpec is "
            f"unreadable ({exc})"
        ) from exc
    if hashlib.sha256(spec_bytes).hexdigest() != validated.workspace_spec_sha256:
        raise EnvExecutionError(
            f"plan is bound to WorkspaceSpec {validated.workspace_spec_sha256} but "
            f"{workspace_root} has a different one -- executing a plan against a "
            "workspace it was not validated for would resolve every path elsewhere"
        )


def _operation_at(
    validated: ValidatedPlan, index: int, expected_kind: str
) -> dict[str, object]:
    operations = validated.plan["operations"]
    if not 0 <= index < len(operations):
        raise EnvExecutionError(
            f"operation index {index} is out of range for a plan with "
            f"{len(operations)} operation(s)"
        )
    op = operations[index]
    kind = op.get("kind")
    if kind != expected_kind:
        raise EnvExecutionError(
            f"operations[{index}] is kind {kind!r}, not {expected_kind!r} -- evidence "
            "must correspond to its operation, in order"
        )
    return op


def probe_venv(venv_root: Path) -> dict[str, str]:
    """Run the venv's own interpreter and ask it where it thinks it lives.

    Invariant 6's bounded probe. `-I` isolates: no site-packages from the user
    base, no inherited PYTHONPATH, no cwd on sys.path -- so the answer describes
    the venv rather than the environment this process happens to be running in.

    Raises rather than returning a sentinel: every caller treats a probe failure
    as 'this is not a usable venv', and a sentinel invites a caller that forgets
    to check it."""
    interpreter = venv_interpreter(venv_root)
    if not interpreter.exists():
        raise EnvExecutionError(
            f"venv at {venv_root} has no interpreter at {interpreter} -- pyvenv.cfg "
            "presence alone is not venv evidence (invariant 6)"
        )
    if not os.access(interpreter, os.X_OK):
        raise EnvExecutionError(
            f"venv interpreter {interpreter} is not executable"
        )
    script = (
        "import json,sys;"
        "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'version':'%d.%d'%sys.version_info[:2]}))"
    )
    try:
        proc = subprocess.run(
            [str(interpreter), "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EnvExecutionError(
            f"venv interpreter {interpreter} did not answer within "
            f"{_PROBE_TIMEOUT_SECONDS}s -- a hung probe would hang the timed path"
        ) from exc
    if proc.returncode != 0:
        raise EnvExecutionError(
            f"venv interpreter {interpreter} failed to run: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EnvExecutionError(
            f"venv probe returned unparseable output: {proc.stdout!r}"
        ) from exc


def verify_venv(venv_root: Path) -> dict[str, str]:
    """Invariant 6 in full. Returns the probe result for receipt evidence."""
    cfg = venv_root / "pyvenv.cfg"
    if not cfg.is_file():
        raise EnvExecutionError(
            f"venv at {venv_root} has no pyvenv.cfg regular file"
        )
    probe = probe_venv(venv_root)

    # Orthogonal to the prefix check below: a bare system interpreter satisfies
    # 'prefix == declared path' only by accident, but ALWAYS has
    # base_prefix == prefix. This is what distinguishes a venv from a Python.
    if probe["base_prefix"] == probe["prefix"]:
        raise EnvExecutionError(
            f"{venv_root} is not a virtual environment: its interpreter reports "
            f"base_prefix == prefix ({probe['prefix']})"
        )

    # And this is what distinguishes THIS venv from one that was copied or moved
    # here -- pyvenv.cfg would still parse, the interpreter would still run, and
    # every relative path inside it would point at the old location.
    if Path(probe["prefix"]).resolve() != venv_root.resolve():
        raise EnvExecutionError(
            f"venv at {venv_root} reports sys.prefix {probe['prefix']} -- it was "
            "created for a different path and its internal paths are stale"
        )
    return probe


def execute_venv_operation(
    validated: ValidatedPlan, index: int, *, workspace_root: Path
) -> dict[str, object]:
    """Create (or accept) the unit venv at `dest_path` using `uv`."""
    workspace_root = Path(os.fspath(workspace_root))
    validated.verify(require_provenance=True)
    _require_workspace_binding(validated, workspace_root)

    op = _operation_at(validated, index, "venv")
    binding = _VenvBinding(
        dest_path=str(op["dest_path"]),
        engine=str(op["engine"]),
        python=str(op["python"]),
    )
    dest = canonicalize_workspace_path(
        workspace_root, binding.dest_path, field_name=f"operations[{index}].dest_path"
    )

    reused = False
    if dest.exists():
        # Validate every time, not only on creation: invariant 5's rule for
        # clones applies here too -- path absence must not be the condition that
        # makes validation reachable.
        probe = verify_venv(dest)
        reused = True
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["uv", "venv", "--python", binding.python, str(dest)],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise EnvExecutionError(
                f"uv venv failed for {dest} with python {binding.python!r}:\n"
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        probe = verify_venv(dest)

    return {
        "kind": "venv",
        "dest_path": binding.dest_path,
        "engine": binding.engine,
        "python": binding.python,
        "interpreter_version": probe["version"],
        "reused": reused,
    }


def _distribution_name(source: Path) -> str | None:
    """Best-effort project name from pyproject.toml, for locating PEP 610
    metadata. Prototype-scoped: a project whose name is set dynamically is not
    handled, and the caller falls back to scanning."""
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
        return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:
        return None
    name = data.get("project", {}).get("name")
    return str(name) if name else None


def read_direct_url(venv_root: Path, source: Path) -> dict[str, object]:
    """PEP 610 metadata for the distribution installed from `source`.

    Fruit 12 wants editable mode AND the correct source recorded. Located by
    scanning `.dist-info/direct_url.json` and matching the recorded url back to
    the source, rather than trusting the first one found -- a unit installs
    several editables and 'some direct_url.json exists' proves nothing about
    the one being asked about."""
    probe = probe_venv(venv_root)
    version = probe["version"]
    site_packages = venv_root / "lib" / f"python{version}" / "site-packages"
    if os.name == "nt":
        site_packages = venv_root / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise EnvExecutionError(f"venv at {venv_root} has no site-packages directory")

    wanted = source.resolve()
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        direct_url = dist_info / "direct_url.json"
        if not direct_url.is_file():
            continue
        try:
            data = json.loads(direct_url.read_text())
        except json.JSONDecodeError:
            continue
        url = str(data.get("url", ""))
        if not url.startswith("file://"):
            continue
        # Percent-decoded: a source path containing a space arrives as %20, so a
        # raw string comparison silently fails to match the very install it is
        # looking for.
        from urllib.parse import unquote, urlparse

        recorded = Path(unquote(urlparse(url).path)).resolve()
        if recorded == wanted:
            return data
    raise EnvExecutionError(
        f"no PEP 610 direct_url.json in {venv_root} records an install from {source} "
        "-- the editable install did not record its source (fruit 12)"
    )


def import_origin(venv_root: Path, module: str) -> str:
    """Where the venv's OWN interpreter resolves `module` from, in isolation.

    `-I` is the whole point (fruit 11): without it an inherited PYTHONPATH or
    the working directory can satisfy the import, and a passing import would say
    nothing about what this venv installed."""
    interpreter = venv_interpreter(venv_root)
    proc = subprocess.run(
        [
            str(interpreter), "-I", "-c",
            f"import {module},os;print(os.path.realpath({module}.__file__))",
        ],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise EnvExecutionError(
            f"module {module!r} does not import under `python -I` in {venv_root}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def execute_editable_install_operation(
    validated: ValidatedPlan, index: int, *, workspace_root: Path
) -> dict[str, object]:
    """Install `source_path` (with extras) as editable into `venv_path`."""
    workspace_root = Path(os.fspath(workspace_root))
    validated.verify(require_provenance=True)
    _require_workspace_binding(validated, workspace_root)

    op = _operation_at(validated, index, "editable_install")
    extras = tuple(str(e) for e in op.get("extras", ()))
    binding = _EditableBinding(
        venv_path=str(op["venv_path"]),
        source_path=str(op["source_path"]),
        extras=extras,
    )
    venv_root = canonicalize_workspace_path(
        workspace_root, binding.venv_path, field_name=f"operations[{index}].venv_path"
    )
    source = canonicalize_workspace_path(
        workspace_root, binding.source_path, field_name=f"operations[{index}].source_path"
    )

    # The venv must be a real one BEFORE we install into it. Installing into a
    # directory that merely looks like a venv is how the operator's own
    # interpreter gets mutated -- section 9.2's "never mutate the operator's
    # Python environment".
    verify_venv(venv_root)
    if not (source / "pyproject.toml").is_file() and not (source / "setup.py").is_file():
        raise EnvExecutionError(
            f"editable source {source} has no pyproject.toml or setup.py -- there is "
            "nothing to install"
        )

    target = str(source) + (f"[{','.join(binding.extras)}]" if binding.extras else "")
    proc = subprocess.run(
        [
            "uv", "pip", "install",
            "--python", str(venv_interpreter(venv_root)),
            "--editable", target,
        ],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise EnvExecutionError(
            f"uv pip install --editable {target} failed:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    direct_url = read_direct_url(venv_root, source)
    if not direct_url.get("dir_info", {}).get("editable"):
        raise EnvExecutionError(
            f"install from {source} is recorded in PEP 610 metadata but not as "
            "editable (fruit 12)"
        )

    return {
        "kind": "editable_install",
        "venv_path": binding.venv_path,
        "source_path": binding.source_path,
        "extras": list(binding.extras),
        "distribution": _distribution_name(source),
        "editable": True,
        "direct_url": str(direct_url.get("url", "")),
    }
