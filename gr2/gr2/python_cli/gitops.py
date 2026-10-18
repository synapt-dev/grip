from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import next_steps


# fsck's name rules ported from git: fsck.c fsck_tree, path.c is_ntfs_dotgit and is_ntfs_dot_generic,
# utf8.c is_hfs_dotgit. A Windows or macOS checkout reads each such name as the repository's own entry.
HFS_IGNORED = frozenset([*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF])


def _is_hfs_dot(name: str, needle: str) -> bool:
    """HFS+ drops these code points and folds ASCII case, so ".g\u200cit" opens .git."""
    folded = "".join(c for c in name if ord(c) not in HFS_IGNORED)
    return folded.isascii() and folded.lower() == "." + needle


def _ntfs_tail(rest: str) -> bool:
    """NTFS drops trailing spaces and dots, and reads ":<stream>" as a stream of the name before it."""
    return rest.split(":", 1)[0].strip(" .") == ""


def _is_ntfs_dotgit(name: str) -> bool:
    """git ends this name at a backslash as well as at ':' (is_xplatform_dir_sep), and fsck checks every part after a backslash again; _is_ntfs_dot does neither."""
    for part in name.split("\\"):
        for head in (".git", "git~1"):
            if part[:len(head)].isascii() and part[:len(head)].lower() == head and _ntfs_tail(part[len(head):]):
                return True
    return False


def _is_ntfs_dot(name: str, needle: str, short_prefix: str) -> bool:
    """`.<needle>`, its short name (six letters, ~1 to ~4), or the hashed fall-back short name."""
    lowered = lambda s: s.isascii() and s.lower()
    if name[:1] == "." and lowered(name[1:1 + len(needle)]) == needle:
        return _ntfs_tail(name[1 + len(needle):])
    if lowered(name[:6]) == needle[:6] and name[6:7] == "~" and "1" <= name[7:8] <= "4" and name[7:8]:
        return _ntfs_tail(name[8:])
    i, saw_tilde = 0, False
    while i < 8:
        c = name[i:i + 1]
        if not c:
            return False
        if saw_tilde:
            if not "0" <= c <= "9":
                return False
        elif c == "~":
            i += 1
            if not "1" <= name[i:i + 1] <= "9" or not name[i:i + 1]:
                return False
            saw_tilde = True
        elif i >= 6 or not c.isascii() or c.lower() != short_prefix[i]:
            return False
        i += 1
    return _ntfs_tail(name[8:])


def fsck_bad_name(name: str) -> str | None:
    """The message id git fsck reports for a tree or gitlink entry named ``name``, or None when fsck accepts
    the name. A tree built here holds no entry fsck would reject."""
    if not name:
        return "emptyName"
    if name == ".":
        return "hasDot"
    if name == "..":
        return "hasDotdot"
    if _is_hfs_dot(name, "git") or _is_ntfs_dotgit(name):
        return "hasDotgit"
    # fsck checks the name after every backslash again for .gitmodules (not .gitattributes).
    tails = [name, *(name[i + 1:] for i, c in enumerate(name) if c == "\\")]
    if _is_hfs_dot(name, "gitmodules") or any(_is_ntfs_dot(t, "gitmodules", "gi7eba") for t in tails):
        return "gitmodulesBlob"
    if _is_hfs_dot(name, "gitattributes") or _is_ntfs_dot(name, "gitattributes", "gi7d29"):
        return "gitattributesBlob"
    return None


# One table for unsafe text, two public names. Both refuse Cc (C0, DEL, C1 such as U+0085 and the one-byte CSI U+009B),
# Zl and Zp (U+2028, U+2029) and Cf, which holds the bidi controls (U+202A..U+202E, U+2066..U+2069, LRM, RLM, ALM) and
# the zero-width and invisible characters (U+200B, WJ, BOM, SHY): printed raw, one can rewrite or hide what a reader
# sees, and a NUL handed to a path or a git argument raises. A person's name keeps only U+200C ZWNJ and U+200D ZWJ,
# which Persian and Kurdish names are written with; an identifier (a remote, a path, a ref) refuses those too.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_NAME_KEEPS = frozenset({"\u200c", "\u200d"})


def unsafe_in_name(text: str) -> bool:
    """True if a person's name carries a Cc, Cf, Zl or Zp character other than U+200C ZWNJ and U+200D ZWJ."""
    return any(c not in _NAME_KEEPS and unicodedata.category(c) in _UNSAFE_CATEGORIES for c in text)


def unsafe_in_identifier(text: str) -> bool:
    """True if a remote, path or ref carries any Cc, Cf, Zl or Zp character (bidi controls are Cf)."""
    return any(unicodedata.category(c) in _UNSAFE_CATEGORIES for c in text)


_SSH_GITHUB_PREFIX = "git@github.com:"
_HTTPS_GITHUB_PREFIX = "https://github.com/"


class GitMissingError(FileNotFoundError):
    """The git executable is not on PATH (a fresh container without git). One
    sentence naming git replaces a traceback — or worse, a False answer that
    reads as 'this is not a git repository' on a machine that simply has no
    git."""


def rewrite_ssh_github_url(url: str) -> str:
    """``git@github.com:OWNER/REPO(.git)`` -> ``https://github.com/OWNER/REPO.git``.

    Any other URL (a different host, a different scheme, an already-HTTPS
    GitHub URL) is returned unchanged. The host's SSH is
    1Password-managed and off-limits to agents, so a spec-declared SSH GitHub
    URL is otherwise permanently unreachable for gr2's own network operations.
    """
    if not url.startswith(_SSH_GITHUB_PREFIX):
        return url
    rest = url[len(_SSH_GITHUB_PREFIX):]
    if not rest.endswith(".git"):
        rest = f"{rest}.git"
    return f"{_HTTPS_GITHUB_PREFIX}{rest}"


