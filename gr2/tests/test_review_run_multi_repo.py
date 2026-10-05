"""`review run` over a MULTI-repo lane: one lane reconstructed from one `gr:` id that binds two
repositories, run in ONE shared venv with the members installed in order and each member's tests
run in its own directory.

Written BEFORE the code. Each row names what must make it go red.

The members are `demo-core` (a pure function) and `demo-web` (a service whose test imports core
and asserts the core it imported is the REVIEWED one, the lane's copy). Install is offline: each
member's own `.review-install` seeds a per-member `.pth`, so two members share one venv without
clobbering each other, and the host's pytest stays importable.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from gr2.python_cli import review_run as rr
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

HOST_PATHS = [p for p in sys.path if p]

PTH_SCRIPT = (
    "import site,sys,pathlib;"
    "sp=pathlib.Path(site.getsitepackages()[0]);"
    "sp.mkdir(parents=True,exist_ok=True);"
    "(sp/('zz_'+sys.argv[1]+'.pth')).write_text('\\n'.join(sys.argv[2:])+'\\n')"
)

CORE_SRC = "def total(q):\n    return q * 250\n"
RED_CORE_TEST = "from demo_core import total\n\ndef test_bad():\n    assert total(4) == 1\n"
CORE_TEST = (
    "from demo_core import total\n\n"
    "def test_total():\n    assert total(4) == 1000\n"
)


def _web_test(core_dir: Path) -> str:
    return (
        "import demo_core\nfrom demo_web import quote\n\n"
        "def test_quote():\n    assert quote(4) == 1000\n\n"
        "def test_core_is_the_reviewed_one():\n"
        f"    assert demo_core.__file__.startswith({str(core_dir)!r}), demo_core.__file__\n"
    )


WEB_SRC = "from demo_core import total\n\ndef quote(q):\n    return total(q)\n"


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _member(lane: Path, key: str, pkg: str, src: str, test: str, *, path_entry: str = "{lane}/src",
            hint_extra: str = "") -> str:
    """A git repo at `<lane>/<key>` holding package `pkg`, its tests and its own `.review-install`.
    Returns the member's head tree."""
    repo = lane / key
    (repo / "src" / pkg).mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / pkg / "__init__.py").write_text(src)
    (repo / "tests" / f"test_{pkg}.py").write_text(test)
    tokens = ["{venv}", "-c", PTH_SCRIPT, key, path_entry, *HOST_PATHS]
    (repo / ".review-install").write_text(
        "install = " + " ".join(shlex.quote(t) for t in tokens) + "\n"
        f"package = {pkg}\n{hint_extra}"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@e.invalid")
    _git(repo, "config", "user.name", "a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", key)
    return _git(repo, "rev-parse", "HEAD^{tree}")


def _lane(tmp_path: Path, *, core_test: str = CORE_TEST, web_test: str | None = None,
          core_path_entry: str = "{lane}/src", core_hint_extra: str = "",
          web_hint_extra: str = "") -> Path:
    lane = tmp_path / "lane"
    lane.mkdir()
    core_tree = _member(lane, "demo-core", "demo_core", CORE_SRC, core_test,
                        path_entry=core_path_entry, hint_extra=core_hint_extra)
    web_tree = _member(
        lane, "demo-web", "demo_web", WEB_SRC,
        web_test if web_test is not None else _web_test(lane / "demo-core"),
        hint_extra=web_hint_extra,
    )
    marker = {
        "kind": rr._MARKER_KIND,
        "gr_commit": "cafef00d" * 5,
        "repos": [
            {"key": "demo-core", "bound_head": "a" * 40, "bound_head_tree": core_tree,
             "reconstructed_tree": core_tree, "tree_match": True},
            {"key": "demo-web", "bound_head": "b" * 40, "bound_head_tree": web_tree,
             "reconstructed_tree": web_tree, "tree_match": True},
        ],
    }
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n")
    return lane


def _tamper(lane: Path, key: str) -> None:
    pkg = key.replace("-", "_")
    f = lane / key / "src" / pkg / "__init__.py"
    f.write_text(f.read_text() + "# tampered\n")


def _log(lane: Path, key: str) -> Path:
    return lane / f"{key}{rr._OUTPUT_LOG_NAME}"


# ------------------------------------------------------------------ the happy path


def test_two_members_both_green_make_a_green_lane(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])

    assert receipt["result"] == "green", receipt
    assert [m["key"] for m in receipt["members"]] == ["demo-core", "demo-web"]
    assert receipt["order"] == ["demo-core", "demo-web"]
    assert receipt["not_run"] == []
    assert all(m["result"] == "green" and m["passed"] >= 1 for m in receipt["members"]), receipt
    # web's test asserts the core it imported is the lane's copy; green means it was.
    assert receipt["members"][1]["passed"] == 2
    assert _log(lane, "demo-core").is_file() and _log(lane, "demo-web").is_file()
    on_disk = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert on_disk["result"] == "green" and on_disk["order"] == receipt["order"]
    assert (lane / rr._VENV_DIRNAME).is_dir(), "one shared venv at the lane root"


