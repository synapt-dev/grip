"""Counter-witness for the autouse `_isolated_git_config` fixture in conftest.py.

Not a test of `git review run` -- a test of the SUITE-WIDE isolation every gr2 test
now runs under. Lives in its own file because it is about the fixture, not about any
one feature that happens to touch git config.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@e.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "a"], check=True)


def test_isolated_git_config_hides_a_simulated_host_ignore(tmp_path, monkeypatch):
    """CONTROL then SUBJECT, on the same repo, host-independent -- AND capable of
    catching a regression even on a CI host whose real ``$HOME`` has no dirty
    config of its own.

    CONTROL: an EXPLICIT global config claiming to ignore `__pycache__/` DOES hide
    it from `git status` -- proves this repo/host combination is capable of showing
    the failure the autouse fixture exists to prevent, without depending on what
    the CI runner or a developer's real machine happens to have configured.

    SUBJECT: ``$HOME`` (and ``$USERPROFILE`` on Windows) is monkeypatched to a
    FAKE home directory that itself carries a dirty ``~/.gitconfig`` ignoring
    `__pycache__/` -- exercising the exact fallback path
    (``GIT_CONFIG_GLOBAL`` unset -> git reads ``~/.gitconfig``) the autouse
    fixture exists to close. Without this, a bare CI runner whose real
    ``$HOME`` has no `.gitconfig` at all would pass this assertion by
    coincidence even with the fixture entirely deleted, since there would be
    nothing in the FALLBACK path to hide either -- the test would look green
    while proving nothing host-specific. With a fake, deliberately dirty
    ``$HOME`` in place, the assertion only holds because
    ``_isolated_git_config``'s ``GIT_CONFIG_GLOBAL`` (an explicit env var)
    takes precedence over that fallback; if the fixture regressed to a no-op,
    this fake home's config would take over and hide `__pycache__` here too.
    """
    repo = tmp_path / "probe"
    _init_repo(repo)
    pycache = repo / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00")

    fake_global = tmp_path / "fake_global_gitignore"
    fake_global.write_text("__pycache__/\n")
    fake_gitconfig = tmp_path / "fake_gitconfig"
    fake_gitconfig.write_text(f"[core]\n\texcludesfile = {fake_global}\n")
    control_env = {**os.environ, "GIT_CONFIG_GLOBAL": str(fake_gitconfig)}
    control_out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True, env=control_env,
    ).stdout
    assert "__pycache__" not in control_out, (
        "control failed to bite: an explicit host-shaped config claiming to ignore "
        f"__pycache__ did not hide it, so this test cannot witness anything. "
        f"porcelain={control_out!r}"
    )

    # Dirty a FAKE $HOME's ~/.gitconfig -- the target of git's fallback when
    # GIT_CONFIG_GLOBAL is unset -- so the subject run below is discriminating
    # even on a CI host whose real $HOME starts out clean.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(f"[core]\n\texcludesfile = {fake_global}\n")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    subject_out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "__pycache__" in subject_out, (
        "the autouse git-config isolation fixture is not neutralizing the host: "
        f"__pycache__ was hidden inside the suite, even with a fake $HOME whose "
        f"own dirty ~/.gitconfig should have been shadowed by GIT_CONFIG_GLOBAL. "
        f"porcelain={subject_out!r}"
    )


def test_isolated_git_config_env_vars_point_at_present_empty_files(tmp_path_factory):
    """The three isolation channels are set to REAL, PRESENT, EMPTY targets, not
    merely unset -- an unset GIT_CONFIG_GLOBAL falls back to ~/.gitconfig, which is
    the exact ambient state this fixture exists to remove, so "unset" would be a
    silent regression back to the bug, not a stricter form of isolation."""
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"

    global_path = os.environ.get("GIT_CONFIG_GLOBAL")
    assert global_path, "GIT_CONFIG_GLOBAL must be set, not absent"
    p = Path(global_path)
    assert p.is_file(), f"GIT_CONFIG_GLOBAL must point at a real file: {global_path!r}"
    assert p.read_text() == "", f"GIT_CONFIG_GLOBAL must be empty: {global_path!r}"

    xdg_path = os.environ.get("XDG_CONFIG_HOME")
    assert xdg_path, "XDG_CONFIG_HOME must be set, not absent"
    xp = Path(xdg_path)
    assert xp.is_dir(), f"XDG_CONFIG_HOME must point at a real directory: {xdg_path!r}"
    ignore_file = xp / "git" / "ignore"
    assert not ignore_file.exists(), (
        f"XDG_CONFIG_HOME/git/ignore must not exist: {ignore_file}"
    )


def test_isolated_git_config_hides_a_count_key_value_env_injection(tmp_path):
    """A FOURTH host-config channel, found by Sentinel: git honors config passed
    purely through the environment -- ``GIT_CONFIG_COUNT`` plus a numbered
    ``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>`` pair -- with precedence ABOVE
    the file sources the other three channels close. A CI wrapper or a
    developer's shell profile exporting this trio re-injects config past
    ``GIT_CONFIG_GLOBAL``/``XDG_CONFIG_HOME``/``GIT_CONFIG_NOSYSTEM`` entirely,
    because none of those three touch it.

    Genuinely testing this needs the dirty trio present BEFORE our autouse
    fixture ever runs -- i.e. inherited at the pytest PROCESS's own OS-level
    startup, the way a CI wrapper's ``export`` would arrive -- and an
    ``os.environ``/``monkeypatch`` trick INSIDE this test cannot construct that:
    by the time this test's body executes, ``_isolated_git_config`` (autouse)
    has already run and already delenv'd; setting the trio afterward via
    ``monkeypatch.setenv`` only proves the injection mechanism works (same as
    the CONTROL blocks elsewhere in this file), not whether the fixture
    protects a process that inherited it from outside. So this witness spawns
    a REAL subprocess pytest run, with the dirty trio in the environment that
    LAUNCHES it -- the only construction that actually exercises "was this
    present when the process started."

    CONTROL: a sibling subprocess run, launched the same way but with
    ``GIT_CONFIG_COUNT``/``KEY_0``/``VALUE_0`` UNSET on the launcher, confirms
    the mini-suite's own probe is capable of seeing an un-hidden directory at
    all (i.e. the CONTROL isn't itself broken in some way that would mask a
    real regression as a false pass).

    SUBJECT: the dirty trio IS set on the launcher; the inner pytest process
    (which loads the real ``gr2/conftest.py``, unmodified) must still report
    the directory as visible, because the autouse fixture's delenv removes the
    trio at its own setup, before the inner test's ``git status`` runs.
    """
    gr2_root = Path(__file__).resolve().parent.parent
    assert (gr2_root / "conftest.py").is_file(), f"expected gr2 root at {gr2_root}"

    repo = tmp_path / "probe"
    _init_repo(repo)
    pycache = repo / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00")

    fake_global = tmp_path / "fake_global_gitignore"
    fake_global.write_text("__pycache__/\n")

    # The probe file must be a DESCENDANT of gr2_root, not merely passed an
    # explicit path: pytest collects conftest.py by walking from a test file's
    # own directory up to rootdir, never from --rootdir/-c downward to an
    # unrelated tree. A probe under tmp_path would never see gr2/conftest.py's
    # autouse fixture at all, which would make this witness pass for the wrong
    # reason regardless of whether the real fixture does anything.
    probe_name = f"_tmp_count_key_value_probe_{os.getpid()}_{id(tmp_path)}.py"
    probe_path = gr2_root / "tests" / probe_name
    probe_path.write_text(
        "import subprocess\n"
        "def test_inner_status_still_shows_pycache():\n"
        f"    out = subprocess.run(['git', '-C', {str(repo)!r}, 'status', '--porcelain'],"
        " capture_output=True, text=True, check=True).stdout\n"
        "    assert '__pycache__' in out, f'hidden: {out!r}'\n"
    )
    # This probe .py lives in the REAL gr2/tests/ tree (see the comment above), so
    # any .pyc the inner pytest writes for it lands in the real
    # gr2/tests/__pycache__/ too -- a stray one there is indistinguishable from a
    # genuinely orphaned bytecode file once the source is unlinked below, and
    # scripts/check-no-orphan-pyc.sh (CI) refuses exactly that. PYTHONDONTWRITEBYTECODE
    # on the launcher is the primary fix (no .pyc is ever written); the glob cleanup
    # in `finally` and the post-finally assertion are the belt-and-suspenders for
    # any interpreter/mode that ignores the env var.
    pycache_glob = str(gr2_root / "tests" / "__pycache__" / f"{probe_name[:-3]}*.pyc")
    try:
        launcher_base = {
            **os.environ,
            "PYTHONPATH": str(gr2_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        dirty_extra = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.excludesFile",
            "GIT_CONFIG_VALUE_0": str(fake_global),
        }
        pytest_argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(probe_path)]

        control = subprocess.run(
            pytest_argv,
            cwd=str(gr2_root), capture_output=True, text=True, env=dict(launcher_base),
        )
        assert control.returncode == 0, (
            "control failed to bite: the mini-suite's own probe does not report the "
            f"directory as visible with no injection present, so this test cannot "
            f"witness a regression. stdout={control.stdout!r} stderr={control.stderr!r}"
        )

        subject_env = {**launcher_base, **dirty_extra}
        subject = subprocess.run(
            pytest_argv,
            cwd=str(gr2_root), capture_output=True, text=True, env=subject_env,
        )
        assert subject.returncode == 0, (
            "the autouse fixture is not neutralizing a GIT_CONFIG_COUNT/KEY/VALUE "
            "trio inherited at process start: the inner suite's own directory went "
            f"hidden. stdout={subject.stdout!r} stderr={subject.stderr!r}"
        )
    finally:
        probe_path.unlink(missing_ok=True)
        for stale_pyc in glob.glob(pycache_glob):
            os.unlink(stale_pyc)
    leftover = glob.glob(pycache_glob)
    assert not leftover, f"orphan .pyc left behind for the unlinked probe source: {leftover}"
