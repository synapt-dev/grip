"""A fresh install reaches gr2's agent skill in one documented step: `gr2 help agent`.

The skill is generated from gr2's own code (`scripts/dump_skill.py`, gated by `test_skill_dump.py`) into
`api/skill.md`, and the package carries a byte copy, `gr2/AGENT_GUIDE.md` (`test_agent_guide.py` checks the copy, the
package-data line and the printed bytes from a checkout). This is the other half: the BUILT wheel carries the copy, the
INSTALLED `gr2 help agent` prints exactly `api/skill.md`, and both READMEs name that step, so a reader with only the
install does not have to find the file in a checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

GR2 = Path(__file__).resolve().parents[1]
SKILL = GR2 / "api" / "skill.md"
STEP = "gr2 help agent > .claude/skills/gr2-review/SKILL.md"
pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build and install the wheel"),
    pytest.mark.skipif(os.name == "nt", reason="runs the installed console script by POSIX path"),
]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    work = tmp_path_factory.mktemp("skill-wheel")
    # Built from a copy without build leftovers: an editable install's ignored egg-info lists every file it saw,
    # and setuptools would ship from that list even with a file gone from package-data.
    src = work / "src"
    shutil.copytree(GR2, src, ignore=shutil.ignore_patterns("*.egg-info", "build", "dist", "__pycache__", ".venv"))
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(work / "dist"), str(src)], check=True)
    return next((work / "dist").glob("gitgrip-*.whl"))


@pytest.fixture(scope="module")
def installed(wheel, tmp_path_factory):
    venv = tmp_path_factory.mktemp("skill-venv") / "venv"
    subprocess.run(["uv", "venv", "-q", "-p", sys.executable, str(venv)], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheel)], check=True)
    return venv / "bin"


def test_the_wheel_carries_the_agent_guide(wheel):
    with zipfile.ZipFile(wheel) as zf:
        assert "gr2/AGENT_GUIDE.md" in zf.namelist()


def test_the_installed_gr2_help_agent_prints_api_skill_md(installed, tmp_path):
    # Only HOME and PATH, so the wheel's own gr2 answers, not a checkout on an inherited PYTHONPATH.
    env = {"HOME": str(tmp_path), "PATH": os.pathsep.join([str(installed), "/usr/bin", "/bin"])}
    result = subprocess.run([str(installed / "gr2"), "help", "agent"], cwd=tmp_path, env=env, capture_output=True)
    assert result.returncode == 0, result
    assert result.stdout == SKILL.read_bytes()
    assert result.stdout.startswith(b"---\nname: gr2-review\n")


@pytest.mark.parametrize("readme", [GR2 / "README.md", GR2.parent / "README.md"], ids=["gr2", "root"])
def test_the_readme_names_the_step(readme):
    assert STEP in readme.read_text(encoding="utf-8")
