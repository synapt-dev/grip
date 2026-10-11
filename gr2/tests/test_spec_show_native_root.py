"""`gr2 spec show` on a native root shows the document the root carries, and a root with none is a named refusal.

A native root has `grip.toml` and no `.grip/workspace_spec.toml`. Text mode read the spec file directly, so the CLI
printed a FileNotFoundError traceback; `--json` went through the loader and refused with advice (`workspace init`)
that is wrong for a native root. The rows run the real CLI in a subprocess (`-P`: no cwd on sys.path), so stdout and
stderr are separate on every click version.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GR2_ROOT = Path(__file__).parents[1]

_CLI = """
import os, runpy, sys
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[1:]
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""

GRIP_TOML = 'schema_version = 1\nworkspace_name = "nativews"\n\n[[members]]\nname = "m"\npath = "m"\n'
SPEC_TOML = 'workspace_name = "legacyws"\n\n[[repos]]\nname = "app"\npath = "app"\nurl = "https://example.com/o/app.git"\n'


def _gr2(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT)}
    return subprocess.run([sys.executable, "-P", "-c", _CLI, *args], cwd=cwd, text=True, capture_output=True, env=env)


def _root(tmp_path: Path, *, grip: bool, spec: bool) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    if grip:
        subprocess.run(["git", "init", "-q", str(root)], check=True)   # a native root is grip.toml beside a root .git
        (root / "grip.toml").write_text(GRIP_TOML, encoding="utf-8")
    if spec:
        (root / ".grip").mkdir()
        (root / ".grip" / "workspace_spec.toml").write_text(SPEC_TOML, encoding="utf-8")
    (root / "lanes" / "x").mkdir(parents=True)
    return root


def _clean(result: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in result.stderr and "FileNotFoundError" not in result.stderr, result.stderr


# "lanes/x" is a directory nested under the root, standing in for a lane the issue measured from inside; it is not a
# real review lane.
@pytest.mark.parametrize("where", [".", "lanes/x"])
def test_a_native_root_shows_its_grip_toml(tmp_path, where):
    root = _root(tmp_path, grip=True, spec=False)
    result = _gr2(root / where, "spec", "show")
    _clean(result)
    assert result.returncode == 0, result.stderr
    assert result.stdout == GRIP_TOML + "\n" or result.stdout == GRIP_TOML, result.stdout


@pytest.mark.parametrize("where", [".", "lanes/x"])
def test_a_native_root_shows_its_grip_toml_as_json(tmp_path, where):
    root = _root(tmp_path, grip=True, spec=False)
    result = _gr2(root / where, "spec", "show", "--json")
    _clean(result)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["workspace_name"] == "nativews" and document["members"][0]["name"] == "m"


def test_a_legacy_root_still_shows_its_workspace_spec(tmp_path):
    root = _root(tmp_path, grip=False, spec=True)
    text = _gr2(root, "spec", "show")
    assert text.returncode == 0 and "legacyws" in text.stdout and "nativews" not in text.stdout
    as_json = _gr2(root, "spec", "show", "--json")
    assert json.loads(as_json.stdout)["workspace_name"] == "legacyws"


def test_a_root_carrying_both_documents_still_shows_the_workspace_spec(tmp_path):
    root = _root(tmp_path, grip=True, spec=True)
    text = _gr2(root, "spec", "show")
    assert text.returncode == 0 and "legacyws" in text.stdout and "nativews" not in text.stdout
    as_json = _gr2(root, "spec", "show", "--json")
    assert json.loads(as_json.stdout)["workspace_name"] == "legacyws"


@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_a_root_with_neither_document_is_a_named_refusal(tmp_path, flags):
    root = _root(tmp_path, grip=False, spec=False)
    result = _gr2(root, "spec", "show", *flags)
    _clean(result)
    assert result.returncode != 0
    assert "workspace spec not found" in result.stderr + result.stdout
    assert "grip.toml" in result.stderr + result.stdout   # names BOTH documents it looked for


def test_a_grip_toml_that_does_not_parse_is_shown_as_text_and_refused_as_json(tmp_path):
    root = _root(tmp_path, grip=True, spec=False)
    broken = "schema_version = 1\nname = [unclosed\n"
    (root / "grip.toml").write_text(broken, encoding="utf-8")
    text = _gr2(root, "spec", "show")
    _clean(text)
    assert text.returncode == 0 and text.stdout.startswith(broken.rstrip("\n"))
    as_json = _gr2(root, "spec", "show", "--json")
    assert "Traceback" not in as_json.stderr and "TOMLDecodeError" not in as_json.stderr, as_json.stderr
    assert as_json.returncode != 0 and "cannot show" in as_json.stderr and "grip.toml" in as_json.stderr


@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_a_grip_toml_that_is_not_utf8_is_a_named_refusal(tmp_path, flags):
    root = _root(tmp_path, grip=True, spec=False)
    (root / "grip.toml").write_bytes(b"\xff\xfe\x00bad")
    result = _gr2(root, "spec", "show", *flags)
    assert "Traceback" not in result.stderr and "UnicodeDecodeError" not in result.stderr, result.stderr
    assert result.returncode != 0 and "cannot show" in result.stderr and "grip.toml" in result.stderr


@pytest.mark.parametrize("since", ["2026-10-11", "2026-10-11T08:30:00-05:00", "2026-10-11T08:30:00", "08:30:00"])
def test_a_grip_toml_with_a_toml_date_or_time_shows_as_json(tmp_path, since):
    """TOML has date, datetime and time values that json.dumps cannot encode; a hand-written [[sets]] table carries one."""
    root = _root(tmp_path, grip=True, spec=False)
    (root / "grip.toml").write_text(GRIP_TOML + f"\n[[sets]]\nname = \"s\"\nsince = {since}\n", encoding="utf-8")
    result = _gr2(root, "spec", "show", "--json")
    _clean(result)
    assert "TypeError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["sets"][0]["since"].startswith(since[:8]) and document["workspace_name"] == "nativews"


def test_a_workspace_spec_with_a_toml_date_shows_as_json(tmp_path):
    root = _root(tmp_path, grip=False, spec=True)
    spec = root / ".grip" / "workspace_spec.toml"
    spec.write_text(SPEC_TOML + "\n[settings]\nsince = 2026-10-11T08:30:00-05:00\n", encoding="utf-8")
    result = _gr2(root, "spec", "show", "--json")
    _clean(result)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["settings"]["since"].startswith("2026-10-11")
