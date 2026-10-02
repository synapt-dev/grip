"""Row 4: run the reviewed repo's tests INSIDE the
materialized review lane and record it in the project-tier receipt as fruit.

`record_review_verification` runs a test command in `review_root/repos/<key>` and
appends a verification record binding the REAL exit code, the head the lane holds,
the lane cwd, and -- measured by a probe under the same interpreter -- which python
and which package file actually resolved there. The last two exist because a stale
editable install can silently import a DIFFERENT desk's package; a receipt that
cannot say whose code it tested has the same defect one layer up.
"""
from __future__ import annotations

from tests.native_root_helper import native_root

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import grip, open_gr_review, project_review


@pytest.fixture(autouse=True)
def _isolated_review_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPT_REVIEW_CACHE_ROOT", str(tmp_path / "review-cache"))
    monkeypatch.delenv("SYNAPT_REVIEW_PROFILE_DIR", raising=False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _source_with_package(root: Path, name: str, *, package: bool) -> tuple[Path, str, str]:
    """A source repo whose review head optionally carries an importable package plus
    a trivial passing test. Returns (source, base_sha, head_sha)."""
    origin = root / f"{name}.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text(f"{name} base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    _git(source, "checkout", "-q", "-b", f"review/{name}")
    if package:
        pkg = source / f"{name}_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('WHERE = "lane"\n')
        (source / "test_smoke.py").write_text(
            f"import {name}_pkg\n\n\ndef test_where():\n    assert {name}_pkg.WHERE == \"lane\"\n"
        )
    (source / "review.txt").write_text(f"{name} review\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review")
    _git(source, "push", "-q", "origin", f"review/{name}")
    head = _git(source, "rev-parse", f"review/{name}")
    _git(source, "checkout", "-q", "main")
    return source, base, head


def _open_single_repo_review(tmp_path: Path, name: str, *, package: bool):
    """Grip-init a one-repo workspace, make a review-kind gr commit, and open it.
    Returns (workspace, review_root, head_sha)."""
    import argparse

    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    src, base, head = _source_with_package(tmp_path, name, package=package)
    url = _git(src, "remote", "get-url", "origin")
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\nurl = "{url}"\n'
        f'\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["{name}"]\n'
    )
    native_root(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos=name, branch="main", source="test", default_commands=[],
    ))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False,
    ))
    pins = [project_review.ProjectReviewPin(
        key=name, repo=f"local:{src}", path=f"repos/{name}", base=base, head=head)]
    spec = project_review.make_spec(workspace, pins)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", spec.grip_commit,
        {name: (src, f"review/{name}")}, prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened", outcome
    return workspace, outcome.review_root, head


def _open_multi_repo_review(tmp_path: Path, names, declared, *, omit_field=None):
    """An N-repo review. Each repo in `declared` gets a full [repos.review_test];
    others get none (skipped by the verifier). `omit_field=(repo, field)` writes that
    repo's review_test WITHOUT that field (for the missing-field witness). Returns
    (workspace, review_root, {name: head})."""
    import argparse

    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    srcs = {n: _source_with_package(tmp_path, n, package=True) for n in names}
    py = sys.executable
    blocks = []
    for n in names:
        src, _base, _head = srcs[n]
        url = _git(src, "remote", "get-url", "origin")
        block = f'[[repos]]\nname = "{n}"\npath = "sources/{n}"\nurl = "{url}"\n'
        if n in declared:
            fields = {
                "command": f'["{py}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="]',
                "interpreter": f'"{py}"',
                "import_module": f'"{n}_pkg"',
            }
            if omit_field and omit_field[0] == n:
                fields.pop(omit_field[1])
            block += "[repos.review_test]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items())
        blocks.append(block)
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m1"\n\n'
        + "\n".join(blocks)
        + f'\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = {names!r}\n'.replace("'", '"')
    )
    native_root(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos=names[0], branch="main", source="test", default_commands=[],
    ))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False,
    ))
    pins = [project_review.ProjectReviewPin(
        key=n, repo=f"local:{srcs[n][0]}", path=f"repos/{n}", base=srcs[n][1], head=srcs[n][2])
        for n in names]
    spec = project_review.make_spec(workspace, pins)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", spec.grip_commit,
        {n: (srcs[n][0], f"review/{n}") for n in names}, prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened", outcome
    return workspace, outcome.review_root, {n: srcs[n][2] for n in names}


