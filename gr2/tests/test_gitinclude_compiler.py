"""The .gitinclude compiler: canonical forms only, reported, and order-free.

Every assertion about tracking is git's own verdict: the fixtures build a
repository with `git init` and read the answer back with
`git status --porcelain -uall`, never by inspecting the generated text. That
distinction found the bug this work started from, where a compiler emitted a
line for a file and git still dropped it silently.

Each control is required to produce the WRONG answer. A control that cannot fail
proves nothing, and four fixtures in this lane have been the defect rather than
the code, so every one of them is recorded where it failed.
"""
from __future__ import annotations

import os
import subprocess

from gr2.python_cli.app import app
from gr2.python_cli.gitinclude import CONFLICT, REFUSED, compile_gitignore
from typer.testing import CliRunner

# HERMETIC GIT, AND AN HONEST LABEL ON IT. This host binds ~/.gitignore_global
# through core.excludesFile, and `--exclude-standard` reads it, so every git call
# here goes through _git below with the ambient config removed.
#
# INSURANCE, NOT A GATE, and the sentence that used to be here claimed otherwise.
# It said a pinned test proved the env load-bearing. Two measurements say it
# cannot be: a global excludes file naming a DECLARED path leaves it listed,
# because the repo's own `!` outranks it, and a global NEGATION adds nothing,
# because the repo's `*` outranks it. The generated .gitignore is higher
# precedence than core.excludesFile AND has a catch-all, so the global file has
# no path left to decide. The exposure exists only for a fixture with no
# generated .gitignore at all, which is what the control below constructs to show
# the injection is not inert. Nothing in this file is in that state, so there is
# no failing control behind this env and it is not claimed to be one.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args, env=None, **kw):
    return subprocess.run(["git", *args], env=env or GIT_ENV, **kw)


runner = CliRunner()


def _repo(tmp_path):
    _git("init", "-q", "-b", "main", str(tmp_path), check=True)
    return tmp_path


def _status(root) -> set[str]:
    out = _git(
        "-C", str(root), "status", "--porcelain", "-uall",
        check=True, capture_output=True, text=True,
    ).stdout
    return {line[3:] for line in out.splitlines() if line.strip()}


def _install(root, declaration: str):
    text, report = compile_gitignore(declaration)
    (root / ".gitinclude").write_text(declaration, encoding="utf-8")
    (root / ".gitignore").write_text(text, encoding="utf-8")
    return report


def _write_naive(root, declaration: str, ignore_text: str) -> None:
    (root / ".gitinclude").write_text(declaration, encoding="utf-8")
    (root / ".gitignore").write_text(ignore_text, encoding="utf-8")


def _tree(*files):
    """A tmp_path fixture helper is not available here, so build under a temp."""
    import tempfile
    from pathlib import Path

    root = _repo(Path(tempfile.mkdtemp()))
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return root


# --------------------------------------------------------------- what is tracked


def test_an_included_file_under_a_nested_directory_shows(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "a.md").write_text("a\n")
    (root / "docs" / "guide" / "b.md").write_text("b\n")
    (root / "README.md").write_text("r\n")
    _install(root, ".gitinclude\nREADME.md\ndocs/guide/a.md\n")

    seen = _status(root)
    assert "docs/guide/a.md" in seen, seen
    assert ".gitinclude" in seen, seen
    assert ".gitignore" not in seen, seen


def test_a_slashless_directory_name_is_accepted_and_covers_its_contents(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "docs" / "deep" / "a.md").write_text("a\n")
    report = _install(root, ".gitinclude\ndocs\n")

    assert report == [], report
    assert "docs/deep/a.md" in _status(root), _status(root)


