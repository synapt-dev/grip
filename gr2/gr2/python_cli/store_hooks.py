"""Git hooks that make a plain `git commit` and `git push` in a native store run the store's own checks.

THE GAP, measured on gr2 2.0.0a8: in a native store a plain `git add <member> && git commit` at the
root records the member's gitlink, but nothing refuses a pin that is not on the member's origin
(`store commit` does), and nothing keeps the `pin` in `grip.toml` level with the gitlink, so the commit
leaves two records of one fact disagreeing. `store init` installed no hooks.

THE SHAPE: two hooks, written by `store init`, that call the SAME engine the verbs call
(`_member_coverage`, `_claimed_member_paths`), so a hook and a verb cannot answer one question
differently. There is no new verb and no second implementation.

  pre-commit   every NEW gitlink in the index must be on its member's upstream (refused with the
               verb's own message, "... push it first"); and the `pin` lines of grip.toml are folded to the
               staged gitlinks and staged, so one commit can never carry two disagreeing records. The commit
               is judged by the grip.toml it will CARRY (the staged blob), only the pin lines are rewritten,
               and a fold is refused when grip.toml has unstaged edits (it never rewrites what the user has
               not staged).
  pre-push     every pin at EVERY pushed root commit must be covered, not only the tip. This is the backstop
               for a commit that skipped pre-commit (`--no-verify`, a named limit: a client hook is cooperative).

THE LIMITS, stated here because a hook that overclaims is worse than none:
  * a hook only runs where Git runs it. `core.hooksPath` hides the installed hooks, and a plain `git
    clone` does not copy hooks at all; `store status` reports both rather than looking healthy.
  * a PARTIAL commit (`git commit <paths>`) runs the hook against a temporary `next-index-<pid>.lock`
    index (measured on this host's git), so a fold there leaves the real index disagreeing with HEAD.
    The hook checks coverage in that case and REFUSES a fold, naming the remedy.
  * a partial commit is recognised by the temporary index's FILE NAME (`next-index-`); a caller who points
    GIT_INDEX_FILE at some other temporary index is treated as if it were the real one.
  * not covered here: a dirty member, a credentialed member URL, and the `.gitinclude` allow-list. Those
    stay with `store commit` and `store check`.

A HOOK NEVER OVERWRITES ANOTHER OWNER'S. A hook file carries MARKER on its second line; anything without
it is somebody else's and is left byte for byte, and `store status` names it.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from . import gitops

MARKER = "# gr2-store-hook v1"
HOOK_NAMES = ("pre-commit", "pre-push")
_ZERO = "0" * 40

#: Git locates repositories through these. A hook inherits them, and the member checkouts the engine
#: measures are DIFFERENT repositories, so they are removed before the shared helpers run.
_LOCATING_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)


# ── installing and reporting ─────────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return gitops.run_argv(["git", "-C", str(root), *args], env=env)


def _hooks_dir(root: Path) -> Path:
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    base = Path(git_dir.stdout.strip()) if git_dir.returncode == 0 and git_dir.stdout.strip() else root / ".git"
    return base / "hooks"


def hooks_path_override(root: Path) -> str | None:
    """The `core.hooksPath` Git would use instead of `.git/hooks`, or None."""
    value = _git(root, "config", "--get", "core.hooksPath")
    text = value.stdout.strip()
    return text if value.returncode == 0 and text else None


def hook_script(name: str) -> str:
    """The text of one hook: it records the exact interpreter and package root that wrote it, so the hook
    runs THIS gr2 and never whichever `gr` is first on PATH."""
    package_root = Path(__file__).resolve().parents[2]
    return (
        "#!/bin/sh\n"
        f"{MARKER} ({name}): written by `gr2 store init`; delete it and rerun init to restore.\n"
        f"PY={shlex.quote(sys.executable)}\n"
        f"PKG={shlex.quote(str(package_root))}\n"
        'if [ ! -x "$PY" ]; then\n'
        '  echo "gr2 store hook: interpreter $PY is missing; run \'gr2 store init\' to reinstall this hook" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'PYTHONPATH="$PKG${{PYTHONPATH:+:$PYTHONPATH}}" exec "$PY" -m gr2.python_cli.store_hooks {name} "$@"\n'
    )


def _is_ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace").splitlines()[1]
    except (OSError, IndexError):
        return False


def _write_atomically(path: Path, text: str) -> None:
    """Write beside the target and rename, so a running git never reads half a hook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".hook.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o755)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def hook_states(root: Path) -> dict[str, str]:
    """`installed`, `absent`, `foreign` (another owner's file) or `hidden` (core.hooksPath), per hook."""
    if hooks_path_override(root):
        return {name: "hidden" for name in HOOK_NAMES}
    states: dict[str, str] = {}
    for name in HOOK_NAMES:
        path = _hooks_dir(root) / name
        if not path.exists():
            states[name] = "absent"
        elif _is_ours(path):
            states[name] = "installed"
        else:
            states[name] = "foreign"
    return states


