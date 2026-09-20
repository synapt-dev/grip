"""The snapshot store's verb is `store`; `grip` stays a hidden alias for one release.
Both names resolve to the same grip_app callbacks (init/snapshot/log/diff/checkout),
`store` is visible in the root help and `grip` is hidden. The store owns .grip/.git;
`workspace init` owns the .grip/ dir + spec (measured: no init-path collision)."""
import tempfile
import pathlib

from typer.testing import CliRunner

from python_cli.app import app

runner = CliRunner()


def _flat(result) -> str:
    return " ".join((result.output or "").split())


def test_store_init_creates_the_grip_git_store():
    ws = pathlib.Path(tempfile.mkdtemp())
    r = runner.invoke(app, ["store", "init", str(ws)])
    assert r.exit_code == 0
    assert (ws / ".grip" / ".git").exists()


def test_grip_alias_still_reaches_the_same_callback():
    ws = pathlib.Path(tempfile.mkdtemp())
    r = runner.invoke(app, ["grip", "init", str(ws)])
    assert r.exit_code == 0
    assert (ws / ".grip" / ".git").exists()


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
