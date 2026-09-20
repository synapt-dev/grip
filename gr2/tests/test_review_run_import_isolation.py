"""review run's import check must resolve the package against the INSTALL, not the
lane cwd.

`resolve_import_file` runs `python -c "import <pkg>"`, and `python -c` prepends the
process cwd to `sys.path[0]`. That subprocess inherits the LANE as cwd, so a repo
whose ROOT holds a directory named like its importable package (grip's `gr2/`)
resolves that project directory as a PEP 420 namespace package with no `__file__`
— `import_no_file` on a lane whose editable install is perfectly correct. `-I`
drops cwd from the path so the import resolves to the installed package under the
lane. Witness proves the resolution; the control proves the shadow is real.
"""
from __future__ import annotations

import os
import site
import subprocess
import sys
import venv
from pathlib import Path

from gr2.python_cli import review_run as rr


def _shadow_lane(tmp_path: Path) -> tuple[Path, Path]:
    """A lane whose ROOT holds a directory `demo/` (a namespace portion when imported
    from cwd) while the REAL package lives at `demo/demo/__init__.py`, editable-
    installed via a setuptools-style meta-path finder — exactly grip's `gr2` ->
    `gr2/gr2` shape. Returns (lane_dir, venv_python).

    The install is a meta-path finder APPENDED to sys.meta_path (as setuptools'
    editable install does), so PathFinder — which sees the cwd `demo/` namespace
    portion first — beats it whenever cwd is on the path. That is the shadow, and it
    is why a plain path entry would NOT reproduce the bug: a regular package on
    sys.path always wins over a namespace portion, so only an appended finder can be
    out-raced by cwd."""
    lane = tmp_path / "lane"
    real = lane / "demo" / "demo"
    real.mkdir(parents=True)
    (real / "__init__.py").write_text("VALUE = 1\n")

    venv_dir = lane / rr._VENV_DIRNAME
    venv.create(venv_dir, with_pip=False)
    venv_python = venv_dir / "bin" / "python"

    sp = subprocess.run(
        [str(venv_python), "-c", "import site; print(site.getsitepackages()[0])"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    Path(sp).mkdir(parents=True, exist_ok=True)
    finder = (
        "import sys, importlib.util\n"
        f"_MAP = {{'demo': {str(real / '__init__.py')!r}}}\n"
        "class _EditableFinder:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        f = _MAP.get(name)\n"
        "        if not f:\n"
        "            return None\n"
        "        return importlib.util.spec_from_file_location(\n"
        "            name, f, submodule_search_locations=[f.rsplit('/', 1)[0]])\n"
        "def install():\n"
        "    if not any(type(x).__name__ == '_EditableFinder' for x in sys.meta_path):\n"
        "        sys.meta_path.append(_EditableFinder())\n"
        "install()\n"
    )
    (Path(sp) / "__editable___demo_finder.py").write_text(finder)
    (Path(sp) / "__editable__.demo.pth").write_text("import __editable___demo_finder\n")
    return lane, venv_python


def test_import_check_resolves_the_install_not_the_cwd_namespace_dir(tmp_path, monkeypatch):
    lane, venv_python = _shadow_lane(tmp_path)
    monkeypatch.chdir(lane)  # the lane is cwd, as in a real `cd lane && review run .`

    # CONTROL: the shadow is real. Without -I, `import demo` from the lane cwd binds
    # the root `demo/` project dir as a namespace package with no __file__.
    ctrl = subprocess.run(
        [str(venv_python), "-c", "import demo; print(demo.__file__ or 'NONE')"],
        text=True, capture_output=True, cwd=str(lane),
    )
    assert ctrl.stdout.strip() == "NONE", (
        "control failed to reproduce the cwd shadow; the fixture no longer exercises "
        f"the bug (got {ctrl.stdout.strip()!r})"
    )

    # WITNESS: resolve_import_file (which uses -I) resolves to the INSTALLED package
    # under the lane, not the namespace dir.
    resolved = rr.resolve_import_file(venv_python, "demo", {**os.environ})
    assert resolved, "import_no_file: -I did not drop the cwd shadow"
    assert Path(resolved).name == "__init__.py"
    assert str((lane / "demo" / "demo").resolve()) in str(Path(resolved).resolve())
    # and the check assert_import_under_lane is satisfied (resolves under the lane)
    rr.assert_import_under_lane(resolved, lane)


def test_pythonpath_shadow_is_ignored_by_the_isolated_import_check(tmp_path, monkeypatch):
    """-I implies -E, so a rogue package on PYTHONPATH no longer shadows the lane
    install: the import resolves to the lane, not the rogue. This is the same
    hardening that lets test_run_refuses_when_the_import_escapes_the_lane move its
    escape vector to the venv site (PYTHONPATH can no longer escape)."""
    lane, venv_python = _shadow_lane(tmp_path)
    rogue = tmp_path / "rogue"
    (rogue / "demo").mkdir(parents=True)
    (rogue / "demo" / "__init__.py").write_text("VALUE = 2\n")
    monkeypatch.chdir(lane)
    monkeypatch.setenv("PYTHONPATH", str(rogue))

    resolved = rr.resolve_import_file(venv_python, "demo", {**os.environ})
    assert str((lane / "demo" / "demo").resolve()) in str(Path(resolved).resolve())
    assert str(rogue.resolve()) not in str(Path(resolved).resolve())


def test_import_check_argv_carries_isolated_flag():
    """A structural pin so a refactor cannot silently drop -I and reintroduce the
    cwd-shadow (the check has no other guard once the fixture stops matching grip)."""
    import inspect

    src = inspect.getsource(rr.resolve_import_file)
    assert '"-I"' in src, "resolve_import_file must run the import subprocess with -I"


def test_scrubbed_python_env_drops_every_python_star_var(monkeypatch):
    """-I isolates the CHECK; the pytest RUN runs without -I, so the run's env is
    isolated by scrubbing PYTHON*. This pins the scrub: every PYTHON* variable is
    removed and non-PYTHON variables are preserved. The behavioral witness that a
    PYTHONPATH rogue does not change the RUN lives in test_review_run.py; this is the
    unit-level guard on the seam that flows to both checks and pytest."""
    for name in (
        "PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH",
        "PYTHONNOUSERSITE", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE",
    ):
        monkeypatch.setenv(name, "rogue-value")
    monkeypatch.setenv("KEEP_ME_UNSCRUBBED", "yes")

    env = rr.scrubbed_python_env()

    assert not any(k.startswith("PYTHON") for k in env), (
        "scrubbed_python_env left a PYTHON* var: "
        f"{sorted(k for k in env if k.startswith('PYTHON'))}"
    )
    assert env.get("KEEP_ME_UNSCRUBBED") == "yes", "a non-PYTHON var must survive the scrub"


def test_run_env_is_built_by_the_scrub_and_flows_to_pytest():
    """A structural pin so a refactor cannot rebuild run_env from a raw os.environ and
    silently reintroduce the PYTHONPATH-shadows-the-run defect: the run's env is the
    scrub's output, and pytest is invoked under that same run_env.

    A follow-up widened the call to `scrubbed_python_env(venv_dir=venv_dir)` (the
    scrub now also shapes PATH/VIRTUAL_ENV to look like the lane venv was
    activated, so a repo's own tests can shell out to their own console scripts);
    the pin follows that shape rather than the bare no-arg call it used to name."""
    import inspect

    src = inspect.getsource(rr._run_review_lane)
    assert "run_env = scrubbed_python_env(venv_dir=venv_dir)" in src, (
        "the review run must build run_env via scrubbed_python_env(venv_dir=...), "
        "not {**os.environ} and not the bare no-arg call"
    )
    # the pytest subprocess must run under run_env (not a fresh/raw env)
    assert "env=run_env" in src, "pytest must run under the scrubbed run_env"