def _ssh_rewrite_enabled() -> bool:
    value = os.environ.get("GR2_SYNC_REWRITE_SSH_URLS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _effective_remote_url(url: str) -> str:
    """The URL gr2 should actually dial for ``url``: rewritten when it is an
    SSH GitHub URL and the rewrite is enabled (default on), else unchanged."""
    if _ssh_rewrite_enabled():
        return rewrite_ssh_github_url(url)
    return url


# Every git call gr2 makes goes through run(): no auto maintenance (a background `git maintenance run --auto`
# writes outside anything a caller checks or waits for), and a bound on how long it may take.
# And a transport allow-list: git runs any installed `git-remote-<name>` for a `<name>::<address>` url, and a
# member url in grip.toml or a record reaches `git clone`. Written as config, not GIT_ALLOW_PROTOCOL, because
# that variable makes each listed protocol `always` and so loosens git's `file=user` default for submodules.
TRANSPORTS = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always",
              "-c", "protocol.ssh.allow=always", "-c", "protocol.git.allow=always", "-c", "protocol.file.allow=user")
NO_AUTO = ("-c", "maintenance.auto=false", "-c", "gc.auto=0", *TRANSPORTS)
DEFAULT_TIMEOUT = 600.0


class GitError(RuntimeError):
    """A git command exited non-zero. Carries the return code and git's own message."""

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str, secrets: frozenset[str] = frozenset()):
        self.git_args, self.returncode, self.stderr = args, returncode, stderr
        shown = [next_steps._shown(_masked(_shown_arg(arg), secrets)) for arg in args[:2]]
        super().__init__(f"git {' '.join(shown)}: {_masked(_shown_stderr(args, stderr), secrets)}".rstrip(": "))


_CREDENTIAL_SHOWN = "<a remote with a credential>"
#: A credential value shorter than this is not masked, so a two-letter token does not shred ordinary words.
_MIN_SECRET = 4
_QUERY_PAIR = re.compile(r"""([A-Za-z0-9_.-]+)=([^&;#\s'"()<>]*)""")


def credential_values(argv, cwd: Path | None = None) -> frozenset[str]:
    """Every credential VALUE a git call could echo back: the userinfo password (and a non-ssh userinfo user),
    each credential-named query value and any fragment, taken from the call's arguments and from every
    `remote.*.url` / `remote.*.pushurl` of the repository at `cwd`, each raw, percent-decoded and re-encoded.
    A git or server message may echo a value in a shape no pattern expects; masking the values catches it."""
    from .grip import _CREDENTIAL_PARAMS  # lazy, as _shown_arg

    texts = [str(arg) for arg in argv]
    if cwd is not None:
        proc = run(cwd, "config", "--get-regexp", r"^remote\..*\.(push)?url$", timeout=30)
        texts += [line.split(" ", 1)[1] for line in proc.stdout.splitlines() if " " in line]
    found: set[str] = set()
    for text in texts:
        if "://" in text:
            try:
                parts = urlsplit(text.split("::", 1)[-1] if "::" in text.split("://", 1)[0] else text)
                found.add(parts.password or "")
                if parts.scheme.lower() not in {"ssh", "git+ssh", "ssh+git"}:
                    found.add(parts.username or "")
            except ValueError:
                pass
        if "#" in text and "://" in text:
            found.add(text.split("#", 1)[1])
        if "?" in text:
            for name, value in _QUERY_PAIR.findall(text.split("?", 1)[1]):
                if name.strip().lower().replace("-", "_") in _CREDENTIAL_PARAMS:
                    found.add(value)
    values = {form for value in found for form in (value, unquote(value), quote(unquote(value), safe=""))}
    return frozenset(value for value in values if len(value) >= _MIN_SECRET)


def _masked(text: str, secrets: frozenset[str]) -> str:
    """`text` with every known credential value replaced by the placeholder, longest first, then every run
    that DECODES to a value: each character written as itself or as its percent-encoded UTF-8 bytes, hex in
    either case, so an echo in an encoding credential_values did not build is masked too."""
    ordered = sorted(secrets, key=len, reverse=True)
    for value in ordered:
        text = text.replace(value, _CREDENTIAL_SHOWN)
    if "%" not in text:
        return text

    def encoded(char: str) -> str:
        hexed = (f"{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        return "".join("%" + "".join(f"[{d}{d.lower()}]" if d.isalpha() else d for d in pair) for pair in hexed)

    for value in ordered:
        pattern = re.compile("".join(f"(?:{re.escape(char)}|{encoded(char)})" for char in value))
        text = pattern.sub(_CREDENTIAL_SHOWN, text)
    return text


def shown_git_text(args, text: str, cwd: Path | None = None) -> str:
    """git's output for a call run with `args` (in `cwd`) as GitError would show it, for a caller that puts it in its own message."""
    return _masked(_shown_stderr(args, text), credential_values(args, cwd))
# git's own rule for an scp-form address: a colon before the first slash (so `./a:b` stays a local path)
_SCP_ADDRESS = re.compile(r"[^/]*:")


def _strip_userinfo(url: str) -> str:
    from .migration import _strip_url_userinfo  # lazy: migration imports this module's importers

    return _strip_url_userinfo(url)


def _shown_arg(arg: str) -> str:
    """An argument as an error message may show it: a URL loses its userinfo, and a URL that still carries a
    credential after that (a token in the query, an unparsable address) is named, not shown. An scp-form address
    (user@host:path, which git reaches over ssh) that carries a credential is named too. Any other argument is
    returned as written."""
    from .grip import url_has_credentials  # lazy, as above

    if "://" not in arg:
        return _CREDENTIAL_SHOWN if _SCP_ADDRESS.match(arg) and url_has_credentials(arg) else arg
    if not url_has_credentials(arg):
        return arg
    stripped = _strip_userinfo(arg)
    return _CREDENTIAL_SHOWN if url_has_credentials(stripped) else stripped


def _shown_stderr(args, stderr: str) -> str:
    """git's stderr for a command run with `args`, each argument that carries a credential replaced as `_shown_arg`
    shows it. git strips userinfo from the URLs it prints, not a credential in the query, so both forms are replaced.
    A remote named in `args` prints the URL from git's config, so every URL in the stderr is shown as `_shown_arg`
    shows it too. git prints an scp-form address without its `user@` (`'host:x.git?access_token=...'`), so every
    word with a colon and no `://` is shown as `_shown_arg` shows it as well; a word with no credential stays."""
    for arg in args:
        if (redacted := _shown_arg(str(arg))) != str(arg):
            stderr = stderr.replace(str(arg), redacted).replace(_strip_userinfo(str(arg)), redacted)
    stderr = _URL_IN_TEXT.sub(lambda m: _shown_arg(m.group(0)), stderr)
    return _SCP_IN_TEXT.sub(lambda m: m.group(0) if "://" in m.group(0) else _shown_arg(m.group(0)), stderr)


_URL_IN_TEXT = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s'"]+""")
_SCP_IN_TEXT = re.compile(r"""[^\s'"/]+:[^\s'"]*""")


# Git's global options that take their value as the next word; `--opt=value` forms are one word.
# Measured on git 2.50: each takes its value as the next word. `--exec-path` alone prints a path and exits.
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"}


def command_of(argv) -> str | None:
    """The git subcommand in a git argv, after git's own global options (`git -C x -c k=v clone ...` -> clone)."""
    words = list(argv)[1:]
    i = 0
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in _GLOBAL_WITH_VALUE else 1
    return words[i] if i < len(words) else None


#: Git locates a repository through these. gr2 always names its repository with `-C` (or a cwd), so a value
#: inherited from the caller's environment (a hook, `GIT_DIR=... gr2 ...`) would aim git at a DIFFERENT
#: repository. Each is dropped when its value is the inherited one; a caller that names its own value (a temporary
#: GIT_INDEX_FILE) keeps it. The limit: a caller that names the SAME value it inherited gets it stripped too.
LOCATING_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)