def _source_installable(root: Path, name: str) -> tuple[Path, str, str]:
    """A source repo whose review head carries an INSTALLABLE package (pyproject +
    setuptools) so `pip install -e .` works and the package imports under the lane's
    own venv. Returns (source, base_sha, head_sha)."""
    origin = root / f"{name}.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text(f"{name} base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    _git(source, "checkout", "-q", "-b", f"review/{name}")
    pkg = source / f"{name}_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('WHERE = "lane"\n')
    (source / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        f'[project]\nname = "{name}-lane-pkg"\nversion = "0.0.0"\n'
        f'[tool.setuptools]\npackages = ["{name}_pkg"]\n'
    )
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review with installable package")
    _git(source, "push", "-q", "origin", f"review/{name}")
    head = _git(source, "rev-parse", f"review/{name}")
    _git(source, "checkout", "-q", "main")
    return source, base, head


def _open_installable_repo_review(tmp_path: Path, name: str):
    import argparse
    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    src, base, head = _source_installable(tmp_path, name)
    url = _git(src, "remote", "get-url", "origin")
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\nurl = "{url}"\n'
        f'\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["{name}"]\n'
    )
    native_root(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos=name, branch="main", source="test", default_commands=[]))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False))
    pins = [project_review.ProjectReviewPin(
        key=name, repo=f"local:{src}", path=f"repos/{name}", base=base, head=head)]
    spec = project_review.make_spec(workspace, pins)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", spec.grip_commit,
        {name: (src, f"review/{name}")}, prior_cwd=prior_cwd, allow_local=True)
    assert outcome.status == "opened", outcome
    return workspace, outcome.review_root, head


def test_provisioned_verification_runs_under_the_lane_venv(tmp_path: Path) -> None:
    # Lane venv provisioning: with provision_venv, the verification creates the lane's
    # own .venv from the repo's pyproject and runs the command under it, so BOTH the
    # recorded interpreter and module_path resolve UNDER the lane -- not the desk's
    # python whose editable install could import a different tree.
    _, review_root, head = _open_installable_repo_review(tmp_path, "solo")
    lane_dir = review_root / "repos" / "solo"
    # The declared command writes ITS OWN sys.executable into the lane, so we can pin
    # that the COMMAND (not just the probe) ran under the lane venv.
    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=["python", "-c",
                 "import sys, pathlib; pathlib.Path('CMD_PY.txt').write_text(sys.executable); "
                 "import solo_pkg; sys.exit(0 if solo_pkg.WHERE == 'lane' else 1)"],
        interpreter=sys.executable,  # bootstrap only; provisioning overrides to the lane venv
        import_module="solo_pkg",
        provision_venv=True,
    )
    assert record["exit_code"] == 0
    assert record["provisioned"] is True
    assert record["head_tested"] == head
    venv_root = (lane_dir / ".venv").resolve()
    # the interpreter that ran is the LANE's venv python, not the desk's. Do NOT
    # resolve() the interpreter: a venv python is a symlink back to the base python,
    # so resolving would follow it to the desk and hide the whole point.
    assert venv_root in Path(record["interpreter"]).parents
    assert record["interpreter"] != sys.executable
    # the COMMAND itself ran under the lane venv (its recorded sys.executable), not the
    # desk python on an inherited PATH.
    cmd_python = (lane_dir / "CMD_PY.txt").read_text().strip()
    assert venv_root in Path(cmd_python).parents
    assert cmd_python != sys.executable
    # and the imported package resolves under the lane
    assert lane_dir.resolve() in Path(record["module_path"]).resolve().parents


