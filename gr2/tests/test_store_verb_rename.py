"""The snapshot store's verb is `store`; `grip` stays a hidden alias for one release.
Both names resolve to the same grip_app callbacks (init/snapshot/log/diff/checkout),
`store` is visible in the root help and `grip` is hidden.

⚠ THE STORE'S HOME CHANGED AND THESE TWO INIT ROWS FOLLOWED IT (2026-09-28). They asserted
`<root>/.grip/.git` -- the alpha store repo. The Show HN slice makes one grip commit a real
commit in the workspace root's OWN `.git` and states "No `.grip` store repo" (design section
3), so both rows asserted the shape the design removes and had been red since the native
verbs landed. Measured: the native `store init` creates `<root>/.git` plus `grip.toml`, and
never `.grip/.git`. The rows keep their real purpose (both spellings reach the same callback
and the store is created) against the native home.
"""
import subprocess
import tempfile
import pathlib

from typer.testing import CliRunner

from python_cli.app import app

runner = CliRunner()


def _flat(result) -> str:
    return " ".join((result.output or "").split())


def _workspace_with_one_member(tmp_path: pathlib.Path) -> pathlib.Path:
    """A gr1-shape root: one member clone side by side, the root itself not a repo.

    `store init` refuses a root with nothing to store (exit 4, "no sibling git repositories
    found"), and the design's section 2 smoke shape is exactly this one -- members beside a
    root that is not yet a repo.
    """
    remote = tmp_path / "alpha.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@e.invalid")):
        subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
    (seed / "README.md").write_text("alpha\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(remote), "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(remote), str(root / "alpha")], check=True)
    return root


def test_store_init_creates_the_root_repo():
    ws = _workspace_with_one_member(pathlib.Path(tempfile.mkdtemp()))
    r = runner.invoke(app, ["store", "init", str(ws)])
    assert r.exit_code == 0, r.output
    assert (ws / ".git").is_dir()
    assert (ws / "grip.toml").is_file()
    assert not (ws / ".grip" / ".git").exists(), "the alpha store repo is not created by the native verb"


def test_grip_alias_still_reaches_the_same_callback():
    ws = _workspace_with_one_member(pathlib.Path(tempfile.mkdtemp()))
    r = runner.invoke(app, ["grip", "init", str(ws)])
    assert r.exit_code == 0, r.output
    assert (ws / ".git").is_dir()
    assert (ws / "grip.toml").is_file()


def test_root_help_shows_store_and_hides_grip():
    flat = _flat(runner.invoke(app, ["--help"]))
    assert "store" in flat
    # the hidden alias must not appear as its own command in root help
    # (guard against a substring hit inside "gitgrip"/"grip_*")
    assert "grip" not in flat.replace("gitgrip", "").replace("grip_", "")


def test_both_names_enumerate_the_same_verbs():
    expected = {"init", "snapshot", "log", "diff", "checkout"}
    for verb in ("store", "grip"):
        flat = _flat(runner.invoke(app, [verb, "--help"]))
        got = {v for v in expected if v in flat}
        assert got == expected, f"{verb} missing verbs: {expected - got}"
