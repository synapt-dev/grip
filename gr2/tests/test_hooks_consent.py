"""TDD spec for the consent gate: member-hook consent.

The property the tests guard: consent is a LOCAL record on the host, never inside the object
it consents to; keyed by the content hash of the member's hooks table;
it lapses when the hook text changes. Unbound members SKIP AND REPORT
(verb completes, exit 0, the skipped commands and the bind command are
printed) — refusing was ruled out because it breaks the stranger's README
walk. Binding is verb-only (`gr2 hooks trust`), the record file carries a
.json suffix (nested member paths would collide otherwise), granted_by is
the HOST USER, never an agent identity. File projections are gated by the
same record, and `gr2 hooks trust` shows every projection with its
resolved destination, flagging escapes (outside the member tree, under
any .git/, absolute outside the workspace).
"""
from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.consent import (
    consent_path,
    consent_state,
    confinement_violations,
    hooks_sha,
    load_consent,
    remove_consent,
    write_consent,
)
from gr2.python_cli.hooks import (
    FileProjection,
    HookContext,
    HookRuntimeError,
    LifecycleHook,
    RepoHooks,
    apply_file_projections,
    load_repo_hooks,
    run_lifecycle_stage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _make_ctx(workspace: Path, repo_name: str = "stranger") -> HookContext:
    repo_root = workspace / repo_name
    repo_root.mkdir(parents=True, exist_ok=True)
    return HookContext(
        workspace_root=workspace,
        unit_root=workspace / "default",
        lane_root=workspace / "lanes" / "default" / "feat",
        repo_root=repo_root,
        repo_name=repo_name,
        lane_owner="default",
        lane_subject=repo_name,
        lane_name="feat",
    )


def _make_hooks(
    *,
    command: str = "touch {repo_root}/marker",
    stage: str = "on_enter",
    copies: list[FileProjection] | None = None,
    links: list[FileProjection] | None = None,
) -> RepoHooks:
    hook = LifecycleHook(
        stage=stage,
        name="marker",
        command=command,
        cwd="{repo_root}",
        when="always",
        on_failure="block",
    )
    kwargs = {"on_materialize": [], "on_enter": [], "on_exit": []}
    kwargs[stage] = [hook]
    return RepoHooks(
        repo_name="stranger",
        file_links=links or [],
        file_copies=copies or [],
        policy={},
        path=Path("/fake/.gr2/hooks.toml"),
        **kwargs,
    )


def _seed_member(workspace: Path, *, hooks_toml: str) -> Path:
    """A member repo clone carrying .gr2/hooks.toml (the consent subject)."""
    repo_root = workspace / "stranger"
    (repo_root / ".gr2").mkdir(parents=True, exist_ok=True)
    (repo_root / ".gr2" / "hooks.toml").write_text(hooks_toml)
    return repo_root


HOOKS_TOML = """\
[[lifecycle.on_enter]]
name = "marker"
command = "touch {repo_root}/marker"
when = "always"
"""


# ---------------------------------------------------------------------------
# The record: path, fields, host-user identity
# ---------------------------------------------------------------------------

class TestConsentRecord:
    def test_record_path_carries_json_suffix(self, workspace: Path):
        # Nested member paths would collide without the suffix.
        path = consent_path(workspace, "units/u/stranger")
        assert path == workspace / ".grip" / "consent" / "units" / "u" / "stranger.json"

    def test_write_consent_stores_host_user_and_iso_time(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        record = write_consent(workspace, "stranger", repo_root)
        assert record["granted_by"] == getpass.getuser()  # host user, never an agent identity
        assert record["granted_at"]  # ISO-8601 stamp present
        assert record["hooks_sha"] == hooks_sha(repo_root)
        stored = json.loads(consent_path(workspace, "stranger").read_text())
        assert stored == record

    def test_load_consent_roundtrip_and_remove(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        assert load_consent(workspace, "stranger") is None
        record = write_consent(workspace, "stranger", repo_root)
        assert load_consent(workspace, "stranger") == record
        assert remove_consent(workspace, "stranger") is True
        assert load_consent(workspace, "stranger") is None
        assert remove_consent(workspace, "stranger") is False

    def test_state_lapses_when_hook_text_changes(self, workspace: Path):
        # Consent lapses when the hook text changes, because the hash changes.
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        write_consent(workspace, "stranger", repo_root)
        assert consent_state(workspace, "stranger", repo_root)[0] == "bound"
        (repo_root / ".gr2" / "hooks.toml").write_text(
            HOOKS_TOML.replace("marker", "marker2")
        )
        state, record = consent_state(workspace, "stranger", repo_root)
        assert state == "changed"
        assert record is not None  # the old record still names the old hash

    def test_unbound_when_no_record(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        assert consent_state(workspace, "stranger", repo_root)[0] == "unbound"


# ---------------------------------------------------------------------------
# Unbound members SKIP AND REPORT — the verb completes, nothing runs
# ---------------------------------------------------------------------------

class TestUnboundSkip:
    def test_unbound_hook_does_not_run_and_verb_completes(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        ctx = _make_ctx(workspace)
        results = run_lifecycle_stage(
            _make_hooks(), "on_enter", ctx, repo_dirty=False, first_materialize=True
        )
        assert not (repo_root / "marker").exists()  # nothing ran
        assert all(r.status == "unbound" for r in results)
        out = capsys.readouterr().out
        assert "unbound" in out
        assert "touch {repo_root}/marker" in out  # the skipped command is shown
        assert "gr2 hooks trust stranger" in out  # the bind command is shown

    def test_bound_hook_runs(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        write_consent(workspace, "stranger", repo_root)
        ctx = _make_ctx(workspace)
        results = run_lifecycle_stage(
            _make_hooks(), "on_enter", ctx, repo_dirty=False, first_materialize=True
        )
        assert (repo_root / "marker").exists()  # the marker is written
        assert all(r.status == "applied" for r in results)

    def test_changed_hooks_do_not_run_again(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        write_consent(workspace, "stranger", repo_root)
        (repo_root / ".gr2" / "hooks.toml").write_text(
            HOOKS_TOML.replace("marker", "marker2")
        )
        ctx = _make_ctx(workspace)
        results = run_lifecycle_stage(
            _make_hooks(), "on_enter", ctx, repo_dirty=False, first_materialize=True
        )
        assert not (repo_root / "marker").exists()  # the new text is refused
        assert all(r.status == "changed" for r in results)
        out = capsys.readouterr().out
        assert "changed since grant" in out
        assert "gr2 hooks trust stranger" in out

    def test_manual_hooks_stay_subordinate_to_consent(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        # consent is the outer gate: --manual-hooks runs manual hooks only
        # for BOUND members.
        manual_toml = HOOKS_TOML.replace('when = "always"', 'when = "manual"')
        repo_root = _seed_member(workspace, hooks_toml=manual_toml)
        ctx = _make_ctx(workspace)
        results = run_lifecycle_stage(
            _make_hooks(command="touch {repo_root}/marker", stage="on_enter"),
            "on_enter",
            ctx,
            repo_dirty=False,
            first_materialize=True,
            allow_manual=True,
        )
        assert not (repo_root / "marker").exists()  # unbound: nothing ran, flag or not
        assert all(r.status == "unbound" for r in results)
        capsys.readouterr()


# ---------------------------------------------------------------------------
# A consent-shaped section in the hooks table is ignored and reported
# ---------------------------------------------------------------------------

class TestConsentInSectionIgnored:
    def test_consent_section_never_binds_and_is_reported(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        toml = HOOKS_TOML + '\n[consent]\ngranted_by = "layne"\n'
        repo_root = _seed_member(workspace, hooks_toml=toml)
        hooks = load_repo_hooks(repo_root)
        assert hooks is not None
        assert hooks.ignored_consent_keys  # collected, not silently dropped
        # the pre-consented section must not create a record...
        ctx = _make_ctx(workspace)
        results = run_lifecycle_stage(
            load_repo_hooks(repo_root), "on_enter", ctx, repo_dirty=False, first_materialize=True
        )
        assert not (repo_root / "marker").exists()  # still unbound
        assert all(r.status == "unbound" for r in results)
        out = capsys.readouterr().out
        assert "consent" in out  # ...and it is REPORTED, not silently ignored


# ---------------------------------------------------------------------------
# Projections: same record, skip-and-report, escape flags at bind time
# ---------------------------------------------------------------------------

class TestProjectionGate:
    def _copy(self, dest: str) -> FileProjection:
        return FileProjection(kind="copy", src="payload.txt", dest=dest, if_exists="overwrite")

    def _seed_payload(self, repo_root: Path) -> None:
        (repo_root / "payload.txt").write_text("payload\n")

    def test_unbound_projection_writes_nothing(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        self._seed_payload(repo_root)
        ctx = _make_ctx(workspace)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/escape.txt")])
        results = apply_file_projections(hooks, ctx)
        assert not (workspace / "escape.txt").exists()  # nothing written
        assert all(r.status == "unbound" for r in results)
        out = capsys.readouterr().out
        assert "unbound" in out

    def test_bound_projection_writes_within_the_member(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        self._seed_payload(repo_root)
        write_consent(workspace, "stranger", repo_root)
        ctx = _make_ctx(workspace)
        hooks = _make_hooks(copies=[self._copy("{repo_root}/local.txt")])
        results = apply_file_projections(hooks, ctx)
        assert (repo_root / "local.txt").exists()
        assert all(r.status == "applied" for r in results)


class TestConfinement:
    """Confinement is IN the gate, not a follow-up.

    Consent answers whether this repo may act; confinement answers where
    (measured 2026-09-24). Every projection dest RESOLVES (symlinks
    included): inside the workspace root (the root itself is inside — the
    root and {unit_root} are designed surfaces), never under any .git,
    never inside ANOTHER member's tree; for [[files.link]] the link's
    TARGET must resolve inside the member's own tree. A violating row is
    REFUSED and REPORTED even when consent is GRANTED. There is no blanket
    absolute-dest refusal: an absolute dest that resolves inside the
    boundary is allowed.
    """

    def _copy(self, dest: str) -> FileProjection:
        return FileProjection(kind="copy", src="payload.txt", dest=dest, if_exists="overwrite")

    def _seed_payload(self, repo_root: Path) -> None:
        (repo_root / "payload.txt").write_text("payload\n")

    def _bound_ctx(self, workspace: Path, *, second_member: bool = False) -> tuple[Path, HookContext]:
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
            + ('[[repos]]\nname = "trusted"\npath = "trusted"\nurl = "unused"\n' if second_member else "")
            + '[[units]]\nname = "default"\npath = "default"\nrepos = ["stranger"]\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        self._seed_payload(repo_root)
        write_consent(workspace, "stranger", repo_root)
        if second_member:
            (workspace / "trusted").mkdir(exist_ok=True)
        return repo_root, _make_ctx(workspace)

    def test_workspace_root_write_is_allowed(self, workspace: Path):
        # the workspace root itself is inside the boundary —
        # projecting to the root is the designed purpose.
        repo_root, ctx = self._bound_ctx(workspace)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/projection-escape.txt")])
        results = apply_file_projections(hooks, ctx)
        assert (workspace / "projection-escape.txt").exists()
        assert all(r.status == "applied" for r in results)

    def test_git_dir_write_refused_even_when_bound(self, workspace: Path):
        # fixture case 2: never under any .git.
        repo_root, ctx = self._bound_ctx(workspace)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/.git/git-marker")])
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (workspace / ".git" / "git-marker").exists()

    def test_absolute_dest_outside_workspace_refused(self, workspace: Path, tmp_path: Path):
        # fixture case 3: a symlink at an arbitrary absolute path — refused
        # by RESOLVED confinement (no blanket absolute rule; an absolute
        # dest that resolves inside the boundary is allowed).
        repo_root, ctx = self._bound_ctx(workspace)
        hooks = _make_hooks(
            copies=[FileProjection(kind="link", src="payload.txt", dest=str(tmp_path / "abs-escape.txt"), if_exists="overwrite")]
        )
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (tmp_path / "abs-escape.txt").exists()

    def test_refusal_reports_dest_and_rule(self, workspace: Path, capsys: pytest.CaptureFixture):
        repo_root, ctx = self._bound_ctx(workspace)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/.git/git-marker")])
        with pytest.raises(HookRuntimeError) as excinfo:
            apply_file_projections(hooks, ctx)
        payload = json.loads(str(excinfo.value))
        assert payload["status"] == "refused"
        assert "confinement" in payload["detail"]

    def test_symlink_dest_resolving_outside_is_refused(self, workspace: Path, tmp_path: Path):
        # symlinks included: an existing symlink inside the member that
        # points outside must not launder the write.
        repo_root, ctx = self._bound_ctx(workspace)
        self._seed_payload(repo_root)
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        link = repo_root / "laundered"
        link.symlink_to(outside)
        hooks = _make_hooks(copies=[self._copy("laundered/probe.txt")])
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (outside / "probe.txt").exists()

    def test_sibling_member_write_refused_even_when_bound(self, workspace: Path):
        # the gap the boundary exists for: a stranger member
        # must not rewrite a trusted member's files, consent or not.
        repo_root, ctx = self._bound_ctx(workspace, second_member=True)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/trusted/README.md")])
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (workspace / "trusted" / "README.md").exists()

    def test_link_target_outside_member_tree_refused(self, workspace: Path):
        # the link's TARGET, not only its location, must resolve inside the
        # member's own tree: a consented link in the workspace pointing at
        # an absolute outside path exposes that path to every reader.
        repo_root, ctx = self._bound_ctx(workspace)
        self._seed_payload(repo_root)
        outside = repo_root.parent.parent  # above the workspace root
        hooks = _make_hooks(
            links=[FileProjection(kind="link", src=str(outside / "secret.txt"), dest="{repo_root}/linked.md", if_exists="overwrite")]
        )
        (outside / "secret.txt").write_text("outside\n")
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (repo_root / "linked.md").exists()

    def test_refused_projection_reports_the_withheld_lifecycle_hooks(self, workspace: Path):
        # a bound member whose hooks table carries a refused projection row:
        # the row is refused (nothing written), AND the member's on_materialize
        # hooks do not run — the refusal must say that the hooks were withheld
        # (measured 2026-09-24: the bound materialize printed only the refusal
        # and said nothing about the consented hook it silently skipped).
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
            '[[units]]\nname = "default"\npath = "default"\nrepos = ["stranger"]\n'
        )
        both_rows = (
            '[[lifecycle.on_materialize]]\nname = "run-marker"\n'
            'command = "touch {repo_root}/hook-ran.txt"\nwhen = "always"\n\n'
            '[[files.copy]]\nsrc = "payload.txt"\n'
            'dest = "{workspace_root}/.grip/consent/planted.json"\nif_exists = "overwrite"\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=both_rows)
        self._seed_payload(repo_root)
        write_consent(workspace, "stranger", repo_root)  # binds the CURRENT table hash
        from gr2.python_cli.spec_apply import _run_materialize_hooks

        with pytest.raises(HookRuntimeError) as excinfo:
            _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        payload = json.loads(str(excinfo.value))
        assert payload["status"] == "refused"
        assert payload["lifecycle_hooks_withheld"] == [
            "run-marker: touch {repo_root}/hook-ran.txt"
        ]
        assert not (workspace / ".grip" / "consent" / "planted.json").exists()
        assert not (repo_root / "hook-ran.txt").exists()  # withheld, not run

    def test_withheld_list_filters_by_the_when_rules(self, workspace: Path):
        # a first_materialize hook on a later materialize was never due: the
        # withheld list must not name hooks the run itself would have skipped
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
            '[[units]]\nname = "default"\npath = "default"\nrepos = ["stranger"]\n'
        )
        first_only = (
            '[[lifecycle.on_materialize]]\nname = "first-only"\n'
            'command = "touch {repo_root}/hook-ran.txt"\nwhen = "first_materialize"\n\n'
            '[[files.copy]]\nsrc = "payload.txt"\n'
            'dest = "{workspace_root}/.grip/consent/planted.json"\nif_exists = "overwrite"\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=first_only)
        self._seed_payload(repo_root)
        write_consent(workspace, "stranger", repo_root)
        from gr2.python_cli.spec_apply import _run_materialize_hooks

        with pytest.raises(HookRuntimeError) as excinfo:
            _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        payload = json.loads(str(excinfo.value))
        assert payload["lifecycle_hooks_withheld"] == []  # not due here

    def test_refused_path_puts_the_pending_marker_back(self, workspace: Path):
        # the late-trust sequence (measured through the CLI 2026-09-24): a
        # member owing a pending first_materialize hook whose table ALSO
        # carries a refused row — the pending pass consumes the marker, the
        # row raises, and until the put-back the user who fixed the row and
        # re-trusted NEVER got the deferred hook (apply #2 said "no changes
        # applied"). The refused path must put the marker back so the
        # deferred hook survives the refusal.
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
            '[[units]]\nname = "default"\npath = "default"\nrepos = ["stranger"]\n'
        )
        bad_table = (
            '[[lifecycle.on_materialize]]\nname = "first-only"\n'
            'command = "touch {repo_root}/hook-ran.txt"\nwhen = "first_materialize"\n\n'
            '[[files.copy]]\nsrc = "payload.txt"\n'
            'dest = "{workspace_root}/.grip/consent/planted.json"\nif_exists = "overwrite"\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=bad_table)
        self._seed_payload(repo_root)
        from gr2.python_cli.spec_apply import _run_materialize_hooks
        from gr2.python_cli import consent as _consent

        # M1 unbound: the consent gate skips and writes the pending marker
        _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        assert _consent.pending_members(workspace) == ["stranger"]
        write_consent(workspace, "stranger", repo_root)  # binds the BAD table

        # apply #1: the pending pass consumes the marker, the row raises
        with pytest.raises(HookRuntimeError) as excinfo:
            _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=True)
        payload = json.loads(str(excinfo.value))
        assert payload["lifecycle_hooks_withheld"] == ["first-only: touch {repo_root}/hook-ran.txt"]
        assert not (workspace / ".grip" / "consent" / "planted.json").exists()
        # the refused path put the marker back
        assert _consent.pending_members(workspace) == ["stranger"], (
            "the deferred hook must survive the refusal"
        )
        # the user's path: fix the row, re-trust, apply again -> the hook runs
        clean_table = (
            '[[lifecycle.on_materialize]]\nname = "first-only"\n'
            'command = "touch {repo_root}/hook-ran.txt"\nwhen = "first_materialize"\n'
        )
        (repo_root / ".gr2" / "hooks.toml").write_text(clean_table)
        write_consent(workspace, "stranger", repo_root)
        _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        assert (repo_root / "hook-ran.txt").exists(), (
            "the deferred hook runs once the row is fixed and the member is re-trusted"
        )

    def test_blocked_row_advice_says_nothing_about_retrust(self, workspace: Path):
        # the same except also catches BLOCKED rows (missing source, an
        # if_exists=error conflict, merge not implemented): for a conflict the
        # table does not change, so the advice must not tell the user to
        # re-trust — the row's own detail says what to fix.
        repo_root, ctx = self._bound_ctx(workspace)
        self._seed_payload(repo_root)
        (repo_root / "already-there.txt").write_text("present\n")
        hooks = _make_hooks(
            stage="on_materialize",
            command="touch {repo_root}/hook-ran.txt",
            copies=[FileProjection(kind="copy", src="payload.txt", dest="{repo_root}/already-there.txt", if_exists="error")],
        )
        with pytest.raises(HookRuntimeError) as excinfo:
            from gr2.python_cli.hooks import run_materialize_hook_block

            run_materialize_hook_block(
                hooks, ctx, repo_dirty=False, first_materialize=True, allow_manual=False
            )
        payload = json.loads(str(excinfo.value))
        assert payload["status"] == "blocked"
        assert "re-trust" not in payload["lifecycle_hooks_withheld_detail"]
        assert "conflict at" in payload["lifecycle_hooks_withheld_detail"]
        assert payload["lifecycle_hooks_withheld"] == ["marker: touch {repo_root}/hook-ran.txt"]

    def test_gate_runs_once_for_a_bound_member_with_no_marker(self, workspace: Path):
        # "runs the consent gate ONCE" is a claim about the most common state:
        # a bound member with NO pending marker. gate=None means both
        # "bound clean" and "not supplied", so an overloaded-None callee calls
        # the gate again — three calls through the helper. The sentinel makes
        # the single call true, not documented.
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        write_consent(workspace, "stranger", repo_root)
        from gr2.python_cli.spec_apply import _run_materialize_hooks
        from gr2.python_cli import hooks as _hooks

        calls: list[object] = []
        real_gate = _hooks._consent_gate

        def spy(ctx):
            calls = spy.count = getattr(spy, "count", 0) + 1
            return real_gate(ctx)

        spy.count = 0
        import inspect

        def spying(ctx):
            spy.count += 1
            return real_gate(ctx)

        _hooks._consent_gate = spying
        try:
            _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        finally:
            _hooks._consent_gate = real_gate
        assert spy.count == 1, (
            f"the consent gate ran {spy.count} times for a bound member with no marker"
        )

    def test_filter_names_a_pending_first_materialize_hook_on_a_refused_row(self, workspace: Path):
        # the filter half of the pending claim needs its own witness: a bound
        # member owing a pending first_materialize hook, with a refused row,
        # entering through a first_materialize=False door — the withheld list
        # must name the hook (the pending bool ORs the filter), even though
        # today's CLI never reaches this state through a real door.
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
            '[[units]]\nname = "default"\npath = "default"\nrepos = ["stranger"]\n'
        )
        bad_table = (
            '[[lifecycle.on_materialize]]\nname = "first-only"\n'
            'command = "touch {repo_root}/hook-ran.txt"\nwhen = "first_materialize"\n\n'
            '[[files.copy]]\nsrc = "payload.txt"\n'
            'dest = "{workspace_root}/.grip/consent/planted.json"\nif_exists = "overwrite"\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=bad_table)
        self._seed_payload(repo_root)
        from gr2.python_cli.spec_apply import _run_materialize_hooks
        from gr2.python_cli import consent as _consent

        # M1 unbound: the gate writes the pending marker
        _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        assert _consent.pending_members(workspace) == ["stranger"]
        write_consent(workspace, "stranger", repo_root)  # binds the BAD table
        # a first_materialize=False door with a pending marker + a refused row
        with pytest.raises(HookRuntimeError) as excinfo:
            _run_materialize_hooks(workspace, repo_root, "stranger", first_materialize=False)
        payload = json.loads(str(excinfo.value))
        assert payload["lifecycle_hooks_withheld"] == [
            "first-only: touch {repo_root}/hook-ran.txt"
        ]
        # the marker is kept: the deferred hook survives the refusal
        assert _consent.pending_members(workspace) == ["stranger"]

    def test_declared_unit_dir_dest_allowed(self, workspace: Path):
        # {unit_root} and the spec's unit dirs are inside the boundary
        # (gr2 declares and owns it).
        repo_root, ctx = self._bound_ctx(workspace)
        self._seed_payload(repo_root)
        hooks = _make_hooks(copies=[self._copy("{workspace_root}/default/out.txt")])
        results = apply_file_projections(hooks, ctx)
        assert (workspace / "default" / "out.txt").exists()
        assert all(r.status == "applied" for r in results)


class TestEscapeFlags:
    """`gr2 hooks trust` shows resolved destinations and flags escapes
    (the user sees file/link escapes BEFORE binding).
    The trust-time classifier is the same check the runtime gate raises on.
    """

    def _member(self, workspace: Path) -> Path:
        return _seed_member(workspace, hooks_toml=HOOKS_TOML)

    def test_no_flag_for_workspace_root_dest(self, workspace: Path):
        # the root is inside the boundary.
        repo_root = self._member(workspace)
        flags = confinement_violations(
            "{workspace_root}/escape.txt",
            repo_root,
            workspace,
            rendered=str(workspace / "escape.txt"),
        )
        assert flags == []

    def test_flag_dest_under_any_git(self, workspace: Path):
        repo_root = self._member(workspace)
        flags = confinement_violations(
            "{workspace_root}/.git/probe",
            repo_root,
            workspace,
            rendered=str(workspace / ".git" / "probe"),
        )
        assert any(".git" in f for f in flags)

    def test_flag_dest_outside_workspace(self, workspace: Path, tmp_path: Path):
        repo_root = self._member(workspace)
        flags = confinement_violations(
            "{workspace_root}/../escape.txt",
            repo_root,
            workspace,
            rendered=str(tmp_path / "escape.txt"),
        )
        assert any("outside the workspace root" in f for f in flags)

    def test_flag_dest_inside_sibling_member(self, workspace: Path):
        repo_root = self._member(workspace)
        sibling = workspace / "trusted"
        sibling.mkdir(exist_ok=True)
        flags = confinement_violations(
            "{workspace_root}/trusted/README.md",
            repo_root,
            workspace,
            rendered=str(sibling / "README.md"),
            sibling_member_roots=[sibling],
        )
        assert any("another member" in f for f in flags)

    def test_flag_link_target_outside_member_tree(self, workspace: Path, tmp_path: Path):
        repo_root = self._member(workspace)
        (tmp_path / "secret.txt").write_text("x\n")
        flags = confinement_violations(
            "{repo_root}/linked.md",
            repo_root,
            workspace,
            rendered=str(repo_root / "linked.md"),
            kind="link",
            link_target=str(tmp_path / "secret.txt"),
        )
        assert any("link target" in f for f in flags)

    def test_no_flag_for_member_internal_dest(self, workspace: Path):
        repo_root = self._member(workspace)
        flags = confinement_violations("{repo_root}/local.txt", repo_root, workspace, rendered=None)
        assert flags == []


# ---------------------------------------------------------------------------
# CLI: binding is verb-only, shows what is consented to before it binds
# ---------------------------------------------------------------------------

class TestTrustVerb:
    def _spec(self, workspace: Path) -> None:
        grip_dir = workspace / ".grip"
        grip_dir.mkdir(parents=True, exist_ok=True)
        (grip_dir / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n'
            '[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
        )

    def _seed(self, workspace: Path, hooks_toml: str = HOOKS_TOML) -> Path:
        self._spec(workspace)
        return _seed_member(workspace, hooks_toml=hooks_toml)

    def test_trust_shows_commands_and_resolved_destinations_then_binds(
        self, workspace: Path, tmp_path: Path
    ):
        toml = (
            HOOKS_TOML
            + "\n[[files.copy]]\nname = \"escape\"\nsrc = \"payload.txt\"\n"
            + f'dest = "{tmp_path}/abs-escape.txt"\nif_exists = "overwrite"\n'
            + '\n[consent]\ngranted_by = "layne"\n'
        )
        repo_root = _seed_member(workspace, hooks_toml=toml)
        (repo_root / "payload.txt").write_text("payload\n")
        self._spec(workspace)
        result = CliRunner().invoke(
            app,
            ["hooks", "trust", "stranger", "--workspace-root", str(workspace)],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert "touch {repo_root}/marker" in out  # the lifecycle command
        assert f"{tmp_path}/abs-escape.txt" in out  # the RESOLVED destination
        assert "consent" in out  # the ignored consent-shaped section is reported
        record = load_consent(workspace, "stranger")
        assert record is not None  # the record was written
        assert record["granted_by"] == getpass.getuser()

    def test_trust_rejects_unknown_member(self, workspace: Path):
        self._spec(workspace)
        result = CliRunner().invoke(
            app,
            ["hooks", "trust", "nope", "--workspace-root", str(workspace)],
        )
        assert result.exit_code != 0

    def test_revoke_removes_the_record(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        self._spec(workspace)
        write_consent(workspace, "stranger", repo_root)
        result = CliRunner().invoke(
            app,
            ["hooks", "revoke", "stranger", "--workspace-root", str(workspace)],
        )
        assert result.exit_code == 0, result.output
        assert load_consent(workspace, "stranger") is None

    def test_no_trust_hooks_flag_exists(self, workspace: Path):
        # binding is verb-only; the inline flag must NOT exist.
        self._spec(workspace)
        result = CliRunner().invoke(
            app,
            ["workspace", "materialize", str(workspace), "--trust-hooks"],
        )
        assert result.exit_code != 0

    def test_unresolvable_dest_refused_at_trust_time(self, workspace: Path):
        # a consented root write is
        # allowed only because the trust screen shows the RESOLVED
        # destination; a row that cannot resolve at trust time is refused,
        # NOT deferred, and no record exists (the record binds the whole
        # table by hash — there is no partial bind of unseen rows).
        toml = (
            HOOKS_TOML
            + '\n[[files.copy]]\nname = "lane-dependent"\nsrc = "payload.txt"\n'
            + 'dest = "{lane_root}/out.txt"\nif_exists = "overwrite"\n'
        )
        self._seed(workspace, hooks_toml=toml)
        result = CliRunner().invoke(
            app,
            ["hooks", "trust", "stranger", "--workspace-root", str(workspace)],
        )
        assert result.exit_code != 0, result.output
        assert "refused at trust time" in result.output
        assert "lane_root" in result.output
        assert load_consent(workspace, "stranger") is None  # no record exists

    def test_unset_variable_dest_refused_at_trust_time(self, workspace: Path):
        toml = (
            HOOKS_TOML
            + '\n[[files.copy]]\nname = "unset-var"\nsrc = "payload.txt"\n'
            + 'dest = "{no_such_variable}/out.txt"\nif_exists = "overwrite"\n'
        )
        self._seed(workspace, hooks_toml=toml)
        result = CliRunner().invoke(
            app,
            ["hooks", "trust", "stranger", "--workspace-root", str(workspace)],
        )
        assert result.exit_code != 0, result.output
        assert load_consent(workspace, "stranger") is None


class TestStatusSurfaces:
    """Materialize/lane-create print the unbound state once (the
    skip report); `workspace status` KEEPS printing it — unbound and clean
    never print the same nothing."""

    def _seed(self, workspace: Path, hooks_toml: str = HOOKS_TOML) -> Path:
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
        )
        return _seed_member(workspace, hooks_toml=hooks_toml)

    def test_repo_status_prints_unbound_member(self, workspace: Path):
        self._seed(workspace)
        result = CliRunner().invoke(app, ["repo", "status", str(workspace)])
        assert result.exit_code == 0, result.output
        assert "hooks unbound stranger" in result.output

    def test_repo_status_prints_changed_member(self, workspace: Path):
        repo_root = self._seed(workspace)
        write_consent(workspace, "stranger", repo_root)
        (repo_root / ".gr2" / "hooks.toml").write_text(HOOKS_TOML.replace("marker", "marker2"))
        result = CliRunner().invoke(app, ["repo", "status", str(workspace)])
        assert result.exit_code == 0, result.output
        assert "hooks changed since grant stranger" in result.output

    def test_repo_status_prints_nothing_for_bound_member(self, workspace: Path):
        repo_root = self._seed(workspace)
        write_consent(workspace, "stranger", repo_root)
        result = CliRunner().invoke(app, ["repo", "status", str(workspace)])
        assert result.exit_code == 0, result.output
        assert "hooks unbound" not in result.output
        assert "hooks changed" not in result.output

    def test_hooks_status_verb_reports_state_per_member(self, workspace: Path):
        repo_root = self._seed(workspace)
        result = CliRunner().invoke(app, ["hooks", "status", "--workspace-root", str(workspace)])
        assert result.exit_code == 0, result.output
        assert "unbound" in result.output
        write_consent(workspace, "stranger", repo_root)
        result = CliRunner().invoke(app, ["hooks", "status", "--workspace-root", str(workspace)])
        assert "bound" in result.output
        assert "unbound" not in result.output

# ---------------------------------------------------------------------------
# the three consented-attack probes, each a
# regression row refused WITH consent granted, one mutation per rule.
# ---------------------------------------------------------------------------

class TestStateAreaBoundary:
    """A bound member must not write into gr2's own state area (.grip): a
    forged consent record there binds another member, and an
    overwritten workspace_spec.toml repoints every member's url."""

    def _bound(self, workspace: Path, second: bool = True) -> tuple[Path, HookContext]:
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        spec = 'workspace_name = "ws"\n\n[[repos]]\nname = "a"\npath = "a"\nurl = "unused"\n'
        if second:
            spec += '[[repos]]\nname = "b"\npath = "b"\nurl = "unused"\n'
        (workspace / ".grip" / "workspace_spec.toml").write_text(spec)
        repo_root = workspace / "a"
        (repo_root / ".gr2").mkdir(parents=True, exist_ok=True)
        (repo_root / ".gr2" / "hooks.toml").write_text(HOOKS_TOML)
        (repo_root / "payload.txt").write_text("payload\n")
        write_consent(workspace, "a", repo_root)
        (workspace / "b").mkdir(exist_ok=True)
        return repo_root, _make_ctx(workspace, repo_name="a")

    def test_forged_consent_record_dest_refused(self, workspace: Path):
        # member a copies a forged record into .grip/consent/b.json —
        # member b must not go from unbound to bound through a projection.
        repo_root, ctx = self._bound(workspace)
        hooks = _make_hooks(
            copies=[FileProjection(kind="copy", src="payload.txt", dest="{workspace_root}/.grip/consent/b.json", if_exists="overwrite")]
        )
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (workspace / ".grip" / "consent" / "b.json").exists()

    def test_spec_overwrite_dest_refused(self, workspace: Path):
        # member a overwrites .grip/workspace_spec.toml, repointing b's
        # url at the attacker's.
        repo_root, ctx = self._bound(workspace)
        hooks = _make_hooks(
            copies=[FileProjection(kind="copy", src="payload.txt", dest="{workspace_root}/.grip/workspace_spec.toml", if_exists="overwrite")]
        )
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        spec = (workspace / ".grip" / "workspace_spec.toml").read_text()
        assert 'name = "a"' in spec  # the spec is untouched

    def test_symlinked_copy_src_refused(self, workspace: Path, tmp_path: Path):
        # a copy whose src is a symlink to a file outside the workspace
        # must not copy those bytes into the workspace.
        repo_root, ctx = self._bound(workspace)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("outside bytes\n")
        smuggle = repo_root / "smuggled.txt"
        smuggle.symlink_to(outside)
        hooks = _make_hooks(
            copies=[FileProjection(kind="copy", src="smuggled.txt", dest="{repo_root}/copied.txt", if_exists="overwrite")]
        )
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        assert not (repo_root / "copied.txt").exists()


class TestPendingFirstMaterialize:
    """The hooks skipped on the unbound first materialize write a
    pending marker; the NEXT bound materialize runs them once — never
    rm -rf."""

    def _workspace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "ws"
        (ws / ".grip").mkdir(parents=True)
        (ws / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\nurl = "unused"\n'
        )
        repo_root = _seed_member(ws, hooks_toml=HOOKS_TOML)
        import subprocess

        subprocess.run(["git", "init", "-q", str(repo_root)], check=True)  # the spec validator wants a git repo
        return ws, repo_root

    def test_unbound_skip_writes_pending_marker(self, workspace: Path):
        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        run_lifecycle_stage(
            load_repo_hooks(repo_root), "on_enter", _make_ctx(workspace),
            repo_dirty=False, first_materialize=True,
        )
        from gr2.python_cli.consent import pending_marker_path

        assert pending_marker_path(workspace, "stranger").exists()

    def test_bound_run_consumes_marker_and_ors_first_materialize(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ):
        from gr2.python_cli.consent import pending_marker_path, take_pending_marker

        repo_root = _seed_member(workspace, hooks_toml=HOOKS_TOML)
        run_lifecycle_stage(
            load_repo_hooks(repo_root), "on_enter", _make_ctx(workspace),
            repo_dirty=False, first_materialize=False,  # NOT first materialize
        )
        assert pending_marker_path(workspace, "stranger").exists()
        write_consent(workspace, "stranger", repo_root)
        # the next bound run: the marker ORs first_materialize (a
        # when=first_materialize hook runs exactly once here)
        first_toml = HOOKS_TOML.replace('when = "always"', 'when = "first_materialize"')
        (repo_root / ".gr2" / "hooks.toml").write_text(first_toml)
        write_consent(workspace, "stranger", repo_root)  # re-bind the new text
        results = run_lifecycle_stage(
            load_repo_hooks(repo_root), "on_enter", _make_ctx(workspace),
            repo_dirty=False, first_materialize=False,
        )
        assert (repo_root / "marker").exists()  # ran despite first_materialize=False
        assert all(r.status == "applied" for r in results)
        assert not pending_marker_path(workspace, "stranger").exists()  # consumed, runs ONCE
        take_pending_marker(workspace, "stranger")  # no-op if absent

    def test_apply_plan_runs_pending_hooks_on_a_converged_materialize(self, tmp_path: Path):
        # the e2e shape, with no rm -rf anywhere: first materialize (clone;
        # hooks skip unbound and write the marker), bind, second materialize
        # (converged, no ops) — the pending pass runs the member's hooks once.
        import subprocess
        from gr2.python_cli.consent import pending_marker_path
        from gr2.python_cli.spec_apply import apply_plan

        # a bare origin carrying the hooks table
        src = tmp_path / "src"
        src.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "config", "user.email", "t@e.invalid"], check=True)
        subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
        (src / ".gr2").mkdir(parents=True, exist_ok=True)
        (src / ".gr2" / "hooks.toml").write_text(
            '[[lifecycle.on_materialize]]\nname = "marker"\n'
            'command = "touch {repo_root}/marker"\nwhen = "first_materialize"\n'
        )
        subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(src), "commit", "-qm", "hooks"], check=True)
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(origin)], check=True)

        ws = tmp_path / "ws2"
        (ws / ".grip").mkdir(parents=True)
        # no pre-seeded member dir: clone_repo must do the materialization,
        # whose unbound hook pass writes the pending marker
        (ws / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "stranger"\npath = "stranger"\n'
            f'url = "{origin}"\n'
        )
        apply_plan(ws, yes=True)  # first materialize: clone lands, hooks skip unbound
        cloned = ws / "stranger"
        assert cloned.exists()
        assert pending_marker_path(ws, "stranger").exists()

        write_consent(ws, "stranger", cloned)  # bind AFTER the hooks table arrived
        payload = apply_plan(ws, yes=True)  # converged, no ops: the pending pass runs
        assert (cloned / "marker").exists()  # the deferred hook ran once
        assert not pending_marker_path(ws, "stranger").exists()
        assert any("pending hooks" in line for line in payload["applied"])


# ---------------------------------------------------------------------------
# macOS volumes are case-insensitive and every
# boundary compared strings — .GRIP, .Grip, .GIT and a case-variant sibling
# name walked straight through. The ONE comparison form is NFC then
# casefold, on both sides, screen and runtime alike. Each row carries a
# precondition guard that skips where the volume is case-sensitive; the
# mutation is dropping the casefold.
# ---------------------------------------------------------------------------

def _volume_case_insensitive(tmp_path: Path) -> bool:
    a = tmp_path / "casetest"
    a.write_text("lower")
    b = tmp_path / "CaseTest"
    try:
        b.write_text("upper")
    except OSError:
        return False
    return a.read_text() == "upper"  # the same file: case-insensitive


class TestCaseBoundary:
    def _bound_a(self, tmp_path: Path, workspace: Path) -> tuple[Path, HookContext]:
        (workspace / ".grip").mkdir(parents=True, exist_ok=True)
        (workspace / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "ws"\n\n[[repos]]\nname = "a"\npath = "a"\nurl = "unused"\n'
            '[[repos]]\nname = "b"\npath = "b"\nurl = "unused"\n'
        )
        repo_root = workspace / "a"
        (repo_root / ".gr2").mkdir(parents=True, exist_ok=True)
        (repo_root / ".gr2" / "hooks.toml").write_text(HOOKS_TOML)
        (repo_root / "payload.txt").write_text("payload\n")
        write_consent(workspace, "a", repo_root)
        (workspace / "b").mkdir(exist_ok=True)
        return repo_root, _make_ctx(workspace, repo_name="a")

    def _refused(self, tmp_path: Path, workspace: Path, dest: str, kind: str = "copy"):
        if not _volume_case_insensitive(tmp_path):
            # these case-variant rows are only constructible where casefold
            # maps distinct spellings onto one directory (macOS/HFS volumes);
            # elsewhere the row cannot exist, so the row skips rather than fails
            pytest.skip("these rows need a case-insensitive volume")
        repo_root, ctx = self._bound_a(tmp_path, workspace)
        hooks = _make_hooks(
            copies=[] if kind == "link" else [FileProjection(kind="copy", src="payload.txt", dest=dest, if_exists="overwrite")],
            links=[] if kind == "copy" else [FileProjection(kind="link", src="payload.txt", dest=dest, if_exists="overwrite")],
        )
        with pytest.raises(HookRuntimeError):
            apply_file_projections(hooks, ctx)
        return workspace

    def test_upper_grip_consent_record_refused(self, tmp_path: Path, workspace: Path):
        self._refused(tmp_path, workspace, "{workspace_root}/.GRIP/consent/b.json")
        assert not (workspace / ".GRIP" / "consent" / "b.json").exists()
        assert not (workspace / ".grip" / "consent" / "b.json").exists()

    def test_mixed_grip_spec_refused(self, tmp_path: Path, workspace: Path):
        self._refused(tmp_path, workspace, "{workspace_root}/.Grip/workspace_spec.toml")
        spec = (workspace / ".grip" / "workspace_spec.toml").read_text()
        assert 'name = "a"' in spec  # the spec is untouched

    def test_upper_git_hooks_refused(self, tmp_path: Path, workspace: Path):
        self._refused(tmp_path, workspace, "{workspace_root}/.GIT/hooks/post-checkout")
        assert not (workspace / ".git" / "hooks" / "post-checkout").exists()

    def test_case_variant_sibling_refused(self, tmp_path: Path, workspace: Path):
        self._refused(tmp_path, workspace, "{workspace_root}/B/planted.txt")
        # on a case-insensitive volume B and b are the SAME directory, so the
        # fruit check is the planted file: absent means the write never ran.
        assert not (workspace / "b" / "planted.txt").exists()
