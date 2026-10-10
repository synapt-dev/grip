"""[[files.compose]]: gr1's composefile in a member's .gr2/hooks.toml.

A compose row writes ONE file from ordered, member-local parts. format "text"
concatenates them with `separator` (default "\\n\\n"); format "json" parses
every part as a JSON object and deep-merges them in order: objects merge,
scalars are replaced by the later part, arrays are APPENDED (no dedupe). The
append rule is the reason json exists here: a per-seat settings part can add
to a shared guard list and can never remove from it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.consent import consent_state, write_consent
from gr2.python_cli.hooks import (
    HookContext,
    HookRuntimeError,
    apply_file_projections,
    load_repo_hooks,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'workspace_name = "ws"\n\n[[repos]]\nname = "seat"\npath = "seat"\nurl = "unused"\n'
        '[[units]]\nname = "default"\npath = "default"\nrepos = ["seat"]\n'
    )
    return ws


def _member(workspace: Path, hooks_toml: str, **files: str) -> Path:
    root = workspace / "seat"
    (root / ".gr2").mkdir(parents=True, exist_ok=True)
    (root / ".gr2" / "hooks.toml").write_text(hooks_toml)
    for name, body in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _ctx(workspace: Path) -> HookContext:
    return HookContext(
        workspace_root=workspace,
        unit_root=workspace / "default",
        lane_root=workspace / "lanes",
        repo_root=workspace / "seat",
        repo_name="seat",
        lane_owner="default",
        lane_subject="seat",
        lane_name="feat",
    )


def _run(workspace: Path, root: Path, *, bind: bool = True):
    if bind:
        write_consent(workspace, "seat", root)
    return apply_file_projections(load_repo_hooks(root), _ctx(workspace))


def _row(parts: list[str], *, dest: str = "{workspace_root}/out.txt", extra: str = "") -> str:
    plist = ", ".join(f'"{p}"' for p in parts)
    return f'[[files.compose]]\ndest = "{dest}"\nparts = [{plist}]\nif_exists = "overwrite"\n{extra}'


def _refusal(workspace: Path, root: Path) -> dict:
    with pytest.raises(HookRuntimeError) as exc:
        _run(workspace, root)
    return json.loads(str(exc.value))


class TestText:
    def test_two_parts_join_with_the_default_separator(self, workspace: Path):
        root = _member(workspace, _row(["a.md", "b.md"]), **{"a.md": "A", "b.md": "B"})
        results = _run(workspace, root)
        assert [r.status for r in results] == ["applied"]
        assert (workspace / "out.txt").read_text() == "A\n\nB"

    def test_custom_separator_and_part_order_are_kept(self, workspace: Path):
        row = _row(["b.md", "a.md"], extra='separator = "---"\n')
        root = _member(workspace, row, **{"a.md": "A", "b.md": "B"})
        _run(workspace, root)
        assert (workspace / "out.txt").read_text() == "B---A"

    def test_rerun_is_byte_identical(self, workspace: Path):
        root = _member(workspace, _row(["a.md", "b.md"]), **{"a.md": "A", "b.md": "B"})
        _run(workspace, root)
        first = (workspace / "out.txt").read_bytes()
        _run(workspace, root)
        assert (workspace / "out.txt").read_bytes() == first


class TestJson:
    SHARED = {"model": "x", "env": {"A": "1", "B": "2"}, "n": 1}
    SEAT = {"model": "y", "env": {"B": "3", "C": "4"}}

    def _compose(self, workspace: Path, shared: str, seat: str) -> Path:
        row = _row(["shared.json", "seat.json"], extra='format = "json"\n')
        return _member(workspace, row, **{"shared.json": shared, "seat.json": seat})

    def test_objects_merge_scalars_later_wins_trailing_newline(self, workspace: Path):
        root = self._compose(workspace, json.dumps(self.SHARED), json.dumps(self.SEAT))
        _run(workspace, root)
        text = (workspace / "out.txt").read_text()
        assert text.endswith("}\n")
        assert json.loads(text) == {"model": "y", "env": {"A": "1", "B": "3", "C": "4"}, "n": 1}
        assert list(json.loads(text)) == ["model", "env", "n"]  # first-appearance order

    def test_arrays_append_in_part_order_without_dedupe(self, workspace: Path):
        shared = {"hooks": {"PreToolUse": [{"command": "push-guard"}, {"command": "kill-guard"}]}}
        seat = {"hooks": {"PreToolUse": [{"command": "kill-guard"}, {"command": "seat-extra"}]}}
        root = self._compose(workspace, json.dumps(shared), json.dumps(seat))
        _run(workspace, root)
        got = json.loads((workspace / "out.txt").read_text())["hooks"]["PreToolUse"]
        assert [h["command"] for h in got] == ["push-guard", "kill-guard", "kill-guard", "seat-extra"]

    def test_a_seat_part_with_an_empty_array_leaves_the_shared_guards(self, workspace: Path):
        # THE REASON append is the only array rule. Under replace, this seat
        # part would remove every guard from the seat.
        shared = {"hooks": {"PreToolUse": [{"command": "push-guard"}, {"command": "secret-guard"}]}}
        seat = {"hooks": {"PreToolUse": []}}
        root = self._compose(workspace, json.dumps(shared), json.dumps(seat))
        _run(workspace, root)
        got = json.loads((workspace / "out.txt").read_text())["hooks"]["PreToolUse"]
        assert [h["command"] for h in got] == ["push-guard", "secret-guard"]

    @pytest.mark.parametrize("seat", ['{"hooks": {"PreToolUse": null}}', '{"hooks": {"PreToolUse": "x"}}',
                                      '{"hooks": []}'])
    def test_a_kind_change_over_a_shared_value_refuses_and_writes_nothing(self, workspace: Path, seat: str):
        shared = json.dumps({"hooks": {"PreToolUse": [{"command": "g"}]}})
        root = self._compose(workspace, shared, seat)
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused"
        assert "seat.json" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_non_object_part_refuses_naming_the_part(self, workspace: Path):
        root = self._compose(workspace, '{"a": 1}', "[1, 2]")
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "seat.json" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_a_lone_non_object_part_is_refused(self, workspace: Path):
        row = _row(["only.json"], extra='format = "json"\n')
        root = _member(workspace, row, **{"only.json": "[1, 2]"})
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "only.json" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_unparseable_part_refuses_and_keeps_the_existing_dest(self, workspace: Path):
        root = self._compose(workspace, '{"a": 1}', "{not json")
        (workspace / "out.txt").write_text("previous")
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "not valid JSON" in payload["detail"]
        assert (workspace / "out.txt").read_text() == "previous"

    def test_separator_is_rejected_under_json(self, workspace: Path):
        row = _row(["a.json"], extra='format = "json"\nseparator = ","\n')
        root = _member(workspace, row, **{"a.json": "{}"})
        with pytest.raises(SystemExit) as exc:
            load_repo_hooks(root)
        assert "separator" in str(exc.value)


class TestGateAndConfinement:
    def test_unbound_member_skips_and_reports_nothing_written(self, workspace: Path):
        root = _member(workspace, _row(["a.md"]), **{"a.md": "A"})
        results = _run(workspace, root, bind=False)
        assert results and results[0].status != "applied"
        assert not (workspace / "out.txt").exists()

    def test_changing_the_part_list_lapses_consent(self, workspace: Path):
        root = _member(workspace, _row(["a.md"]), **{"a.md": "A", "b.md": "B"})
        write_consent(workspace, "seat", root)
        assert consent_state(workspace, "seat", root)[0] == "bound"
        (root / ".gr2" / "hooks.toml").write_text(_row(["a.md", "b.md"]))
        assert consent_state(workspace, "seat", root)[0] == "changed"

    def test_part_outside_the_member_tree_refused_though_consent_is_granted(self, workspace: Path, tmp_path: Path):
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        root = _member(workspace, _row(["a.md", str(outside)]), **{"a.md": "A"})
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "outside the member's own tree" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_symlinked_part_pointing_outside_is_refused(self, workspace: Path, tmp_path: Path):
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        root = _member(workspace, _row(["link.md"]))
        (root / "link.md").symlink_to(outside)
        assert _refusal(workspace, root)["status"] == "refused"
        assert not (workspace / "out.txt").exists()

    def test_dest_outside_the_workspace_is_refused(self, workspace: Path):
        root = _member(workspace, _row(["a.md"], dest="{workspace_root}/../escaped.md"), **{"a.md": "A"})
        assert _refusal(workspace, root)["status"] == "refused"
        assert not (workspace.parent / "escaped.md").exists()

    def test_dest_under_the_state_directory_is_refused(self, workspace: Path):
        root = _member(workspace, _row(["a.md"], dest="{workspace_root}/.grip/consent/x.json"), **{"a.md": "A"})
        assert _refusal(workspace, root)["status"] == "refused"
        assert not (workspace / ".grip" / "consent" / "x.json").exists()

    def test_missing_part_blocks_and_writes_nothing(self, workspace: Path):
        root = _member(workspace, _row(["a.md", "gone.md"]), **{"a.md": "A"})
        payload = _refusal(workspace, root)
        assert payload["status"] == "blocked" and "gone.md" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_if_exists_error_blocks_on_an_existing_dest(self, workspace: Path):
        root = _member(workspace, _row(["a.md"]).replace("overwrite", "error"), **{"a.md": "A"})
        (workspace / "out.txt").write_text("mine")
        assert _refusal(workspace, root)["status"] == "blocked"
        assert (workspace / "out.txt").read_text() == "mine"

    def test_a_dest_that_is_one_of_its_own_parts_is_refused(self, workspace: Path):
        # the row reads the file, then rewrites it: without the refusal the
        # file grew by one part on every run.
        root = _member(workspace, _row(["a.md", "b.md"], dest="{repo_root}/b.md"), **{"a.md": "A", "b.md": "B"})
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "own parts" in payload["detail"]
        assert (root / "b.md").read_text() == "B"

    def test_a_dest_symlinked_to_one_of_its_parts_is_refused(self, workspace: Path):
        root = _member(workspace, _row(["a.md"]), **{"a.md": "A"})
        (workspace / "out.txt").symlink_to(root / "a.md")
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "own parts" in payload["detail"]
        assert (root / "a.md").read_text() == "A"

    def test_a_non_utf8_part_is_a_named_refusal_not_a_traceback(self, workspace: Path):
        root = _member(workspace, _row(["a.md", "bin.md"]), **{"a.md": "A"})
        (root / "bin.md").write_bytes(b"\xff\xfe\x00bad")
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "bin.md" in payload["detail"] and "UTF-8" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_a_json_part_with_a_duplicate_key_is_refused(self, workspace: Path):
        row = _row(["dup.json"], extra='format = "json"\n')
        root = _member(workspace, row, **{"dup.json": '{"a": 1, "a": 2}'})
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "duplicate key" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_json_constants_are_refused(self, workspace: Path, literal: str):
        row = _row(["n.json"], extra='format = "json"\n')
        root = _member(workspace, row, **{"n.json": '{"x": %s}' % literal})
        payload = _refusal(workspace, root)
        assert payload["status"] == "refused" and "not valid JSON" in payload["detail"]
        assert not (workspace / "out.txt").exists()

    def test_empty_parts_is_rejected_at_load(self, workspace: Path):
        root = _member(workspace, '[[files.compose]]\ndest = "{workspace_root}/o"\nparts = []\n')
        with pytest.raises(SystemExit):
            load_repo_hooks(root)


class TestTrustScreen:
    def test_trust_shows_the_compose_row_and_flags_an_outside_part(self, workspace: Path, tmp_path: Path):
        outside = tmp_path / "outside.md"
        outside.write_text("x")
        root = _member(workspace, _row(["a.md", str(outside)], extra='format = "text"\n'), **{"a.md": "A"})
        result = CliRunner().invoke(app, ["hooks", "trust", "seat", "--workspace-root", str(workspace)])
        assert result.exit_code == 0, result.output
        assert "projection compose dest" in result.output
        assert "a.md" in result.output and "ESCAPE" in result.output


class TestPendingMarkerSurvivesARefusedCompose:
    """A bound member whose first-materialize pass is refused must keep its
    pending marker, so the next pass runs the withheld hooks once. A missing
    part and a non-UTF-8 part are the same case: both must be a refusal the
    materialize pass can recover from, never a raw exception."""

    HOOKS = (
        '[[lifecycle.on_materialize]]\nname = "m"\ncommand = "true"\nwhen = "first_materialize"\n\n'
        '[[files.compose]]\ndest = "{workspace_root}/out.txt"\nparts = ["a.md", "bad.md"]\n'
        'if_exists = "overwrite"\n'
    )

    def _pending(self, workspace: Path, bad: bytes | None) -> Path:
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "seat"\npath = "seat"\nurl = "unused"\n'
            '[[units]]\nname = "default"\npath = "default"\nrepos = ["seat"]\n'
        )
        root = _member(workspace, self.HOOKS, **{"a.md": "A"})
        if bad is not None:
            (root / "bad.md").write_bytes(bad)
        from gr2.python_cli.spec_apply import _run_materialize_hooks

        # the unbound first pass skips and writes the marker
        _run_materialize_hooks(workspace, root, "seat", first_materialize=True)
        return root

    @pytest.mark.parametrize("bad", [None, b"\xff\xfe\x00bad"], ids=["missing-part", "non-utf8-part"])
    def test_marker_survives_the_refusal(self, workspace: Path, bad: bytes | None):
        from gr2.python_cli.consent import pending_marker_path
        from gr2.python_cli.spec_apply import _run_materialize_hooks

        root = self._pending(workspace, bad)
        assert pending_marker_path(workspace, "seat").exists()
        write_consent(workspace, "seat", root)
        with pytest.raises(HookRuntimeError):
            _run_materialize_hooks(workspace, root, "seat", first_materialize=False)
        assert pending_marker_path(workspace, "seat").exists()
        assert not (workspace / "out.txt").exists()