def install_store_hooks(root: Path) -> dict[str, str]:
    """Install the two hooks where nobody else owns the slot, and report what happened per hook.

    Idempotent: re-running leaves a current hook untouched. A foreign hook, or a `core.hooksPath` that
    would make the install invisible, is reported and never written over."""
    if hooks_path_override(root):
        return {name: "hidden" for name in HOOK_NAMES}
    result: dict[str, str] = {}
    for name in HOOK_NAMES:
        path = _hooks_dir(root) / name
        text = hook_script(name)
        if path.exists() and not _is_ours(path):
            result[name] = "foreign"
        elif path.exists() and path.read_text(encoding="utf-8", errors="replace") == text and os.access(path, os.X_OK):
            result[name] = "installed"
        else:
            _write_atomically(path, text)
            result[name] = "installed"
    return result


def hook_problems(root: Path, states: dict[str, str]) -> list[str]:
    """One line per hook that will NOT run the store's checks, for `store init` and `store status`."""
    lines: list[str] = []
    override = hooks_path_override(root)
    if override:
        lines.append(
            f"hooks: core.hooksPath is set ({override}), so git ignores .git/hooks; the store's checks do "
            "not run on a plain git commit or git push"
        )
        return lines
    entry = f"{shlex.quote(sys.executable)} -m gr2.python_cli.store_hooks"
    for name, state in states.items():
        if state == "foreign":
            lines.append(
                f"hooks: {name} belongs to another owner and was left as it is, so the store's checks do not "
                f"run on it; call `{entry} {name}` from that hook to enable them"
            )
        elif state == "absent":
            lines.append(f"hooks: {name} is not installed; run gr2 store init to install it")
    return lines


# ── the engine, shared with the verbs ────────────────────────────────────────────────────────────


def _index_gitlinks(root: Path, index: str | None) -> dict[str, str]:
    env = {**os.environ, "GIT_INDEX_FILE": index} if index else None
    links: dict[str, str] = {}
    for record in _git(root, "ls-files", "-s", "-z", env=env).stdout.split("\0"):
        if "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode == "160000":
            links[path] = sha
    return links


def _head_gitlinks(root: Path) -> dict[str, str]:
    if _git(root, "rev-parse", "--verify", "-q", "HEAD").returncode:
        return {}
    links: dict[str, str] = {}
    for record in _git(root, "ls-tree", "-r", "-z", "HEAD").stdout.split("\0"):
        if "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        mode, _kind, sha = meta.split()
        if mode == "160000":
            links[path] = sha
    return links


def _scrub_git_env() -> str | None:
    """Remove the variables that aim git at the ROOT, returning the index path (absolute) if one was set."""
    index = os.environ.get("GIT_INDEX_FILE")
    absolute = str(Path(index).resolve()) if index else None
    for key in _LOCATING_ENV:
        os.environ.pop(key, None)
    return absolute


def _offline_hint(code: int) -> str:
    return " (offline? `git commit --no-verify` skips this check and pre-push checks again)" if code == 5 else ""


def _index_toml(root: Path, index: str | None) -> bytes | None:
    """The grip.toml the commit WILL carry (the staged blob) as BYTES, or None when none is staged. Bytes, not
    text, so a CRLF file is compared and folded without any line ending being rewritten."""
    env = {**os.environ, "GIT_INDEX_FILE": index} if index else None
    shown = subprocess.run(["git", "-C", str(root), "show", ":grip.toml"], capture_output=True, env=env)
    return shown.stdout if shown.returncode == 0 else None


def _members_from(text: str) -> list[dict[str, str]]:
    """The members of a grip.toml TEXT, in the shape the engine's helpers read (`remote` from `remotes.origin`)."""
    from . import grip_cli as gc

    members = tomllib.loads(text).get("members", [])
    if not isinstance(members, list) or not members:
        raise gc.NativeStoreRefusal("grip.toml has no members; run store init against a root that has them", 4)
    result: list[dict[str, str]] = []
    for member in members:
        item = dict(member)
        remotes = item.pop("remotes", {})
        if not isinstance(remotes, dict) or not isinstance(remotes.get("origin"), str):
            raise gc.NativeStoreRefusal(f"{item.get('name', 'member')} has no origin remote", 4)
        item["remote"] = remotes["origin"]
        result.append(item)
    return result


def _fold_pins(text: str, moves: dict[str, tuple[str, str]]) -> str:
    """Rewrite ONLY the `pin = "..."` line of each moved member ({name: (old, new)}); every other byte of the
    document, comments and keys the writer does not emit included, is kept. Refuses (None-safe) when a line cannot
    be found exactly once, so a document this cannot fold is never half-folded."""
    from . import grip_cli as gc

    chunks = re.split(r"(?m)^(?=\[\[members\]\])", text)
    for name, (old, new) in moves.items():
        name_line = re.compile(r"(?m)^name\s*=\s*" + re.escape(json.dumps(name)) + r"\s*$")
        pin_line = re.compile(r"(?m)^(pin\s*=\s*)" + re.escape(json.dumps(old)) + r"(\s*)$")
        owners = [i for i, chunk in enumerate(chunks) if name_line.search(chunk) and len(pin_line.findall(chunk)) == 1]
        if len(owners) != 1:
            raise gc.NativeStoreRefusal(
                f"cannot fold the pin of {name} into grip.toml by line; run `gr2 store commit` for this change", 4
            )
        chunks[owners[0]] = pin_line.sub(lambda m: m.group(1) + json.dumps(new) + m.group(2), chunks[owners[0]], count=1)
    return "".join(chunks)


