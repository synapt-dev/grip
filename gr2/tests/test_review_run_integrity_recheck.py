"""`review run` re-checks the bound tree AFTER the code it runs has had a chance to change it.

THE DEFECT: the tree and untracked-drift checks ran once, before the venv and the install, and never
again. The lane's own `.review-install` install step, and in a multi-member lane another member's
tests, are code the review itself runs; either can rewrite a reviewed tracked file, and the run
still read green with a receipt that claimed the bound head tree.

THE CONTRACT these rows pin, in both lane shapes:
  * TRACKED files are strict: after install and again after the tests, every member's tracked
    tree must still equal its bound tree. The check also runs right BEFORE a member's tests,
    because a change the tests then undo leaves a final tree that matches while the result is
    about another one;
  * UNTRACKED files: the set is taken before the install. A path that is new after the install
    or after the tests refuses, naming the path, unless it sits under `build/` or an
    `*.egg-info/` (what a plain `pip install <dir>` leaves, measured) or is something the run
    itself creates (the venv, caches, the receipt and log). A new `conftest.py` or any
    importable `.py` outside the admitted paths refuses wherever it lands. The admitted paths
    are code-capable: anything under a `__pycache__`, `.pytest_cache`, `*.egg-info` or `build/`
    segment is admitted, so a planted `__pycache__` bytecode file whose header matches a tracked
    source is NOT seen (measured: it is imported in place of the source);
  * a refusal names the member whose tree changed (`tree_drift` / `untracked_drift`).
A repo whose tests write untracked artifacts (a coverage file) must ignore them: git does not
report an ignored path, and an untracked one is exactly what this check exists to see.

Each tamper row has a control that proves the tamper is what turns the verdict, and each control row
shows the same lane reads red or green WITHOUT the tamper.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
from gr2.python_cli import review_run as rr

from tests.test_review_run import _git, _offline_install, _pkg_repo, _write_marker
from tests.test_review_run_multi_repo import HOST_PATHS, PTH_SCRIPT, _lane

BUGGY_TEST = "from demo_pkg import VALUE\n\ndef test_value():\n    assert VALUE == 2\n"
OK_TEST = "from demo_pkg import VALUE\n\ndef test_value():\n    assert VALUE == 1\n"


# ------------------------------------------------------------------ single-repo lane


def _run(repo: Path, install: list[str] | None = None) -> dict:
    return rr.run_review_lane(
        repo,
        package="demo_pkg",
        pytest_args=["-q"],
        install=install if install is not None else _offline_install(repo),
    )


def _install_hint(tokens: list[str]) -> str:
    return "install = " + " ".join(shlex.quote(t) for t in tokens) + "\n"


def _single(tmp_path: Path, test_body: str) -> Path:
    repo, head_tree = _pkg_repo(tmp_path, test_body=test_body)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    return repo


def _tampering_install(repo: Path, target: Path, new_text: str) -> list[str]:
    """An offline install that FIRST rewrites a tracked file, as a repo's own install step can."""
    vpy = repo / rr._VENV_DIRNAME / "bin" / "python"
    script = (
        "import site,sys,pathlib;"
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2]);"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[3:])+'\\n')"
    )
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    return [str(vpy), "-c", script, str(target), new_text, *paths]


def test_control_a_buggy_single_repo_lane_reads_red(tmp_path: Path) -> None:
    repo = _single(tmp_path, BUGGY_TEST)
    receipt = _run(repo)
    assert receipt["result"] == "red", receipt


