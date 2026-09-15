"""Neutral launch primitive (S5).

Executes a LaunchPlan entry: run this argv, in this working directory, with
these environment KEY NAMES. gr2 cannot tell one caller's workspace from
another -- it sees an opaque `unit_key`, an argv, and a set of env key names
whose VALUES the caller supplies in memory at launch.

Design: the spawn launch contract, §2 (LaunchPlan is the
opaque tier) and §5 (cold start, no `--resume`).

THE BOUNDARY CORRECTION THIS SLICE EXISTS TO MAKE
--------------------------------------------------
gr1's spawn builds an on-disk launch script containing `export KEY=value` lines.
That puts the caller's environment VALUES on disk -- every declared key
bindings -- inside the OSS layer, in a file that outlives the launch.

The neutral plan carries key NAMES; the values are injected in memory and never
persisted. This primitive therefore has no script-writing path at all: values
are passed straight to the child process environment and are unreachable from
the filesystem afterwards. `test_no_environment_value_reaches_the_filesystem`
searches the workspace for a value rather than trusting that nothing wrote one.

Same rule that held through the whole materializer: no identity in any neutral
artifact. A launch script IS a neutral artifact.

WHAT "LAUNCHED" HAS TO MEAN
---------------------------
A spawn call returning successfully is not an agent running. A process that
exits immediately -- a missing binary, a bad flag, an instant crash -- produces
exactly the same "success" as one that came up healthy, and the failure surfaces
later as an empty pane nobody notices. So launch is not acknowledged until the
child has been observed ALIVE after a settle interval, which is the same
distinction invariant 6 draws between a venv's files existing and its
interpreter answering.
"""

from __future__ import annotations

import dataclasses
import os
import re
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .spec_apply import MaterializationPlanError, canonicalize_workspace_path

# Long enough that an immediate crash (bad binary, bad flag, instant exit) has
# happened, short enough not to matter against §13's 180s budget.
_LIVENESS_SETTLE_SECONDS = 0.35

# macOS exposes 104 bytes for sockaddr_un.sun_path including the terminating
# NUL, so 103 encoded path bytes is the portable ceiling this runtime accepts.
# Linux permits a few more; using the smaller measured limit makes the same
# explicit socket viable on every supported POSIX host instead of branching on
# whichever generated workspace path or kernel happens to be present.
_AF_UNIX_SOCKET_PATH_MAX_BYTES = 103
_MINIMUM_TMUX_VERSION = (3, 2)
_TMUX_ROLLBACK_SETTLE_SECONDS = 2.0
_TMUX_ROLLBACK_POLL_SECONDS = 0.02


class LaunchExecutionError(MaterializationPlanError):
    """A launch entry could not be executed safely."""


@dataclasses.dataclass(frozen=True)
class LaunchEntry:
    """One unit's opaque launch declaration.

    Deliberately carries no model, tool, role or agent name -- gr2 runs an argv,
    it does not know what the argv IS."""

    unit_key: str
    workdir: str
    argv: tuple[str, ...]
    env_allowlist_keys: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LaunchEntry:
        allowed = {"unit_key", "workdir", "argv", "env_allowlist_keys"}
        unknown = set(data) - allowed
        if unknown:
            # Closed by construction, same reason the plan schema is: a field
            # nobody thought to reject cannot smuggle anything if it cannot
            # exist. An unexpected key here is how identity would arrive.
            raise LaunchExecutionError(
                f"launch entry has unknown field(s) {sorted(unknown)} -- the opaque "
                "tier carries exactly unit_key, workdir, argv and env_allowlist_keys"
            )
        for field in ("unit_key", "workdir"):
            if not isinstance(data.get(field), str) or not data[field]:
                raise LaunchExecutionError(f"launch entry {field!r} must be a non-empty string")
        argv = data.get("argv")
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise LaunchExecutionError(
                "launch entry argv must be a non-empty list of strings -- a single "
                "string would invite shell interpretation, and the launcher never "
                "hands a plan's contents to a shell"
            )
        keys = data.get("env_allowlist_keys", [])
        if not isinstance(keys, (list, tuple)) or not all(isinstance(k, str) for k in keys):
            raise LaunchExecutionError("env_allowlist_keys must be a list of strings")
        return cls(
            unit_key=data["unit_key"],
            workdir=data["workdir"],
            argv=tuple(argv),
            env_allowlist_keys=tuple(keys),
        )