def _child_env(env: dict | None) -> dict | None:
    """The environment git runs with: `env` (or the inherited one) without the inherited locating variables."""
    inherited = [key for key in LOCATING_ENV if key in os.environ]
    if not inherited:
        return env
    child = dict(os.environ if env is None else env)
    for key in inherited:
        if child.get(key) == os.environ[key]:
            del child[key]
    return child


#: The git commands that talk to a remote, whose failure output can carry a URL with a credential in it.
NETWORK = frozenset({"clone", "fetch", "ls-remote", "push", "pull", "remote", "submodule"})


def _repo_dir(args, cwd: Path | None) -> Path | None:
    """The repository a git call runs in, read from git's own global options in `args` as git reads them.

    Every `-C` is applied in order from `cwd`: a relative one joins the path so far, an absolute one replaces it, an
    empty one is skipped (`git -C W -C repo` runs in W/repo). The last `--git-dir` / `--git-dir=` then applies on top."""
    here, git_dir = cwd, None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        word = args[i]
        if word in _GLOBAL_WITH_VALUE and i + 1 < len(args):
            value = args[i + 1]
            if word == "-C" and value:
                here = here / value if here is not None else Path(value)
            elif word == "--git-dir":
                git_dir = value
            i += 2
            continue
        if word.startswith("--git-dir="):
            git_dir = word[len("--git-dir="):]
        i += 1
    if git_dir is not None:
        return here / git_dir if here is not None else Path(git_dir)
    return here


def _spawn(argv: list[str], *, cwd: Path | None, input, binary: bool, env: dict | None,
           timeout: float | None, raise_timeout: bool) -> subprocess.CompletedProcess:
    """The one place gr2 starts git: an expiry is rc 124 with empty output unless the caller asks for the raise.

    A clone is never bounded, whichever helper asked for it: its duration scales with the repository.
    A failed text call of a NETWORK command comes back with its stdout and stderr masked as shown_git_text masks
    them, because callers put git's output in a message as it is (often `stderr or stdout`)."""
    command = command_of(argv)
    if command == "clone":
        timeout = None
    try:
        proc = subprocess.run(argv, cwd=cwd, input=input, env=_child_env(env), capture_output=True, text=not binary,
                              check=False, timeout=timeout)
        # `config` (which credential_values runs to read the remote urls) is not in NETWORK, so this cannot recurse.
        if proc.returncode == 0 or binary or command not in NETWORK:
            return proc
        args = logical_argv(argv)[1:]
        repo = _repo_dir(args, cwd)
        return subprocess.CompletedProcess(proc.args, proc.returncode,
                                           shown_git_text(args, proc.stdout, repo) if proc.stdout else proc.stdout,
                                           shown_git_text(args, proc.stderr, repo) if proc.stderr else proc.stderr)
    except subprocess.TimeoutExpired:
        if raise_timeout:
            raise
        note = f"git timed out after {timeout}s"
        return subprocess.CompletedProcess(argv, 124, stdout=b"" if binary else "",
                                           stderr=note.encode() if binary else note)
    except FileNotFoundError as exc:
        # The spawn itself failed because the git EXECUTABLE is absent (exc.filename == "git"). A nonexistent
        # cwd raises FileNotFoundError naming the cwd, which is a different state and stays untouched.
        if exc.filename == "git":
            raise GitMissingError("git is not on PATH; install git, then run the verb again") from None
        raise


def run(
    cwd: Path, *args: str, input: str | bytes | None = None, binary: bool = False,
    env: dict | None = None, timeout: float | None = DEFAULT_TIMEOUT, via_cwd: bool = False,
    raise_timeout: bool = False,
) -> subprocess.CompletedProcess:
    """Run one git command; never raises for a non-zero exit.

    `via_cwd` runs in `cwd` (a missing directory raises FileNotFoundError naming it); otherwise `-C cwd`, so a
    missing directory is git's own non-zero exit."""
    argv = ["git", *NO_AUTO, *(() if via_cwd else ("-C", str(cwd))), *args]
    return _spawn(argv, cwd=cwd if via_cwd else None, input=input, binary=binary, env=env, timeout=timeout,
                  raise_timeout=raise_timeout)


