"""Executable contract for the socket-isolated tmux launch runtime.

This file names a runtime interface rather than making tmux itself the calling
seam:

    launch_team(..., runtime=TmuxPaneRuntime(socket_path=..., session_name=...))

The load-bearing claims are proved from the other side of the process boundary.
A successful tmux command is not launch evidence.  The tests query the pane that
actually exists, compare its cwd and PID with the returned handle, and prove a
client on another socket cannot resolve the launched session.

The command recorder is also deliberate.  ``-S`` is a property of EVERY tmux
operation, not a guard a caller may remember on most paths.  The rollback test
records the failure path separately so deleting socket threading from cleanup
cannot hide behind a green happy path.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gr2.python_cli import launch_exec

TMUX = shutil.which("tmux")
DECLARED_VALUE = "TMUX-DECLARED-VALUE-NOT-FOR-DISK"
AMBIENT_KEY = "SYNAPT_TMUX_AMBIENT_MUST_NOT_INHERIT"
DECLARED_KEY = "DECLARED_RUNTIME_VALUE"


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same-user tmux should be inspectable
        return True
    return True


def _terminate_process(pid: int) -> None:
    if _pid_is_alive(pid):
        os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not _pid_is_alive(pid):
            return
        time.sleep(0.02)


def _test_client_env() -> dict[str, str]:
    """A tmux client environment that cannot fall back through $TMUX."""
    keys = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    assert TMUX is not None
    return subprocess.run(
        [TMUX, *args],
        env=_test_client_env(),
        capture_output=True,
        text=True,
        check=check,
    )


def _write_tmux_proxy(
    path: Path,
    *,
    real_tmux: str,
    log_path: Path,
    sabotage_marker: Path | None = None,
    fail_kill_server: bool = False,
    acknowledge_kill_without_stopping: bool = False,
    unlink_socket_without_stopping: bool = False,
    pre_kill_evidence_path: Path | None = None,
) -> None:
    """Record every argv then exec real tmux.

    When ``sabotage_marker`` exists, the proxy kills one unit pane immediately
    before the runtime removes its bootstrap pane.  That creates the exact
    between-observations race the success path must close: a unit was alive at
    the first observation and is gone after bootstrap removal.
    """
    sabotage = repr(str(sabotage_marker)) if sabotage_marker is not None else "None"
    evidence = repr(str(pre_kill_evidence_path)) if pre_kill_evidence_path else "None"
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import subprocess
import sys

REAL_TMUX = {real_tmux!r}
LOG_PATH = {str(log_path)!r}
SABOTAGE_MARKER = {sabotage}
FAIL_KILL_SERVER = {fail_kill_server!r}
ACKNOWLEDGE_KILL_WITHOUT_STOPPING = {acknowledge_kill_without_stopping!r}
UNLINK_SOCKET_WITHOUT_STOPPING = {unlink_socket_without_stopping!r}
PRE_KILL_EVIDENCE = {evidence}
args = sys.argv[1:]
with open(LOG_PATH, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

if PRE_KILL_EVIDENCE and "kill-server" in args:
    socket_path = args[args.index("-S") + 1]
    client = subprocess.run(
        [REAL_TMUX, "-S", socket_path, "list-panes", "-a"],
        capture_output=True,
        text=True,
        check=False,
    )
    server_pid = int(
        subprocess.run(
            [REAL_TMUX, "-S", socket_path, "display-message", "-p", "#{{pid}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    try:
        os.kill(server_pid, 0)
        pid_alive = True
    except ProcessLookupError:
        pid_alive = False
    with open(PRE_KILL_EVIDENCE, "w", encoding="utf-8") as stream:
        json.dump(
            {{
                "socket_exists": os.path.lexists(socket_path),
                "client_succeeds": client.returncode == 0,
                "server_pid": server_pid,
                "pid_alive": pid_alive,
            }},
            stream,
        )

if FAIL_KILL_SERVER and "kill-server" in args:
    raise SystemExit(29)
if ACKNOWLEDGE_KILL_WITHOUT_STOPPING and "kill-server" in args:
    raise SystemExit(0)
if UNLINK_SOCKET_WITHOUT_STOPPING and "kill-server" in args:
    os.unlink(args[args.index("-S") + 1])
    raise SystemExit(0)

if (
    SABOTAGE_MARKER
    and os.path.exists(SABOTAGE_MARKER)
    and ("kill-pane" in args or "kill-window" in args)
):
    socket_path = args[args.index("-S") + 1]
    target = args[args.index("-t") + 1]
    listed = subprocess.run(
        [
            REAL_TMUX,
            "-S",
            socket_path,
            "list-panes",
            "-a",
            "-F",
            "#{{pane_id}}\\t#{{window_id}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    victims = []
    for line in listed.stdout.splitlines():
        pane_id, window_id = line.split("\\t")
        if target not in (pane_id, window_id):
            victims.append(pane_id)
    if victims:
        subprocess.run(
            [REAL_TMUX, "-S", socket_path, "kill-pane", "-t", victims[0]],
            capture_output=True,
            text=True,
            check=True,
        )
    os.unlink(SABOTAGE_MARKER)

os.execv(REAL_TMUX, [REAL_TMUX, *args])
"""
    )
    path.chmod(0o700)