def test_multi_repo_records_declared_repos_in_receipt_order(tmp_path: Path) -> None:
    # Mixed case pins both the SKIP and the ORDER: three repos, the middle one
    # undeclared, so exactly two records land, app before tool, each bound to its own
    # reviewed head and lane.
    names = ["app", "lib", "tool"]
    workspace, review_root, heads = _open_multi_repo_review(tmp_path, names, {"app", "tool"})
    records = open_gr_review.record_review_verifications(workspace, review_root)

    assert [r["key"] for r in records] == ["app", "tool"]  # lib skipped; order preserved
    for name in ("app", "tool"):
        rec = next(r for r in records if r["key"] == name)
        assert rec["exit_code"] == 0
        assert rec["head_tested"] == heads[name]
        lane_dir = review_root / "repos" / name
        assert Path(rec["cwd"]).resolve() == lane_dir.resolve()
        assert Path(rec["module_path"]).resolve() == (lane_dir / f"{name}_pkg" / "__init__.py").resolve()

    receipt = json.loads(open_gr_review.open_gr_receipt_path(review_root).read_text())
    assert [r["key"] for r in receipt["verification"]] == ["app", "tool"]


def test_multi_repo_refuses_when_no_repo_declares_a_test(tmp_path: Path) -> None:
    # A multi-repo verify that verified nothing is a silent pass, not a result.
    workspace, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    with pytest.raises(open_gr_review.OpenGrReviewError, match="nothing to verify"):
        open_gr_review.record_review_verifications(workspace, review_root)


def test_multi_repo_refuses_a_receipt_key_absent_from_the_spec(tmp_path: Path) -> None:
    # A receipt naming a repo the spec does not carry must RAISE naming that key, not
    # silently skip it (the verifier would otherwise verify fewer repos than reviewed).
    workspace, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    receipt_path = open_gr_review.open_gr_receipt_path(review_root)
    receipt = json.loads(receipt_path.read_text())
    receipt["repos"].append({"key": "ghost", "base": "0" * 40, "head": "1" * 40})
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    with pytest.raises(open_gr_review.OpenGrReviewError, match="ghost"):
        open_gr_review.record_review_verifications(workspace, review_root)


def test_multi_repo_refuses_a_review_test_missing_a_field(tmp_path: Path) -> None:
    # A review_test missing a required field must RAISE naming the field, not run a
    # verification with a hole in it.
    workspace, review_root, _ = _open_multi_repo_review(
        tmp_path, ["app"], {"app"}, omit_field=("app", "import_module"))
    with pytest.raises(open_gr_review.OpenGrReviewError, match="import_module"):
        open_gr_review.record_review_verifications(workspace, review_root)


def test_verification_records_a_green_run_inside_the_lane(tmp_path: Path) -> None:
    # The e2e: open a review lane, run the repo's pytest INSIDE it, read the receipt
    # back. The record must bind exit 0, the reviewed head, a cwd inside the lane, the
    # interpreter, and a module_path that resolves to the LANE's package.
    _, review_root, head = _open_single_repo_review(tmp_path, "solo", package=True)

    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="],
        interpreter=sys.executable,
        import_module="solo_pkg",
    )
    assert record["exit_code"] == 0
    assert record["head_tested"] == head
    # provisioned is DERIVED from the interpreter path; a non-provisioned run (desk
    # python, no lane .venv) must record False, not carry a request flag.
    assert record["provisioned"] is False
    lane_dir = review_root / "repos" / "solo"
    # cwd is MEASURED (the probe's os.getcwd()), so compare resolved paths -- on macOS
    # the lane dir under /var/folders resolves through the /private symlink.
    assert Path(record["cwd"]).resolve() == lane_dir.resolve()
    # the probe ran under the interpreter we asked for
    assert Path(record["interpreter"]).resolve() == Path(sys.executable).resolve()
    # and the package it imported is the LANE's copy, not some other checkout's
    assert Path(record["module_path"]).resolve() == (lane_dir / "solo_pkg" / "__init__.py").resolve()

    # the record is persisted in the receipt's verification list
    receipt = json.loads(open_gr_review.open_gr_receipt_path(review_root).read_text())
    assert receipt["verification"][-1] == record