def test_an_install_step_that_rewrites_tracked_source_is_refused(tmp_path: Path) -> None:
    """The install turns the buggy repo's own source into the value its test wants. Without a
    re-check that reads GREEN with a receipt still claiming the bound tree."""
    repo = _single(tmp_path, BUGGY_TEST)
    install = _tampering_install(repo, repo / "src" / "demo_pkg" / "__init__.py", "VALUE = 2\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo, install)
    assert exc.value.code == "tree_drift", exc.value
    receipt = __import__("json").loads((repo / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["refusal_code"] == "tree_drift"


def test_tests_that_rewrite_tracked_source_during_the_run_are_refused(tmp_path: Path) -> None:
    """The run itself changed the reviewed tree, so the result is not about the bound head."""
    src = tmp_path / "lane" / "src" / "demo_pkg" / "__init__.py"
    body = (
        "import pathlib\n"
        "from demo_pkg import VALUE\n\n"
        "def test_value():\n"
        f"    pathlib.Path({str(src)!r}).write_text('VALUE = 2\\n')\n"
        "    assert VALUE == 1\n"
    )
    repo = _single(tmp_path, body)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo)
    assert exc.value.code == "tree_drift", exc.value


def test_control_the_same_test_without_the_rewrite_reads_green(tmp_path: Path) -> None:
    repo = _single(tmp_path, OK_TEST)
    receipt = _run(repo)
    assert receipt["result"] == "green", receipt


def test_a_test_writing_a_new_untracked_file_is_refused_naming_it(tmp_path: Path) -> None:
    """Strict on purpose: a file the tests add that is not build output could be a conftest or an
    importable module, and the check can not tell a coverage file from one. Ignore the artifact."""
    artifact = tmp_path / "lane" / ".coverage-data"
    body = (
        "import pathlib\n"
        "from demo_pkg import VALUE\n\n"
        "def test_value():\n"
        f"    pathlib.Path({str(artifact)!r}).write_text('data')\n"
        "    assert VALUE == 1\n"
    )
    repo = _single(tmp_path, body)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo)
    assert exc.value.code == "untracked_drift" and ".coverage-data" in exc.value.detail, exc.value
    assert ".gitignore" in exc.value.detail, "the refusal must name the fix"


def test_an_install_that_drops_a_conftest_is_refused_naming_it(tmp_path: Path) -> None:
    repo = _single(tmp_path, OK_TEST)
    vpy = repo / rr._VENV_DIRNAME / "bin" / "python"
    script = (
        "import site,sys,pathlib;"
        "pathlib.Path(sys.argv[1]).write_text('# planted\\n');"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[2:])+'\\n')"
    )
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo, [str(vpy), "-c", script, str(repo / "conftest.py"), *paths])
    assert exc.value.code == "untracked_drift" and "conftest.py" in exc.value.detail, exc.value


def test_a_change_the_tests_undo_is_caught_before_they_run(tmp_path: Path) -> None:
    """THE PRE-TEST CHECK: the install rewrites the source so the buggy repo's test passes, and the
    test puts the original back afterwards. The tree at the end equals the bound tree, so only a
    check made BEFORE the tests sees that the run was about another tree."""
    src = tmp_path / "lane" / "src" / "demo_pkg" / "__init__.py"
    body = (
        "import pathlib\n"
        "from demo_pkg import VALUE\n\n"
        "def test_value():\n"
        "    assert VALUE == 2\n"
        f"    pathlib.Path({str(src)!r}).write_text('VALUE = 1\\n')\n"
    )
    repo = _single(tmp_path, body)
    install = _tampering_install(repo, src, "VALUE = 2\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo, install)
    assert exc.value.code == "tree_drift", exc.value


def test_what_the_single_repo_installs_own_build_leaves_behind_is_not_drift(tmp_path: Path) -> None:
    """THE BOUNDARY: a non-editable `pip install <dir>` leaves an untracked `build/` in the tree it
    installs. That is the install's own output, not an injected file."""
    repo = _single(tmp_path, OK_TEST)
    vpy = repo / rr._VENV_DIRNAME / "bin" / "python"
    script = (
        "import site,sys,pathlib;"
        "pathlib.Path(sys.argv[1]).mkdir(parents=True, exist_ok=True);"
        "(pathlib.Path(sys.argv[1])/'artifact.txt').write_text('x');"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[2:])+'\\n')"
    )
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    install = [str(vpy), "-c", script, str(repo / "build"), *paths]
    receipt = _run(repo, install)
    assert receipt["result"] == "green" and (repo / "build" / "artifact.txt").is_file(), receipt