def _write_old_tmux(path: Path, *, mutation_marker: Path, log_path: Path) -> None:
    """A 3.1c executable that proves version refusal precedes server mutation."""
    path.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(log_path)!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[-1:] == ["-V"]:
    print("tmux 3.1c")
    raise SystemExit(0)
pathlib.Path({str(mutation_marker)!r}).write_text("tmux launch reached")
raise SystemExit(19)
"""
    )
    path.chmod(0o700)


class TmuxContractBase(unittest.TestCase):
    def setUp(self):
        # /tmp is intentional.  The production socket locator must also have a
        # length independent of a generated workspace root.  Using the host's
        # potentially long TMPDIR here would make the test fixture recreate the
        # bug it is trying to isolate.
        self.tmp = Path(tempfile.mkdtemp(prefix="g2tmux-", dir="/tmp"))
        self.ws = self.tmp / "workspace"
        # The production socket locator is deliberately outside the workspace's
        # generated path.  Keep those roots separate in the fixture too, or a
        # persistence scan rooted at self.tmp can pass while missing everything
        # the runtime writes beside its socket.
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="g2tmux-runtime-", dir="/tmp"))
        self.runtime_dir.chmod(0o700)
        self.socket = self.runtime_dir / "team.sock"
        self.session = f"team-{os.getpid()}-{self.tmp.name}"
        self.sockets: set[Path] = {self.socket}
        self.logs: list[Path] = []

    def tearDown(self):
        if TMUX is not None:
            for socket_path in self.sockets:
                _tmux("-S", str(socket_path), "kill-server")
        os.environ.pop(AMBIENT_KEY, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.runtime_dir, ignore_errors=True)

    def _runtime_class(self):
        runtime = getattr(launch_exec, "TmuxPaneRuntime", None)
        self.assertIsNotNone(
            runtime,
            "RED contract: launch_exec must expose TmuxPaneRuntime",
        )
        return runtime

    def _runtime(
        self,
        *,
        socket_path: Path | str | None = None,
        session_name: str | None = None,
        tmux_binary: str | None = None,
    ):
        runtime = self._runtime_class()
        kwargs = {
            "socket_path": self.socket if socket_path is None else socket_path,
            "session_name": self.session if session_name is None else session_name,
        }
        if tmux_binary is not None:
            kwargs["tmux_binary"] = tmux_binary
        return runtime(**kwargs)

    def _entry(
        self,
        key: str,
        *,
        workspace: Path | None = None,
        argv: list[str] | None = None,
        env_keys: list[str] | None = None,
    ):
        root = workspace or self.ws
        home = root / "units" / key / "home"
        home.mkdir(parents=True, exist_ok=True)
        return launch_exec.LaunchEntry.from_mapping(
            {
                "unit_key": key,
                "workdir": f"units/{key}/home",
                "argv": argv or [sys.executable, "-c", "import time; time.sleep(30)"],
                "env_allowlist_keys": env_keys or [],
            }
        )

    def _launch(
        self,
        entries,
        *,
        runtime=None,
        workspace: Path | None = None,
        env_by_unit: dict[str, dict[str, str]] | None = None,
        settle_seconds: float = 0.12,
    ):
        return launch_exec.launch_team(
            entries,
            workspace_root=workspace or self.ws,
            env_values_by_unit=env_by_unit or {entry.unit_key: {} for entry in entries},
            settle_seconds=settle_seconds,
            runtime=runtime or self._runtime(),
        )

    def _pane_rows(self, socket_path: Path | None = None) -> list[dict[str, object]]:
        result = _tmux(
            "-S",
            str(socket_path or self.socket),
            "list-panes",
            "-a",
            "-F",
            "#{session_id}|#{pane_id}|#{pane_pid}|#{pane_current_path}|#{pane_dead}",
            check=True,
        )
        rows = []
        for line in result.stdout.splitlines():
            # Independent framing oracle, not the product parser. Paths own
            # all bytes between the numeric prefix and final dead suffix.
            remainder = line
            prefix = []
            for _ in range(3):
                value, delimiter, remainder = remainder.partition("|")
                self.assertEqual(delimiter, "|")
                prefix.append(value)
            cwd, delimiter, dead = remainder.rpartition("|")
            self.assertEqual(delimiter, "|")
            session, pane, pid = prefix
            self.assertTrue(session.startswith("$") and session[1:].isascii() and session[1:].isdigit())
            self.assertTrue(pane.startswith("%") and pane[1:].isascii() and pane[1:].isdigit())
            self.assertTrue(pid.isascii() and pid.isdigit() and int(pid) > 0)
            self.assertIn(dead, ("0", "1"))
            self.assertTrue(Path(cwd).is_absolute())
            self.assertNotIn(pane, {row["pane_id"] for row in rows})
            rows.append(
                {
                    "session": session,
                    "pane_id": pane,
                    "pid": int(pid),
                    "cwd": str(Path(cwd).resolve()),
                    "dead": dead,
                }
            )
        return rows

    def _recording_runtime(
        self,
        *,
        sabotage_marker: Path | None = None,
        fail_kill_server: bool = False,
        acknowledge_kill_without_stopping: bool = False,
        unlink_socket_without_stopping: bool = False,
        pre_kill_evidence_path: Path | None = None,
    ):
        assert TMUX is not None
        proxy = self.tmp / "tmux-proxy"
        log = self.tmp / "tmux-argv.jsonl"
        self.logs.append(log)
        _write_tmux_proxy(
            proxy,
            real_tmux=TMUX,
            log_path=log,
            sabotage_marker=sabotage_marker,
            fail_kill_server=fail_kill_server,
            acknowledge_kill_without_stopping=acknowledge_kill_without_stopping,
            unlink_socket_without_stopping=unlink_socket_without_stopping,
            pre_kill_evidence_path=pre_kill_evidence_path,
        )
        return self._runtime(tmux_binary=str(proxy)), log


class TestTmuxEvidenceRetention(unittest.TestCase):
    """Small owner witnesses without creating a tmux server."""

    def test_printable_session_format_matches_exact_numeric_tuple(self):
        with tempfile.TemporaryDirectory(prefix="ap37-evidence-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            runtime = launch_exec.TmuxPaneRuntime(root / "s", "proof")
            response = subprocess.CompletedProcess([], 0, "123|$4|%5\n", "")
            with patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=response) as invoke:
                output = runtime._create_session(root)
            args = invoke.call_args.args
            self.assertEqual(args[args.index("-F") + 1], "#{pid}|#{session_id}|#{pane_id}")
            self.assertEqual(runtime._parse_session_handles(output), (123, "$4", "%5"))
            for malformed in ("123_$4_%5", "123|$4", "abc|$4|%5", "0|$4|%5", "123|4|%5"):
                with self.subTest(malformed=malformed), self.assertRaises(launch_exec.LaunchExecutionError):
                    runtime._parse_session_handles(malformed)

    def _malformed_launch(self, cleanup_succeeds):
        with tempfile.TemporaryDirectory(prefix="ap37-evidence-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            runtime = launch_exec.TmuxPaneRuntime(root / "s", "proof")
            primaries = []
            parser = launch_exec.TmuxPaneRuntime._parse_session_handles

            def capture(instance, output):
                try:
                    return parser(instance, output)
                except launch_exec.LaunchExecutionError as primary:
                    primaries.append(primary)
                    raise

            with (
                patch.object(launch_exec.TmuxPaneRuntime, "_prepare", return_value=[]),
                patch.object(launch_exec.TmuxPaneRuntime, "_require_supported_version"),
                patch.object(launch_exec.TmuxPaneRuntime, "_create_session", return_value="123_$4_%5"),
                patch.object(launch_exec.TmuxPaneRuntime, "_parse_session_handles", capture),
                patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=subprocess.CompletedProcess([], 0, "", "")) as invoke,
                patch.object(Path, "unlink") as unlink,
            ):
                if cleanup_succeeds:
                    with patch.object(launch_exec.TmuxPaneRuntime, "_kill_created_server") as cleanup:
                        with self.assertRaises(launch_exec.LaunchExecutionError) as caught:
                            runtime.launch_team((object(),), workspace_root=root, env_values_by_unit={}, settle_seconds=0)
                    cleanup.assert_called_once_with(None, None)
                    self.assertFalse(hasattr(caught.exception, "__notes__"))
                    invoke.assert_not_called()
                else:
                    with self.assertRaises(launch_exec.LaunchExecutionError) as caught:
                        runtime.launch_team((object(),), workspace_root=root, env_values_by_unit={}, settle_seconds=0)
                    invoke.assert_called_once_with("kill-server", operation="rollback")
                    note = "\n".join(caught.exception.__notes__)
                    self.assertIn("rollback failed or remains unverified", note)
                    self.assertIn(str(runtime.socket_path), note)
                    self.assertIn("LaunchExecutionError", note)
                    self.assertIn("without captured PID evidence", note)
                self.assertEqual(len(primaries), 1)
                self.assertIs(caught.exception, primaries[0])
                self.assertEqual(str(caught.exception), "tmux session creation returned malformed handle evidence")
                unlink.assert_not_called()

    def test_malformed_evidence_retains_same_primary_and_unverified_rollback_note(self):
        self._malformed_launch(cleanup_succeeds=False)

    def test_successful_cleanup_retains_same_primary_without_failure_note(self):
        self._malformed_launch(cleanup_succeeds=True)


class TestTmuxPaneObservationEvidence(unittest.TestCase):
    """Actual owner and independent oracle, with command transport intercepted."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="g2observe-", dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.socket = Path(self.directory.name).resolve() / "control.sock"

    def _production(self, text):
        runtime = launch_exec.TmuxPaneRuntime(self.socket, "control")
        with patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=subprocess.CompletedProcess([], 0, text, "")) as invoke:
            result = runtime._observe_panes()
        self.assertEqual(invoke.call_args.args, ("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_current_path}|#{pane_dead}"))
        return result

    def _independent(self, text):
        oracle = TmuxContractBase()
        oracle.socket = self.socket
        with patch(__name__ + "._tmux", return_value=subprocess.CompletedProcess([], 0, text, "")) as invoke:
            result = oracle._pane_rows()
        self.assertEqual(invoke.call_args.args[-1], "#{session_id}|#{pane_id}|#{pane_pid}|#{pane_current_path}|#{pane_dead}")
        return result

    def test_middle_path_preserved_by_actual_owner_and_independent_oracle(self):
        paths = [
            "/tmp/g2tmux-example/workspace/units/u_one/home",
            "/tmp/g2tmux-example/workspace/units/u_two/home",
            "/tmp/g2tmux-example/generated-session-" + "x" * 170 + "/scratchpad/workspace/units/u_long/home",
            "/tmp/ap37-path-example/cwd with spaces|pipe",
            "/tmp/ap37-path-example/ spaced | mid || trailing|",
            "/tmp/ap37-path-example/café|文",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(os.fsencode(self._production(f"%2|123|{path}|0\n")["%2"].workdir), os.fsencode(path))
                self.assertEqual(os.fsencode(self._independent(f"$3|%2|123|{path}|0\n")[0]["cwd"]), os.fsencode(Path(path).resolve()))

    def test_malformed_truncated_and_duplicate_rows_refuse(self):
        rows = ["%2|123", "%2|123|/tmp/cwd", "%x|123|/tmp/cwd|0",
                "%2|bad|/tmp/cwd|0", "%2|0|/tmp/cwd|0", "%2|123|/tmp/cwd|2",
                "%2|123|/tmp/cwd|0|extra", "%2|123||0", "%2|123|relative|0",
                "%2|123|/tmp/cwd\n/extra|0"]
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(launch_exec.LaunchExecutionError):
                    self._production(row)
                with self.assertRaises((AssertionError, ValueError)):
                    self._independent("$3|" + row)
        with self.assertRaises(launch_exec.LaunchExecutionError):
            self._production("%2|123|/tmp/cwd|0\n%2|123|/tmp/cwd|0\n")
        with self.assertRaises(AssertionError):
            self._independent("$3|%2|123|/tmp/cwd|0\n$3|%2|123|/tmp/cwd|0\n")

    def test_exact_members_with_only_explicit_bootstrap_exception(self):
        from types import SimpleNamespace
        runtime = launch_exec.TmuxPaneRuntime(self.socket, "control")
        panes = {"one": "%2", "two": "%3"}
        prepared = {key: SimpleNamespace(workdir=Path("/tmp/" + key)) for key in panes}
        rows = "%2|123|/tmp/one|0\n%3|124|/tmp/two|0\n"
        with patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=subprocess.CompletedProcess([], 0, rows, "")):
            self.assertEqual(set(runtime._require_live_units(panes, prepared)), set(panes))
        for altered in (rows.splitlines()[0] + "\n", rows + "%4|125|/tmp/extra|0\n"):
            with self.subTest(rows=altered), patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=subprocess.CompletedProcess([], 0, altered, "")):
                with self.assertRaisesRegex(launch_exec.LaunchExecutionError, "missing or extra pane members"):
                    runtime._require_live_units(panes, prepared)
        with patch.object(launch_exec.TmuxPaneRuntime, "_invoke", return_value=subprocess.CompletedProcess([], 0, rows + "%0|125|/tmp/bootstrap|0\n", "")):
            self.assertEqual(set(runtime._require_live_units(panes, prepared, bootstrap_pane="%0")), set(panes))