def run_pre_commit(root: Path) -> int:
    """0 to let the commit proceed, 1 to refuse it. Prints the refusal in the verbs' own words.

    The commit is judged by the grip.toml it will CARRY (the staged blob), never by the working-tree file."""
    from . import grip_cli as gc

    index = _scrub_git_env()
    try:
        staged_bytes = _index_toml(root, index)
        if staged_bytes is None:
            return 0
        staged_text = staged_bytes.decode("utf-8")
        members = _members_from(staged_text)
        staged = _index_gitlinks(root, index)
        committed = _head_gitlinks(root)
        partial = bool(index) and Path(index).name.startswith("next-index-")
        stale: list[dict[str, str]] = []
        for member in members:
            sha = staged.get(member["path"])
            if sha is None:
                continue
            if committed.get(member["path"]) != sha:
                claimed = gc._claimed_member_paths(root, members, exclude=member)
                gc._member_coverage(root, member, sha, claimed)
            if sha != member["pin"]:
                stale.append(member)
        if stale:
            names = ", ".join(member["name"] for member in stale)
            if partial:
                raise gc.NativeStoreRefusal(
                    f"grip.toml pins differ from the staged gitlinks ({names}) and a partial commit "
                    "(git commit <paths>) cannot fold them; run git commit without paths", 4,
                )
            working = root / "grip.toml"
            if not working.is_file() or working.read_bytes() != staged_bytes:
                raise gc.NativeStoreRefusal(
                    f"grip.toml pins differ from the staged gitlinks ({names}) and grip.toml has edits that are "
                    "not staged; stage or stash your grip.toml edits, then commit", 4,
                )
            folded = _fold_pins(staged_text, {m["name"]: (m["pin"], staged[m["path"]]) for m in stale})
            working.write_bytes(folded.encode("utf-8"))
            env = {**os.environ, "GIT_INDEX_FILE": index} if index else None
            added = _git(root, "add", "grip.toml", env=env)
            if added.returncode:
                raise RuntimeError(added.stderr.strip() or "git add grip.toml failed")
    except gc.NativeStoreRefusal as exc:
        print(f"gr2 store: {exc}{_offline_hint(exc.code)}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:   # ValueError covers a TOML or UTF-8 decode failure
        print(f"gr2 store hook could not complete: {exc}", file=sys.stderr)
        return 1
    return 0


def _pushed_commits(root: Path, local: str, remote_sha: str, remote: str) -> list[str]:
    """Every commit the push would send, oldest first: `remote..local` when the remote tip is known here, else
    everything `local` reaches that no remote-tracking ref already has."""
    if remote_sha != _ZERO and _git(root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        walk = _git(root, "rev-list", "--reverse", f"{remote_sha}..{local}")
    else:
        known = _git(root, "remote").stdout.split()
        spec = f"--remotes={remote}" if remote in known else "--remotes"
        walk = _git(root, "rev-list", "--reverse", local, "--not", spec)
    if walk.returncode:
        raise RuntimeError(walk.stderr.strip() or "git rev-list failed")
    return walk.stdout.split()


def run_pre_push(root: Path, lines: list[str], remote: str = "") -> int:
    """0 to let the push proceed. EVERY pushed root commit's pins must be covered by their upstreams (a bypassed
    commit below the tip is checked like the tip)."""
    from . import grip_cli as gc

    _scrub_git_env()
    seen: set[tuple[str, str, str, str]] = set()
    try:
        for line in lines:
            fields = line.split()
            if len(fields) < 2 or fields[1] == _ZERO:
                continue
            remote_sha = fields[3] if len(fields) > 3 else _ZERO
            for revision in _pushed_commits(root, fields[1], remote_sha, remote):
                if _git(root, "cat-file", "-e", f"{revision}:grip.toml").returncode:
                    continue  # not a store commit: nothing here to check
                members = gc._native_members(root, revision=revision)
                for member in members:
                    key = (member["path"], member["pin"], member.get("upstream", ""), member["remote"])
                    if key in seen:
                        continue
                    claimed = gc._claimed_member_paths(root, members, exclude=member)
                    gc._member_coverage(root, member, member["pin"], claimed)
                    seen.add(key)
    except gc.NativeStoreRefusal as exc:
        print(f"gr2 store: {exc}{_offline_hint(exc.code)}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"gr2 store hook could not complete: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in HOOK_NAMES:
        print(f"usage: python -m gr2.python_cli.store_hooks {{{'|'.join(HOOK_NAMES)}}}", file=sys.stderr)
        return 2
    root = Path.cwd()
    if argv[0] == "pre-commit":
        return run_pre_commit(root)
    return run_pre_push(root, sys.stdin.read().splitlines(), argv[1] if len(argv) > 1 else "")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
