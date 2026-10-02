"""Plugin discovery and the runner, and plan 3: a Cargo lane through the reference external executable (step 3).

Rows that need ``cargo`` skip loudly with their reason when it is absent; a CI image without cargo shows skips,
not passes. Everything else runs on any host.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from gr2.python_cli import lane_graph as lg
from gr2.python_cli import lane_plugins as lp
from gr2.python_cli.lane_graph import LaneRefused, plan_lane

CARGO_EXE = Path(__file__).resolve().parents[1] / "examples" / "grip-ecosystem-cargo"
needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed on this host")


def make_exe(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


OK_BODY = (
    "import json, sys\n"
    "req = json.load(sys.stdin)\n"
    "if sys.argv[1] == 'describe':\n"
    "    print(json.dumps({'protocol': 1, 'ok': True, 'units': [{'id': 'fake:' + req['key'], 'dir': req['dir'], 'edges': []}]}))\n"
    "else:\n"
    "    print(json.dumps({'protocol': 1, 'ok': True, 'method': 'm'}))\n"
)


def lane_of(tmp_path: Path, *keys: str) -> Path:
    lane = tmp_path / "lane"
    for k in keys:
        (lane / k).mkdir(parents=True)
    return lane


# --- discovery: only the user's PATH, only the named prefix ----------------------------------------------


def test_discovery_finds_only_executables_with_the_prefix_in_the_given_path(tmp_path: Path) -> None:
    bin1 = tmp_path / "bin1"
    make_exe(bin1, "grip-ecosystem-fake", OK_BODY)
    make_exe(bin1, "not-a-plugin", OK_BODY)
    (bin1 / "grip-ecosystem-notexec").write_text("x")  # not executable
    assert lp.discover_plugins(str(bin1)) == {"fake": str(bin1 / "grip-ecosystem-fake")}


def test_the_first_directory_wins_like_a_shell(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    make_exe(a, "grip-ecosystem-x", OK_BODY)
    make_exe(b, "grip-ecosystem-x", OK_BODY)
    assert lp.discover_plugins(f"{a}{os.pathsep}{b}")["x"] == str(a / "grip-ecosystem-x")


def test_an_empty_path_entry_is_not_the_current_directory(tmp_path: Path, monkeypatch) -> None:
    make_exe(tmp_path, "grip-ecosystem-here", OK_BODY)
    monkeypatch.chdir(tmp_path)
    assert lp.discover_plugins(f"{os.pathsep}") == {}


def test_a_plugin_file_inside_a_member_is_never_found_or_executed_but_the_same_one_on_the_path_is(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    body = f"import json, sys\njson.load(sys.stdin)\nopen({str(marker)!r}, 'a').write('x')\nprint(json.dumps({{'protocol': 1, 'ok': True, 'units': []}}))\n"
    lane = lane_of(tmp_path, "m")
    make_exe(lane / "m", "grip-ecosystem-evil", body)  # named by a MEMBER: must never run
    user_bin = tmp_path / "userbin"
    user_bin.mkdir()
    table = lp.plugin_table(str(user_bin))
    plan_lane(lane, ["m"], table)
    assert not marker.exists(), "a plugin that a member carries was executed"
    make_exe(user_bin, "grip-ecosystem-evil", body)  # CONTROL: the user's PATH names it
    plan_lane(lane, ["m"], lp.plugin_table(str(user_bin)))
    assert marker.exists(), "the control plugin on the user's PATH did not run, so the row above proves nothing"


def test_an_external_plugin_cannot_take_the_built_in_name(tmp_path: Path) -> None:
    make_exe(tmp_path, "grip-ecosystem-python", "raise SystemExit(7)\n")
    table = lp.plugin_table(str(tmp_path))
    assert list(table) == ["python"]
    lane = lane_of(tmp_path, "m")
    plan_lane(lane, ["m"], table)  # the built-in answered; the failing external was never called


# --- the runner: every failure is a refusal naming the plugin and the call -------------------------------


def one(tmp_path: Path, body: str, **kw):
    exe = make_exe(tmp_path / "bin", "grip-ecosystem-t", body)
    return lp.external_plugin("t", str(exe), **kw)


def describe_req(tmp_path: Path) -> dict:
    d = tmp_path / "m"
    d.mkdir(exist_ok=True)
    return {"protocol": 1, "key": "m", "dir": str(d)}


def test_a_good_external_plugin_round_trips_and_runs_in_the_member_directory(tmp_path: Path) -> None:
    body = "import json, os, sys\njson.load(sys.stdin)\nprint(json.dumps({'protocol': 1, 'ok': True, 'units': [{'id': 'x:' + os.path.basename(os.getcwd()), 'dir': os.getcwd()}]}))\n"
    ans = one(tmp_path, body)("describe", describe_req(tmp_path))
    assert ans["units"][0]["id"] == "x:m"


@pytest.mark.parametrize("label,body,needle", [
    ("non-zero exit", "import sys\nsys.stderr.write('boom\\n')\nraise SystemExit(3)\n", "exit status 3"),
    ("invalid json", "print('not json')\n", "not JSON"),
    ("empty output", "pass\n", "not JSON"),
    ("an exception", "raise RuntimeError('x')\n", "exit status 1"),
])
def test_a_failing_plugin_refuses_naming_the_plugin_and_the_call(tmp_path: Path, label, body, needle) -> None:
    with pytest.raises(LaneRefused) as exc:
        one(tmp_path, body)("describe", describe_req(tmp_path))
    assert exc.value.code == "plugin_failure"
    assert "'t'" in exc.value.detail and "describe" in exc.value.detail and needle in exc.value.detail


def test_a_plugin_that_does_not_answer_in_time_is_killed_and_refuses(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    body = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    import time

    t0 = time.monotonic()
    with pytest.raises(LaneRefused) as exc:
        one(tmp_path, body, timeout=1.0)("describe", describe_req(tmp_path))
    assert time.monotonic() - t0 < 15, "the call waited out the plugin instead of killing it at the timeout"
    assert "no answer within 1s" in exc.value.detail
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # it is gone, not left running


def test_an_oversize_answer_is_refused_not_parsed(tmp_path: Path) -> None:
    body = "import json\nprint(json.dumps({'protocol': 1, 'ok': True, 'units': [], 'pad': 'x' * 5000}))\n"
    with pytest.raises(LaneRefused) as exc:
        one(tmp_path, body, max_bytes=1000)("describe", describe_req(tmp_path))
    assert "over the 1000 byte bound" in exc.value.detail
    assert one(tmp_path, body, max_bytes=100_000)("describe", describe_req(tmp_path))["ok"] is True  # the bound is the only difference


def test_a_plugin_that_cannot_be_started_refuses(tmp_path: Path) -> None:
    with pytest.raises(LaneRefused) as exc:
        lp.external_plugin("ghost", str(tmp_path / "does-not-exist"))("describe", describe_req(tmp_path))
    assert "could not be started" in exc.value.detail


def test_the_plugin_gets_a_minimal_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN_FOR_THE_TEST", "hunter2")
    body = "import json, os, sys\njson.load(sys.stdin)\nprint(json.dumps({'protocol': 1, 'ok': True, 'units': [{'id': 'env:' + ','.join(sorted(os.environ)), 'dir': '.'}]}))\n"
    names = one(tmp_path, body)("describe", describe_req(tmp_path))["units"][0]["id"]
    assert "SECRET_TOKEN_FOR_THE_TEST" not in names and "PATH" in names


def test_a_failing_external_plugin_refuses_the_lane_and_never_falls_back_to_marker_order(tmp_path: Path) -> None:
    lane = lane_of(tmp_path, "a", "b")
    make_exe(tmp_path / "bin", "grip-ecosystem-bad", "raise SystemExit(9)\n")
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b"], lp.plugin_table(str(tmp_path / "bin")))
    assert exc.value.code == "plugin_failure" and "'bad'" in exc.value.detail


def test_a_wrong_protocol_version_refuses_naming_both_versions(tmp_path: Path) -> None:
    body = "import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'protocol': 2, 'ok': True, 'units': []}))\n"
    lane = lane_of(tmp_path, "a")
    make_exe(tmp_path / "bin", "grip-ecosystem-future", body)
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a"], lp.plugin_table(str(tmp_path / "bin")))
    assert exc.value.code == "plugin_protocol_mismatch"
    assert "protocol 2" in exc.value.detail and "speaks 1" in exc.value.detail


# --- plan 3: a Cargo lane through the reference executable ------------------------------------------------


def crate(lane: Path, name: str, deps: dict[str, str] | None = None, dev: dict[str, str] | None = None, build: dict[str, str] | None = None) -> None:
    d = lane / name
    (d / "src").mkdir(parents=True)
    (d / "src" / "lib.rs").write_text("pub fn f() {}\n")
    text = f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
    if deps:
        text += "[dependencies]\n" + "".join(f'{k} = {{ path = "../{k}" }}\n' for k in deps)
    if build:
        text += "[build-dependencies]\n" + "".join(f'{k} = {{ path = "../{k}" }}\n' for k in build)
    if dev:
        text += "[dev-dependencies]\n" + "".join(f'{k} = {{ path = "../{k}" }}\n' for k in dev)
    (d / "Cargo.toml").write_text(text)


@pytest.fixture
def cargo_path(tmp_path: Path) -> str:
    """A PATH directory holding only the reference executable, copied (a symlink would still be a user's choice)."""
    d = tmp_path / "userbin"
    d.mkdir()
    shutil.copy(CARGO_EXE, d / "grip-ecosystem-cargo")
    return f"{d}{os.pathsep}{os.environ['PATH']}"


@needs_cargo
def test_plan_3_a_dev_dependency_cycle_is_test_edges_no_group_and_normal_order(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", deps={"b": ""})  # a needs b (normal)
    crate(lane, "b", dev={"a": ""})  # b's TESTS need a: Cargo allows this cycle
    plan = plan_lane(lane, ["a", "b"], lp.plugin_table(cargo_path))
    assert [list(g.units) for g in plan.groups] == [["cargo:b"], ["cargo:a"]]  # b installs first; a needs it
    assert not plan.cyclic_groups
    kinds = {(e.src, e.dst): e.kind for e in plan.edges}
    assert kinds == {("cargo:a", "cargo:b"): "install", ("cargo:b", "cargo:a"): "test"}
    assert {e.via for e in plan.edges} == {"a/Cargo.toml [dependencies]", "b/Cargo.toml [dev-dependencies]"}
    assert not (lane / "a" / "Cargo.lock").exists() and not (lane / "b" / "Cargo.lock").exists()  # no new file in a member


@needs_cargo
def test_a_lane_reached_through_a_symlink_still_gets_a_clean_via(tmp_path: Path, cargo_path: str) -> None:
    """cargo reports real paths; the request may carry a symlinked lane path (/tmp, a linked workspace). The via must
    stay `<key>/Cargo.toml [section]`, not a walk up through the link (found by running the plugin from /tmp)."""
    real = tmp_path / "real"
    real.mkdir()
    crate(real, "a", deps={"b": ""})
    crate(real, "b")
    link = tmp_path / "link"
    link.symlink_to(real)
    plan = plan_lane(link, ["a", "b"], lp.plugin_table(cargo_path))
    assert [e.via for e in plan.edges] == ["a/Cargo.toml [dependencies]"]


@needs_cargo
def test_plan_3_control_the_same_pair_with_the_cycle_removed_plans_cleanly(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", deps={"b": ""})
    crate(lane, "b")
    plan = plan_lane(lane, ["a", "b"], lp.plugin_table(cargo_path))
    assert [list(g.units) for g in plan.groups] == [["cargo:b"], ["cargo:a"]]
    assert {(e.src, e.dst, e.kind) for e in plan.edges} == {("cargo:a", "cargo:b", "install")}


@needs_cargo
def test_plan_3_a_normal_edge_cycle_refuses_in_cargos_own_words_and_nothing_is_installed(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", deps={"b": ""})
    crate(lane, "b", deps={"a": ""})
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b"], lp.plugin_table(cargo_path))
    assert exc.value.code == "plugin_failure"
    assert "'cargo'" in exc.value.detail and "cyclic package dependency" in exc.value.detail
    assert not (lane / "a" / "Cargo.lock").exists()  # the refusal left no lockfile behind either


@needs_cargo
def test_a_build_dependency_is_an_install_edge_and_orders_installs(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", build={"b": ""})
    crate(lane, "b")
    plan = plan_lane(lane, ["a", "b"], lp.plugin_table(cargo_path))
    assert [(e.src, e.dst, e.kind, e.via) for e in plan.edges] == [("cargo:a", "cargo:b", "install", "a/Cargo.toml [build-dependencies]")]
    assert [list(g.units) for g in plan.groups] == [["cargo:b"], ["cargo:a"]]


@needs_cargo
def test_a_stale_lockfile_is_refused_and_never_rewritten(tmp_path: Path, cargo_path: str) -> None:
    """With a lockfile present the plugin reads with --locked, so a manifest that no longer matches it is a refusal
    and the file is not touched; without --locked, cargo would quietly rewrite the member's lockfile."""
    import subprocess

    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", deps={"b": ""})
    crate(lane, "b")
    crate(lane, "c")
    subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=lane / "a", check=True, capture_output=True)
    before = (lane / "a" / "Cargo.lock").read_bytes()
    crate_text = (lane / "a" / "Cargo.toml").read_text() + 'c = { path = "../c" }\n'
    (lane / "a" / "Cargo.toml").write_text(crate_text)  # the manifest now needs a package the lockfile lacks
    with pytest.raises(LaneRefused) as exc:
        plan_lane(lane, ["a", "b", "c"], lp.plugin_table(cargo_path))
    assert exc.value.code == "plugin_failure" and "lock" in exc.value.detail.lower()
    assert (lane / "a" / "Cargo.lock").read_bytes() == before


@needs_cargo
def test_a_member_with_a_committed_lockfile_is_read_with_locked_and_the_lockfile_is_untouched(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "a", deps={"b": ""})
    crate(lane, "b")
    import subprocess

    subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=lane / "a", check=True, capture_output=True)
    before = (lane / "a" / "Cargo.lock").read_bytes()
    plan_lane(lane, ["a", "b"], lp.plugin_table(cargo_path))
    assert (lane / "a" / "Cargo.lock").read_bytes() == before


@needs_cargo
def test_a_python_member_and_a_cargo_member_mix_in_one_lane(tmp_path: Path, cargo_path: str) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    crate(lane, "rs")
    (lane / "py").mkdir()
    (lane / "py" / "pyproject.toml").write_text('[project]\nname = "py-lib"\nversion = "0"\ndependencies = []\n')
    plan = plan_lane(lane, ["py", "rs"], lp.plugin_table(cargo_path))
    assert [list(g.units) for g in plan.groups] == [["python:py-lib"], ["cargo:rs"]]


def test_the_cargo_plugin_answers_not_mine_for_a_directory_with_no_cargo_toml(tmp_path: Path) -> None:
    lane = lane_of(tmp_path, "x")
    exe = lp.external_plugin("cargo", str(CARGO_EXE))
    ans = exe("describe", {"protocol": 1, "key": "x", "dir": str(lane / "x")})
    assert ans == {"protocol": 1, "ok": True, "units": []}


def test_the_cargo_plugin_refuses_plan_group_in_its_own_words(tmp_path: Path) -> None:
    exe = lp.external_plugin("cargo", str(CARGO_EXE))
    ans = exe("plan_group", {"protocol": 1, "units": [{"id": "cargo:a", "dir": "/x"}, {"id": "cargo:b", "dir": "/y"}], "edges": []})
    assert ans["ok"] is False and "cycle in normal or build dependencies" in ans["reason"] and "cargo:a, cargo:b" in ans["detail"]
    lg.check_answer("cargo", "plan_group", ans)  # and it is a valid refusal under the shared validator