# ------------------------------------------------------------------ multi-member lane


def _rewrite_test(path: Path, text: str) -> str:
    """Test source whose single test rewrites `path` (a tracked file of ANOTHER member)."""
    return (
        "import pathlib\n"
        "from demo_core import total\n\n"
        "def test_total():\n"
        f"    pathlib.Path({str(path)!r}).write_text({text!r})\n"
        "    assert total(4) == 1000\n"
    )


WEB_RED_TEST = "from demo_web import quote\n\ndef test_quote():\n    assert quote(4) == 1001\n"
WEB_GREEN_TEST = "def test_quote():\n    assert True\n"


def test_control_a_buggy_web_member_reads_red_in_the_lane(tmp_path: Path) -> None:
    lane = _lane(tmp_path, web_test=WEB_RED_TEST)
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "red" and receipt["members"][1]["result"] == "red", receipt


def test_an_earlier_members_tests_rewriting_a_later_member_are_refused(tmp_path: Path) -> None:
    """P1: a test in core, which runs first, rewrites web's tracked test file so the buggy web reads
    green. The tree is re-checked after web's install and before web's tests, naming web."""
    web_test_file = tmp_path / "lane" / "demo-web" / "tests" / "test_demo_web.py"
    lane = _lane(
        tmp_path,
        core_test=_rewrite_test(web_test_file, WEB_GREEN_TEST),
        web_test=WEB_RED_TEST,
    )
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "demo-web", exc.value
    receipt = __import__("json").loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["refusal_member"] == "demo-web"


def test_the_last_members_tests_rewriting_an_earlier_member_are_refused(tmp_path: Path) -> None:
    """Nothing runs after the last member, so only the check after its tests sees this."""
    core_src = tmp_path / "lane" / "demo-core" / "src" / "demo_core" / "__init__.py"
    web_test = (
        "import pathlib\n"
        "from demo_web import quote\n\n"
        "def test_quote():\n"
        f"    pathlib.Path({str(core_src)!r}).write_text('def total(q):\\n    return 1\\n')\n"
        "    assert quote(4) == 1000\n"
    )
    lane = _lane(tmp_path, web_test=web_test)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "demo-core", exc.value


def test_a_later_members_install_rewriting_an_earlier_member_is_refused(tmp_path: Path) -> None:
    core_src = tmp_path / "lane" / "demo-core" / "src" / "demo_core" / "__init__.py"
    new_source = "'def total(q):\\n    return 1\\n'"
    tamper = f"import pathlib; pathlib.Path({str(core_src)!r}).write_text({new_source});"
    tokens = ["{venv}", "-c", tamper + PTH_SCRIPT, "demo-web", "{lane}/src", *HOST_PATHS]
    lane = _lane(tmp_path, web_hint_extra=_install_hint(tokens))
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "demo-core", exc.value


def test_an_earlier_members_tests_dropping_an_untracked_file_into_a_later_member_are_refused(
    tmp_path: Path,
) -> None:
    """An injected conftest changes what a LATER member's tests do with no tracked file touched, so
    the untracked-drift check runs for every member whose tests have not run yet."""
    planted = tmp_path / "lane" / "demo-web" / "conftest.py"
    core_test = (
        "import pathlib\n"
        "from demo_core import total\n\n"
        "def test_total():\n"
        f"    pathlib.Path({str(planted)!r}).write_text('# planted\\n')\n"
        "    assert total(4) == 1000\n"
    )
    lane = _lane(tmp_path, core_test=core_test)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "untracked_drift" and exc.value.member == "demo-web", exc.value
    assert "conftest.py" in exc.value.detail, exc.value


