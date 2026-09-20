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

import sys
import types
from pathlib import Path

import pytest

_project_root = Path(__file__).parent

if "gr2" not in sys.modules:
    # Namespace with two roots: the project dir (python_cli lives flat at
    # gr2/python_cli, imported as gr2.python_cli) and the real package dir
    # gr2/gr2 (the 1.5.0 import-package layout: gr2.overlay, gr2.schemas).
    _gr2 = types.ModuleType("gr2")
    _gr2.__path__ = [str(_project_root), str(_project_root / "gr2")]
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
    (Sentinel's R1 finding on the runner PATH lane): git also accepts config
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