def test_a_trailing_slash_is_stripped_so_a_file_with_one_is_not_dropped(tmp_path):
    """`README.md/` used to emit `/README.md/`, which matches only directories,
    so the file was dropped with no report."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    report = _install(root, "README.md/\n")

    assert report == [], report
    assert "README.md" in _status(root), _status(root)


def test_a_bare_name_means_the_root_copy_only(tmp_path):
    root = _repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "Makefile").write_text("nested\n")
    (root / "Makefile").write_text("root\n")
    _install(root, ".gitinclude\nMakefile\n")

    seen = _status(root)
    assert "Makefile" in seen, seen
    assert "src/Makefile" not in seen, seen


def test_the_declaration_tracks_itself_even_when_it_forgets_to_name_itself(tmp_path):
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    _install(root, "README.md\n")

    seen = _status(root)
    assert ".gitinclude" in seen, seen
    assert "README.md" in seen, seen


def test_the_generated_ignore_never_tracks_itself_or_gr2_state(tmp_path):
    root = _repo(tmp_path)
    (root / ".grip" / "state").mkdir(parents=True)
    (root / ".grip" / "state" / "lane.json").write_text("{}\n")
    (root / "agents" / "default" / "home").mkdir(parents=True)
    (root / "agents" / "default" / "home" / "x").write_text("x\n")
    (root / "README.md").write_text("r\n")
    _install(root, ".gitinclude\nREADME.md\n")

    assert _status(root) == {".gitinclude", "README.md"}, _status(root)


# ------------------------------------------------------------- order independence


def _two_orderings():
    """The same declaration, written two ways. The ignore is ABOVE its include
    in the second, which is the shape that silently tracked the file the user
    had written to exclude."""
    return "docs/\n!docs/secret.txt\n", "!docs/secret.txt\ndocs/\n"


def test_an_ignore_wins_wherever_it_is_written(tmp_path):
    """Finding 1 of the r1 read: the output was emitted in declaration order, so
    `!docs/secret.txt` written above `docs/` lost to the include emitted after
    it and the file was TRACKED. The report was empty, which is what made it a
    silent failure."""
    for i, declaration in enumerate(_two_orderings()):
        root = _repo(tmp_path / f"order{i}")
        (root / "docs").mkdir()
        (root / "docs" / "keep.txt").write_text("k\n")
        (root / "docs" / "secret.txt").write_text("s\n")
        _install(root, declaration)
        seen = _status(root)
        assert "docs/secret.txt" not in seen, (declaration, seen)
        assert "docs/keep.txt" in seen, (declaration, seen)


def test_a_shuffled_declaration_compiles_byte_identical(tmp_path):
    """The property, run through the VERB so the thing tested is what a user
    gets: two declarations that differ only in line order must produce the same
    bytes."""
    root_a = _repo(tmp_path / "a")
    root_b = _repo(tmp_path / "b")
    first, second = _two_orderings()
    for root, declaration in ((root_a, first), (root_b, second)):
        (root / ".gitinclude").write_text(declaration, encoding="utf-8")

    for root in (root_a, root_b):
        result = runner.invoke(app, ["workspace", "gitinclude", str(root)])
        assert result.exit_code == 0, result.output
    assert (root_a / ".gitignore").read_bytes() == (root_b / ".gitignore").read_bytes()


def test_control_an_order_dependent_compile_would_differ(tmp_path, monkeypatch):
    """The property above holds only while this control PASSES. The mutation is
    the defect the r1 read found: emit the lines in the order the declaration
    gives them. If this ever goes green the witness above has stopped meaning
    anything."""
    import gr2.python_cli.gitinclude as gi

    def order_dependent(text):
        # Emitted in DECLARATION order. Collecting into include/ignore lists and
        # emitting them grouped is what the real compiler does, and my first
        # version of this mutation did exactly that, so it was not a mutation at
        # all and both order controls passed vacuously.
        lines = ["*", "!/.gitinclude"]
        report = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            is_ignore = line.startswith("!")
            body = line[1:].strip() if is_ignore else line
            reason = gi._refusal_reason(body)
            if reason is not None:
                report.append(gi.Notice(line=raw, reason=reason, kind=gi.REFUSED))
                continue
            lines.extend(
                gi._lines_for(body.rstrip("/"), "" if is_ignore else "!",
                              with_ancestors=not is_ignore)
            )
        return gi.HEADER + "\n".join(lines) + "\n", report

    monkeypatch.setattr(gi, "compile_gitignore", order_dependent)
    root_a = _repo(tmp_path / "a")
    root_b = _repo(tmp_path / "b")
    first, second = _two_orderings()
    for root, declaration in ((root_a, first), (root_b, second)):
        (root / ".gitinclude").write_text(declaration, encoding="utf-8")
    for root in (root_a, root_b):
        assert runner.invoke(app, ["workspace", "gitinclude", str(root)]).exit_code == 0

    assert (root_a / ".gitignore").read_bytes() != (root_b / ".gitignore").read_bytes(), (
        "the order-dependent mutation produced IDENTICAL files, so the witness "
        "above cannot fail and proves nothing"
    )


# ------------------------------------------------------------- the conflicts


def test_an_include_under_an_ignored_path_is_reported_as_a_conflict(tmp_path):
    """Finding 4 of the r1 read: the include is emitted, and the ignore emitted
    after it wins, so it cannot do what it says. It used to say nothing."""
    root = _repo(tmp_path)
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "sub" / "tracked.txt").write_text("x\n")
    report = _install(root, "!docs/\ndocs/sub/tracked.txt\n")

    kinds = [(n.kind, n.line) for n in report]
    assert kinds == [(CONFLICT, "docs/sub/tracked.txt")], kinds
    assert "docs/sub/tracked.txt" not in _status(root), _status(root)


def test_control_ignoring_one_child_of_an_included_directory_is_not_a_conflict(tmp_path):
    """The natural use of the ignore form: include a directory, exclude one file
    inside it. If this reported a conflict the report would be noise and nobody
    would read it."""
    root = _repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "keep.txt").write_text("k\n")
    (root / "docs" / "secret.txt").write_text("s\n")
    report = _install(root, "docs/\n!docs/secret.txt\n")

    assert report == [], report


# ------------------------------------------------- the refusal-and-report path


def test_each_refused_form_is_reported_and_emits_nothing():
    refused = {
        "/README.md": "leading slash",
        "*.md": "glob",
        "docs/**": "glob",
        "a[bc].md": "glob",
        "a?b.md": "glob",
        "a\\ b.md": "escape",
        "../outside.md": "segment",
        "./docs/": "segment",
        "docs//a.md": "empty segment",
    }
    for line, why in refused.items():
        text, report = compile_gitignore(f"{line}\n")
        assert len(report) == 1, f"{line} ({why}) was not reported: {report}"
        assert report[0].kind == REFUSED, report
        assert line not in text, f"{line} ({why}) was emitted anyway: {text}"


def test_dotfiles_are_ordinary_names_and_stay_accepted(tmp_path):
    root = _repo(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on:\n")
    (root / ".env").write_text("K=V\n")
    report = _install(root, ".gitinclude\n.github/\n.env\n")

    assert report == [], report
    seen = _status(root)
    assert ".github/workflows/ci.yml" in seen, seen
    assert ".env" in seen, seen


def test_comments_and_blank_lines_are_accepted_and_emit_nothing():
    text, report = compile_gitignore("# a note\n\n   \n")
    assert report == [], report
    emitted = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert emitted == ["*", "!/.gitinclude"], emitted


def test_the_report_is_returned_not_printed():
    _, report = compile_gitignore("*.md\nREADME.md\n")
    assert [(n.kind, n.line) for n in report] == [(REFUSED, "*.md")], report


# ------------------------------------------------------- the controls (tracking)


def test_control_no_ancestors_drops_the_file_silently(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "a.md").write_text("a\n")
    (root / "README.md").write_text("r\n")
    _write_naive(root, ".gitinclude\nREADME.md\ndocs/guide/a.md\n",
                 "*\n!/.gitinclude\n!/README.md\n!/README.md/**\n"
                 "!/docs/guide/a.md\n!/docs/guide/a.md/**\n")

    assert "docs/guide/a.md" not in _status(root), (
        "the ancestor-free form did not drop the file, so this control cannot "
        "fail and proves nothing"
    )


def test_control_a_directory_without_its_glob_line_shows_nothing():
    root = _tree("docs/deep/a.md")
    _write_naive(root, ".gitinclude\ndocs\n", "*\n!/.gitinclude\n!/docs\n")

    assert "docs/deep/a.md" not in _status(root), (
        "the form without the glob line did not drop the file, so this control "
        "cannot fail and proves nothing"
    )


def test_control_an_unanchored_include_reaches_a_nested_copy():
    """`!/src/` is load-bearing in this fixture: with src excluded, git never
    descends into it, so the anchor difference cannot show at all and the
    control would pass for the wrong reason."""
    root = _tree("src/Makefile", "Makefile")
    _write_naive(root, ".gitinclude\nMakefile\n",
                 "*\n!/.gitinclude\n!/src/\n!Makefile\n!Makefile/**\n")

    assert "src/Makefile" in _status(root), (
        "the unanchored form did not reach the nested file, so this control "
        "cannot fail and proves nothing"
    )


def test_control_an_unanchored_ignore_reaches_a_nested_copy():
    root = _tree("docs/build/x", "build/y")
    _write_naive(root, ".gitinclude\ndocs/\n!/build\n",
                 "*\n!/.gitinclude\n!/docs/\n!/docs/**\nbuild\nbuild/**\n")

    assert "docs/build/x" not in _status(root), (
        "the unanchored ignore did not reach the nested directory, so this "
        "control cannot fail and proves nothing"
    )


def test_control_without_the_self_line_the_declaration_vanishes(tmp_path):
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    _write_naive(root, "README.md\n", "*\n!/README.md\n!/README.md/**\n")

    assert ".gitinclude" not in _status(root), (
        "the declaration was tracked anyway, so this control cannot fail and "
        "proves nothing"
    )


# ------------------------------------------- the tree-state property, and its control


def _two_roots(tmp_path):
    """A stands for a fresh clone (declared paths absent), B for a materialized
    desk (they exist: `docs` a real directory, `README.md` a real file)."""
    declaration = ".gitinclude\nREADME.md\ndocs\n"
    root_a = tmp_path / "fresh"
    root_b = tmp_path / "materialized"
    for root in (root_a, root_b):
        root.mkdir()
        (root / ".gitinclude").write_text(declaration, encoding="utf-8")
    (root_b / "docs").mkdir()
    (root_b / "docs" / "a.md").write_text("a\n")
    (root_b / "README.md").write_text("r\n")
    return root_a, root_b


def test_a_fresh_clone_and_a_materialized_desk_compile_the_same_gitignore(tmp_path):
    root_a, root_b = _two_roots(tmp_path)
    for root in (root_a, root_b):
        result = runner.invoke(app, ["workspace", "gitinclude", str(root)])
        assert result.exit_code == 0, result.output

    assert (root_a / ".gitignore").read_bytes() == (root_b / ".gitignore").read_bytes()


def test_control_a_tree_aware_compile_would_make_the_two_roots_differ(tmp_path, monkeypatch):
    """The mutation is the defect the ruling names: an `os.path.isdir` branch on
    the compile path. It must consult the tree of the root BEING COMPILED, which
    is why the root travels in a holder the test sets per invocation; the first
    version of this control closed over one root, fired for both invocations,
    produced identical files, and read as "the witness cannot fail"."""
    import gr2.python_cli.gitinclude as gi

    root_a, root_b = _two_roots(tmp_path)
    real = gi.compile_gitignore
    holder = {"root": root_a}

    def tree_aware(text):
        out, report = real(text)
        for raw in text.splitlines():
            body = raw.strip()
            if (
                body
                and not body.startswith(("!", "#"))
                and not body.endswith("/")
                and (holder["root"] / body).is_dir()
            ):
                out = out.replace(f"!/{body}/**", f"!/{body}/**\n!/{body}/TREE_AWARE")
        return out, report

    monkeypatch.setattr(gi, "compile_gitignore", tree_aware)
    holder["root"] = root_a
    assert runner.invoke(app, ["workspace", "gitinclude", str(root_a)]).exit_code == 0
    holder["root"] = root_b
    assert runner.invoke(app, ["workspace", "gitinclude", str(root_b)]).exit_code == 0

    assert (root_a / ".gitignore").read_bytes() != (root_b / ".gitignore").read_bytes(), (
        "the tree-aware mutation produced IDENTICAL files, so the witness above "
        "cannot fail and proves nothing"
    )


# --------------------------------------------------------------------- the verb


def test_the_verb_prints_the_report_and_fails_on_a_refused_line(tmp_path):
    root = _repo(tmp_path)
    (root / ".gitinclude").write_text("*.md\nREADME.md\n", encoding="utf-8")

    result = runner.invoke(app, ["workspace", "gitinclude", str(root)])

    assert result.exit_code != 0, result.output
    assert "*.md" in result.output and "glob" in result.output, result.output
    assert "README.md" in (root / ".gitignore").read_text(encoding="utf-8")


def test_check_reports_without_writing(tmp_path):
    root = _repo(tmp_path)
    (root / ".gitinclude").write_text("README.md\n", encoding="utf-8")

    result = runner.invoke(app, ["workspace", "gitinclude", str(root), "--check"])

    assert result.exit_code == 0, result.output
    assert not (root / ".gitignore").exists()


# ============================================================================
# The generator, with GIT AS THE ORACLE.
#
# Nine silent drops in three rounds is the argument for this: hand-picked cases
# pin what we already thought of, and the generator is what ends the rounds. The
# oracle is `git ls-files --others --exclude-standard`, and EXPECTED comes from a
# deliberately dumb model that shares NO code with the compiler -- if it shared
# code the two would agree by construction and the test could not fail.
# ============================================================================

import random  # noqa: E402

GENERATOR_SEEDS = range(8)

# Real files, including the decoys the ruling asks for: siblings of a named
# path, a nested same-name copy (src/Makefile beside Makefile, src/docs/ beside
# docs/), and files under a parent that a generated ignore may name.
UNIVERSE_FILES = [
    "README.md",
    "Makefile",
    ".env",
    ".github/workflows/ci.yml",
    "docs/keep.txt",
    "docs/secret.txt",
    "docs/sub/tracked.txt",
    "src/Makefile",
    "src/docs/x.txt",
    "src/main.rs",
]

INCLUDE_CHOICES = ["README.md", "Makefile", ".env", ".github/", "docs", "docs/sub/", "src/main.rs"]
IGNORE_CHOICES = ["docs/secret.txt", "docs/", "src/Makefile", ".github/workflows/"]


def _generate(seed):
    """A declaration, plus the canonical directives the model is allowed to use.

    Some spellings are deliberately non-canonical (a doubled slash). They are
    REFUSED, so they contribute nothing to the model's view and must contribute
    nothing to git's either -- the report is what tells the user, and the
    comparison is what proves the refusal actually held.
    """
    rng = random.Random(seed)
    decls = []
    lines = []
    for path in rng.sample(INCLUDE_CHOICES, rng.randint(1, 3)):
        spelling = path + "/" if not path.endswith("/") and rng.random() < 0.3 else path
        lines.append(spelling)
        decls.append(("include", path.rstrip("/")))
    for path in rng.sample(IGNORE_CHOICES, rng.randint(0, 2)):
        lines.append("!" + path)
        decls.append(("ignore", path.rstrip("/")))
    if rng.random() < 0.4:
        bad = rng.choice(INCLUDE_CHOICES).rstrip("/") + "//bad.md"
        lines.append(bad)
    if rng.random() < 0.4:
        # Refused: the declaration is always tracked, so this line must not be
        # able to defeat the self line the compiler emits.
        lines.append("!.gitinclude")
    rng.shuffle(lines)
    return "\n".join(lines) + "\n", decls


def _expected(decls):
    """The dumb model. Shares nothing with the compiler on purpose.

    A file is tracked when it equals or sits under an include, and is not equal
    to or under any ignore; ignore wins. `.gitinclude` is always tracked, and the
    generated `.gitignore` never is.
    """
    includes = [p for kind, p in decls if kind == "include"]
    ignores = [p for kind, p in decls if kind == "ignore"]
    tracked = {".gitinclude"}
    for f in UNIVERSE_FILES:
        under_include = any(f == i or f.startswith(i + "/") for i in includes)
        under_ignore = any(f == g or f.startswith(g + "/") for g in ignores)
        if under_include and not under_ignore:
            tracked.add(f)
    return tracked


def _build(root):
    for f in UNIVERSE_FILES:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")


def _untracked(root, env=None) -> set[str]:
    out = _git(
        "-C", str(root), "ls-files", "--others", "--exclude-standard",
        env=env, check=True, capture_output=True, text=True,
    ).stdout
    return {line for line in out.splitlines() if line}


def _blame(root, paths):
    """`git check-ignore -v --no-index <path>` for each path, so a failure names
    the emitted line that decided it instead of only reporting a set difference."""
    out = []
    for path in paths:
        r = _git(
            "-C", str(root), "check-ignore", "-v", "--no-index", path,
            capture_output=True, text=True,
        )
        out.append(f"  {path}: {(r.stdout or r.stderr).strip() or '(not ignored)'}")
    return "\n".join(out)


def _check_seed(seed, tmp_path):
    """Compile seed's declaration through the VERB and compare with the model.

    Returns None when they agree, or a printable description of the difference.
    """
    root = _repo(tmp_path / f"seed{seed}")
    _build(root)
    declaration, decls = _generate(seed)
    (root / ".gitinclude").write_text(declaration, encoding="utf-8")

    result = runner.invoke(app, ["workspace", "gitinclude", str(root)])
    # A generated non-canonical spelling makes the verb exit non-zero by design;
    # the file is still written, and the comparison below is what matters.
    if result.exit_code not in (0, 1):
        return f"seed {seed}: verb failed rc={result.exit_code}\n{result.output}"

    actual = _untracked(root)
    expected = _expected(decls)
    if actual == expected:
        return None
    return (
        f"seed {seed}\ndeclaration:\n{declaration}\n"
        f"expected only: {sorted(expected - actual)}\n"
        f"actual only:   {sorted(actual - expected)}\n"
        f"WHICH LINE DECIDED IT (git check-ignore -v --no-index):\n"
        f"{_blame(root, sorted((expected - actual) | (actual - expected)))}\n"
        f"generated:\n{(root / '.gitignore').read_text(encoding='utf-8')}"
    )


def test_the_generator_agrees_with_git_on_every_seed(tmp_path):
    for seed in GENERATOR_SEEDS:
        failure = _check_seed(seed, tmp_path)
        assert failure is None, failure


def test_a_shuffled_declaration_compiles_the_same_for_every_seed(tmp_path):
    """The order witness, folded into the same loop."""
    for seed in GENERATOR_SEEDS:
        declaration, _ = _generate(seed)
        shuffled = declaration.splitlines()
        random.Random(seed + 1000).shuffle(shuffled)

        roots = []
        for i, text in enumerate((declaration, "\n".join(shuffled) + "\n")):
            root = _repo(tmp_path / f"shuf{seed}-{i}")
            _build(root)
            (root / ".gitinclude").write_text(text, encoding="utf-8")
            assert runner.invoke(
                app, ["workspace", "gitinclude", str(root)]
            ).exit_code in (0, 1)
            roots.append(root)
        assert (roots[0] / ".gitignore").read_bytes() == (
            roots[1] / ".gitignore"
        ).read_bytes(), f"seed {seed}: a shuffle changed the output"


def _mutation_failures(monkeypatch, mutation, tmp_path):
    """Run every seed under a mutated compiler; return the seeds it breaks."""
    import gr2.python_cli.gitinclude as gi

    monkeypatch.setattr(gi, "compile_gitignore", mutation(gi))
    broken = []
    for seed in GENERATOR_SEEDS:
        if _check_seed(seed, tmp_path) is not None:
            broken.append(seed)
    return broken


def test_control_mutation_1_bare_ancestors_turn_the_generator_red(monkeypatch, tmp_path):
    """The original v1 bug, which git reported by dropping the file silently."""

    def mutation(module):
        real = module._lines_for

        def bare_ancestors(path, prefix, with_ancestors):
            return [
                line[1:] if line.startswith("!") else line
                for line in real(path, prefix, with_ancestors)
            ]

        return lambda text: _recompile_with(module, bare_ancestors, text)

    assert _mutation_failures(monkeypatch, mutation, tmp_path), (
        "bare ancestors did not turn the generator red on any seed, so the "
        "generator does not cover that case"
    )


def test_control_mutation_2_declaration_order_turns_the_generator_red(monkeypatch, tmp_path):

    def mutation(module):
        def order_dependent(text):
            # Declaration order, emitted as encountered. See the note on the
            # standalone control: grouping into lists first is NOT a mutation.
            lines = ["*", "!/.gitinclude"]
            report = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                is_ignore = line.startswith("!")
                body = line[1:].strip() if is_ignore else line
                reason = module._refusal_reason(body)
                if reason is not None:
                    report.append(
                        module.Notice(line=raw, reason=reason, kind=module.REFUSED)
                    )
                    continue
                lines.extend(
                    module._lines_for(body.rstrip("/"), "" if is_ignore else "!",
                                      with_ancestors=not is_ignore)
                )
            return module.HEADER + "\n".join(lines) + "\n", report

        return order_dependent

    assert _mutation_failures(monkeypatch, mutation, tmp_path), (
        "declaration order did not turn the generator red on any seed"
    )


def test_control_mutation_3_dropping_the_glob_line_turns_the_generator_red(monkeypatch, tmp_path):

    def mutation(module):
        real = module._lines_for

        def no_glob(path, prefix, with_ancestors):
            return [ln for ln in real(path, prefix, with_ancestors) if not ln.endswith("/**")]

        return lambda text: _recompile_with(module, no_glob, text)

    assert _mutation_failures(monkeypatch, mutation, tmp_path), (
        "dropping the glob line did not turn the generator red on any seed"
    )


def _recompile_with(module, line_fn, text):
    """Re-run the compiler with a substituted line builder, keeping the rest."""
    includes, ignores, report = [], [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        is_ignore = line.startswith("!")
        body = line[1:].strip() if is_ignore else line
        reason = module._refusal_reason(body)
        if reason is not None:
            report.append(module.Notice(line=raw, reason=reason, kind=module.REFUSED))
            continue
        (ignores if is_ignore else includes).append(body.rstrip("/"))
    lines = ["*", "!/.gitinclude"]
    for path in sorted(set(includes)):
        lines.extend(line_fn(path, "!", True))
    for path in sorted(set(ignores)):
        lines.extend(line_fn(path, "", False))
    return module.HEADER + "\n".join(lines) + "\n", report


# ------------------------------------------- the declaration cannot be ignored


def test_an_ignore_of_the_declaration_is_refused(tmp_path):
    """With ignores emitted last, `!.gitinclude` defeated the forced self line
    and the declaration was silently untracked, report empty. It is refused
    instead, with a sentence saying the declaration is always tracked."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    report = _install(root, "!.gitinclude\nREADME.md\n")

    assert [(n.kind, n.line) for n in report] == [(REFUSED, "!.gitinclude")], report
    assert ".gitinclude" in _status(root), _status(root)