def _require_exact_env(entry: LaunchEntry, values: Mapping[str, str]) -> dict[str, str]:
    """The allowlist is exact in BOTH directions.

    Extra: the caller supplied a value for a key the plan never declared. The plan
    is what a reviewer reads to know what a process receives, so a value outside
    it is invisible to review.

    Missing: the plan declared a key the launch has no value for. Starting the
    process anyway hands the agent a half-built environment and the failure
    appears later as behaviour nobody traces back to launch.

    Both are refusals, and they are checked separately because a single
    set-equality assertion would report the wrong one first and teach the
    operator the wrong thing to fix."""
    declared = set(entry.env_allowlist_keys)
    supplied = set(values)

    extra = sorted(supplied - declared)
    if extra:
        raise LaunchExecutionError(
            f"unit {entry.unit_key}: environment value(s) supplied for undeclared "
            f"key(s) {extra} -- the launch plan is what a reviewer reads to know what "
            "a process receives, so anything outside it is invisible to review"
        )
    missing = sorted(declared - supplied)
    if missing:
        raise LaunchExecutionError(
            f"unit {entry.unit_key}: no value supplied for declared key(s) {missing} -- "
            "starting with a half-built environment defers the failure to runtime"
        )
    return {k: str(values[k]) for k in entry.env_allowlist_keys}


# Launcher-owned process mechanics, supplied EXPLICITLY rather than inherited.
# §6 classes these as launcher-owned precisely so a plan cannot declare them --
# but the child still needs them to run at all (a binary resolved by name needs
# PATH). Naming them here is the difference between "the launcher provides these,
# for these reasons" and "whatever the parent happened to have".
_LAUNCHER_OWNED_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SystemRoot")


def _child_environment(entry: LaunchEntry, values: Mapping[str, str]) -> dict[str, str]:
    """The child's COMPLETE environment, built rather than inherited.

    Sentinel, #825: passing `{**os.environ, **declared}` inherits the parent's
    entire environment, so a child sees ambient variables that were never declared
    anywhere -- reproduced on a live host, where variables belonging to the
    launching process reached a spawned child.

    My allowlist check was exact in two directions and both were about the
    SUPPLIED dict: a value for an undeclared key, and a declared key with no
    value. Neither asks what the child ACTUALLY ENDS UP WITH. Receiving the
    right thing and receiving ONLY that are different claims, and the test that
    asserted the child got the declared value could not see the difference.

    So the environment is CONSTRUCTED: exactly the declared keys, plus a named
    set of launcher-owned mechanics the child cannot run without. Nothing is
    inherited implicitly. Everything present is there because something declared
    it or because this list names it.

    That matters beyond tidiness: a caller may pass values it does not want
    written anywhere, and inheriting os.environ would put the launching
    process's entire environment into every child regardless."""
    child = _require_exact_env(entry, values)
    for key in _LAUNCHER_OWNED_PASSTHROUGH:
        ambient = os.environ.get(key)
        if ambient is not None and key not in child:
            child[key] = ambient
    return child


@dataclasses.dataclass(frozen=True)
class TmuxPaneHandle:
    """A handle returned by the runtime, never a name to resolve later.

    ``alive_at_launch`` is deliberately a momentary observation.  A field named
    ``alive`` would read as current truth even though the process can exit the
    instant after this value is returned.
    """

    kind: str
    unit_key: str
    socket_path: Path
    session_id: str
    server_pid: int
    pane_id: str
    pid: int
    workdir: Path
    env_keys: tuple[str, ...]
    alive_at_launch: bool


class LaunchRuntime(Protocol):
    """Common interface implemented by the available launch modes."""

    def launch_team(
        self,
        entries: Sequence[LaunchEntry],
        *,
        workspace_root: Path,
        env_values_by_unit: Mapping[str, Mapping[str, str]],
        settle_seconds: float,
    ) -> list[dict[str, object]] | list[TmuxPaneHandle]: ...


