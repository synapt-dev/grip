from __future__ import annotations

import json
import tomllib
from pathlib import Path

from gr2.python_cli import app as app_module
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner

runner = make_cli_runner()


def _write_topology(
    workspace_root: Path,
    *,
    workspace_name: str | None = "declarative-team",
) -> None:
    declared_workspace_name = (
        f'workspace_name = "{workspace_name}"\n' if workspace_name is not None else ""
    )
    (workspace_root / "workspace.toml").write_text(
        f"""\
schema_version = 2
{declared_workspace_name}

[[repos]]
key = "product"
url = "https://example.invalid/product.git"
path = "repos/product"
default_ref = "main"

[[repos]]
key = "config"
url = "https://example.invalid/config.git"
path = "config"
default_ref = "dev"
"""
    )


def test_workspace_init_from_topology_writes_declared_repos_in_an_empty_directory(
    tmp_path: Path,
) -> None:
    """The zero-to-team wire must not fall back to scanning local Git repos.

    Mutation: replace the declared-topology reader with `_scan_existing_repos`.
    This root has no Git repositories, so the old adoption path refuses and this
    witness goes red before a WorkspaceSpec can be written.
    """
    workspace_root = tmp_path / "empty-team"
    workspace_root.mkdir()
    _write_topology(workspace_root)

    result = runner.invoke(
        app,
        [
            "workspace",
            "init-from-topology",
            str(workspace_root),
            "--default-unit",
            "team",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["workspace_root"] == str(workspace_root)
    assert payload["spec_path"] == str(workspace_root / ".grip" / "workspace_spec.toml")
    assert payload["repo_count"] == 2
    assert [repo["name"] for repo in payload["repos"]] == ["product", "config"]
    assert [repo["path"] for repo in payload["repos"]] == ["repos/product", "config"]
    assert [repo["url"] for repo in payload["repos"]] == [
        "https://example.invalid/product.git",
        "https://example.invalid/config.git",
    ]
    assert payload["default_unit"] == "team"
    assert payload["source"] == "workspace.toml"
    assert (workspace_root / ".grip" / ".git").is_dir()
    assert not [path for path in workspace_root.glob("*/.git") if path.parent.name != ".grip"]
    assert not (workspace_root / "repos" / "product").exists()
    assert not (workspace_root / "config").exists()

    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as spec_file:
        spec = tomllib.load(spec_file)
    assert spec["workspace_name"] == "declarative-team"
    assert spec["repos"] == payload["repos"]
    assert spec["units"] == [
        {
            "name": "team",
            "path": "agents/team/home",
            "repos": ["product", "config"],
        }
    ]

    text_result = runner.invoke(
        app,
        ["workspace", "init-from-topology", str(workspace_root), "--default-unit", "team"],
    )
    assert text_result.exit_code == 0, text_result.output
    assert "source = workspace.toml" in text_result.output


def test_workspace_init_from_topology_falls_back_to_root_name_when_name_is_absent(
    tmp_path: Path,
) -> None:
    """The pre-existing scan-style fallback remains explicit when topology omits a name."""
    workspace_root = tmp_path / "fallback-team"
    workspace_root.mkdir()
    _write_topology(workspace_root, workspace_name=None)

    result = runner.invoke(app, ["workspace", "init-from-topology", str(workspace_root)])

    assert result.exit_code == 0, result.output
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as spec_file:
        spec = tomllib.load(spec_file)
    assert spec["workspace_name"] == "fallback-team"


def test_workspace_init_from_topology_refuses_an_incomplete_declared_repo_before_writing(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "incomplete-team"
    workspace_root.mkdir()
    (workspace_root / "workspace.toml").write_text(
        """\
[[repos]]
key = "product"
path = "repos/product"
default_ref = "main"
"""
    )

    result = runner.invoke(app, ["workspace", "init-from-topology", str(workspace_root)])

    assert result.exit_code != 0
    assert "workspace.toml repos[0] ('product') is missing 'url'" in result.output
    assert not (workspace_root / ".grip" / "workspace_spec.toml").exists()


def test_workspace_init_from_topology_refuses_an_encoding_error_before_creating_grip_directory(
    tmp_path: Path,
) -> None:
    """A writer encoding refusal must leave neither the spec nor its directory.

    TOML itself rejects a surrogate escape in ``workspace.toml`` before the
    command reaches the writer, so this uses the CLI-owned default-unit value,
    one of the writer's seven string positions, to exercise the serializer.

    Mutation: move ``spec_path.parent.mkdir`` above ``lines`` construction.
    The command still refuses, but this witness reaches its directory assertion
    and fails while the three pre-existing witnesses remain green.
    """
    workspace_root = tmp_path / "encoding-refusal-team"
    workspace_root.mkdir()
    _write_topology(workspace_root)

    result = runner.invoke(
        app,
        [
            "workspace",
            "init-from-topology",
            str(workspace_root),
            "--default-unit",
            "team-\ud800",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "surrogate" in str(result.exception)
    assert not (workspace_root / ".grip" / "workspace_spec.toml").exists()
    assert not (workspace_root / ".grip").exists()


def test_workspace_init_from_topology_serializes_every_writer_value(
    tmp_path: Path,
) -> None:
    """Hostile strings must round-trip through every WorkspaceSpec value slot.

    Mutation: replace the writer serializer with raw interpolation. The command
    still exits 0, but this direct parse of its bytes fails. The witness thus
    catches the WRONG-BUT-GREEN failure mode rather than only a refusal path.
    """
    workspace_root = tmp_path / 'unsafe"team'
    workspace_root.mkdir()
    (workspace_root / "workspace.toml").write_text(
        r'''
workspace_name = 'declared"\\workspace'

[[repos]]
key = 'product"\\name'
url = 'https://example.invalid/a"\\b.git'
path = 'repos/product"\\path'
'''
    )

    default_unit = 'team"\\unit'
    result = runner.invoke(
        app,
        [
            "workspace",
            "init-from-topology",
            str(workspace_root),
            "--default-unit",
            default_unit,
        ],
    )

    assert result.exit_code == 0, result.output
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as spec_file:
        spec = tomllib.load(spec_file)
    assert spec["workspace_name"] == r'declared"\\workspace'
    assert spec["repos"] == [
        {
            "name": r'product"\\name',
            "path": r'repos/product"\\path',
            "url": r'https://example.invalid/a"\\b.git',
        }
    ]
    assert spec["units"] == [
        {
            "name": default_unit,
            "path": f"agents/{default_unit}/home",
            "repos": [r'product"\\name'],
        }
    ]


def test_json_output_survives_a_concurrent_stderr_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """Stream-channel contract, pinned by fruit rather than by the ambient click
    version: if this command's underlying reader ever grows a
    stderr warning (e.g. an ambiguous or deprecated ``workspace.toml`` field),
    that warning must land on stderr, not get prepended to the ``--json``
    payload on stdout. Under click<8.2 with the old bare
    ``CliRunner()``, ``mix_stderr`` defaulted True and folded a concurrent
    stderr write into ``result.output`` ahead of the JSON, breaking
    ``json.loads`` exactly like the original merge-parent-warning incident.

    Forces the shape with a monkeypatch on the real reader
    (``_declared_workspace_topology``) rather than adding a real warning to
    production code: the reader's return value is unchanged, so this is a
    pure stream-routing witness, not a behavior change."""
    workspace_root = tmp_path / "warning-team"
    workspace_root.mkdir()
    _write_topology(workspace_root)

    real_reader = app_module._declared_workspace_topology

    def _reader_that_also_warns(root: Path):
        app_module.typer.echo("warning: simulated concurrent stderr write", err=True)
        return real_reader(root)

    monkeypatch.setattr(app_module, "_declared_workspace_topology", _reader_that_also_warns)

    result = runner.invoke(
        app,
        [
            "workspace",
            "init-from-topology",
            str(workspace_root),
            "--default-unit",
            "team",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["repo_count"] == 2
    assert "simulated concurrent stderr write" in result.stderr
    assert "simulated concurrent stderr write" not in result.stdout