def test_control_without_the_generated_file_a_global_excludes_file_drops_paths(tmp_path):
    """The control for the precedence pin above: with NO generated `.gitignore`,
    the same injected global file DOES hide declared paths, so the injection
    works and the precedence claim is about the generated file rather than about
    an inert environment. Stromus re-ran this shape independently and got the
    same pair."""
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("r\n")
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("a\n")
    # deliberately NO .gitignore in this fixture

    fake_global = tmp_path / "gitignore_global"
    fake_global.write_text("README.md\n*.md\n", encoding="utf-8")
    leaky = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.excludesFile",
        "GIT_CONFIG_VALUE_0": str(fake_global),
    }

    seen_leaky = _untracked(root, env=leaky)
    assert "README.md" not in seen_leaky and "docs/a.md" not in seen_leaky, (
        "the injected global file did not drop anything, so it is inert and the "
        f"precedence pin above cannot mean anything: {seen_leaky}"
    )
    assert _untracked(root) == {"README.md", "docs/a.md"}, (
        "the hermetic env did not restore both paths"
    )


def test_control_without_the_refusal_the_declaration_goes_untracked(tmp_path):
    """The control: the emitted file with the ignore line present and winning,
    which is what the compiler produced before this refusal."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    _write_naive(root, "!.gitinclude\nREADME.md\n",
                 "*\n!/.gitinclude\n!/README.md\n!/README.md/**\n"
                 "/.gitinclude\n/.gitinclude/**\n")

    assert ".gitinclude" not in _status(root), (
        "the declaration was tracked anyway, so this control cannot fail and "
        "proves nothing"
    )


# ----------------------------------------- hermetic git, and the proof it matters


def test_the_generated_gitignore_outranks_a_global_excludes_file(tmp_path):
    """Why the hermetic env is INSURANCE rather than a gate, stated as the
    property that was actually measured.

    A global excludes file cannot change what `--exclude-standard` reports for a
    repo whose `.gitignore` is the generated one: that file outranks
    `core.excludesFile` and starts with a catch-all `*`, so there is no path left
    for the global file to decide. Both directions were measured before this test
    was written:

    - a global file naming a DECLARED path (`README.md`) leaves it listed,
      because the repo's own `!` outranks the global exclusion;
    - a global file containing a NEGATION (`!secret.txt`) adds nothing, because
      the repo's `*` outranks it.

    The exposure therefore only exists for a fixture with NO generated
    `.gitignore` yet, where `--exclude-standard` reads info/exclude and
    core.excludesFile with nothing above them. No fixture here is in that state,
    so `GIT_ENV` is defensive and has no failing control behind it; if one is
    ever added, this test is where the reason is recorded.
    """
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("r\n")
    (root / ".gitinclude").write_text("README.md\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        compile_gitignore("README.md\n")[0], encoding="utf-8"
    )

    fake_global = tmp_path / "gitignore_global"
    fake_global.write_text("README.md\n!surprise.txt\n", encoding="utf-8")
    (root / "surprise.txt").write_text("s\n")
    leaky = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.excludesFile",
        "GIT_CONFIG_VALUE_0": str(fake_global),
    }

    assert "README.md" in _untracked(root, env=leaky), (
        "a global excludes file hid a path the generated .gitignore declares, "
        "which means the precedence this file relies on has changed"
    )
    assert "surprise.txt" not in _untracked(root, env=leaky), (
        "a global negation re-included a path the generated .gitignore excludes"
    )


def test_a_named_path_survives_a_global_excludes_file_through_the_verb(tmp_path):
    """The pinned property: compile through the verb with the hermetic env and
    the declared path is present, whatever the host's global excludes say."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("r\n")
    (root / ".gitinclude").write_text("README.md\n", encoding="utf-8")
    assert runner.invoke(app, ["workspace", "gitinclude", str(root)]).exit_code == 0

    assert "README.md" in _untracked(root), _untracked(root)