def test_verification_records_the_real_nonzero_exit(tmp_path: Path) -> None:
    # Mutant (b) witness: a command that really exits non-zero must be recorded as
    # non-zero. A "verification always green" implementation dies here.
    _, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=[sys.executable, "-c", "import sys; sys.exit(3)"],
        interpreter=sys.executable,
        import_module="solo_pkg",
    )
    assert record["exit_code"] == 3


def test_module_path_reveals_an_out_of_lane_import(tmp_path: Path, monkeypatch) -> None:
    # Stromus's addition witness: when the reviewed lane does NOT contain the package
    # and a stale copy is on the interpreter's path, the receipt must EXPOSE that the
    # imported code lives outside the lane -- naming the foreign file, not silently
    # passing. Here the lane has no package; a shadow dir on PYTHONPATH does. Because
    # the shadow module is imported by ABSOLUTE name via PYTHONPATH (cwd holds no such
    # package), module_path points OUTSIDE the lane, which is exactly the defect a
    # reviewer needs the receipt to surface.
    _, review_root, _ = _open_single_repo_review(tmp_path, "bare", package=False)
    shadow = tmp_path / "desk-shadow"
    (shadow / "ghost_pkg").mkdir(parents=True)
    (shadow / "ghost_pkg" / "__init__.py").write_text('WHERE = "desk"\n')
    monkeypatch.setenv("PYTHONPATH", str(shadow))

    record = open_gr_review.record_review_verification(
        review_root, "bare",
        command=[sys.executable, "-c", "pass"],
        interpreter=sys.executable,
        import_module="ghost_pkg",
    )
    resolved = Path(record["module_path"]).resolve()
    assert resolved == (shadow / "ghost_pkg" / "__init__.py").resolve()
    # and it is NOT under the review lane -- the receipt names the foreign code
    assert (review_root / "repos" / "bare") not in resolved.parents


def test_verification_measures_the_cwd_where_the_command_ran(tmp_path: Path) -> None:
    # cwd witness: the test command drops a marker file into ITS OWN cwd. The marker
    # must land inside the lane dir, and the recorded cwd (MEASURED via the probe's
    # os.getcwd(), not copied from the request) must resolve to the lane dir. A record
    # that writes str(lane_dir) without measuring, or a lane_dir pointed elsewhere,
    # fails one or both.
    _, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    lane_dir = review_root / "repos" / "solo"
    marker = "MARKER_ran_here.txt"
    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=[sys.executable, "-c", f"open({marker!r}, 'w').write('x')"],
        interpreter=sys.executable,
        import_module="solo_pkg",
    )
    assert (lane_dir / marker).exists(), "the command did not run inside the lane dir"
    assert Path(record["cwd"]).resolve() == lane_dir.resolve()


def test_head_tested_uses_reconstructed_head_when_present(tmp_path: Path) -> None:
    # reconstructed_head witness: for a carried-range pin the lane holds the
    # reconstructed head (a different sha than the pinned head). head_tested must be
    # the reconstructed one. A mutant that ignores reconstructed_head and reads
    # head only survives without this fixture.
    _, review_root, head = _open_single_repo_review(tmp_path, "solo", package=True)
    receipt_path = open_gr_review.open_gr_receipt_path(review_root)
    receipt = json.loads(receipt_path.read_text())
    fake_reconstructed = "0" * 40
    for row in receipt["repos"]:
        if row["key"] == "solo":
            row["reconstructed_head"] = fake_reconstructed
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=[sys.executable, "-c", "pass"],
        interpreter=sys.executable,
        import_module="solo_pkg",
    )
    assert record["head_tested"] == fake_reconstructed
    assert record["head_tested"] != head


def test_two_verifications_are_both_recorded(tmp_path: Path) -> None:
    # append witness: a second verification must NOT replace the first. A mutant that
    # assigns [record] instead of appending survives without a second call.
    _, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    for _ in range(2):
        open_gr_review.record_review_verification(
            review_root, "solo",
            command=[sys.executable, "-c", "pass"],
            interpreter=sys.executable,
            import_module="solo_pkg",
        )
    receipt = json.loads(open_gr_review.open_gr_receipt_path(review_root).read_text())
    assert len(receipt["verification"]) == 2