def test_what_a_members_own_install_leaves_behind_is_not_drift_for_the_lane(tmp_path: Path) -> None:
    """THE BOUNDARY, multi-member: web's install writes an untracked `build/` into web, as a plain
    `pip install <dir>` does. It is the install's own output; the lane stays green."""
    build = tmp_path / "lane" / "demo-web" / "build"
    leave = (
        f"import pathlib; b = pathlib.Path({str(build)!r}); b.mkdir(exist_ok=True); "
        "(b / 'artifact.txt').write_text('x');"
    )
    tokens = ["{venv}", "-c", leave + PTH_SCRIPT, "demo-web", "{lane}/src", *HOST_PATHS]
    lane = _lane(tmp_path, web_hint_extra=_install_hint(tokens))
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green" and (build / "artifact.txt").is_file(), receipt


def test_a_members_own_new_untracked_file_is_refused_naming_it(tmp_path: Path) -> None:
    artifact = tmp_path / "lane" / "demo-core" / ".coverage-data"
    core_test = (
        "import pathlib\n"
        "from demo_core import total\n\n"
        "def test_total():\n"
        f"    pathlib.Path({str(artifact)!r}).write_text('data')\n"
        "    assert total(4) == 1000\n"
    )
    lane = _lane(tmp_path, core_test=core_test)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "untracked_drift" and exc.value.member == "demo-core", exc.value
    assert ".coverage-data" in exc.value.detail, exc.value


def test_a_change_an_earlier_member_undoes_is_caught_before_the_later_members_tests(
    tmp_path: Path,
) -> None:
    """Multi-member form of the pre-test check: core's tests rewrite web's test file into one
    that passes and then puts the original back, so web's tree matches its bound tree again by the
    end of the run; only the check made before web's tests sees it."""
    web_test_file = tmp_path / "lane" / "demo-web" / "tests" / "test_demo_web.py"
    restoring = (
        "import pathlib\n\n"
        "def test_quote():\n"
        f"    pathlib.Path({str(web_test_file)!r}).write_text({WEB_RED_TEST!r})\n"
    )
    lane = _lane(
        tmp_path, core_test=_rewrite_test(web_test_file, restoring), web_test=WEB_RED_TEST
    )
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "demo-web", exc.value


# ------------------------------------------------------------------ the helper itself


def test_list_untracked_names_files_inside_an_untracked_directory(tmp_path: Path) -> None:
    """git collapses a wholly untracked directory to one entry unless told otherwise, and a file
    added inside it later is then invisible: the listing is `['notes/']` before and after."""
    repo = _single(tmp_path, OK_TEST)
    (repo / "notes").mkdir()
    (repo / "notes" / "a.txt").write_text("x")
    assert "notes/a.txt" in rr.list_untracked(repo), rr.list_untracked(repo)


def test_a_file_planted_in_a_pre_existing_untracked_dir_is_refused(tmp_path: Path) -> None:
    repo = _single(tmp_path, OK_TEST)
    (repo / "notes").mkdir()
    (repo / "notes" / "a.txt").write_text("x")
    baseline = rr.list_untracked(repo)
    (repo / "notes" / "conftest.py").write_text("# planted\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.assert_no_new_untracked(repo, baseline)
    assert exc.value.code == "untracked_drift", exc.value
    assert "notes/conftest.py" in exc.value.detail, exc.value


def test_a_pre_existing_untracked_dir_refuses_before_anything_runs(tmp_path: Path) -> None:
    """Why the gap above is not reachable through the lane today: the first drift check
    refuses any untracked directory that is not on the allow-list, so none reaches the baseline."""
    repo = _single(tmp_path, OK_TEST)
    (repo / "notes").mkdir()
    (repo / "notes" / "a.txt").write_text("x")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        _run(repo)
    assert exc.value.code == "untracked_drift" and "notes/" in exc.value.detail, exc.value
