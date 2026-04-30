#!/usr/bin/env python3
"""Probe whether repo remotes are reachable/authenticated before gr2 apply.

This is a UX prototype, not a full transport manager. Its job is to answer:

- will this remote likely clone successfully from this environment?
- if not, is the problem transport reachability or authentication?
- what should the user try next?
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


SSH_TIMEOUT_SECONDS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe repo transport/auth readiness")
    parser.add_argument("workspace_spec", type=Path, help="path to workspace_spec.toml")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    return parser.parse_args()


def detect_transport(url: str) -> str:
    if url.startswith("git@") or url.startswith("ssh://"):
        return "ssh"
    if url.startswith("https://") or url.startswith("http://"):
        return "https"
    return "unknown"


def classify_failure(stderr: str, transport: str) -> tuple[str, str]:
    text = stderr.lower()
    if "operation timed out" in text or "connection timed out" in text:
        return ("transport-unreachable", f"{transport} transport timed out")
    if "connection refused" in text:
        return ("transport-unreachable", f"{transport} transport refused connection")
    if "permission denied (publickey)" in text:
        return ("auth-failed", "ssh key was rejected")
    if "could not read username" in text or "authentication failed" in text:
        return ("auth-failed", "https authentication is missing or invalid")
    if "repository not found" in text:
        return ("repo-not-found", "repository not found or not visible to current auth")
    if "could not resolve host" in text:
        return ("dns-failed", "host could not be resolved")
    return ("probe-failed", stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error")


def recommendation(status: str, transport: str) -> str:
    if status == "ok":
        return "selected transport looks usable"
    if status == "transport-unreachable" and transport == "ssh":
        return "try HTTPS or fix SSH network reachability"
    if status == "transport-unreachable":
        return "check network access or remote availability"
    if status == "auth-failed" and transport == "ssh":
        return "load the correct SSH key or switch to HTTPS"
    if status == "auth-failed":
        return "configure GitHub HTTPS auth or switch to SSH"
    if status == "repo-not-found":
        return "verify repo visibility and credentials"
    if status == "dns-failed":
        return "fix host resolution before retrying"
    return "inspect stderr and retry with an explicit transport"


def probe_url(url: str) -> tuple[str, str]:
    transport = detect_transport(url)
    env = None
    if transport == "ssh":
        env = {
            **dict(os.environ),
            "GIT_SSH_COMMAND": (
                f"ssh -o BatchMode=yes -o ConnectTimeout={SSH_TIMEOUT_SECONDS} "
                "-o StrictHostKeyChecking=accept-new"
            ),
        }

    result = subprocess.run(
        ["git", "ls-remote", "--symref", url, "HEAD"],
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode == 0:
        head_ref = ""
        for line in result.stdout.splitlines():
            match = re.match(r"ref: refs/heads/(.+)\s+HEAD", line)
            if match:
                head_ref = match.group(1)
                break
        return ("ok", head_ref or "HEAD reachable")
    status, detail = classify_failure(result.stderr, transport)
    return (status, detail)


def load_spec(path: Path) -> list[dict[str, str]]:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    return list(doc.get("repos", []))


def main() -> int:
    args = parse_args()
    repos = load_spec(args.workspace_spec)
    rows: list[dict[str, str]] = []
    exit_code = 0
    for repo in repos:
        url = repo["url"]
        transport = detect_transport(url)
        status, detail = probe_url(url)
        if status != "ok":
            exit_code = 1
        rows.append(
            {
                "repo": repo["name"],
                "url": url,
                "transport": transport,
                "status": status,
                "detail": detail,
                "recommendation": recommendation(status, transport),
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("REPO\tTRANSPORT\tSTATUS\tDETAIL\tNEXT")
        for row in rows:
            print(
                f'{row["repo"]}\t{row["transport"]}\t{row["status"]}\t'
                f'{row["detail"]}\t{row["recommendation"]}'
            )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