@dataclasses.dataclass(frozen=True)
class DirectProcessRuntime:
    """Today's headless process runtime, retained as the default behavior."""

    def launch_team(
        self,
        entries: Sequence[LaunchEntry],
        *,
        workspace_root: Path,
        env_values_by_unit: Mapping[str, Mapping[str, str]],
        settle_seconds: float,
    ) -> list[dict[str, object]]:
        return _launch_team_direct(
            entries,
            workspace_root=workspace_root,
            env_values_by_unit=env_values_by_unit,
            settle_seconds=settle_seconds,
        )


@dataclasses.dataclass(frozen=True)
class ForegroundProcessRuntime:
    """Interactive mode without a multiplexer: ONE agent, in this terminal.

    The ruling this implements (tracked privately): interactive means ONE named agent
    runs in the foreground with its configured cwd, environment, prompt and
    argv; a fleet with no multiplexer is refused, never approximated. The
    child's stdin/stdout/stderr are inherited, so the prompt runs against the
    caller's terminal, and "launched" here means the run HAPPENED: the runtime
    waits on the child and returns its exit code as evidence. That is a
    different contract from the headless settle check -- an interactive prompt
    that exits instantly is a failure the operator watched, not one launch
    must guess about afterwards -- so `settle_seconds` is accepted to keep the
    LaunchRuntime signature uniform and is not applied.

    The transport boundary is the LaunchRuntime Protocol, not this class: a
    Windows/WSL or remote transport attaches as one more runtime implementing
    the same signature, selected by the same explicit `runtime` argument.
    Nothing here is what that transport attaches to."""

    def launch_team(
        self,
        entries: Sequence[LaunchEntry],
        *,
        workspace_root: Path,
        env_values_by_unit: Mapping[str, Mapping[str, str]],
        settle_seconds: float,
    ) -> list[dict[str, object]]:
        if len(entries) != 1:
            raise LaunchExecutionError(
                f"foreground launch runs ONE agent and was handed {len(entries)} -- "
                "interactive mode without a multiplexer attaches exactly one prompt "
                "to this terminal; a fleet needs a multiplexer runtime, and refusing "
                "is what keeps a half-launched team from presenting as a session"
            )
        entry = entries[0]
        values = env_values_by_unit.get(entry.unit_key)
        if values is None:
            raise LaunchExecutionError(f"no environment supplied for unit {entry.unit_key}")
        workdir = _prepared_workdir(entry, workspace_root)
        env = _child_environment(entry, values)
        try:
            # No shell. argv is a list, and a plan's contents are never handed to
            # a shell for interpretation. Streams inherited, no new session: the
            # child is attached to this terminal for the length of the run.
            proc = subprocess.Popen(  # noqa: S603
                list(entry.argv),
                cwd=str(workdir),
                env=env,
            )
        except FileNotFoundError as exc:
            raise LaunchExecutionError(
                f"unit {entry.unit_key}: {entry.argv[0]!r} not found on PATH"
            ) from exc
        except OSError as exc:
            raise LaunchExecutionError(f"unit {entry.unit_key}: launch failed: {exc}") from exc

        code = proc.wait()
        return [
            {
                "kind": "launch",
                "mode": "foreground",
                "unit_key": entry.unit_key,
                "workdir": entry.workdir,
                "pid": proc.pid,
                "env_keys": list(entry.env_allowlist_keys),  # NAMES only, never values
                "exit_code": code,
            }
        ]