def test_a_red_member_makes_a_red_lane_and_does_not_stop_the_run(tmp_path: Path) -> None:
    lane = _lane(tmp_path, core_test=RED_CORE_TEST)
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])

    assert receipt["result"] == "red", receipt
    core, web = receipt["members"]
    assert core["result"] == "red" and core["failed"] == 1
    assert any("test_bad" in i for i in core["failed_ids"]), core["failed_ids"]
    assert web["result"] == "green", "a red member must not stop the others (D5)"
    assert receipt["not_run"] == []


# ------------------------------------------------------------ refusals stop the run


def test_a_tampered_member_refuses_before_anything_runs(tmp_path: Path) -> None:
    """Integrity first: every member's tree is checked before ANY member installs or executes, so
    a lane that already fails integrity runs nothing. The refusal names the member."""
    lane = _lane(tmp_path)
    _tamper(lane, "demo-web")

    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "demo-web"

    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused"
    assert receipt["refusal_code"] == "tree_drift" and receipt["refusal_member"] == "demo-web"
    assert receipt["not_run"] == ["demo-core"], "the member that did not run is named, not hidden"
    assert not _log(lane, "demo-core").exists() and not _log(lane, "demo-web").exists()
    assert not (lane / rr._VENV_DIRNAME).exists(), "a lane that failed integrity installs nothing"


def test_an_untracked_file_in_one_member_refuses_before_anything_runs(tmp_path: Path) -> None:
    """The tracked-tree check cannot see an injected `conftest.py`; the untracked-drift check can,
    and in a multi-member lane it must run for EVERY member, not only the first."""
    lane = _lane(tmp_path)
    (lane / "demo-web" / "conftest.py").write_text("# injected\n")

    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "untracked_drift" and exc.value.member == "demo-web"
    assert exc.value.not_run == ["demo-core"]
    assert not (lane / rr._VENV_DIRNAME).exists()


def test_a_refusal_at_the_first_member_leaves_the_later_one_in_not_run(tmp_path: Path) -> None:
    """A refusal in one member stops the run and names it. core's install command cannot run,
    so core REFUSES; the venv is shared, so web is not run in an environment the review did not
    claim. web is in `not_run` with no log, and nothing about the lane reads as green."""
    lane = _lane(tmp_path)
    (lane / "demo-core" / ".review-install").write_text(
        "install = /nonexistent/installer-for-this-test\npackage = demo_core\n"
    )
    _git(lane / "demo-core", "add", ".")
    _git(lane / "demo-core", "commit", "-q", "-m", "broken install")
    tree = _git(lane / "demo-core", "rev-parse", "HEAD^{tree}")
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["repos"][0]["bound_head_tree"] = tree
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))

    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "install_failed" and exc.value.member == "demo-core"
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["not_run"] == ["demo-web"]
    assert not _log(lane, "demo-web").exists()