def test_verification_refuses_when_the_lane_dir_is_missing(tmp_path: Path) -> None:
    # is_dir witness: if the materialized lane dir is gone, refuse rather than run a
    # command in the wrong place. A mutant dropping the is_dir check survives without this.
    _, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    shutil.rmtree(review_root / "repos" / "solo")
    with pytest.raises(open_gr_review.OpenGrReviewError, match="materialized lane dir missing"):
        open_gr_review.record_review_verification(
            review_root, "solo",
            command=[sys.executable, "-c", "pass"],
            interpreter=sys.executable,
            import_module="solo_pkg",
        )


def test_verification_refuses_a_key_the_review_did_not_materialize(tmp_path: Path) -> None:
    _, review_root, _ = _open_single_repo_review(tmp_path, "solo", package=True)
    with pytest.raises(open_gr_review.OpenGrReviewError, match="not in the review receipt"):
        open_gr_review.record_review_verification(
            review_root, "nope",
            command=[sys.executable, "-c", "pass"],
            interpreter=sys.executable,
            import_module="solo_pkg",
        )


def _source_installable_with_extra(root: Path, name: str) -> tuple[Path, str, str]:
    """Like _source_installable, but the review head's pyproject declares a TEST extra
    whose dependency is a SECOND local package (referenced by a file:// URL so it
    installs offline in CI -- no PyPI fetch). This is the shape a real repo uses to put
    its test deps (pytest + what the tests import) behind `.[test]`; here the extra's
    lone dep stands in for that so the proof is hermetic. Returns (source, base, head)."""
    # the dependency package, built once, referenced by absolute file:// URL
    dep = root / f"{name}_extradep"
    dep_pkg = dep / f"{name}_extradep_pkg"
    dep_pkg.mkdir(parents=True)
    (dep_pkg / "__init__.py").write_text("OK = True\n")
    (dep / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        f'[project]\nname = "{name}extradep"\nversion = "0.0.0"\n'
        f'[tool.setuptools]\npackages = ["{name}_extradep_pkg"]\n'
    )
    dep_uri = dep.resolve().as_uri()

    origin = root / f"{name}.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text(f"{name} base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    _git(source, "checkout", "-q", "-b", f"review/{name}")
    pkg = source / f"{name}_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('WHERE = "lane"\n')
    (source / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        f'[project]\nname = "{name}-lane-pkg"\nversion = "0.0.0"\n'
        f'[project.optional-dependencies]\ntest = ["{name}extradep @ {dep_uri}"]\n'
        f'[tool.setuptools]\npackages = ["{name}_pkg"]\n'
    )
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review with an installable package + test extra")
    _git(source, "push", "-q", "origin", f"review/{name}")
    head = _git(source, "rev-parse", f"review/{name}")
    _git(source, "checkout", "-q", "main")
    return source, base, head


def _open_extra_repo_review(tmp_path: Path, name: str):
    import argparse
    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    src, base, head = _source_installable_with_extra(tmp_path, name)
    url = _git(src, "remote", "get-url", "origin")
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\nurl = "{url}"\n'
        f'\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["{name}"]\n'
    )
    native_root(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos=name, branch="main", source="test", default_commands=[]))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False))
    pins = [project_review.ProjectReviewPin(
        key=name, repo=f"local:{src}", path=f"repos/{name}", base=base, head=head)]
    spec = project_review.make_spec(workspace, pins)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", spec.grip_commit,
        {name: (src, f"review/{name}")}, prior_cwd=prior_cwd, allow_local=True)
    assert outcome.status == "opened", outcome
    return workspace, outcome.review_root, head


# the command every extras test runs: it needs the EXTRA's dependency to import, so it
# passes only when the declared extra was installed into the lane venv.
_EXTRA_CMD = ["python", "-c", "import solo_extradep_pkg; import solo_pkg; import sys; sys.exit(0)"]


