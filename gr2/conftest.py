"""Root conftest: make python_cli importable as gr2.python_cli.

Also the isolation-fixture home. `testpaths` (pyproject.toml) spans two
directories that are SIBLINGS of each other, not one ancestor of the other --
`tests/` and `gr2/overlay/tests/` -- so an autouse fixture placed in
`tests/conftest.py` reaches only the first of them; pytest applies a
directory-scoped conftest to that directory's own subtree, never to a
sibling tree. `_isolated_git_config` lives HERE, at the one conftest that is
an ancestor of both, so "every gr2 test" is true rather than aspirational.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_project_root = Path(__file__).parent
# RESOLVED ONCE, and both sides of the containment test use it. The probe reports the path the
# IMPORT resolved to, which is always canonical, so comparing it against an UNresolved root
# falsely refuses a checkout that lives under a symlink -- `/tmp/...` reports as
# `/private/tmp/...` on this host and the test would fire on a correct install. Measured while
# witnessing this precondition's own second branch.
_project_root_resolved = _project_root.resolve()


def pytest_configure(config: pytest.Config) -> None:
    """Refuse the session ONCE, in one line, when a SUBPROCESS cannot import the tree under test.

    WHY A SUBPROCESS AND NOT THIS PROCESS. The block below injects `gr2` into ``sys.modules``
    so that IN-PROCESS tests import the tree they live in -- which means this process can import
    `gr2.python_cli` whether or not anything is installed, and can therefore never see the defect.
    Three test files spawn ``[sys.executable, "-m", "gr2.python_cli.app", ...]`` as a child
    process. TWO of them pass ``PYTHONPATH=<gr2 dir>`` and treat that as sufficient; the third
    (``test_review_cli.py``'s ``_run_bind_real``) passes no environment at all and inherits the
    cwd. Neither WAS enough: `gr2.python_cli` used to exist only through
    a packaging map (a flat directory mapped onto a dotted name), which an EDITABLE INSTALL
    provided and a bare tree did not. grip#826: without that install the child died with
    ``No module named 'gr2.python_cli'``; measured on the fleet interpreter, the three
    spawning files were 13 failed / 14 passed. The map is retired and the directory now equals
    the import name, so ``PYTHONPATH=<gr2 dir>`` IS sufficient; this probe stays because the
    property it checks, "it imported FROM THIS TREE", is separate (see the next paragraph).

    THE RESOLVED PATH IS CHECKED, NOT JUST THAT THE IMPORT WORKED, and that half is the one that
    has actually bitten this team: an editable install of a DIFFERENT CLONE makes the import
    succeed and the tests measure someone else's bytes, so "it imported" is not the property the
    suite needs. The property is "it imported FROM THIS TREE".

    Refusing rather than skipping: a skip would hide the difference between "this clone cannot run
    these tests" and "these tests pass", and the red it replaces is already being misread as a
    product failure. CI installs before it runs, so this never fires there -- if it ever does, CI
    is the thing that changed.

    THE PROBE REPRODUCES THE TESTS' OWN ENVIRONMENT AS CLOSELY AS IT CAN, because a probe that
    answers a DIFFERENT question is an instrument that cannot fail. Two details, both measured:

      - It imports ``gr2.python_cli.app`` -- the module the tests spawn with ``-m`` -- rather than
        the parent package, so "the parent imports" is not allowed to stand in for "the thing they
        run imports".
      - It sets ``PYTHONPATH`` to the SAME directory the test helpers set. That is load-bearing and
        not obvious: with ``PYTHONPATH=<gr2 dir>``, `gr2` resolves to the INNER regular package
        (``gr2/gr2/``, which has ``__init__.py``) and that BEATS the namespace-package portion a
        cwd would otherwise offer. Measured across three cwds on both interpreters, the answer is
        identical at all three, so this probe is not cwd-dependent -- but it is PYTHONPATH-dependent,
        and setting it differently from the tests would measure a tree the tests never see.

    KNOWN LIMIT, stated rather than implied. This precondition FORCES ``PYTHONPATH``, so it is
    blind to an ambient ``PYTHONPATH`` misdirecting the one helper that sets none of its own
    (``test_review_cli.py::_run_bind_real``). A tree where only that file measured someone else's
    bytes would pass here. The fix belongs in that helper -- give it the same explicit
    ``PYTHONPATH`` its two siblings carry -- and is deliberately not in this change.
    """
    probe = "import gr2.python_cli.app as m; print(m.__file__)"
    env = {**os.environ, "PYTHONPATH": str(_project_root)}
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host-dependent
        pytest.exit(
            f"gr2 conftest precondition could not run its own probe: {exc!r}\n"
            f"  interpreter: {sys.executable}",
            returncode=1,
        )
        return

    resolved = (result.stdout or "").strip()
    if result.returncode != 0:
        pytest.exit(
            "gr2 conftest precondition: a SUBPROCESS cannot import `gr2.python_cli`, so the "
            "subprocess-driven tests would fail for a reason that is not a product failure.\n"
            f"  interpreter : {sys.executable}\n"
            f"  PYTHONPATH  : {_project_root}\n"
            f"  error       : {(result.stderr or '').strip().splitlines()[-1] if result.stderr else '(none)'}\n"
            "  FIX: install the tree under test -- "
            '`.venv/bin/python -m pip install -e ".[dev]"` -- and re-run from that venv.',
            returncode=1,
        )
        return

    if _project_root_resolved not in Path(resolved).resolve().parents:
        pytest.exit(
            "gr2 conftest precondition: `gr2.python_cli` imported, but NOT from the tree under "
            "test -- the tests would validate someone else's bytes.\n"
            f"  interpreter : {sys.executable}\n"
            f"  resolved to : {resolved}\n"
            f"  expected in : {_project_root}\n"
            "  FIX: one venv per tree under test; never borrow a venv that has an editable "
            "install of a different clone.",
            returncode=1,
        )

if "gr2" not in sys.modules:
    # The package dir is gr2/gr2 and holds every subpackage (python_cli, prototypes, overlay,
    # schemas), so the import name equals the directory name. This used to be a namespace
    # with TWO roots because python_cli lived flat at gr2/python_cli and was mapped onto
    # `gr2.python_cli` by the packaging map; the map is retired and the flat root is gone.
    _gr2 = types.ModuleType("gr2")
    _gr2.__path__ = [str(_project_root / "gr2")]
    sys.modules["gr2"] = _gr2


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    """Every gr2 test runs against a BLANK host git config by default.

    `git review run`'s cleanup lost two review cycles to the same class of defect:
    `git status --porcelain` reads the AUTHORING MACHINE's global ignore rules
    (`core.excludesFile`, falling back to `$XDG_CONFIG_HOME/git/ignore`), so a test
    on a host that happens to ignore `__pycache__/` globally measures a git that
    can't see what it should -- and the CI host, with no such config, measured
    something else entirely. That gap has nothing to do with `git review run`
    specifically: ANY gr2 test that shells out to git inherits whatever the
    developer's own machine happens to have configured, unless something removes
    it. This fixture is that something, for the whole suite, not just one file --
    which requires it to live at THIS conftest, the ancestor of both `tests/` and
    `gr2/overlay/tests/` (see the module docstring above).

    Three independent host-config channels, all closed:
      - ``GIT_CONFIG_GLOBAL`` points at an EMPTY file. An *absent* variable falls
        back to ``~/.gitconfig`` -- exactly the ambient state this fixture exists
        to remove -- so it must point at a real, present, empty file, not be unset.
      - ``XDG_CONFIG_HOME`` points at an empty temp dir, removing the
        ``$XDG_CONFIG_HOME/git/ignore`` fallback git consults when
        ``core.excludesFile`` is unset (measured separately from the above: the
        two are different config keys read by different fallback paths).
      - ``GIT_CONFIG_NOSYSTEM=1`` removes ``/etc/gitconfig`` from the read path.

    Every subprocess call site in gr2 either omits ``env=`` (inherits ``os.environ``
    directly) or builds its env dict FROM ``os.environ`` at call time
    (``scrubbed_python_env``, the drift-guard's index env in review_run.py) -- never
    a cached copy taken before this fixture runs -- so ``monkeypatch.setenv`` here
    reaches every git subprocess the suite invokes, not just ones that call the
    bare ``subprocess`` module directly.

    A test that WANTS a host-shaped ignore rule sets one explicitly and locally
    (see ``test_the_run_cleans_up_on_a_host_whose_global_gitignore_hides_the_artifact``
    in test_git_review.py, which overrides these same two env vars for its own
    simulated host) -- the correct shape is opt IN to ambient-like config, never
    opt OUT of isolation, because opt-out is exactly the silent, host-shaped
    blindness this fixture exists to close.

    A FOURTH channel, added after the first three were measured closed
    (measured on the runner PATH change): git also accepts config
    injected purely through the environment, with precedence ABOVE the file
    sources above --
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>`` (a
    numbered trio a CI wrapper or a developer's shell profile can export) and
    ``GIT_CONFIG_PARAMETERS`` (the shell-quoted form git itself sets when a
    caller uses ``-c``, equally exportable by a wrapper). Measured directly
    on this host: a ``core.excludesFile`` set via either mechanism hides a
    directory `GIT_CONFIG_GLOBAL`/`XDG_CONFIG_HOME`/`GIT_CONFIG_NOSYSTEM`
    cannot touch, because the earlier three only close FILE sources and this
    is not one. ``GIT_CONFIG_COUNT`` gates whether git reads any
    ``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` pair at all (measured: absent
    COUNT, present KEY_0/VALUE_0, no effect) so deleting COUNT alone is
    sufficient without hunting down every numbered pair a caller might have
    exported.

    ``GIT_CONFIG_SYSTEM`` (the override for the system-config PATH, distinct
    from ``GIT_CONFIG_NOSYSTEM`` which disables reading system config
    entirely) was measured and NOT added: on this host, git 2.50.1, setting
    ``GIT_CONFIG_SYSTEM`` to a dirty file while ``GIT_CONFIG_NOSYSTEM=1`` is
    also set does not leak -- ``NOSYSTEM`` wins outright, so the channel this
    fixture already closes covers it. Revisit if a supported git version is
    ever measured to behave otherwise.
    """
    blank_global = tmp_path_factory.mktemp("isolated-git-global") / "gitconfig"
    blank_global.write_text("")
    blank_xdg = tmp_path_factory.mktemp("isolated-git-xdg")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(blank_global))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blank_xdg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
