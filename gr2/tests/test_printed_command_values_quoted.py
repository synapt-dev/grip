"""Guard: every f-string that spells a `git ` or `gr2 ` command a person is told to run quotes each value it
interpolates into that command (next_steps._shown, shown or shlex.quote), so a value from a spec, a record or a
PR cannot run shell text when the line is pasted. A new unquoted site in a shape it knows reds here; the shapes under NAMED LIMITS still need a reader. The
allowlist holds the sites whose value the person typed in the same command and the values
that are fixed placeholders or constants, each with its reason.

NAMED LIMITS. The guard is a lexical net over f-string text, not a proof; these shapes
pass it unquoted, and a reader still checks them by hand:
1. a command that starts after '; ' (only the start of the text, a backtick, '(', ': ' or
   then/run/with opens a span);
2. 'failed' or 'returned' anywhere inside the span, which marks the whole string a diagnostic;
3. a command built by '+' concatenation of strings, since only one JoinedStr is walked at a time;
4. a command built with str.format() or %, which is not an f-string;
5. urllib's quote counted as a quoter, since only the called name is matched, not its module;
6. a command that starts after a newline."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "gr2"
# A command starts at the start of the text, after a backtick or '(', after ': ', or after then/run/with; a backticked one ends at the next
# backtick, any other at the first newline, '; ', ', ' or ': ' (implicitly joined f-strings are one node).
_START = re.compile(r"(?:^|[`(]|: |\b(?:then|run|with) )(git|gr2) ")
_ENDS = ("\n", "; ", ", ", ": ")
# Text that names a git or gr2 run that already happened (its failure, exit or label) is a diagnostic, not a line
# to run; its values are not checked.
_DIAGNOSTIC = re.compile(r"^(?:git|gr2) [\w-]+:|\b(?:failed|exited|timed out|returned|succeeded|staged|FAILED|"
                         r"runs inside|PR group|= true)\b")
_QUOTERS = {"_shown", "shown", "quote"}

ALLOWLIST: dict[tuple[str, str], str] = {
    ('gr2/python_cli/app.py', 'raise SystemExit(f"shared repo missing for review checkout: {repo_root}\\nrun `gr2 apply {workspace_root} --yes` first")'):
        'user-typed: the person passed this value in the command that prints it (app.py:518)',
    ('gr2/python_cli/app.py', 'f"another branch belongs in a new lane: gr2 lane create <workspace> {next_steps._shown(unit)} <lane>{repos} "'):
        "repos is built quoted: ' --repos ' + next_steps._shown(key), app.py:2090",
    ('gr2/python_cli/app.py', 'f"--branch {name}",'):
        'user-typed: the person passed this value in the command that prints it (app.py:2134)',
    ('gr2/python_cli/app.py', 'f"  continue with it: gr2 review create-project {workspace_root} {owner_unit} {lane_name}\\n"'):
        'user-typed: the person passed this value in the command that prints it (app.py:3387)',
    ('gr2/python_cli/app.py', 'stash_if_dirty(path, f"gr2 exit {owner_unit}/{lane_name}")'):
        'not printed: the stash message, passed to git as one argv item',
    ('gr2/python_cli/app.py', 'restore = f"git -C {shlex.quote(str(path))} stash apply --index {sha}"'):
        "placeholder-safe: sha is git's own rev-parse of stash@{0}, hex",
    ('gr2/python_cli/app.py', 'f"run `gr2 apply {workspace_root} --yes` first"'):
        'user-typed: the person passed this value in the command that prints it (app.py:4345)',
    # each value in shown is quoted with next_steps._shown (gitops.py:158); _shown_arg alone is not quote-safe
    ('gr2/python_cli/gitops.py', 'super().__init__(f"git {\' \'.join(shown)}: {_masked(_shown_stderr(args, stderr), secrets)}".rstrip(": "))'):
        'shown is built quoted: next_steps._shown(_shown_arg(arg)), gitops.py:158',
    ('gr2/python_cli/grip.py', '"next": None if complete else f"gr2 review publish {next_steps._shown(str(workspace))} gr:{full}" + ('):
        "placeholder-safe: full is the review commit's hex oid, resolved and pushed by gr2",
    ('gr2/python_cli/grip_cli.py', 'advice = f"gr2 store {verb}"'):
        'placeholder: verb is a literal store verb name from the caller',
    ('gr2/python_cli/merge_gate.py', 'failed = {"command": f"git -C {next_steps._shown(repo)} fetch --no-tags <remote> {spec}",'):
        'spec is built quoted: next_steps._shown(refspec), merge_gate.py:659',
    ('gr2/python_cli/next_steps.py', 'f"{\'\' if n == \'test\' else f\' --name {_shown(n)}\'} -- <your {_shown(n)} command>" for n in checks'):
        "the value is '' or a nested f-string that quotes n with _shown (walked on its own)",
    ('gr2/python_cli/next_steps.py', 'f"{_shown(p)}){pushed} && gr2 review publish {_shown(workspace)} \\"$R\\" --remote {_shown(rem)} && "'):
        'pushed is built quoted: _shown(p), _shown(rem), _shown(feature), next_steps.py:231',
    ('gr2/python_cli/next_steps.py', 'f"gr2 check run {_shown(p)} --remote {_shown(rem)} --head {_shown(head)} -- <your test command> && {merge}")'):
        'merge is built quoted: _shown(workspace), _shown(feature), _shown(into), an int, next_steps.py:228',
    ('gr2/python_cli/open_gr_review.py', 'raise OpenGrReviewError(f"lane {target}: could not read {path} (git {args[0]}): "'):
        'diagnostic: names the git verb gr2 ran, not a line to run',
    ('gr2/python_cli/review_diff.py', 'return (f"next: if the clone is shallow (git -C {clone} rev-parse --is-shallow-repository), deepen it from the "'):
        'clone is built quoted: _shown(str(repo)), review_diff.py:24',
    ('gr2/python_cli/review_diff.py', 'f"remote that holds the head (git -C {clone} fetch --unshallow <remote> {_shown(head)}) and show again; "'):
        'clone is built quoted: _shown(str(repo)), review_diff.py:24',
    ('gr2/python_cli/workspace_guidance.py', 'f"`gr2 workspace migrate-gr1 {workspace_root}` to add gr2 alongside it; your gr1 setup keeps working")'):
        'user-typed: the person passed this value in the command that prints it (workspace_guidance.py:47)',
}


def _quoted(value: ast.expr) -> bool:
    if isinstance(value, ast.Call):
        f = value.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        return name in _QUOTERS
    return False


def _command_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for m in _START.finditer(text):
        start = m.end() - len(m.group(1)) - 1
        if m.group(0).startswith("`"):
            end = text.find("`", start)
        else:
            ends = [i for i in (text.find(t, start) for t in _ENDS) if i >= 0]
            end = min(ends, default=len(text))
        end = len(text) if end < 0 else end
        if not _DIAGNOSTIC.search(text[start:end]):
            spans.append((start, end))
    return spans


def unquoted_sites(root: Path = PKG) -> list[tuple[str, str]]:
    found = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.JoinedStr):
                continue
            text = "".join(str(p.value) if isinstance(p, ast.Constant) else "\0" for p in node.values)
            holes = [i for i, c in enumerate(text) if c == "\0"]
            values = [p for p in node.values if not isinstance(p, ast.Constant)]
            spans = _command_spans(text)
            for at, part in zip(holes, values):
                if any(s <= at < e for s, e in spans) and not _quoted(part.value):
                    found.append((str(path.relative_to(root.parent)), lines[part.value.lineno - 1].strip()))
    return found


def test_every_value_in_a_printed_git_or_gr2_command_is_quoted_or_allowlisted() -> None:
    bare = [site for site in unquoted_sites() if site not in ALLOWLIST]
    assert not bare, "\n".join(f"{f}: {line}" for f, line in bare)


def test_every_allowlist_entry_still_names_a_site() -> None:
    sites = set(unquoted_sites())
    assert not [key for key in ALLOWLIST if key not in sites]


def test_the_walker_finds_a_bare_value_and_passes_a_quoted_one(tmp_path: Path) -> None:
    pkg = tmp_path / "gr2"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "a = f'run `git -C {p} fetch` or {allow}'\n"
        "b = f'next: gr2 review open {_shown(w)} {n}'\n"
        "c = f'{name}: plain prose {x}'\n"
        "d = f'git push {shlex.quote(r)}'\n"
        "e = f'git add failed with exit {rc}'\n"
        "f = f'next: deepen it (git -C {c} fetch) then git push {r}'\n"
        "g = f'it moved; then git push {t}'\n"
        "h = f'to see it run gr2 review show {u}'\n"
        "i = f'start over with git fetch {v}'\n", encoding="utf-8")
    assert unquoted_sites(pkg) == [("gr2/m.py", "a = f'run `git -C {p} fetch` or {allow}'"),
                                   ("gr2/m.py", "b = f'next: gr2 review open {_shown(w)} {n}'"),
                                   ("gr2/m.py", "f = f'next: deepen it (git -C {c} fetch) then git push {r}'"),
                                   ("gr2/m.py", "f = f'next: deepen it (git -C {c} fetch) then git push {r}'"),
                                   ("gr2/m.py", "g = f'it moved; then git push {t}'"),
                                   ("gr2/m.py", "h = f'to see it run gr2 review show {u}'"),
                                   ("gr2/m.py", "i = f'start over with git fetch {v}'")]
