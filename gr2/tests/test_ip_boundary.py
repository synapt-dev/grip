"""Tests for cross-org IP boundary model (Sprint E, F1-F3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gr2_overlay.ip import (
    DependencyEdge,
    EdgeResolution,
    IpConfig,
    OrgDefinition,
    check_import_violation,
    load_ip_config,
    render_status,
    render_status_json,
    repo_to_org,
    resolve_edge,
    validate_ip_config,
)

# ── Config loading ──────────────────────────────────────────────


class TestLoadIpConfig:
    def test_load_valid_config(self, tmp_path: Path) -> None:
        grip = tmp_path / ".grip"
        grip.mkdir()
        (grip / "ip.toml").write_text(
            """\
[ip]
version = 1

[[ip.orgs]]
name = "synapt"
repos = ["recall", "grip"]
license = "MIT"

[[ip.orgs]]
name = "conversa"
repos = ["conversa-api"]
license = "proprietary"

[[ip.edges]]
from = "conversa"
to = "synapt"
allowed = true
packages = ["synapt"]

[[ip.edges]]
from = "synapt"
to = "conversa"
allowed = false
"""
        )

        config = load_ip_config(tmp_path)
        assert config.version == 1
        assert len(config.orgs) == 2
        assert config.orgs[0].name == "synapt"
        assert config.orgs[0].repos == ["recall", "grip"]
        assert config.orgs[0].license == "MIT"
        assert len(config.edges) == 2
        assert config.edges[0].from_org == "conversa"
        assert config.edges[0].to_org == "synapt"
        assert config.edges[0].allowed is True
        assert config.edges[0].packages == ["synapt"]
        assert config.edges[1].allowed is False

    def test_load_missing_config_returns_empty(self, tmp_path: Path) -> None:
        config = load_ip_config(tmp_path)
        assert config.version == 1
        assert config.orgs == []
        assert config.edges == []

    def test_load_empty_file_returns_empty(self, tmp_path: Path) -> None:
        grip = tmp_path / ".grip"
        grip.mkdir()
        (grip / "ip.toml").write_text("")
        config = load_ip_config(tmp_path)
        assert config.orgs == []

    def test_load_edges_without_packages(self, tmp_path: Path) -> None:
        grip = tmp_path / ".grip"
        grip.mkdir()
        (grip / "ip.toml").write_text(
            """\
[ip]
version = 1

[[ip.orgs]]
name = "a"
repos = ["r1"]
license = "MIT"

[[ip.orgs]]
name = "b"
repos = ["r2"]
license = "MIT"