def _tmux_client_environment() -> dict[str, str]:
    """Build the tmux client's complete environment from a closed allowlist.

    The server inherits the environment of the client that creates it.  An
    inherited ``TMUX`` can also redirect a bare client to an ambient server.
    Building rather than filtering makes both properties closed by
    construction: unknown selector variables and unrelated ambient values do
    not cross merely because nobody remembered to add them to a denylist.
    """

    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _parse_tmux_version(output: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"tmux\s+(\d+)\.(\d+)[a-z]?", output.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclasses.dataclass(frozen=True)
class _PreparedTmuxEntry:
    entry: LaunchEntry
    workdir: Path
    env: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class _ObservedPane:
    pane_id: str
    pid: int
    workdir: Path
    dead: bool


@dataclasses.dataclass(frozen=True)
class TmuxPaneRuntime:
    """Launch one unit per pane on an explicit, create-only tmux server.

    The socket and session are caller inputs. The runtime never derives either
    coordinate, uses tmux's default server, attaches, or reuses a socket. Every
    invocation is structurally prefixed by ``-S``. After session creation, the
    runtime addresses only tmux IDs.
    """

    socket_path: Path | str
    session_name: str
    tmux_binary: str = "tmux"

    def __post_init__(self) -> None:
        raw_socket = os.fspath(self.socket_path)
        if not raw_socket or not isinstance(self.session_name, str) or not self.session_name:
            raise LaunchExecutionError(
                "tmux socket path and session name are required explicit inputs"
            )
        if not isinstance(self.tmux_binary, str) or not self.tmux_binary:
            raise LaunchExecutionError("tmux binary is required")

        socket_path = Path(raw_socket)
        object.__setattr__(self, "socket_path", socket_path)
        self._validate_socket_coordinate()

    def _validate_socket_coordinate(self) -> None:
        socket_path = Path(self.socket_path)
        measured = len(os.fsencode(socket_path))
        if measured > _AF_UNIX_SOCKET_PATH_MAX_BYTES:
            raise LaunchExecutionError(
                f"tmux socket path is {measured} encoded bytes, exceeding the "
                f"portable AF_UNIX limit of {_AF_UNIX_SOCKET_PATH_MAX_BYTES}"
            )
        if not socket_path.is_absolute():
            raise LaunchExecutionError("tmux socket path must be absolute")

        parent = socket_path.parent
        if parent.is_symlink():
            raise LaunchExecutionError("tmux socket parent must not be a symlink")
        try:
            parent_stat = parent.stat()
        except OSError as exc:
            raise LaunchExecutionError(
                f"tmux socket parent must already exist as a private 0700 directory: {exc}"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise LaunchExecutionError("tmux socket parent must be a directory")
        mode = stat.S_IMODE(parent_stat.st_mode)
        if mode != 0o700:
            raise LaunchExecutionError(f"tmux socket parent must have mode 0700, found {mode:04o}")
        if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
            raise LaunchExecutionError("tmux socket parent must be owned by the current user")

    def _invoke(self, *args: str, operation: str) -> subprocess.CompletedProcess[str]:
        command = [self.tmux_binary, "-S", str(self.socket_path), *args]
        try:
            return subprocess.run(  # noqa: S603
                command,
                env=_tmux_client_environment(),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LaunchExecutionError(
                f"tmux executable {self.tmux_binary!r} was not found"
            ) from exc
        except OSError as exc:
            raise LaunchExecutionError(f"tmux {operation} could not run: {exc}") from exc

    def _require_success(self, result: subprocess.CompletedProcess[str], *, operation: str) -> str:
        if result.returncode != 0:
            raise LaunchExecutionError(
                f"tmux {operation} failed with exit code {result.returncode}"
            )
        return result.stdout.strip()

    def _require_supported_version(self) -> None:
        result = self._invoke("-V", operation="version check")
        reported = (result.stdout or result.stderr).strip()
        version = _parse_tmux_version(reported) if result.returncode == 0 else None
        if version is None or version < _MINIMUM_TMUX_VERSION:
            shown = reported or f"exit code {result.returncode}"
            raise LaunchExecutionError(
                f"tmux {shown!r} is unsupported; tmux 3.2 or newer is required "
                "for per-pane -e environment isolation"
            )

    def _prepare(
        self,
        entries: Sequence[LaunchEntry],
        *,
        workspace_root: Path,
        env_values_by_unit: Mapping[str, Mapping[str, str]],
    ) -> list[_PreparedTmuxEntry]:
        prepared = []
        for entry in entries:
            values = env_values_by_unit.get(entry.unit_key)
            if values is None:
                raise LaunchExecutionError(f"no environment supplied for unit {entry.unit_key}")
            workdir = canonicalize_workspace_path(
                workspace_root,
                entry.workdir,
                field_name=f"launch[{entry.unit_key}].workdir",
            )
            if not workdir.is_dir():
                raise LaunchExecutionError(
                    f"unit {entry.unit_key}: workdir {entry.workdir} does not exist -- "
                    "materialization runs before launch"
                )
            prepared.append(
                _PreparedTmuxEntry(
                    entry=entry,
                    workdir=workdir,
                    env=_child_environment(entry, values),
                )
            )
        return prepared

    def _create_session(self, workspace_root: Path) -> str:
        result = self._invoke(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pid}\t#{session_id}\t#{pane_id}",
            "-s",
            self.session_name,
            "-c",
            str(workspace_root),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(86400)",
            operation="session creation",
        )
        return self._require_success(result, operation="session creation")

    def _parse_session_handles(self, output: str) -> tuple[int, str, str]:
        fields = output.split("\t")
        if len(fields) != 3 or not fields[1].startswith("$") or not fields[2].startswith("%"):
            raise LaunchExecutionError("tmux session creation returned malformed handle evidence")
        try:
            server_pid = int(fields[0])
        except ValueError as exc:
            raise LaunchExecutionError(
                "tmux session creation returned a non-numeric server PID"
            ) from exc
        if server_pid <= 0:
            raise LaunchExecutionError("tmux session creation returned an invalid server PID")
        return server_pid, fields[1], fields[2]

    def _create_unit_pane(self, prepared: _PreparedTmuxEntry, *, session_id: str) -> str:
        environment_args: list[str] = []
        for key, value in prepared.env.items():
            environment_args.extend(("-e", f"{key}={value}"))
        result = self._invoke(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            session_id,
            "-c",
            str(prepared.workdir),
            *environment_args,
            "--",
            *prepared.entry.argv,
            operation=f"unit {prepared.entry.unit_key} pane creation",
        )
        pane_id = self._require_success(
            result, operation=f"unit {prepared.entry.unit_key} pane creation"
        )
        if not pane_id.startswith("%") or "\n" in pane_id:
            raise LaunchExecutionError(
                f"unit {prepared.entry.unit_key}: tmux returned malformed pane handle evidence"
            )
        return pane_id

    def _observe_panes(self) -> dict[str, _ObservedPane]:
        result = self._invoke(
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_dead}",
            operation="pane observation",
        )
        output = self._require_success(result, operation="pane observation")
        observed: dict[str, _ObservedPane] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                raise LaunchExecutionError("tmux returned malformed pane observation evidence")
            pane_id, pid_text, workdir_text, dead_text = fields
            try:
                pid = int(pid_text)
            except ValueError as exc:
                raise LaunchExecutionError("tmux returned a non-numeric pane PID") from exc
            observed[pane_id] = _ObservedPane(
                pane_id=pane_id,
                pid=pid,
                workdir=Path(workdir_text),
                dead=dead_text != "0",
            )
        return observed

    def _require_live_units(
        self,
        pane_ids: Mapping[str, str],
        prepared_by_unit: Mapping[str, _PreparedTmuxEntry],
    ) -> dict[str, _ObservedPane]:
        observed = self._observe_panes()
        live: dict[str, _ObservedPane] = {}
        for unit_key, pane_id in pane_ids.items():
            pane = observed.get(pane_id)
            if pane is None or pane.dead:
                raise LaunchExecutionError(
                    f"unit {unit_key}: pane was not alive at launch observation"
                )
            expected_workdir = prepared_by_unit[unit_key].workdir.resolve()
            if pane.workdir.resolve() != expected_workdir:
                raise LaunchExecutionError(
                    f"unit {unit_key}: pane started in an unexpected working directory"
                )
            live[unit_key] = pane
        return live

    def _socket_listener_is_reachable(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(os.fspath(self.socket_path))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except OSError as exc:
            raise LaunchExecutionError(
                f"tmux rollback termination could not be observed safely: {exc}"
            ) from exc
        else:
            return True
        finally:
            probe.close()

    def _kill_created_server(
        self,
        socket_identity: tuple[int, int] | None,
        server_pid: int | None,
    ) -> None:
        result = self._invoke("kill-server", operation="rollback")
        self._require_success(result, operation="rollback")
        if server_pid is None:
            raise LaunchExecutionError(
                "tmux rollback cannot verify server-process death without captured PID evidence"
            )

        # Verify both fruits independently of the tmux client whose
        # acknowledgement is under test. Calling that same executable a second
        # time would be one stance twice: a wrapper could falsely report both
        # successful termination and failed lookup. Listener death and process
        # death are separate too: an orphan can close or lose its socket while
        # remaining alive.
        deadline = time.monotonic() + _TMUX_ROLLBACK_SETTLE_SECONDS
        while True:
            listener_reachable = self._socket_listener_is_reachable()
            process_alive = _process_is_alive(server_pid)
            if not listener_reachable and not process_alive:
                break

            if time.monotonic() >= deadline:
                survivors = []
                if listener_reachable:
                    survivors.append("socket listener is still reachable")
                if process_alive:
                    survivors.append(f"server process {server_pid} is still alive")
                raise LaunchExecutionError(
                    "tmux rollback was acknowledged but " + " and ".join(survivors)
                )
            time.sleep(_TMUX_ROLLBACK_POLL_SECONDS)

        # tmux can leave the AF_UNIX node behind after the server has exited.
        # Remove only the exact socket this call observed creating.  If another
        # process replaced the path, inode identity no longer matches and this
        # rollback must not spend ownership it does not have.
        if socket_identity is None:
            return
        try:
            current = Path(self.socket_path).lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == socket_identity:
            Path(self.socket_path).unlink()

    def launch_team(
        self,
        entries: Sequence[LaunchEntry],
        *,
        workspace_root: Path,
        env_values_by_unit: Mapping[str, Mapping[str, str]],
        settle_seconds: float,
    ) -> list[TmuxPaneHandle]:
        entries = tuple(entries)
        if not entries:
            return []

        workspace_root = Path(os.fspath(workspace_root))
        prepared = self._prepare(
            entries,
            workspace_root=workspace_root,
            env_values_by_unit=env_values_by_unit,
        )
        prepared_by_unit = {item.entry.unit_key: item for item in prepared}
        self._validate_socket_coordinate()
        self._require_supported_version()
        if os.path.lexists(self.socket_path):
            raise LaunchExecutionError(
                f"tmux socket {self.socket_path} already exists; launch is create-only"
            )

        server_created = False
        created_socket_identity: tuple[int, int] | None = None
        server_pid: int | None = None
        try:
            session_output = self._create_session(workspace_root)
            server_created = True
            server_pid, session_id, bootstrap_pane = self._parse_session_handles(session_output)

            socket_stat = Path(self.socket_path).lstat()
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise LaunchExecutionError(
                    "tmux did not create a Unix socket at the requested path"
                )
            created_socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            Path(self.socket_path).chmod(0o600)

            pane_ids = {
                item.entry.unit_key: self._create_unit_pane(item, session_id=session_id)
                for item in prepared
            }
            time.sleep(settle_seconds)
            self._require_live_units(pane_ids, prepared_by_unit)

            # The bootstrap is removed only while unit panes exist.  Then every
            # unit is observed again, because the first observation is stale the
            # instant cleanup begins and a dead unit must not return success.
            cleanup = self._invoke("kill-pane", "-t", bootstrap_pane, operation="bootstrap removal")
            self._require_success(cleanup, operation="bootstrap removal")
            live = self._require_live_units(pane_ids, prepared_by_unit)

            return [
                TmuxPaneHandle(
                    kind="tmux-pane",
                    unit_key=item.entry.unit_key,
                    socket_path=Path(self.socket_path),
                    session_id=session_id,
                    server_pid=server_pid,
                    pane_id=pane_ids[item.entry.unit_key],
                    pid=live[item.entry.unit_key].pid,
                    workdir=live[item.entry.unit_key].workdir,
                    env_keys=item.entry.env_allowlist_keys,
                    alive_at_launch=True,
                )
                for item in prepared
            ]
        except BaseException:
            if server_created:
                self._kill_created_server(created_socket_identity, server_pid)
            raise


def _prepared_workdir(entry: LaunchEntry, workspace_root: Path) -> Path:
    """Resolve and validate one entry's workdir against the workspace root."""
    workspace_root = Path(os.fspath(workspace_root))
    workdir = canonicalize_workspace_path(
        workspace_root, entry.workdir, field_name=f"launch[{entry.unit_key}].workdir"
    )
    if not workdir.is_dir():
        raise LaunchExecutionError(
            f"unit {entry.unit_key}: workdir {entry.workdir} does not exist -- "
            "materialization runs before launch, so a missing workspace means the "
            "unit was never materialized rather than that launch should create it"
        )
    return workdir


def launch_unit(
    entry: LaunchEntry,
    *,
    workspace_root: Path,
    env_values: Mapping[str, str],
    settle_seconds: float = _LIVENESS_SETTLE_SECONDS,
) -> dict[str, object]:
    """Start one unit's process and prove it is alive.

    `env_values` is consumed in memory and never written anywhere. Returns
    neutral evidence -- no argv values, no env, nothing identity-bearing."""
    workdir = _prepared_workdir(entry, workspace_root)
    env = _child_environment(entry, env_values)

    try:
        # No shell. argv is a list, and a plan's contents are never handed to a
        # shell for interpretation.
        proc = subprocess.Popen(  # noqa: S603
            list(entry.argv),
            cwd=str(workdir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise LaunchExecutionError(
            f"unit {entry.unit_key}: {entry.argv[0]!r} not found on PATH"
        ) from exc
    except OSError as exc:
        raise LaunchExecutionError(f"unit {entry.unit_key}: launch failed: {exc}") from exc

    # LIVENESS, not spawn-return. A process that exits immediately produces the
    # same successful Popen as one that came up healthy.
    time.sleep(settle_seconds)
    code = proc.poll()
    if code is not None:
        raise LaunchExecutionError(
            f"unit {entry.unit_key}: process exited with code {code} within "
            f"{settle_seconds}s of launch -- a spawn that returns is not an agent "
            "that runs, and an immediately-dead process is indistinguishable from a "
            "healthy one until someone looks"
        )

    return {
        "kind": "launch",
        "unit_key": entry.unit_key,
        "workdir": entry.workdir,
        "pid": proc.pid,
        "env_keys": list(entry.env_allowlist_keys),  # NAMES only, never values
        "alive": True,
    }


def _launch_team_direct(
    entries: Sequence[LaunchEntry],
    *,
    workspace_root: Path,
    env_values_by_unit: Mapping[str, Mapping[str, str]],
    settle_seconds: float = _LIVENESS_SETTLE_SECONDS,
) -> list[dict[str, object]]:
    """Launch every unit, or none of them.

    A partially launched team is the same failure shape as a partially
    materialized one: some agents alive, some absent, presenting as a confused
    team rather than an error. Anything already started is terminated before the
    failure propagates, so a retry does not race a half-live team."""
    started: list[tuple[dict[str, object], int]] = []
    try:
        for entry in entries:
            values = env_values_by_unit.get(entry.unit_key)
            if values is None:
                raise LaunchExecutionError(f"no environment supplied for unit {entry.unit_key}")
            evidence = launch_unit(
                entry,
                workspace_root=workspace_root,
                env_values=values,
                settle_seconds=settle_seconds,
            )
            started.append((evidence, int(evidence["pid"])))
    except BaseException:
        for _, pid in started:
            try:
                os.killpg(os.getpgid(pid), 15)
            except (ProcessLookupError, PermissionError):  # pragma: no cover
                pass
        raise
    return [ev for ev, _ in started]


def launch_team(
    entries: Sequence[LaunchEntry],
    *,
    workspace_root: Path,
    env_values_by_unit: Mapping[str, Mapping[str, str]],
    settle_seconds: float = _LIVENESS_SETTLE_SECONDS,
    runtime: LaunchRuntime | None = None,
) -> list[dict[str, object]] | list[TmuxPaneHandle]:
    """Launch a team through the caller-selected runtime.

    Direct processes remain the default for compatibility. An alternate
    runtime is an explicit strategy input rather than an ambient choice.
    """

    selected: LaunchRuntime = runtime if runtime is not None else DirectProcessRuntime()
    return selected.launch_team(
        entries,
        workspace_root=Path(os.fspath(workspace_root)),
        env_values_by_unit=env_values_by_unit,
        settle_seconds=settle_seconds,
    )
