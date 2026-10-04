"""The workspace root as ``--root/-C`` on every verb that took it as a leading positional, and, for the
verbs with a FIXED number of positionals, as a word that may be left out.

Two command classes (`gr2/python_cli/root_option.py`):
  * `RootOptionalCommand` (23 verbs): `--root/-C`, and one fewer positional than the verb requires means
    the root is the missing one: it is put in front from `--root/-C` or the nearest workspace above cwd.
  * `RootOptionCommand` (8 verbs, each with an OPTIONAL TRAILING positional): `--root/-C` only. One fewer
    word has two readings there (`pr status unit lane` is "root omitted" and "root = unit"), so the bare
    form is never inferred, and an error that came from reading the first word as the root names `-C`.

Rows parse each verb's real command (`make_context`) without running it, so they cover every verb in
both lists; a few drive the real CLI. Each row names what must make it go red:
  * the count is off by one                       -> the bare rows
  * `--root/-C` is not inserted                   -> the `-C` rows
  * a root given twice is accepted                -> the twice rows
  * a bare call outside any workspace is accepted -> the outside row
  * the 8 start inferring                         -> the not-inferred rows
  * the `-C` hint is dropped from an error        -> the hint rows
  * a verb loses its class                        -> the partition row
  * the root stays required in the usage line     -> the usage row
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from gr2.python_cli import root_option
from gr2.python_cli.app import app
from typer.testing import CliRunner

RESOLVED = ["lane/create", "lane/current", "lane/enter", "lane/exit", "lane/resolve", "lane/show"]  # ContextCommand: fixed arity, and the unit may be left out too
FIXED_ARITY = [  # RootOptionalCommand: the 16, and ContextCommand: the 6 above
    "repo/hook-run", "repo/projection-run",
    "lane/create", "lane/enter", "lane/resolve", "lane/exit", "lane/current", "lane/show", "lane/bind",
    "lane/lease/acquire", "lane/lease/release", "lane/lease/show",
    "review/check", "review/requirements", "review/checkout-pr", "review/create-project", "review/open-project",
    "review/exit-gr", "review/open-gr", "review/verify", "review/show",
    "hooks/run", "config/restore",
]
OPTIONAL_TRAILING = [  # RootOptionCommand: the 8
    "pr/create", "pr/status", "pr/checks", "pr/view", "pr/merge", "exec/status", "exec/run", "review/open",
]
UsageError = root_option._usage_error()
runner = CliRunner()


def _tree() -> dict[str, object]:
    leaves: dict[str, object] = {}

    def walk(command, path):
        if hasattr(command, "commands"):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])
        else:
            leaves["/".join(path)] = command

    walk(typer.main.get_command(app), [])
    return leaves


LEAVES = _tree()


def _arguments(command):
    return [p for p in command.params if p.param_type_name == "argument"]


def _words(command, tmp_path: Path, *, with_optional: bool) -> list[str]:
    """Plausible words for every positional AFTER the root: required ones, plus the optional ones."""
    words = []
    for index, param in enumerate(_arguments(command)[1:]):
        if not param.required and not with_optional:
            continue
        kind = type(param.type).__name__
        if "Int" in kind:
            words.append("7")
        elif "Path" in kind:
            words.append(str(tmp_path))
        else:
            words.append(f"w{index}")
    return words


def _options(command, tmp_path: Path) -> list[str]:
    """The REQUIRED options of a verb (some need `--actor`, `--repos`, `--lane-dir`), so that a parse is
    only ever refused for the root."""
    out: list[str] = []
    for param in command.params:
        if param.param_type_name == "option" and param.required and param.name != "root":
            out += [param.opts[0], str(tmp_path) if "Path" in type(param.type).__name__ else "x"]
    return out


def _parse(verb: str, args: list[str], tmp_path: Path | None = None) -> dict:
    extra = _options(LEAVES[verb], tmp_path) if tmp_path is not None else []
    return LEAVES[verb].make_context(verb.split("/")[-1], [*extra, *args]).params


def _root(params: dict) -> Path:
    """The parsed root as a resolved Path (click hands some verbs a string; typer converts it later)."""
    return Path(params["workspace_root"]).resolve()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".grip").mkdir(parents=True)
    (root / ".grip" / "workspace_spec.toml").write_text('schema_version = 1\nworkspace_name = "ws"\n')
    (root / "sub" / "deeper").mkdir(parents=True)
    return root


@pytest.fixture
def at(monkeypatch):
    return monkeypatch.chdir


def test_the_partition_is_exactly_the_23_and_the_8_and_nothing_else_takes_a_leading_root() -> None:
    """Every verb whose first positional is the workspace root is in exactly one list, by class. A NEW verb
    with a leading root fails here until it is placed in one of them, on purpose."""
    leading = sorted(
        name for name, c in LEAVES.items()
        if _arguments(c) and _arguments(c)[0].name == "workspace_root"
        and name.split("/")[0] not in {"workspace", "spec", "sync", "store", "grip", "plan", "apply", "repo/status"}
        and type(c).__name__ in {"RootOptionalCommand", "RootOptionCommand", "TyperCommand", "ContextCommand"}
        and (_arguments(c)[0].required or type(c).__name__ in {"RootOptionalCommand", "ContextCommand"})
    )
    by_class = {
        "RootOptionalCommand": sorted(
            n for n, c in LEAVES.items() if type(c).__name__ in {"RootOptionalCommand", "ContextCommand"}
        ),
        "RootOptionCommand": sorted(n for n, c in LEAVES.items() if type(c).__name__ == "RootOptionCommand"),
    }
    assert by_class["RootOptionalCommand"] == sorted(FIXED_ARITY)
    assert sorted(n for n, c in LEAVES.items() if type(c).__name__ == "ContextCommand") == sorted(RESOLVED)
    assert by_class["RootOptionCommand"] == sorted(OPTIONAL_TRAILING)
    assert [n for n in leading if n not in FIXED_ARITY and n not in OPTIONAL_TRAILING] == [], (
        "a verb takes the workspace root as a required leading positional and is in neither class"
    )


def test_the_8_are_exactly_the_verbs_with_an_optional_trailing_positional() -> None:
    """WHY the 8 are not inferred: each has an optional positional after the required ones, so one fewer
    word has two readings. The 23 have none, so counting is exact for them."""
    for verb in FIXED_ARITY:
        assert all(p.required or p is _arguments(LEAVES[verb])[0] for p in _arguments(LEAVES[verb])), verb
    for verb in OPTIONAL_TRAILING:
        assert any(not p.required for p in _arguments(LEAVES[verb])), verb


@pytest.mark.parametrize("verb", FIXED_ARITY)
def test_a_bare_call_takes_the_workspace_above_the_current_directory(verb, ws, at, tmp_path) -> None:
    at(ws / "sub" / "deeper")
    words = _words(LEAVES[verb], tmp_path, with_optional=False)
    explicit = _parse(verb, [str(ws), *words], tmp_path)
    bare = _parse(verb, words, tmp_path)
    assert _root(bare) == ws.resolve() == _root(explicit)
    assert {k: v for k, v in bare.items() if k != "workspace_root"} == {
        k: v for k, v in explicit.items() if k != "workspace_root"
    }


@pytest.mark.parametrize("verb", FIXED_ARITY)
def test_a_bare_call_outside_any_workspace_is_refused_by_name(verb, tmp_path, at) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    at(outside)
    with pytest.raises(UsageError) as exc:
        _parse(verb, _words(LEAVES[verb], tmp_path, with_optional=False), tmp_path)
    assert "no workspace at or above" in str(exc.value.format_message()) and "-C <root>" in exc.value.format_message()


@pytest.mark.parametrize("verb", [*FIXED_ARITY, *OPTIONAL_TRAILING])
def test_the_explicit_leading_root_is_unchanged(verb, ws, tmp_path, at) -> None:
    at(tmp_path)  # a directory that is not the workspace: the argument wins
    params = _parse(verb, [str(ws), *_words(LEAVES[verb], tmp_path, with_optional=True)], tmp_path)
    assert _root(params) == ws.resolve()


@pytest.mark.parametrize("verb", [*FIXED_ARITY, *OPTIONAL_TRAILING])
@pytest.mark.parametrize("form", ["-C", "--root", "--root=", "-Cglued"])
def test_root_option_names_the_root_before_the_words_or_after_them(verb, form, ws, tmp_path, at) -> None:
    """From a directory that is NOT the workspace, `-C ROOT words`, `words -C ROOT`, `--root=ROOT words` and
    the glued `-CROOT words` give the same root. The words include the OPTIONAL trailing ones, so a verb
    that counted them as a surplus would refuse a correct call. The glued form is the one an earlier head
    missed: the scan did not see it, inferred the root from cwd, and the verb acted in the wrong workspace."""
    at(tmp_path)
    words = _words(LEAVES[verb], tmp_path, with_optional=True)
    given = {"--root=": [f"--root={ws}"], "-Cglued": [f"-C{ws}"]}.get(form, [form, str(ws)])
    for args in ([*given, *words], [*words, *given]):
        assert _root(_parse(verb, args, tmp_path)) == ws.resolve()


@pytest.mark.parametrize("verb", [*FIXED_ARITY, *OPTIONAL_TRAILING])
def test_a_root_given_twice_is_refused(verb, ws, tmp_path, at) -> None:
    """With every word present. At fewer words a verb that has an optional trailing positional cannot tell
    `verb ROOT unit -C ROOT` from `verb unit lane -C ROOT`, which is why the 8 are not inferred."""
    at(tmp_path)
    words = _words(LEAVES[verb], tmp_path, with_optional=True)
    with pytest.raises(UsageError) as exc:
        _parse(verb, [str(ws), *words, "-C", str(ws)], tmp_path)
    assert "given twice" in exc.value.format_message()


@pytest.mark.parametrize("verb", OPTIONAL_TRAILING)
def test_the_8_never_infer_a_missing_root(verb, ws, at, tmp_path) -> None:
    """Inside a workspace, with the root left out, the bare call is an error and the message names -C.
    This is the row that goes red if the 8 start counting."""
    at(ws)
    required = [p for p in _arguments(LEAVES[verb])[1:] if p.required]
    bare = _words(LEAVES[verb], tmp_path, with_optional=False)
    assert len(bare) == len(required)
    with pytest.raises(UsageError) as exc:
        _parse(verb, bare, tmp_path)
    message = exc.value.format_message()
    assert "-C <root>" in message and "workspace root" in message


@pytest.mark.parametrize("verb", [v for v in OPTIONAL_TRAILING if v != "exec/run"])
def test_the_8_name_c_when_the_first_word_was_read_as_a_root_that_is_not_a_directory(verb, ws, at, tmp_path) -> None:
    """The ambiguous count: one word too many to be 'missing the root'. The first word is taken as the
    root, which it cannot be (not a directory), so the error says so and names -C."""
    at(ws)
    words = _words(LEAVES[verb], tmp_path, with_optional=True)
    assert not Path(words[0]).is_dir()
    with pytest.raises(UsageError) as exc:
        _parse(verb, words, tmp_path)
    message = exc.value.format_message()
    assert "is not a directory" in message and "-C <root>" in message


@pytest.mark.parametrize(
    "verb",
    [v for v in OPTIONAL_TRAILING if any(p.param_type_name == "option" and p.required for p in LEAVES[v].params)],
)
def test_the_8_do_not_blame_the_root_for_a_missing_option(verb, ws, at, tmp_path) -> None:
    """A required OPTION left out is not a sign the root was read as a word, so the -C hint does not
    ride along. Goes red if the hint attaches to every missing-parameter error."""
    at(tmp_path)
    words = _words(LEAVES[verb], tmp_path, with_optional=False)
    with pytest.raises(UsageError) as exc:
        LEAVES[verb].make_context(verb.split("/")[-1], [str(ws), *words])
    message = exc.value.format_message()
    assert message.startswith("Missing option") and "-C <root>" not in message


def test_exec_run_names_c_when_its_first_word_is_not_a_directory(ws, at) -> None:
    at(ws)
    with pytest.raises(UsageError) as exc:
        _parse("exec/run", ["unit", "echo", "hi"], ws.parent)
    assert "is not a directory" in exc.value.format_message() and "-C <root>" in exc.value.format_message()


@pytest.mark.parametrize("verb", OPTIONAL_TRAILING)
def test_the_8_do_not_judge_a_root_that_is_a_directory(verb, tmp_path, at) -> None:
    """Only a first word that is not a directory is refused: a directory that is not a workspace goes on
    to whatever the verb already said about it."""
    plain = tmp_path / "plain"
    plain.mkdir()
    at(tmp_path)
    assert _root(_parse(verb, [str(plain), *_words(LEAVES[verb], tmp_path, with_optional=False)], tmp_path)) == plain.resolve()


def test_the_root_is_optional_in_the_usage_of_the_23_and_required_in_the_8() -> None:
    for verb in FIXED_ARITY:
        assert _arguments(LEAVES[verb])[0].required is False, verb
    for verb in OPTIONAL_TRAILING:
        assert _arguments(LEAVES[verb])[0].required is True, verb


def test_a_stray_dash_c_inside_a_variadic_command_is_the_commands_not_ours(ws, at) -> None:
    """`exec run ROOT unit -- cmd -C x`: after `--` every token is positional, and a -C there belongs to
    the command being run."""
    at(ws)
    params = _parse("exec/run", [str(ws), "unit", "--", "ls", "-C", "x"], ws.parent)
    assert _root(params) == ws.resolve() and tuple(params["command"]) == ("ls", "-C", "x")


def _workspace_with_a_remote(tmp_path: Path) -> Path:
    import subprocess

    bare = tmp_path / "alpha.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(bare), str(root / "alpha")], check=True, capture_output=True)
    for step in (["commit", "-q", "--allow-empty", "-m", "init"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(root / "alpha"), "-c", "user.name=t", "-c", "user.email=t@example.com", *step],
                       check=True, capture_output=True)
    done = runner.invoke(app, ["workspace", "init", str(root)])
    assert done.exit_code == 0, done.output
    (root / "sub").mkdir()
    return root


def test_lane_create_bare_from_a_subdirectory_makes_the_lane_in_that_workspace(tmp_path, at) -> None:
    """End to end: the verb RUNS, from a subdirectory, with the root left out."""
    root = _workspace_with_a_remote(tmp_path)
    at(root / "sub")
    done = runner.invoke(app, ["lane", "create", "default", "bare-lane", "--repos", "alpha", "--branch", "f/bare"])
    assert done.exit_code == 0, done.output
    assert (root / ".grip" / "state" / "lanes" / "default" / "bare-lane" / "lane.toml").is_file()


def test_lane_create_from_outside_with_dash_c_makes_the_lane_and_a_bare_call_makes_nothing(tmp_path, at) -> None:
    root = _workspace_with_a_remote(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    at(outside)
    refused = runner.invoke(app, ["lane", "create", "default", "none", "--repos", "alpha", "--branch", "f/none"])
    assert refused.exit_code == 2 and not (root / ".grip" / "state" / "lanes" / "default" / "none").exists()
    done = runner.invoke(app, ["lane", "create", "-C", str(root), "default", "viaC", "--repos", "alpha", "--branch", "f/c"])
    assert done.exit_code == 0, done.output
    assert (root / ".grip" / "state" / "lanes" / "default" / "viaC" / "lane.toml").is_file()


def test_a_root_form_the_scan_misses_is_refused_not_placed_in_the_wrong_workspace(ws, tmp_path, at, monkeypatch) -> None:
    """The BACKSTOP on its own. The scan is blinded to `-C...` tokens (as it was for the glued form), so the
    count infers the root from the current directory while click parses `-C<other>` as the root option. The
    two disagree, so the parse is refused. Goes red if the post-parse comparison is removed."""
    other = tmp_path / "other"
    (other / ".grip").mkdir(parents=True)
    (other / ".grip" / "workspace_spec.toml").write_text('schema_version = 1\nworkspace_name = "other"\n')
    real = root_option.RootOptionCommand._scan

    def blind(self, args):
        return real(self, [a for a in args if not a.startswith("-C")])

    monkeypatch.setattr(root_option.RootOptionCommand, "_scan", blind)
    at(ws)
    words = _words(LEAVES["lane/show"], tmp_path, with_optional=False)
    with pytest.raises(UsageError) as exc:
        _parse("lane/show", [*words, f"-C{other}"], tmp_path)
    assert "could not place" in exc.value.format_message() and "-C <root>" in exc.value.format_message()


def test_lane_create_from_inside_one_workspace_with_a_glued_dash_c_names_the_other(tmp_path, at) -> None:
    """End to end, two workspaces. From inside A, `lane create -C<B> ...` makes the lane in B and A gets none.
    This was the wrong-workspace defect: exit 0, the lane in A."""
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    a = _workspace_with_a_remote(tmp_path / "A")
    b = _workspace_with_a_remote(tmp_path / "B")
    at(a / "sub")
    done = runner.invoke(app, ["lane", "create", f"-C{b}", "default", "glued", "--repos", "alpha", "--branch", "f/g"])
    assert done.exit_code == 0, done.output
    assert (b / ".grip" / "state" / "lanes" / "default" / "glued" / "lane.toml").is_file()
    assert not (a / ".grip" / "state" / "lanes" / "default" / "glued").exists(), "the lane was made in the cwd's workspace"