class TestExplicitRuntimeCoordinates(TmuxContractBase):
    def test_socket_and_session_are_required_constructor_inputs(self):
        runtime = self._runtime_class()
        signature = inspect.signature(runtime)
        self.assertIs(signature.parameters["socket_path"].default, inspect.Parameter.empty)
        self.assertIs(signature.parameters["session_name"].default, inspect.Parameter.empty)

        with self.assertRaises(TypeError):
            runtime()
        with self.assertRaises(TypeError):
            runtime(socket_path=self.socket)
        with self.assertRaises(TypeError):
            runtime(session_name=self.session)

    def test_empty_coordinates_refuse_instead_of_falling_back(self):
        runtime = self._runtime_class()
        for socket_path, session_name in (
            ("", self.session),
            (self.socket, ""),
        ):
            with self.subTest(socket_path=socket_path, session_name=session_name):
                with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
                    runtime(socket_path=socket_path, session_name=session_name)
                message = str(ctx.exception).lower()
                self.assertIn("required", message)
                self.assertNotIn("synapt", message, "a refusal must not disclose a fallback")

    def test_overlong_explicit_socket_refuses_with_measured_length(self):
        runtime = self._runtime_class()
        socket_path = self.runtime_dir / ("s" * 105)
        measured = len(os.fsencode(socket_path))
        self.assertGreater(measured, 103)

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            runtime(socket_path=socket_path, session_name=self.session)
        message = str(ctx.exception)
        self.assertIn(str(measured), message)
        self.assertIn("AF_UNIX", message)

    def test_world_accessible_socket_parent_is_refused(self):
        unsafe = self.tmp / "unsafe-runtime"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._runtime(socket_path=unsafe / "team.sock")
        self.assertIn("0700", str(ctx.exception))
        self.assertFalse((unsafe / "team.sock").exists())

    def test_symlinked_socket_parent_is_refused(self):
        real = self.tmp / "real-runtime"
        real.mkdir(mode=0o700)
        link = self.tmp / "linked-runtime"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._runtime(socket_path=link / "team.sock")
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertFalse((real / "team.sock").exists())


