"""Cross-org IP boundary model for gripspace workspaces.

Declares organizational ownership of repos and enforces directional
dependency constraints between orgs. The config lives at .grip/ip.toml.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class EdgeResolution(StrEnum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    UNDECLARED = "undeclared"
    INTERNAL = "internal"


@dataclass(frozen=True)
class OrgDefinition:
    name: str
    repos: list[str]
    license: str


@dataclass(frozen=True)
class DependencyEdge:
    from_org: str
    to_org: str
    allowed: bool
    packages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IpConfig:
    version: int = 1
    orgs: list[OrgDefinition] = field(default_factory=list)
    edges: list[DependencyEdge] = field(default_factory=list)


@dataclass
class IpViolation:
    kind: str
    source_repo: str
    source_file: str
    target_org: str
    target_package: str
    message: str


def ip_config_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "ip.toml"


def load_ip_config(workspace_root: Path) -> IpConfig:
    path = ip_config_path(workspace_root)
    if not path.exists():
        return IpConfig()

    text = path.read_text()
    if not text.strip():
        return IpConfig()

    data = tomllib.loads(text)
    ip_section = data.get("ip", {})

    orgs = [
        OrgDefinition(
            name=o["name"],
            repos=o["repos"],
            license=o.get("license", "unspecified"),
        )
        for o in ip_section.get("orgs", [])
    ]

    edges = [
        DependencyEdge(
            from_org=e["from"],
            to_org=e["to"],
            allowed=e["allowed"],
            packages=e.get("packages", []),
        )
        for e in ip_section.get("edges", [])
    ]

    return IpConfig(
        version=ip_section.get("version", 1),
        orgs=orgs,
        edges=edges,
    )


def validate_ip_config(config: IpConfig) -> None:
    if config.version != 1:
        raise ValueError(f"Unsupported IP config version: {config.version}")

    seen_orgs: set[str] = set()
    seen_repos: dict[str, str] = {}

    for org in config.orgs:
        if not org.name:
            raise ValueError("Org has empty name")
        if not org.repos:
            raise ValueError(f"Org '{org.name}' has no repos")
        if org.name in seen_orgs:
            raise ValueError(f"Duplicate org name: '{org.name}'")
        seen_orgs.add(org.name)

        for repo in org.repos:
            if repo in seen_repos:
                raise ValueError(
                    f"Repo '{repo}' appears in multiple orgs: '{seen_repos[repo]}' and '{org.name}'"
                )
            seen_repos[repo] = org.name

    seen_edges: set[tuple[str, str]] = set()

    for edge in config.edges:
        if edge.from_org == edge.to_org:
            raise ValueError(f"Edge from '{edge.from_org}' to '{edge.to_org}' is self-referencing")
        if edge.from_org not in seen_orgs:
            raise ValueError(f"'{edge.from_org}' is not a declared org")
        if edge.to_org not in seen_orgs:
            raise ValueError(f"'{edge.to_org}' is not a declared org")
        edge_key = (edge.from_org, edge.to_org)
        if edge_key in seen_edges:
            raise ValueError(f"Duplicate edge from '{edge.from_org}' to '{edge.to_org}'")
        seen_edges.add(edge_key)


def repo_to_org(repo_name: str, config: IpConfig) -> str | None:
    for org in config.orgs:
        if repo_name in org.repos:
            return org.name
    return None


def _find_edge(from_org: str, to_org: str, config: IpConfig) -> DependencyEdge | None:
    for edge in config.edges:
        if edge.from_org == from_org and edge.to_org == to_org:
            return edge
    return None


def resolve_edge(from_org: str, to_org: str, config: IpConfig) -> EdgeResolution:
    if from_org == to_org:
        return EdgeResolution.INTERNAL

    for edge in config.edges:
        if edge.from_org == from_org and edge.to_org == to_org:
            return EdgeResolution.ALLOWED if edge.allowed else EdgeResolution.FORBIDDEN

    return EdgeResolution.UNDECLARED


def check_import_violation(
    *,
    source_repo: str,
    source_file: str,
    imported_package: str,
    config: IpConfig,
) -> IpViolation | None:
    source_org = repo_to_org(source_repo, config)
    if source_org is None:
        return None

    target_org = _package_to_org(imported_package, config)
    if target_org is None:
        return None

    if source_org == target_org:
        return None

    resolution = resolve_edge(source_org, target_org, config)

    if resolution == EdgeResolution.ALLOWED:
        edge = _find_edge(source_org, target_org, config)
        if edge and edge.packages and imported_package not in edge.packages:
            return IpViolation(
                kind="package_not_allowed",
                source_repo=source_repo,
                source_file=source_file,
                target_org=target_org,
                target_package=imported_package,
                message=(
                    f"{source_org} → {target_org} allows only "
                    f"{edge.packages}, but {source_file} imports {imported_package}"
                ),
            )
        return None

    if resolution == EdgeResolution.FORBIDDEN:
        return IpViolation(
            kind="forbidden_edge",
            source_repo=source_repo,
            source_file=source_file,
            target_org=target_org,
            target_package=imported_package,
            message=(
                f"{source_org} → {target_org} is forbidden: "
                f"{source_file} imports {imported_package}"
            ),
        )

    return IpViolation(
        kind="undeclared_edge",
        source_repo=source_repo,
        source_file=source_file,
        target_org=target_org,
        target_package=imported_package,
        message=(
            f"{source_org} → {target_org} has no declared edge: "
            f"{source_file} imports {imported_package}"
        ),
    )


def _package_to_org(package_name: str, config: IpConfig) -> str | None:
    """Best-effort mapping from package name to org.

    Uses a simple heuristic: if the package name matches a repo name
    (with underscores normalized to hyphens), return that repo's org.
    This covers the common case where package name == repo name.
    """
    normalized = package_name.replace("-", "_")
    for org in config.orgs:
        for repo in org.repos:
            repo_normalized = repo.replace("-", "_")
            if normalized == repo_normalized or normalized.startswith(repo_normalized + "_"):
                return org.name
    return None


def render_status(config: IpConfig) -> str:
    if not config.orgs:
        return "No organizations declared in .grip/ip.toml"

    lines: list[str] = ["Organizations:"]
    for org in config.orgs:
        repos_str = ", ".join(org.repos)
        lines.append(f"  {org.name} ({len(org.repos)} repos): {repos_str} [{org.license}]")

    if config.edges:
        lines.append("")
        lines.append("Dependency Edges:")
        for edge in config.edges:
            status = "ALLOWED" if edge.allowed else "FORBIDDEN"
            pkg_note = ""
            if edge.packages:
                pkg_note = f"  (packages: {', '.join(edge.packages)})"
            lines.append(f"  {edge.from_org} → {edge.to_org}  {status}{pkg_note}")

    return "\n".join(lines)


def render_status_json(config: IpConfig) -> dict[str, Any]:
    return {
        "version": config.version,
        "orgs": [
            {"name": org.name, "repos": org.repos, "license": org.license} for org in config.orgs
        ],
        "edges": [
            {
                "from": edge.from_org,
                "to": edge.to_org,
                "allowed": edge.allowed,
                "packages": edge.packages,
            }
            for edge in config.edges
        ],
    }