def test_a_member_that_selects_zero_tests_refuses(tmp_path: Path) -> None:
    lane = _lane(tmp_path, core_test="def helper():\n    return 1\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "zero_collected" and exc.value.member == "demo-core"
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["not_run"] == ["demo-web"]


def test_a_package_that_resolves_outside_the_lane_refuses_for_that_member(tmp_path: Path) -> None:
    """CONTROL for the import check: core's install puts a stub `demo_core` that lives OUTSIDE
    the lane on the path instead of the lane's own src. Without the per-member check web's
    tests would pass against a core nobody reviewed."""
    outside = tmp_path / "outside" / "demo_core"
    outside.mkdir(parents=True)
    (outside / "__init__.py").write_text(CORE_SRC)
    lane = _lane(tmp_path, core_path_entry=str(outside.parent))

    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "import_escapes_lane" and exc.value.member == "demo-core"


# -------------------------------------------------------------------------- order


def test_the_order_is_printed_and_a_wrong_order_does_not_read_as_green(tmp_path: Path) -> None:
    """web before core: web's tests cannot import the reviewed core yet. Whatever it is called,
    it is not a green, and the receipt carries the order that was used."""
    lane = _lane(tmp_path)
    try:
        receipt = rr.run_review_lane(lane, pytest_args=["-q"], order=["demo-web", "demo-core"])
    except rr.ReviewRunRefused as exc:
        assert exc.member == "demo-web"
        receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
        assert receipt["result"] == "refused"
    else:
        assert receipt["result"] != "green", receipt
    assert receipt["order"] == ["demo-web", "demo-core"]


@pytest.mark.parametrize("bad", [["demo-core"], ["demo-core", "demo-web", "demo-web"],
                                 ["demo-core", "elsewhere"]])
def test_an_order_that_is_not_every_member_once_is_refused(tmp_path: Path, bad: list[str]) -> None:
    lane = _lane(tmp_path)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"], order=bad)
    assert exc.value.code == "bad_order"
    assert not (lane / rr._VENV_DIRNAME).exists()


# ---------------------------------------------------- the two refusing sites (D4, D8)


@pytest.mark.parametrize("flag", [{"package": "demo_core"}, {"install": ["true"]}])
def test_single_repo_flags_on_a_multi_member_lane_are_refused(tmp_path: Path, flag: dict) -> None:
    lane = _lane(tmp_path)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=[], **flag)
    assert exc.value.code == "member_flags_ambiguous"
    assert ".review-install" in exc.value.detail, "the refusal names the fix"
    assert not (lane / rr._VENV_DIRNAME).exists()


def test_the_non_pytest_runner_site_refuses_a_multi_member_lane(tmp_path: Path) -> None:
    """The SECOND refusing site (`run_test_command_in_lane`) keeps refusing, with its own code."""
    lane = _lane(tmp_path)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_test_command_in_lane(lane, runner="cargo", test_command=["cargo", "test"])
    assert exc.value.code == "member_runner_unsupported"


def test_a_member_that_declares_a_non_pytest_runner_is_refused_in_v1(tmp_path: Path) -> None:
    lane = _lane(tmp_path, core_hint_extra="runner = cargo\ntest = cargo test\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "member_runner_unsupported" and exc.value.member == "demo-core"


# --------------------------------------------------------------------- the CLI table


def test_cli_green_prints_one_line_per_member_then_the_lane_and_exits_0(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    code, out = _cli("review", "run", str(lane))
    assert code == 0, out
    assert "demo-core" in out and "demo-web" in out
    assert "lane green" in out and "members=2" in out, out


def test_cli_red_exits_1(tmp_path: Path) -> None:
    lane = _lane(tmp_path, core_test=RED_CORE_TEST)
    code, out = _cli("review", "run", str(lane))
    assert code == 1, out
    assert "lane red" in out, out


def test_cli_refused_exits_2_names_the_member_and_what_did_not_run(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    _tamper(lane, "demo-core")
    code, out = _cli("review", "run", str(lane))
    assert code == 2, out
    assert "refused" in out and "tree_drift" in out and "demo-core" in out, out
    assert "not run" in out and "demo-web" in out, out
    assert "green" not in out, "a stopped run must never read as green"


def test_cli_json_prints_the_lane_receipt_and_order_flag_is_accepted(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    code, out = _cli("review", "run", str(lane), "--order", "demo-core,demo-web", "--json")
    assert code == 0, out
    receipt = json.loads(out[out.index("{"):])
    assert receipt["result"] == "green" and receipt["order"] == ["demo-core", "demo-web"]
    assert [m["key"] for m in receipt["members"]] == ["demo-core", "demo-web"]


# --------------------------------------------------------------------------- close


def test_close_carries_the_receipt_and_every_member_log_out(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from gr2.python_cli.review_allocation import record_created_allocation
    record_created_allocation(workspace, lane, "workspace", "review", [lane / "demo-core", lane / "demo-web"], disposable=True)
    marker_path = lane / rr._MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["workspace_root"] = str(workspace)
    marker_path.write_text(json.dumps(marker))
    rr.run_review_lane(lane, pytest_args=["-q"])
    code, out = _cli("review", "close", str(lane), "--json")
    assert code == 0, out

    closed = json.loads(out)
    kept = Path(closed["preserved_run"]["dir"])
    assert kept.is_relative_to(workspace / ".grip" / "state" / "review-allocations")
    receipt = json.loads(Path(closed["preserved_run"]["receipt"]).read_text())
    for member in receipt["members"]:
        preserved = Path(member["output_log"])
        assert preserved.is_file() and preserved.parent == kept, member
    assert not lane.exists()
