from gr2.python_cli.app import spec_app
from typer.testing import CliRunner
import typer

app = typer.Typer(); app.add_typer(spec_app, name="spec")

def _write(root, pin):
    (root / "grip.toml").write_text(f'''schema_version = 1
workspace_name = "demo"
[[members]]
name = "config"
path = "config"
upstream = "origin/main"
ref = "main"
pin = "{pin}"
mode = "full"
[members.remotes]
origin = "https://example.invalid/config.git"
''')

def test_spec_validate_grip_toml_v1_accepts_and_refuses_bad_pin(tmp_path):
    _write(tmp_path, "a" * 40)
    runner = CliRunner()
    ok = runner.invoke(app, ["spec", "validate", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    _write(tmp_path, "not-a-pin")
    bad = runner.invoke(app, ["spec", "validate", str(tmp_path)])
    assert bad.exit_code == 1
    assert "grip_toml_schema" in bad.output and "pin" in bad.output