def test_declared_extras_install_the_test_deps_into_the_lane_venv(tmp_path: Path) -> None:
    # The deferred lane-venv item: a fresh venv has pip but not the repo's test deps, so
    # a command that imports one records non-zero. Declaring the extra in
    # [repos.review_test] installs it into the lane venv (pip install -e ".[test]"), so
    # the command now records exit 0 -- the dep is present under the lane's own python.
    _, review_root, head = _open_extra_repo_review(tmp_path, "solo")
    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=_EXTRA_CMD,
        interpreter=sys.executable,  # bootstrap; provisioning overrides to the lane venv
        import_module="solo_pkg",
        provision_venv=True,
        extras=["test"],
    )
    assert record["exit_code"] == 0            # the extra's dep imported -> command passed
    assert record["provisioned"] is True
    assert record["head_tested"] == head


def test_without_the_extra_the_dep_is_absent_and_the_command_records_nonzero(tmp_path: Path) -> None:
    # The guarantee, as its own regression: the SAME command in the SAME provisioned lane
    # WITHOUT the extra records non-zero -- the dep is not installed, so it cannot pass.
    # This is what makes the extras field load-bearing rather than decorative: drop it
    # and the run fails honestly instead of silently passing on a missing dependency.
    _, review_root, _head = _open_extra_repo_review(tmp_path, "solo")
    record = open_gr_review.record_review_verification(
        review_root, "solo",
        command=_EXTRA_CMD,
        interpreter=sys.executable,
        import_module="solo_pkg",
        provision_venv=True,
        extras=None,  # no extra -> the test dep never enters the venv
    )
    assert record["exit_code"] != 0            # ImportError on the missing dep
    assert record["provisioned"] is True       # the venv WAS provisioned; only the dep is absent


def _open_extra_repo_review_with_spec_test(tmp_path: Path, name: str, *, extras_in_spec: bool):
    """Like _open_extra_repo_review but ALSO writes a full [repos.review_test] block
    into the workspace spec (command/interpreter/import_module/provision_venv), so the
    review runs through record_review_verifications -- the multi-repo entry that READS
    the toml -- exercising the extras field-read path. `extras_in_spec` toggles the
    `extras = ["test"]` line, which is the only difference between the pass and the
    field-witness mutant."""
    import argparse
    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    src, base, head = _source_installable_with_extra(tmp_path, name)
    url = _git(src, "remote", "get-url", "origin")
    review_test = (
        "[repos.review_test]\n"
        'command = ["python", "-c", "import ' + name + '_extradep_pkg; import '
        + name + '_pkg; import sys; sys.exit(0)"]\n'
        'interpreter = "python3"\n'
        f'import_module = "{name}_pkg"\n'
        "provision_venv = true\n"
        + ('extras = ["test"]\n' if extras_in_spec else "")
    )
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\nurl = "{url}"\n'
        f'{review_test}'
        f'\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["{name}"]\n'
    )
    native_root(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos=name, branch="main", source="test", default_commands=[]))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False))
    pins = [project_review.ProjectReviewPin(
        key=name, repo=f"local:{src}", path=f"repos/{name}", base=base, head=head)]
    spec = project_review.make_spec(workspace, pins)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", spec.grip_commit,
        {name: (src, f"review/{name}")}, prior_cwd=prior_cwd, allow_local=True)
    assert outcome.status == "opened", outcome
    return workspace, outcome.review_root, head


def test_extras_field_in_the_spec_installs_the_dep_through_the_multi_repo_verify(tmp_path: Path) -> None:
    # Witness for the [repos.review_test] extras FIELD itself (not the kwarg): the review
    # runs through record_review_verifications, which reads `extras` from the toml and
    # passes it on. With `extras = ["test"]` in the spec the dep is installed and the
    # command records exit 0. Setting the field-read plumbing to extras=None (or dropping
    # the line) reds this test -- the direct-call tests above cannot, since they never
    # touch the toml.
    workspace, review_root, head = _open_extra_repo_review_with_spec_test(
        tmp_path, "solo", extras_in_spec=True)
    records = open_gr_review.record_review_verifications(workspace, review_root)
    assert len(records) == 1
    assert records[0]["key"] == "solo"
    assert records[0]["exit_code"] == 0      # the extra from the toml reached provision
    assert records[0]["provisioned"] is True
    assert records[0]["head_tested"] == head
