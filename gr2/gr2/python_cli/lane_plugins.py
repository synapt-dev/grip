"""Finding and running ecosystem plugins for the plan phase.

The trust boundary, as code (design note, section 4):

* A plugin is chosen by the USER's environment, never by a member. ``discover_plugins`` looks only at the
  directories of the PATH it is handed (production passes the user's own ``PATH``), for executables named
  ``grip-ecosystem-<name>``. Nothing here reads a workspace file, a member repository, or the network, and the
  set is fixed before any member is read. A member that carries a file with such a name is never executed.
* An external plugin is the same callable as the built-in one: ``plugin(call, request) -> answer``. Its answer
  goes through ``lane_graph.check_answer``, the one validator, so the built-in cannot become a special case.
* Every call has a timeout, a size bound on its answer, a minimal environment, and the member directory as its
  working directory. A crash, a non-zero exit, a timeout, an oversize answer or invalid JSON is a PLUGIN FAILURE:
  the lane is refused naming the plugin and the call. There is no fallback to marker order.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import ecosystems_python
from .lane_graph import LaneRefused

PREFIX = "grip-ecosystem-"
DEFAULT_TIMEOUT = 30.0  # seconds per call
DEFAULT_MAX_BYTES = 1_000_000  # bytes of answer


def discover_plugins(path: str) -> dict[str, str]:
    """``{name: executable path}`` for every executable ``grip-ecosystem-<name>`` in the directories of ``path``.
    The first directory wins, like a shell. Only the directories named in ``path`` are looked at."""
    found: dict[str, str] = {}
    for d in path.split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue  # an empty or relative PATH entry means "the current directory" (the member's, when a lane runs there); it is NOT a plugin directory here
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            full = os.path.join(d, n)
            name = n[len(PREFIX):] if n.startswith(PREFIX) else ""
            if name and name not in found and os.path.isfile(full) and os.access(full, os.X_OK):
                found[name] = full
    return found


def _kill(proc: subprocess.Popen) -> None:
    """Stop a plugin that did not answer in time, and its own children where the platform has process groups.
    POSIX: the plugin was started in its own session, so the whole group is killed. Where there is no
    ``killpg`` (Windows), or the group is already gone, the plugin process itself is killed."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def external_plugin(
    name: str, exe: str, *, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES, env: dict | None = None
) -> Callable[[str, dict], dict]:
    """The plugin callable for one executable: ``exe <call>``, the request as JSON on stdin, the answer as JSON on stdout."""

    def call(call_name: str, request: dict) -> dict:
        def fail(why: str) -> LaneRefused:
            return LaneRefused("plugin_failure", f"plugin {name!r} failed on {call_name}: {why}")

        cwd = request.get("dir") if call_name == "describe" else None
        run_env = env if env is not None else {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LC_ALL": "C"}
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(
                    [exe, call_name],
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    cwd=cwd if cwd and os.path.isdir(cwd) else None,
                    env=run_env,
                    start_new_session=True,  # so a timeout can take the plugin's own children with it
                )
            except OSError as exc:
                raise fail(f"could not be started ({exc})") from exc
            try:
                proc.communicate(json.dumps(request).encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.wait()
                raise fail(f"no answer within {timeout:g}s") from None
            except BrokenPipeError:
                proc.wait()  # it closed stdin without reading; its exit status says what happened
            if proc.returncode != 0:
                err.seek(0)
                tail = err.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
                raise fail(f"exit status {proc.returncode}" + (f" ({tail})" if tail else ""))
            size = out.seek(0, os.SEEK_END)
            if size > max_bytes:
                raise fail(f"an answer of {size} bytes, over the {max_bytes} byte bound")
            out.seek(0)
            raw = out.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise fail(f"output that is not JSON ({exc})") from exc

    return call


def plugin_table(path: str, **kw) -> dict[str, Callable[[str, dict], dict]]:
    """The plugins a lane plan may use, in the order they are asked: the built-in Python plugin first, then every
    external plugin discovered on ``path`` (an external named ``python`` is ignored: the built-in owns that name)."""
    table: dict[str, Callable[[str, dict], dict]] = {ecosystems_python.NAME: ecosystems_python.call}
    for name, exe in sorted(discover_plugins(path).items()):
        if name not in table:
            table[name] = external_plugin(name, exe, **kw)
    return table
