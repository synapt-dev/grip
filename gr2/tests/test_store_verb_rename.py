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

⚠ ROWS HELD UNTIL THE ALIAS DROP -- the list the deletion must consume. These pin the ALIAS NAME,
so they stay GREEN now and, but for the two marked below, go RED at the drop (measured: all nine
hidden aliases dropped in a scratch copy, full suite run); they are deliberately NOT re-pointed,
because they are the alias's only coverage while the alias is still live, and deleting them now
would remove that coverage invisibly.
  - test_store_verb_rename.py::test_grip_alias_still_reaches_the_same_callback
  - test_store_verb_rename.py::test_both_names_enumerate_the_same_verbs
  - test_store_verb_rename.py::test_root_help_shows_store_and_hides_grip   (asserts the hidden name is ABSENT,
    so it stays GREEN at the drop: delete it by hand with the alias, it will not announce itself)
  - test_store_verb_witnesses.py::test_snapshot_is_a_hidden_alias_of_commit
  - test_open_gr_enter.py::test_both_review_verbs_help_names_commit_kind_and_path  (the old-help spellings)
  - test_project_review_three_repo.py::test_project_review_cli_adapter_and_structured_rendering_are_registered
    (`assert "open-project" in commands`; re-pointing it to "open" would be VACUOUS -- "open" is always
    registered -- so this one is HELD rather than re-pointed)
⚠ AND TWO ROWS THAT ARE NOT RE-POINTABLE AT ALL, because the kept name's behaviour DIFFERS from the
alias it replaces (measured; reported to Fathom, whose row 3 owns the CLI surface):
  - test_open_project_refusal_reason_cli.py (both rows): `review open-project` accepts `--allow-local`;
    `review open` REFUSES it -- "No such option: --allow-local", exit 2 instead of exit 1. So
    `open-project` is not a pure alias of `open` at the option surface.
  - test_review_create_project_cli.py::test_create_project_is_discoverable_from_help: `review
    open-project --help` names "create-project"; `review open --help` does not. It stays GREEN at
    the drop for the wrong reason: `review open-project --help` then exits 2 with "No such command
    'open-project'. Did you mean 'create-project'?", which contains the string the row looks for. The
    drop must delete or re-point it by hand; nothing will say it tested nothing.
⚠ ROWS THAT ARRIVED ON dev AFTER THE LIST ABOVE WAS FIRST WRITTEN (named from a grep of the test names
and bodies added since this branch's base f8b1c95e; each pins a hidden alias by name, and all were
RUN against an alias drop, in the same scratch copy as above: all red but the registry sweep). The
drop must consume these too, or it finds them by going red:
  - test_store_commit_noop.py::test_the_hidden_snapshot_alias_is_a_no_op_too
  - test_hooks_group_names.py::test_hooks_show_and_the_hidden_repo_hooks_print_the_same_json
  - test_hooks_group_names.py::test_hooks_run_and_the_hidden_repo_hook_run_agree
  - test_hooks_group_names.py::test_the_old_repo_spellings_are_hidden_but_the_new_ones_are_listed
  - test_lane_show_name.py::test_lane_show_and_the_hidden_lane_current_print_the_same_thing
  - test_lane_show_name.py::test_the_old_spelling_is_hidden_and_the_new_one_is_listed
  - test_deprecation_registry.py::test_every_hidden_alias_is_registered_or_explicitly_exempted
    (the registry sweep: it reads the alias set, so it is the row that SHOULD shrink with the drop; it
    stays GREEN at the drop, because the set it reads is then empty)
⚠ AND SIX ROWS IN test_api_dump.py GO RED AT THE DROP THROUGH THE COMMITTED api/cli.api, which lists the
aliases. They are not alias-pinning rows; the drop must regenerate the file (gr2/scripts/dump_api.py):
  - test_api_dump.py::test_api_dump_is_current
  - test_api_dump.py::test_every_json_item_names_a_real_verb
  - test_api_dump.py::test_may_change_entries_all_name_a_real_item
  - test_api_dump.py::test_the_committed_file_carries_exactly_the_items
  - test_api_dump.py::test_the_store_group_json_verbs_all_have_a_shape
  - test_api_dump.py::test_uncovered_json_verbs_are_counted
"""
import re
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


# CSI sequences: ESC, then "[" , then parameters/intermediates, then a final byte.
_ANSI_RE = re.compile("\x1b\\[[0-9;?]*[ -/]*[@-~]")


def test_both_names_enumerate_the_same_verbs():
    """⚠ REWRITTEN 2026-09-28, because the first version could not fail on a hidden verb.

    It asked whether each expected name appears ANYWHERE in the help page, and the group's
    own description -- "Grip object model: workspace snapshots and history" -- CONTAINS
    "snapshot". So when the port made `store snapshot` a hidden alias (design section 5 line
    100), the row kept passing while the verb had correctly left the command list. A
    substring of the whole page is not a reading of the command list: an instrument that
    cannot tell a listed command from a description word.

    The reading now comes from the Commands block itself, with a chrome control so the
    extraction cannot silently return option rows, and the hidden alias is asserted
    INVOCABLE while unlisted -- hidden means unlisted, not removed.
    """
    expected = {
        "init", "commit", "check", "push", "status", "materialize", "migrate",
        "log", "diff", "checkout",
    }
    for group in ("store", "grip"):
        raw = runner.invoke(app, [group, "--help"]).output or ""
        # v5 — ANSI IS STRIPPED, NOT THE ENVIRONMENT CHANGED. typer/rich_utils forces a
        # terminal when GITHUB_ACTIONS (or FORCE_COLOR / PY_COLORS) is set, so in CI the
        # help text carries escape codes and the box-pipe parse below finds an EMPTY
        # Commands block -- measured: this row is green on a desk and red in CI with
        # "missing [...] from []". The row asserts WHICH VERBS ARE LISTED, not how they
        # render, so it strips the codes and reads the output CI actually produces, rather
        # than deleting the variable and testing a rendering CI never emits.
        raw = _ANSI_RE.sub("", raw)
        lines = raw.splitlines()
        # the Commands block ONLY: start at its header, stop at the first row without a box
        # pipe. Reading the whole page is what made the first version blind to a hidden verb,
        # and reading it loosely is what let option rows and wrapped descriptions in.
        start = next(i for i, line in enumerate(lines) if "Commands" in line)
        listed = set()
        for line in lines[start + 1:]:
            parts = line.split("│")
            if len(parts) < 3:
                break
            cell = parts[1]
            if not cell.startswith(" "):
                continue
            body = cell[1:]  # rich pads each cell with exactly one space
            if not body.strip() or body.startswith(" "):
                # a WRAPPED DESCRIPTION line: its name cell is blank, so the text sits
                # indented. Reading it as a command is how "upstreams." got into the set.
                continue
            listed.add(body.split()[0])
        assert expected <= listed, f"{group}: missing {sorted(expected - listed)} from {sorted(listed)}"
        assert "snapshot" not in listed, (
            f"{group}: the hidden alias must not be listed as its own command: {sorted(listed)}"
        )
        # chrome control: option rows and wrapped description fragments must not get in
        assert all(name.replace("-", "").isalnum() for name in listed), (
            f"{group}: extraction caught non-command text: {sorted(listed)}"
        )

    # hidden means UNLISTED, not removed: the alias still runs
    r = runner.invoke(app, ["store", "snapshot", "--help"])
    assert r.exit_code == 0, r.output