def start(cwd: Path, *args: str, env: dict | None = None) -> subprocess.Popen:
    """One long-lived git process with binary pipes on stdin and stdout, for a caller that streams many
    requests through one command (``cat-file --batch``). The same flags as run(); no timeout, because the
    caller owns its lifetime and must close it. stderr is discarded: batch commands answer on stdout.
    `env`, as for run(), replaces the inherited environment."""
    try:
        return subprocess.Popen(["git", *NO_AUTO, "-C", str(cwd), *args],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=_child_env(env))
    except FileNotFoundError as exc:
        if exc.filename == "git":
            raise GitMissingError("git is not on PATH; install git, then run the verb again") from None
        raise


def run_argv(argv: list[str], *, cwd: Path | None = None, input: str | bytes | None = None, binary: bool = False,
             env: dict | None = None, timeout: float | None = DEFAULT_TIMEOUT,
             raise_timeout: bool = False) -> subprocess.CompletedProcess:
    """For callers that build a git argv as a list: the same flags and bound as run(). argv[0] must be git."""
    if not argv or argv[0] != "git":
        raise ValueError("run_argv runs git only")
    return _spawn(["git", *NO_AUTO, *argv[1:]], cwd=cwd, input=input, binary=binary, env=env, timeout=timeout,
                  raise_timeout=raise_timeout)