class TestTmuxVersionPrecondition(TmuxContractBase):
    def test_tmux_before_3_2_refuses_before_any_server_mutation(self):
        old_tmux = self.tmp / "tmux-old"
        marker = self.tmp / "mutation-reached"
        log = self.tmp / "old-tmux-argv.jsonl"
        _write_old_tmux(old_tmux, mutation_marker=marker, log_path=log)
        entry = self._entry("u_old")

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._launch([entry], runtime=self._runtime(tmux_binary=str(old_tmux)))

        message = str(ctx.exception)
        self.assertIn("3.1c", message)
        self.assertIn("3.2", message)
        self.assertFalse(marker.exists(), "launch continued after the failed version gate")
        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(invocations, [["-S", str(self.socket), "-V"]])


@unittest.skipUnless(TMUX is not None and os.name == "posix", "requires tmux on POSIX")
class TestTmuxLaunchFruit(TmuxContractBase):
    def test_returned_handles_name_the_panes_that_actually_exist(self):
        entries = [self._entry("u_one"), self._entry("u_two")]
        runtime, log = self._recording_runtime()
        handles = self._launch(entries, runtime=runtime)

        rows = self._pane_rows()
        self.assertEqual(len(rows), 2, "bootstrap pane survived or a unit pane is absent")
        by_pane = {row["pane_id"]: row for row in rows}
        self.assertEqual({handle.pane_id for handle in handles}, set(by_pane))
        for handle in handles:
            row = by_pane[handle.pane_id]
            self.assertEqual(row["pid"], handle.pid)
            self.assertEqual(row["cwd"], str(Path(handle.workdir).resolve()))
            self.assertEqual(row["dead"], "0")
            os.kill(handle.server_pid, 0)
            self.assertIs(handle.alive_at_launch, True)
            self.assertFalse(
                hasattr(handle, "alive"),
                "a launch-time observation must not be named as current truth",
            )
            self.assertEqual(Path(handle.socket_path), self.socket)

        mode = stat.S_IMODE(self.socket.stat().st_mode)
        self.assertEqual(mode, 0o600)

        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertGreaterEqual(len(invocations), 6)
        for args in invocations:
            self.assertEqual(
                args[:2],
                ["-S", str(self.socket)],
                f"bare tmux operation escaped the workspace server: {args}",
            )

    def test_a_long_generated_workspace_uses_the_short_explicit_socket(self):
        long_component = "generated-session-" + ("x" * 170)
        workspace = self.tmp / long_component / "scratchpad" / "workspace"
        self.assertGreater(len(os.fsencode(workspace)), 136)
        entry = self._entry("u_long", workspace=workspace)

        handles = self._launch([entry], workspace=workspace)

        self.assertEqual(len(handles), 1)
        self.assertLess(len(os.fsencode(handles[0].socket_path)), 100)
        self.assertEqual(self._pane_rows()[0]["cwd"], str((workspace / entry.workdir).resolve()))

    def test_another_socket_and_the_default_server_cannot_see_the_session(self):
        other_dir = self.tmp / "other-runtime"
        other_dir.mkdir(mode=0o700)
        other_socket = other_dir / "other.sock"
        self.sockets.add(other_socket)
        decoy_session = f"decoy-{self.tmp.name}"
        _tmux(
            "-S",
            str(other_socket),
            "new-session",
            "-d",
            "-s",
            decoy_session,
            "/bin/sleep",
            "30",
            check=True,
        )

        # Positive control: the other-socket instrument can see its own pane.
        visible = _tmux("-S", str(other_socket), "list-panes", "-t", f"={decoy_session}")
        self.assertEqual(visible.returncode, 0)

        self._launch([self._entry("u_isolated")])

        foreign = _tmux("-S", str(other_socket), "list-panes", "-t", f"={self.session}")
        self.assertNotEqual(foreign.returncode, 0)

        default_env = _test_client_env()
        default_env.pop("TMUX", None)
        default_server = subprocess.run(
            [TMUX, "has-session", "-t", f"={self.session}"],
            env=default_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(default_server.returncode, 0)

    def test_existing_socket_is_create_only_and_remains_unchanged(self):
        existing = f"existing-{self.tmp.name}"
        _tmux(
            "-S",
            str(self.socket),
            "new-session",
            "-d",
            "-s",
            existing,
            "/bin/sleep",
            "30",
            check=True,
        )
        before = _tmux(
            "-S",
            str(self.socket),
            "list-panes",
            "-a",
            "-F",
            "#{pane_id} #{pane_pid}",
            check=True,
        ).stdout

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._launch([self._entry("u_must_not_attach")])

        after = _tmux(
            "-S",
            str(self.socket),
            "list-panes",
            "-a",
            "-F",
            "#{pane_id} #{pane_pid}",
            check=True,
        ).stdout
        self.assertEqual(after, before)
        self.assertIn("create-only", str(ctx.exception))

    def test_declared_env_arrives_but_ambient_env_does_not(self):
        os.environ[AMBIENT_KEY] = "leaked-from-parent"
        seen = self.ws / "units" / "u_env" / "home" / "seen.json"
        entry = self._entry(
            "u_env",
            argv=[
                sys.executable,
                "-c",
                f"import json,os,time;open({str(seen)!r},'w').write("
                "json.dumps(dict(os.environ)));time.sleep(30)",
            ],
            env_keys=[DECLARED_KEY],
        )
        self._launch(
            [entry],
            env_by_unit={entry.unit_key: {DECLARED_KEY: DECLARED_VALUE}},
        )
        for _ in range(80):
            if seen.is_file() and seen.read_text():
                break
            time.sleep(0.05)

        child_env = json.loads(seen.read_text())
        self.assertEqual(child_env[DECLARED_KEY], DECLARED_VALUE)
        self.assertNotIn(AMBIENT_KEY, child_env)

        session_env = _tmux(
            "-S",
            str(self.socket),
            "show-environment",
            "-t",
            f"={self.session}",
            check=True,
        ).stdout
        self.assertNotIn(DECLARED_VALUE, session_env)
        self.assertNotIn(AMBIENT_KEY, session_env)

    def test_environment_value_is_not_persisted_by_the_launcher(self):
        entry = self._entry("u_secret", env_keys=[DECLARED_KEY])
        self._launch(
            [entry],
            env_by_unit={entry.unit_key: {DECLARED_KEY: DECLARED_VALUE}},
        )

        searched_roots = (self.tmp, self.socket.parent)
        self.assertEqual(
            {root.resolve() for root in searched_roots},
            {self.tmp.resolve(), self.runtime_dir.resolve()},
            "the persistence instrument must enumerate every controlled write root",
        )
        found = []
        for root in searched_roots:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if DECLARED_VALUE in path.read_text(errors="ignore"):
                        found.append(str(path))
                except OSError:  # pragma: no cover
                    pass
        self.assertEqual(found, [], f"environment value persisted to disk: {found}")

    def test_partial_failure_rolls_back_only_the_created_socket_server(self):
        entries = [
            self._entry("u_survivor"),
            self._entry(
                "u_failure",
                argv=[sys.executable, "-c", "raise SystemExit(23)"],
            ),
        ]
        pre_kill = self.tmp / "pre-kill-evidence.json"
        runtime, log = self._recording_runtime(pre_kill_evidence_path=pre_kill)

        with self.assertRaises(launch_exec.LaunchExecutionError):
            self._launch(entries, runtime=runtime)

        control = json.loads(pre_kill.read_text())
        self.addCleanup(_terminate_process, control["server_pid"])
        self.assertIs(control["socket_exists"], True)
        self.assertIs(control["client_succeeds"], True)
        self.assertIs(control["pid_alive"], True)

        for _ in range(100):
            if not self.socket.exists():
                break
            time.sleep(0.02)
        self.assertFalse(self.socket.exists(), "failed launch left its tmux server alive")
        after_client = _tmux("-S", str(self.socket), "list-panes", "-a")
        self.assertNotEqual(after_client.returncode, 0)
        for _ in range(100):
            if not _pid_is_alive(control["server_pid"]):
                break
            time.sleep(0.02)
        self.assertFalse(
            _pid_is_alive(control["server_pid"]),
            "failed launch left its captured tmux server process alive",
        )

        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any("kill-server" in args for args in invocations))
        for args in invocations:
            self.assertEqual(
                args[:2],
                ["-S", str(self.socket)],
                f"rollback escaped the explicit socket: {args}",
            )

    def test_failed_server_termination_is_not_hidden_by_unlinking_the_socket(self):
        entry = self._entry(
            "u_failure",
            argv=[sys.executable, "-c", "raise SystemExit(23)"],
        )
        runtime, log = self._recording_runtime(fail_kill_server=True)

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._launch([entry], runtime=runtime)

        self.assertIn("rollback", "\n".join(ctx.exception.__notes__))
        self.assertTrue(
            self.socket.exists(),
            "a failed termination was hidden by unlinking its still-live server socket",
        )
        still_live = _tmux("-S", str(self.socket), "list-panes", "-a")
        self.assertEqual(
            still_live.returncode,
            0,
            "the failure proxy did not prove that an unacknowledged kill leaves a live server",
        )
        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any("kill-server" in args for args in invocations))

    def test_acknowledged_termination_must_still_be_observed_by_fruit(self):
        entry = self._entry(
            "u_failure",
            argv=[sys.executable, "-c", "raise SystemExit(23)"],
        )
        runtime, log = self._recording_runtime(acknowledge_kill_without_stopping=True)

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._launch([entry], runtime=runtime)

        self.assertIn("still reachable", "\n".join(ctx.exception.__notes__))
        self.assertTrue(
            self.socket.exists(),
            "a false-success termination was hidden by unlinking its live server socket",
        )
        still_live = _tmux("-S", str(self.socket), "list-panes", "-a")
        self.assertEqual(still_live.returncode, 0)
        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any("kill-server" in args for args in invocations))

    def test_listener_death_does_not_mask_an_orphaned_server_process(self):
        entry = self._entry(
            "u_failure",
            argv=[sys.executable, "-c", "raise SystemExit(23)"],
        )
        pre_kill = self.tmp / "orphan-pre-kill-evidence.json"
        runtime, _ = self._recording_runtime(
            unlink_socket_without_stopping=True,
            pre_kill_evidence_path=pre_kill,
        )

        with self.assertRaises(launch_exec.LaunchExecutionError) as ctx:
            self._launch([entry], runtime=runtime)

        control = json.loads(pre_kill.read_text())
        self.addCleanup(_terminate_process, control["server_pid"])
        self.assertIs(control["socket_exists"], True)
        self.assertIs(control["client_succeeds"], True)
        self.assertIs(control["pid_alive"], True)
        self.assertIn("process", "\n".join(ctx.exception.__notes__))
        self.assertIn("still alive", "\n".join(ctx.exception.__notes__))
        self.assertFalse(self.socket.exists())
        after_client = _tmux("-S", str(self.socket), "list-panes", "-a")
        self.assertNotEqual(after_client.returncode, 0)
        self.assertTrue(
            _pid_is_alive(control["server_pid"]),
            "orphan witness did not preserve the server process after listener removal",
        )

        _terminate_process(control["server_pid"])
        self.assertFalse(_pid_is_alive(control["server_pid"]))

    def test_unit_death_during_bootstrap_removal_is_caught_before_return(self):
        sabotage = self.tmp / "sabotage-on-bootstrap-removal"
        sabotage.write_text("armed")
        runtime, _ = self._recording_runtime(sabotage_marker=sabotage)
        entries = [self._entry("u_first"), self._entry("u_second")]

        with self.assertRaises(launch_exec.LaunchExecutionError):
            self._launch(entries, runtime=runtime)

        for _ in range(50):
            if not self.socket.exists():
                break
            time.sleep(0.02)
        self.assertFalse(
            self.socket.exists(),
            "runtime returned or left a server after a unit died between observations",
        )


if __name__ == "__main__":
    unittest.main()