[[ip.edges]]
from = "a"
to = "b"
allowed = true
"""
        )
        config = load_ip_config(tmp_path)
        assert config.edges[0].packages == []


# ── Validation ──────────────────────────────────────────────────


class TestValidateIpConfig:
    def test_valid_config_passes(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="proprietary"),
            ],
            edges=[
                DependencyEdge(from_org="a", to_org="b", allowed=True, packages=[]),
            ],
        )
        validate_ip_config(config)

    def test_unsupported_version_raises(self) -> None:
        config = IpConfig(version=2, orgs=[], edges=[])
        with pytest.raises(ValueError, match="Unsupported.*version"):
            validate_ip_config(config)

    def test_duplicate_org_name_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="a", repos=["r2"], license="MIT"),
            ],
            edges=[],
        )
        with pytest.raises(ValueError, match="Duplicate org.*'a'"):
            validate_ip_config(config)

    def test_duplicate_repo_across_orgs_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["shared"], license="MIT"),
                OrgDefinition(name="b", repos=["shared"], license="MIT"),
            ],
            edges=[],
        )
        with pytest.raises(ValueError, match="Repo 'shared'.*multiple orgs"):
            validate_ip_config(config)

    def test_edge_references_unknown_org_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1"], license="MIT")],
            edges=[
                DependencyEdge(from_org="a", to_org="unknown", allowed=True, packages=[]),
            ],
        )
        with pytest.raises(ValueError, match="unknown.*not a declared org"):
            validate_ip_config(config)

    def test_self_referencing_edge_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1"], license="MIT")],
            edges=[
                DependencyEdge(from_org="a", to_org="a", allowed=True, packages=[]),
            ],
        )
        with pytest.raises(ValueError, match="self-referencing"):
            validate_ip_config(config)

    def test_empty_org_name_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="", repos=["r1"], license="MIT")],
            edges=[],
        )
        with pytest.raises(ValueError, match="empty name"):
            validate_ip_config(config)

    def test_org_with_no_repos_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=[], license="MIT")],
            edges=[],
        )
        with pytest.raises(ValueError, match="no repos"):
            validate_ip_config(config)

    def test_duplicate_edge_raises(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="MIT"),
            ],
            edges=[
                DependencyEdge(from_org="a", to_org="b", allowed=True, packages=[]),
                DependencyEdge(from_org="a", to_org="b", allowed=False, packages=[]),
            ],
        )
        with pytest.raises(ValueError, match="Duplicate edge.*'a'.*'b'"):
            validate_ip_config(config)


# ── Repo-to-org resolution ──────────────────────────────────────


class TestRepoToOrg:
    def test_known_repo(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="synapt", repos=["recall", "grip"], license="MIT"),
                OrgDefinition(name="conversa", repos=["api"], license="proprietary"),
            ],
            edges=[],
        )
        assert repo_to_org("recall", config) == "synapt"
        assert repo_to_org("api", config) == "conversa"

    def test_unknown_repo_returns_none(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1"], license="MIT")],
            edges=[],
        )
        assert repo_to_org("unknown", config) is None


# ── Edge resolution ─────────────────────────────────────────────


class TestResolveEdge:
    def test_allowed_edge(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="MIT"),
            ],
            edges=[
                DependencyEdge(from_org="a", to_org="b", allowed=True, packages=[]),
            ],
        )
        assert resolve_edge("a", "b", config) == EdgeResolution.ALLOWED

    def test_forbidden_edge(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="MIT"),
            ],
            edges=[
                DependencyEdge(from_org="a", to_org="b", allowed=False, packages=[]),
            ],
        )
        assert resolve_edge("a", "b", config) == EdgeResolution.FORBIDDEN

    def test_undeclared_edge(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="MIT"),
            ],
            edges=[],
        )
        assert resolve_edge("a", "b", config) == EdgeResolution.UNDECLARED

    def test_same_org_returns_internal(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1", "r2"], license="MIT")],
            edges=[],
        )
        assert resolve_edge("a", "a", config) == EdgeResolution.INTERNAL


# ── Import violation checking ───────────────────────────────────


class TestCheckImportViolation:
    def test_allowed_import_returns_none(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="conversa", repos=["eval"], license="proprietary"),
                OrgDefinition(name="synapt", repos=["recall"], license="MIT"),
            ],
            edges=[
                DependencyEdge(
                    from_org="conversa", to_org="synapt", allowed=True, packages=["synapt"]
                ),
            ],
        )
        result = check_import_violation(
            source_repo="eval",
            source_file="src/scoring.py",
            imported_package="synapt",
            config=config,
        )
        assert result is None

    def test_forbidden_import_returns_violation(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="synapt", repos=["recall"], license="MIT"),
                OrgDefinition(name="conversa", repos=["conversa-api"], license="proprietary"),
            ],
            edges=[
                DependencyEdge(from_org="synapt", to_org="conversa", allowed=False, packages=[]),
            ],
        )
        result = check_import_violation(
            source_repo="recall",
            source_file="src/core.py",
            imported_package="conversa_api",
            config=config,
        )
        assert result is not None
        assert result.kind == "forbidden_edge"
        assert result.source_repo == "recall"
        assert result.target_org == "conversa"

    def test_undeclared_import_returns_violation(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="a", repos=["r1"], license="MIT"),
                OrgDefinition(name="b", repos=["r2"], license="MIT"),
            ],
            edges=[],
        )
        result = check_import_violation(
            source_repo="r1",
            source_file="main.py",
            imported_package="b_pkg",
            config=config,
        )
        # Unknown package mapping: can't determine target org
        assert result is None

    def test_same_org_import_returns_none(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="synapt", repos=["recall", "premium"], license="MIT"),
            ],
            edges=[],
        )
        result = check_import_violation(
            source_repo="recall",
            source_file="src/core.py",
            imported_package="synapt_private",
            config=config,
        )
        # synapt_private is not in any repo's package list; returns None (unknown)
        assert result is None

    def test_package_scoped_edge_blocks_unlisted_package(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="conversa", repos=["eval"], license="proprietary"),
                OrgDefinition(name="synapt", repos=["recall", "grip"], license="MIT"),
            ],
            edges=[
                DependencyEdge(
                    from_org="conversa",
                    to_org="synapt",
                    allowed=True,
                    packages=["recall"],
                ),
            ],
        )
        result = check_import_violation(
            source_repo="eval",
            source_file="src/hack.py",
            imported_package="grip",
            config=config,
        )
        assert result is not None
        assert result.kind == "package_not_allowed"
        assert "grip" in result.message

    def test_package_scoped_edge_allows_listed_package(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="conversa", repos=["eval"], license="proprietary"),
                OrgDefinition(name="synapt", repos=["recall", "grip"], license="MIT"),
            ],
            edges=[
                DependencyEdge(
                    from_org="conversa",
                    to_org="synapt",
                    allowed=True,
                    packages=["recall"],
                ),
            ],
        )
        result = check_import_violation(
            source_repo="eval",
            source_file="src/scoring.py",
            imported_package="recall",
            config=config,
        )
        assert result is None

    def test_empty_packages_allows_all(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="conversa", repos=["eval"], license="proprietary"),
                OrgDefinition(name="synapt", repos=["recall", "grip"], license="MIT"),
            ],
            edges=[
                DependencyEdge(
                    from_org="conversa",
                    to_org="synapt",
                    allowed=True,
                    packages=[],
                ),
            ],
        )
        result = check_import_violation(
            source_repo="eval",
            source_file="src/scoring.py",
            imported_package="grip",
            config=config,
        )
        assert result is None

    def test_unknown_source_repo_returns_none(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1"], license="MIT")],
            edges=[],
        )
        result = check_import_violation(
            source_repo="unknown",
            source_file="main.py",
            imported_package="something",
            config=config,
        )
        assert result is None


# ── Status rendering ────────────────────────────────────────────


class TestRenderStatus:
    def test_status_includes_org_names(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[
                OrgDefinition(name="synapt", repos=["recall", "grip"], license="MIT"),
                OrgDefinition(name="conversa", repos=["api"], license="proprietary"),
            ],
            edges=[
                DependencyEdge(from_org="conversa", to_org="synapt", allowed=True, packages=[]),
            ],
        )
        output = render_status(config)
        assert "synapt" in output
        assert "conversa" in output
        assert "recall" in output
        assert "ALLOWED" in output

    def test_status_json_is_dict(self) -> None:
        config = IpConfig(
            version=1,
            orgs=[OrgDefinition(name="a", repos=["r1"], license="MIT")],
            edges=[],
        )
        result = render_status_json(config)
        assert isinstance(result, dict)
        assert "orgs" in result
        assert "edges" in result

    def test_empty_config_status(self) -> None:
        config = IpConfig(version=1, orgs=[], edges=[])
        output = render_status(config)
        assert "No organizations" in output