def clone(*args: str, cwd: Path | None = None, binary: bool = False, env: dict | None = None,
          timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git clone <args>`: the one place gr2 clones. Unbounded by default, because a clone's
    duration scales with the repository, not with anything gr2 controls."""
    return run_argv(["git", "clone", *args], cwd=cwd, binary=binary, env=env, timeout=timeout)


def logical_argv(argv) -> list[str]:
    """`argv` without the shared no-auto flags: the git command as its caller wrote it (for tests and logs)."""
    argv = list(argv)
    if argv[:1] == ["git"] and argv[1:1 + len(NO_AUTO)] == list(NO_AUTO):
        return ["git", *argv[1 + len(NO_AUTO):]]
    return argv


def check(proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """`check=True` for a run()/run_argv() result: raise CalledProcessError on a non-zero exit."""
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc


def _raise(proc: subprocess.CompletedProcess, args: tuple[str, ...], error: type[Exception] | None,
           cwd: Path | None = None) -> None:
    if proc.returncode:
        err = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else proc.stderr
        exc = GitError(args, proc.returncode, err.strip(), credential_values([*proc.args, *args], cwd))
        raise exc if error is None else error(str(exc))


def out(cwd: Path, *args: str, error: type[Exception] | None = None, strip: bool = True, **kw) -> str:
    """Text stdout of one git command; a non-zero exit raises GitError, or `error` when given."""
    proc = run(cwd, *args, **kw)
    _raise(proc, args, error, cwd)
    return proc.stdout.strip() if strip else proc.stdout


def out_bytes(cwd: Path, *args: str, error: type[Exception] | None = None, **kw) -> bytes:
    """Raw stdout bytes of one git command (stdin may be bytes); a non-zero exit raises like out()."""
    proc = run(cwd, *args, binary=True, **kw)
    _raise(proc, args, error, cwd)
    return proc.stdout


def positional(value: str, kind: str, error: type[Exception] = RuntimeError) -> str:
    """A user value bound for a git argv as a positional (a remote, a ref, a path, a url) that git would parse as
    an option (e.g. --upload-pack=..., which runs a program) never reaches it. A configured remote name, a path and
    a url all pass; callers still place the value after ``--`` where the git command takes one."""
    if not value or value.startswith("-"):
        raise error(f"{kind}_option_shaped: refusing a {kind} that begins with '-'")
    return value


def plain_remote(remote: str, error: type[Exception] = RuntimeError) -> str:
    """A remote git would parse as an option (e.g. --upload-pack=...) never reaches a git argv."""
    return positional(remote, "remote", error)


class LsRemoteMalformed(ValueError):
    """An ls-remote answer holding a line that is not a row (no tab, or an oid that is not 40 or 64 lowercase hex):
    a fault, never an absent ref."""


_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def ls_remote_rows(listing: str, symref: bool = False) -> list[tuple[str, str]]:
    """(oid, name) per row of ls-remote output, split on "\n" only: str.splitlines and str.split() also cut at U+2028
    and U+0085, which a ref name may hold. A non-empty line without a tab, or whose oid is not 40 (sha1) or 64 (sha256)
    lowercase hex, raises LsRemoteMalformed: dropping it would read a garbled answer as "the remote does not hold that
    ref", and keeping it would hand a caller a tip git never named, so each caller turns it into its own fault. Only a
    caller that asked ``--symref`` passes ``symref=True``, and then a "ref: <target>" row stands in place of an oid."""
    rows = []
    for line in listing.split("\n"):
        if not line:
            continue
        if "\t" not in line:
            raise LsRemoteMalformed(f"ls-remote answered a line that is not a row: {line[:80]!r}")
        oid, name = line.split("\t", 1)
        if not (_OID.fullmatch(oid) or (symref and oid.startswith("ref: "))):
            raise LsRemoteMalformed(f"ls-remote answered a row whose oid is not an object id: {line[:80]!r}")
        rows.append((oid, name))
    return rows


def ls_remote_oid(listing: str, ref: str) -> str | None:
    """The oid of the one ls-remote row whose name is exactly ``ref``, or None when no row names it. ls-remote matches
    its pattern by tail, so refs/heads/a/<ref> answers too and may sort first: the first row is an order, not the asked
    ref. Two rows naming ``ref`` raise ValueError rather than pick one."""
    oids = [oid for oid, name in ls_remote_rows(listing) if name == ref]
    if len(oids) > 1:
        raise ValueError(f"ls-remote listed {len(oids)} rows named {ref!r}")
    return oids[0] if oids else None


def for_each_ref_rows(listing: str) -> list[str]:
    """The non-empty rows of for-each-ref output, split on "\n" only. A ref name cannot hold "\n" (or any control
    character), but it may hold U+2028 and U+0085, which str.splitlines also cuts at and str.strip/str.split() also
    eat; each caller splits its own --format fields from the row."""
    return [line for line in listing.split("\n") if line]


def git(
    cwd: Path, *args: str, timeout: float | None = DEFAULT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run git. `timeout` bounds the call; an expiry is a FAILED result, not a raise.

    A timeout is a way of not finding out, and callers here already have a shape for
    that: a non-zero return code. Returning `returncode=124` (the conventional
    timeout code) with an empty stdout keeps them total, and keeps an unanswered
    remote on the same channel as a refused one instead of adding an exception every
    caller must remember to catch. Measured need: `ls-remote` against a peer that
    accepts and never replies blocks indefinitely, which is how a bound became
    necessary rather than nice.
    """
    return run(cwd, *args, timeout=timeout, via_cwd=True)


#: A replace ref (refs/replace/<sha>) swaps one object for another on every read, so a tip replaced by a commit
#: whose parent is X would read as descending from X; a line "<tip> <X>" in .git/info/grafts gives the tip X as a
#: parent the same way, and GIT_NO_REPLACE_OBJECTS does not turn grafts off. An ancestry answer gr2 acts on reads
#: the real history: no replace refs, an empty graft file in place of info/grafts, and no commit-graph (ancestry_proc).
REAL_HISTORY_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull}


def ancestry_proc(repo: Path, ancestor: str, descendant: str, *, via_cwd: bool = False) -> subprocess.CompletedProcess:
    """`git merge-base --is-ancestor ancestor descendant` in the real history (replace refs, grafts and
    commit-graph files ignored), for a caller that reports git's own stderr; rc 0 is yes, 1 is no, anything else is "could not tell". `via_cwd` as in run()."""
    # Any graft file, even an empty one, makes git print a deprecation hint on stderr ahead of its own error. A
    # commit-graph file (this repo's or an alternate's) stores parents of its own that git trusts over the objects, so
    # the walk reads the commit objects instead. REAL_HISTORY_ENV goes last so a caller's GIT_GRAFT_FILE cannot win.
    return run(repo, "-c", "advice.graftFileDeprecated=false", "-c", "core.commitGraph=false",
               "merge-base", "--is-ancestor", ancestor, descendant,
               env={**os.environ, **REAL_HISTORY_ENV}, via_cwd=via_cwd)


def is_ancestor(repo: Path, ancestor: str, descendant: str, *, via_cwd: bool = False) -> bool | None:
    """True when `ancestor` is an ancestor of `descendant` in the real history (replace refs and grafts ignored),
    False when it is not, None when git could not tell (a missing object, a bad name, a timeout)."""
    rc = ancestry_proc(repo, ancestor, descendant, via_cwd=via_cwd).returncode
    return True if rc == 0 else False if rc == 1 else None


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        # A FILE as cwd raises NotADirectoryError out of subprocess.run rather
        # than returning a failed CompletedProcess (measured: a unit home
        # holding a plain `unit.toml` crashed every single-repo verb through
        # repos_under). A non-directory is not a git repo, so this is the
        # answer, not an exception.
        return False
    try:
        proc = git(path, "rev-parse", "--is-inside-work-tree")
    except PermissionError:
        # A directory the caller cannot enter (chmod 000) raises
        # PermissionError out of the subprocess cwd; a directory that cannot
        # be probed is not answerable as a repository, so False is the
        # answer, not a traceback. OSError would be wider: it would swallow
        # the FileNotFoundError for a missing git EXECUTABLE and turn a
        # no-git machine into a false 'not a git repository' sentence;
        # GitMissingError propagates instead.
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_bare_git_repo(path: Path) -> bool:
    """A bare repository (HEAD + a valid git dir, no work tree). The shape a
    stranger uses as a local upstream; the repo scanner skipped it before."""
    if not path.is_dir():
        # Same non-directory guard as is_git_repo (B1 of the alpha-2 gate).
        return False
    try:
        proc = git(path, "rev-parse", "--is-bare-repository")
    except PermissionError:
        # Same unenterable-directory answer as is_git_repo.
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_repo_root(path: Path) -> bool:
    """This path ITSELF is the top of a work tree -- not merely inside one.

    `is_git_repo` above answers the second question, and answers it
    truthfully, from the ENCLOSING repository. The two diverge for any plain
    directory that happens to sit inside a checkout, which is exactly what a
    staging directory or a unit member path is, so a caller meaning "is there
    already a clone HERE" got True for an empty directory and skipped its work.
    `--show-toplevel` is the discriminator: it names the root the path resolves
    into, and comparing it to the path is the question this answers. Both sides
    are resolved so a symlinked temp root (/var -> /private/var) does not read
    as a mismatch.
    """
    if not path.is_dir():
        return False
    try:
        proc = git(path, "rev-parse", "--show-toplevel")
    except PermissionError:
        # Same unenterable-directory answer as is_git_repo: a directory that
        # cannot be probed is not answerable as a repository.
        return False
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == path.resolve()
    except OSError:
        return False


def is_git_repository(path: Path) -> bool:
    """A repository of either shape: a work tree or a bare repo."""
    return is_git_repo(path) or is_bare_git_repo(path)


def repo_path_state(path: Path) -> str:
    """What a member path actually holds: ``repo_root``, ``empty_placeholder``, or ``neither``.

    Three answers, because callers decide differently on each one, and collapsing
    them into a boolean is how the read-through survived four sites: the
    validator skips a placeholder and reports ``neither``; the sync planner
    plans a clone into a placeholder and reports ``neither``; the cache seed
    treats both non-repo answers as "no local source"; merge verification
    refuses both, because the DAG is unavailable for either. Asking
    `is_git_repo` here was the defect: it answers --is-inside-work-tree, which
    is true for any directory inside a checkout, so a plain directory at a
    member path read as the enclosing repository and every site above skipped
    its work.

    ``empty_placeholder`` is a directory that can be READ and holds nothing:
    the ordinary state of a freshly cloned superproject's member paths, where
    `git clone` creates the submodule mount points and leaves them empty until
    `submodule update --init`. An UNREADABLE directory is ``neither``, not an
    empty one: we cannot know what it holds, and the code this helper replaces
    (`is_git_repo`, which catches PermissionError and answers False) reported a
    conflict there, so the conflict is what must keep firing. A path that does
    not exist at all is ``neither`` too; callers that must distinguish missing
    from present-and-wrong ask `exists()` first, exactly as they do today --
    this helper answers the question about what a PRESENT path holds.
    """
    if not path.is_dir():
        # A file at a member path, or a path that does not exist at all:
        # neither is a repository nor an unfilled placeholder. The sites keep
        # their own exists() branches for the plan-the-work decisions.
        return "neither"
    try:
        empty = not any(path.iterdir())
    except OSError:
        # Unreadable: not an empty one, and not answerable as a repository.
        return "neither"
    if is_repo_root(path):
        return "repo_root"
    return "empty_placeholder" if empty else "neither"


class OutsideRepoError(Exception):
    """A single-repo verb was run from a path that is not a git work tree."""


def repos_under(path: Path, limit: int = 5) -> list[Path]:
    """Git WORK TREE repositories directly under `path` (and `path` itself),
    nearest name order, capped so a refusal line stays a line. Only
    directories are probed: a regular file as cwd raises NotADirectoryError
    out of the git probes. Work trees only: the consumers of this listing
    (the single-repo refusals) name repos the verb can run inside, and those
    verbs need a work tree — a bare repository would be an unusable
    suggestion. An unenterable child (chmod 000) is skipped: the helpers
    answer False for it."""
    found: list[Path] = []
    if is_git_repo(path):
        found.append(path)
    if not path.is_dir():
        return found
    for child in sorted(path.iterdir()):
        if len(found) >= limit:
            break
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if is_git_repo(child):
            found.append(child)
    return found


def require_git_repo(path: Path, verb: str) -> None:
    """One-sentence refusal for a single-repo verb run from outside a git work
    tree, naming what the verb needs and listing the repos found under the
    cwd. Measured failures being replaced: git's own raw text ("fatal: not a
    git repository"), git's full usage dump from the commit path, and, inside
    a bare repository, git's "this operation must be run in a work tree".
    These verbs need a WORK TREE, not any repository."""
    if is_git_repo(path):
        return
    if is_bare_git_repo(path):
        found = repos_under(path)
        listing = f" Repositories found under it: {', '.join(item.name for item in found)}." if found else ""
        raise OutsideRepoError(
            f"gr2 {verb} runs inside one repository of the workspace; "
            f"{path} is a bare repository, and this verb needs a work tree."
            f"{listing} Run inside a work tree, or pass --repo-path"
        )
    found = repos_under(path)
    if found:
        listing = ", ".join(item.name for item in found)
        raise OutsideRepoError(
            f"gr2 {verb} runs inside one repository of the workspace; "
            f"{path} is not a git repository (repositories found under it: "
            f"{listing}; run inside one, or pass --repo-path)"
        )
    raise OutsideRepoError(
        f"gr2 {verb} runs inside one repository of the workspace; "
        f"{path} is not a git repository (no repositories found under it)"
    )


def repo_dirty(path: Path) -> bool:
    proc = git(path, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def remote_origin_url(path: Path) -> str | None:
    proc = git(path, "config", "--get", "remote.origin.url")
    if proc.returncode != 0:
        return None
    # git's one line terminator only: str.strip() would drop U+0085 and U+001C..U+001F before the identity check.
    value = proc.stdout.removesuffix("\n")
    return value or None


def current_head_sha(path: Path) -> str | None:
    proc = git(path, "rev-parse", "HEAD")
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def commits_between(path: Path, old_sha: str | None, new_sha: str | None) -> int:
    if not new_sha:
        return 0
    if not old_sha:
        proc = git(path, "rev-list", "--count", new_sha)
    else:
        proc = git(path, "rev-list", "--count", f"{old_sha}..{new_sha}")
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError:
        return 0


def conflicting_files(path: Path) -> list[str]:
    proc = git(path, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ensure_repo_cache(url: str, cache_repo_root: Path, *, local_source: Path | None = None) -> bool:
    """Ensure a local bare mirror exists for a repo URL.

    When the cache does not yet exist and ``local_source`` names an existing git
    checkout (a repo already materialized as a shared checkout next
    to the missing cache), the mirror is seeded from that LOCAL checkout -- no
    network involved -- and the mirror's ``origin`` remote is then repointed to
    the EFFECTIVE url (rewritten when it is an SSH GitHub url, see step 2 below)
    so it still tracks a remote that will actually work on the next refresh.
    Falls back to a network clone from the effective url when ``local_source``
    is absent or is not a git repo.

    An EXISTING cache self-heals the same way before refreshing: its origin is
    repointed to the effective url first (a cache seeded before this fix
    existed, or by a human whose own SSH works, must not keep failing on every
    later refresh), then ``remote update --prune`` runs.

    Returns True if a cache was created, False if it already existed and was refreshed.
    """
    effective_url = _effective_remote_url(url)

    if cache_repo_root.exists():
        if not is_git_dir(cache_repo_root):
            raise SystemExit(f"repo cache path exists but is not a git dir: {cache_repo_root}")

        if effective_url != url:
            remote_proc = run_argv(["git", "--git-dir", str(cache_repo_root), "remote", "set-url", "origin", effective_url])
            if remote_proc.returncode != 0:
                raise SystemExit(
                    f"failed to repoint repo cache origin to {_shown_arg(effective_url)}:\n"
                    f"{remote_proc.stderr or remote_proc.stdout}"
                )

        proc = run_argv(["git", "--git-dir", str(cache_repo_root), "remote", "update", "--prune"])
        if proc.returncode != 0:
            raise SystemExit(f"failed to refresh repo cache {cache_repo_root}:\n{proc.stderr or proc.stdout}")
        return False

    # SEAM DEFENSE, not just the callers' fix: the state helper decides
    # whether a caller-supplied source is a repository, because this function
    # is the one that clones from it. The read-through let a plain directory
    # be passed as ``local_source`` (is_git_repo answered for the ENCLOSING
    # checkout) and the seed then failed with git's misleading
    # "repository '<plain dir>' does not exist". A plain directory and an
    # empty placeholder are both "no local source": the seed falls back to
    # the effective url, which is the one thing here that can actually
    # resolve.
    seed_from_local = local_source is not None and repo_path_state(local_source) == "repo_root"
    seed_source = str(local_source) if seed_from_local else effective_url

    cache_repo_root.parent.mkdir(parents=True, exist_ok=True)
    proc = clone("--mirror", "--", seed_source, str(cache_repo_root))
    if proc.returncode != 0:
        raise SystemExit(f"failed to seed repo cache {_shown_arg(seed_source)} -> {cache_repo_root}:\n{proc.stderr or proc.stdout}")

    if seed_from_local:
        # The mirror's origin must be the URL that will actually work for a
        # later refresh (`remote update`), not necessarily the raw spec url --
        # an unreachable SSH url here would just move today's seed failure
        # to every future refresh instead.
        remote_proc = run_argv(
            ["git", "--git-dir", str(cache_repo_root), "remote", "set-url", "origin", effective_url],
        )
        if remote_proc.returncode != 0:
            raise SystemExit(
                f"seeded repo cache from local checkout {local_source} but failed to repoint "
                f"origin to {_shown_arg(effective_url)}:\n{remote_proc.stderr or remote_proc.stdout}"
            )
    return True


def clone_repo(url: str, target_repo_root: Path, *, reference_repo_root: Path | None = None) -> bool:
    # "Is there already a clone AT this path", not "is this path inside a
    # repository". `is_git_repo` answers the second question -- truthfully, from
    # the ENCLOSING repository -- so a directory merely inside a workspace root
    # read as an existing clone and this function returned False WITHOUT
    # cloning. Measured: a staging directory created inside a superproject came
    # back "already a repo", the clone was skipped, and the pin check then ran
    # against a repository that was never created, refusing over an empty
    # directory.
    if target_repo_root.exists() and is_repo_root(target_repo_root):
        return False
    target_repo_root.parent.mkdir(parents=True, exist_ok=True)
    reference = ["--reference-if-able", str(reference_repo_root)] if reference_repo_root is not None else []
    proc = clone(*reference, "--", url, str(target_repo_root))
    if proc.returncode != 0:
        raise SystemExit(f"failed to clone {_shown_arg(url)} -> {target_repo_root}:\n{proc.stderr or proc.stdout}")
    return True


def checkout_declared_pin(repo_root: Path, pin: str, *, member: str) -> None:
    """Put a member at the commit the root declares, after a clone.

    A clone lands on the REMOTE'S DEFAULT BRANCH TIP, which for a superproject
    member is not where the root says it is: the gitlink pins a commit, and that
    pin is the declaration the whole feature exists to record. Measured on
    ``git-training-open/submodule-example``: after ``workspace init
    --from-superproject`` and ``materialize``, ``example1`` sat on ``master`` at
    ``ceed35b970e2`` while the root pinned ``065be099e95a`` -- and
    ``jabberwocky`` looked correct only because that repo's master tip happens
    to equal its pin. So a fixture built on the second member passes while doing
    the wrong thing, which is why the test uses the pair.

    TWO EDGES, decided rather than assumed:

    - **A pin the clone cannot reach** (a shallow or depth-limited clone). Fetch
      that one object by sha. If the server refuses, REFUSE with an error naming
      the pin, the member and the command to try by hand. **Never fall back to
      the default tip**: a silent fallback is exactly the defect being fixed, so
      the failure mode here is loud on purpose.
    - **An empty pin** is not this function's business; the caller skips it. A
      member with no pin stays on its default branch and the status calls it
      UNPINNED rather than at-pin.
    """
    # A caller SHOULD skip an unpinned member, and this no-op is what makes the
    # contract safe when one does not: without it an empty pin falls into the
    # unreachable-pin path below and refuses with a message naming an empty pin,
    # which is a worse answer than doing nothing. The test that pins the contract
    # is what found this -- the docstring promised a no-op the code did not do.
    if not pin:
        return
    positional(pin, "pin", SystemExit)

    have = lambda: git(repo_root, "cat-file", "-e", f"{pin}^{{commit}}").returncode == 0
    if not have():
        # `--depth 1` ONLY when the clone is already shallow. Passing it
        # unconditionally re-shallowed a FULL clone, which throws away history
        # the caller never asked to lose and is a worse outcome than the missing
        # object it was meant to fetch.
        fetch_args = ["fetch"]
        shallow = git(repo_root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        if shallow:
            fetch_args.extend(["--depth", "1"])
        fetch_args.extend(["origin", "--", pin])
        git(repo_root, *fetch_args)
        if not have():
            raise SystemExit(
                f"member '{member}' declares pin {pin}, the clone cannot reach that commit, and "
                f"fetching it by sha from origin did not provide it. Try "
                f"`git -C {next_steps._shown(repo_root)} fetch --depth 1 origin -- {next_steps._shown(pin)}` by hand to see why. This verb "
                "will not leave the member on its default branch instead: that is a commit the "
                "root never declared."
            )
    checked = git(repo_root, "checkout", "--detach", pin)
    if checked.returncode != 0:
        raise SystemExit(
            f"member '{member}' declares pin {pin} but checking it out failed:\n"
            f"{checked.stderr or checked.stdout}"
        )


def branch_exists(repo_root: Path, branch: str) -> bool:
    return git(repo_root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0


def fetch_ref(repo_root: Path, remote: str, source_ref: str, local_branch: str) -> None:
    proc = git(repo_root, "fetch", "--no-tags", "--refmap=", remote, f"{source_ref}:refs/heads/{local_branch}")
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch {source_ref} from {remote} into {local_branch} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def refresh_existing_branch(repo_root: Path, remote: str, source_ref: str, local_branch: str) -> None:
    proc = git(repo_root, "fetch", "--no-tags", "--refmap=", remote, source_ref)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch {source_ref} from {remote} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )
    proc = git(repo_root, "branch", "-f", local_branch, "FETCH_HEAD")
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to refresh existing branch {local_branch} from {source_ref} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def fetch_repo(repo_root: Path, remote: str = "origin") -> None:
    """Fetch new refs for ``remote`` into ``repo_root``.

    When the remote's CONFIGURED url is an SSH GitHub url (unreachable to an
    agent), the fetch is routed through the rewritten HTTPS url
    with an explicit full refspec instead -- so the standard
    ``<remote>/<branch>`` tracking refs still land where every caller expects
    them. The checkout's OWN ``git config`` is never written to: no
    ``remote set-url`` runs here, ever.
    """
    origin_url = remote_origin_url(repo_root) if remote == "origin" else None
    effective_url = _effective_remote_url(origin_url) if origin_url is not None else None

    if effective_url is not None and effective_url != origin_url:
        proc = git(repo_root, "fetch", effective_url, f"+refs/heads/*:refs/remotes/{remote}/*")
    else:
        proc = git(repo_root, "fetch", remote)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch from {remote} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def ahead_behind(repo_root: Path, tracking_ref: str) -> tuple[int, int]:
    """(ahead, behind) of HEAD relative to ``tracking_ref`` -- commits only HEAD
    has, commits only the tracking ref has. ``(0, 0)`` when the comparison
    cannot be made (e.g. the tracking ref does not exist yet)."""
    proc = git(repo_root, "rev-list", "--left-right", "--count", f"HEAD...{tracking_ref}")
    if proc.returncode != 0:
        return (0, 0)
    parts = proc.stdout.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


def fast_forward_to(repo_root: Path, tracking_ref: str) -> bool:
    """Fast-forward the checked-out branch to ``tracking_ref``. Refuses (git's
    own refusal, not ours) if the branch is not a strict ancestor of the
    tracking ref -- only a CLEAN, non-diverged checkout may move;
    a diverged one must be reported, never force-moved. Returns True if HEAD
    actually moved."""
    before = current_head_sha(repo_root)
    proc = git(repo_root, "merge", "--ff-only", tracking_ref)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fast-forward {repo_root} to {tracking_ref}:\n{proc.stderr or proc.stdout}"
        )
    return current_head_sha(repo_root) != before


def probe_remote(url: str) -> tuple[bool, str]:
    """One-shot reachability probe for ``url`` (rewritten per
    ``_effective_remote_url`` first, same rewrite as step 2 above). Returns
    ``(reachable, detail)`` -- detail is empty on success, the git
    stderr/stdout on failure. Used by ``sync status`` (never ``sync run``'s
    own internal plan-building) to surface a refused remote as an ISSUE
    before ``sync run`` would hit it mid-operation."""
    try:
        positional(url, "url", ValueError)
    except ValueError as exc:
        return False, str(exc)
    effective_url = _effective_remote_url(url)
    proc = run_argv(["git", "ls-remote", "--", effective_url])
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()


def is_git_dir(path: Path) -> bool:
    proc = run_argv(["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _derive_workspace_root(source_repo_root: Path) -> Path:
    """The nearest ancestor holding a ``.grip`` directory is the workspace root;
    used to locate the object cache when a caller does not pass it. Falls back to
    the source's parent so the reference-clone path still runs (with no cache)
    outside a materialized workspace."""
    for candidate in (source_repo_root, *source_repo_root.parents):
        if (candidate / ".grip").is_dir():
            return candidate
    return source_repo_root.parent


def ensure_lane_checkout(
    *,
    source_repo_root: Path,
    target_repo_root: Path,
    branch: str,
    workspace_root: Path | None = None,
    seed_commit: str | None = None,
    cache_root: Path | None = None,
) -> bool:
    """Ensure a real, state-isolated lane checkout exists (grip#807).

    The lane is materialized as an INDEPENDENT reference clone, never a
    ``git worktree add``: two lanes checking out the same branch must not share
    refs, HEAD, reflogs, index, locks, config, or working-tree state. The clone
    contract lives in the single clone_exec seam; this is the thin lane entry to
    it. Returns True on first materialization, False when a valid lane is reused.
    """
    # Lazy import: clone_exec imports gitops, so a module-level import here would
    # be a cycle.
    from . import clone_exec

    if workspace_root is None:
        workspace_root = _derive_workspace_root(source_repo_root)

    return clone_exec.materialize_lane_clone(
        source_repo_root=source_repo_root,
        dest=target_repo_root,
        branch=branch,
        seed_commit=seed_commit,
        workspace_root=workspace_root,
        cache_root=cache_root,
    )


def branch_name_refusal(branch: object) -> str | None:
    """Why ``branch`` cannot be handed to git as a branch name, or None. `-f` or `--detach` would be read as an option."""
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        return f"{branch!r} is not a branch name"
    if run_argv(["git", "check-ref-format", "--branch", branch]).returncode:
        return f"{branch!r} is not a valid branch name"
    return None


def checkout_branch(repo_root: Path, branch: str) -> None:
    refusal = branch_name_refusal(branch)
    if refusal is not None:
        raise SystemExit(f"failed to checkout in {repo_root}: {refusal}")
    proc = git(repo_root, "checkout", branch, "--")
    if proc.returncode != 0:
        raise SystemExit(f"failed to checkout {branch} in {repo_root}:\n{proc.stderr or proc.stdout}")


def current_branch(repo_root: Path) -> str:
    proc = git(repo_root, "branch", "--show-current")
    if proc.returncode != 0:
        raise SystemExit(f"failed to determine current branch in {repo_root}:\n{proc.stderr or proc.stdout}")
    return proc.stdout.strip()


def stash_if_dirty(repo_root: Path, message: str) -> bool:
    if not repo_dirty(repo_root):
        return False
    proc = git(repo_root, "stash", "push", "-u", "-m", message)
    if proc.returncode != 0:
        raise SystemExit(f"failed to stash dirty work in {repo_root}:\n{proc.stderr or proc.stdout}")
    return True


def discard_if_dirty(repo_root: Path) -> bool:
    if not repo_dirty(repo_root):
        return False
    proc = git(repo_root, "reset", "--hard", "HEAD")
    if proc.returncode != 0:
        raise SystemExit(f"failed to discard tracked changes in {repo_root}:\n{proc.stderr or proc.stdout}")
    proc = git(repo_root, "clean", "-fd")
    if proc.returncode != 0:
        raise SystemExit(f"failed to discard untracked changes in {repo_root}:\n{proc.stderr or proc.stdout}")
    return True
