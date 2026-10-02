"""Plan 2's real install row (step 4): the planned install of a Python cycle group actually installs.

The plan says ``one-invocation``: the whole group goes to ONE ``pip install -e A -e B``. That is a claim about pip,
so this row runs it, into a scratch venv, offline (``--no-index --no-build-isolation``), and imports both packages
from OUTSIDE the lane to show they resolve to the lane source.

The members build with a tiny in-tree PEP 660 backend that uses only the standard library, so the row does not depend
on setuptools being importable in the interpreter running the tests (a CI venv often has none, and a row that skips
there proves nothing). The negative control is the measurement that makes grouping necessary at all: one member of the
cycle alone cannot be installed (it needs the other, and there is no index), so a plan that installed per member would
fail at the first install. The row skips loudly only when a venv cannot be made on this host.
"""
from __future__ import annotations

import subprocess
import venv
from pathlib import Path

import pytest
from gr2.python_cli import ecosystems_python as ep
from gr2.python_cli.lane_graph import plan_lane

PY = {"python": ep.call}

BACKEND = '''\
"""A stdlib-only PEP 660 backend: an editable install is one .pth pointing at the member directory."""
import base64, hashlib, os, tomllib, zipfile


def get_requires_for_build_editable(config_settings=None):
    return []


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    root = os.getcwd()
    with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
        project = tomllib.load(fh)["project"]
    norm = project["name"].replace("-", "_")
    version = project["version"]
    dist_info = f"{norm}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\\nName: {project['name']}\\nVersion: {version}\\n"
    meta += "".join(f"Requires-Dist: {dep}\\n" for dep in project.get("dependencies", []))
    files = {
        f"{dist_info}/METADATA": meta,
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\\nGenerator: lane-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
        f"{norm}.pth": root + "\\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\\n".join(record) + "\\n"
    name = f"{norm}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)
    return name
'''


def member(lane: Path, key: str, mod: str, needs: str) -> None:
    d = lane / key
    d.mkdir(parents=True)
    (d / f"{mod}.py").write_text(f"NAME = {key!r}\n")
    (d / "lane_test_backend.py").write_text(BACKEND)
    (d / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "lane_test_backend"\nbackend-path = ["."]\n'
        f'[project]\nname = "{key}"\nversion = "0"\ndependencies = ["{needs}"]\n'
    )


@pytest.fixture
def scratch_venv(tmp_path: Path) -> Path:
    env = tmp_path / "venv"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(env)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"a scratch venv could not be created here ({exc})")
    return env / "bin" / "python"


def pip_install(py: Path, dirs: list[str]) -> subprocess.CompletedProcess:
    cmd = [str(py), "-m", "pip", "install", "-q", "--no-index", "--no-build-isolation", "--disable-pip-version-check"]
    for d in dirs:
        cmd += ["-e", d]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_plan_2_the_planned_one_invocation_install_of_a_python_cycle_installs_and_imports_from_the_lane(
    tmp_path: Path, scratch_venv: Path
) -> None:
    lane = tmp_path / "lane"
    member(lane, "atl-pa", "atl_pa", "atl-pb")
    member(lane, "atl-pb", "atl_pb", "atl-pa")
    plan = plan_lane(lane, ["atl-pa", "atl-pb"], PY)
    (group,) = plan.cyclic_groups
    assert plan.methods[group.index]["method"] == "one-invocation"
    dirs = {u.id: u.dir for u in plan.units}
    planned = [dirs[u] for u in group.units]

    # the negative control: ONE member of the cycle alone cannot be installed, which is why the group is the unit
    alone = pip_install(scratch_venv, [planned[0]])
    assert alone.returncode != 0 and "atl-pb" in (alone.stderr + alone.stdout), (alone.stdout, alone.stderr)

    # the plan: the whole group in one invocation
    done = pip_install(scratch_venv, planned)
    assert done.returncode == 0, (done.stdout, done.stderr)
    out = subprocess.run(
        [str(scratch_venv), "-c", "import atl_pa, atl_pb; print(atl_pa.__file__); print(atl_pb.__file__)"],
        capture_output=True, text=True, cwd=str(tmp_path / "venv"),  # not the lane: the import must come from the install
    )
    assert out.returncode == 0, out.stderr
    where = [Path(line).resolve() for line in out.stdout.split()]
    assert all(str(lane.resolve()) in str(w) for w in where), where


def test_the_note_the_plugin_prints_names_the_one_invocation_with_every_dir(tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    member(lane, "atl-pa", "atl_pa", "atl-pb")
    member(lane, "atl-pb", "atl_pb", "atl-pa")
    plan = plan_lane(lane, ["atl-pa", "atl-pb"], PY)
    note = plan.methods[1]["note"]
    assert note.count("-e ") == 2 and str(lane / "atl-pa") in note and str(lane / "atl-pb") in note
