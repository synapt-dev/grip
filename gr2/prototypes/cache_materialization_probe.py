#!/usr/bin/env python3
"""Prototype cache-backed materialization for gr2 working clones.

This is not a final implementation. It exists to answer a narrower question:

- if `apply` seeds working clones from a local mirror/reference cache, does that
  materially improve materialization speed while keeping the user-facing model
  unchanged?
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PLAYGROUND_REPOS = {
    "app": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-app.git",
        "https": "https://github.com/synapt-dev/gr2-playground-app.git",
    },
    "api": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-api.git",
        "https": "https://github.com/synapt-dev/gr2-playground-api.git",
    },
    "web": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-web.git",
        "https": "https://github.com/synapt-dev/gr2-playground-web.git",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure direct clone vs cache-backed clone materialization"
    )
    parser.add_argument(
        "--transport",
        choices=["ssh", "https"],
        default="ssh",
        help="remote transport to test",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="repo(s) to test; defaults to all playground repos",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="optional persistent temp root; defaults to a temporary directory",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def repo_url(repo_name: str, transport: str) -> str:
    return PLAYGROUND_REPOS[repo_name][transport]


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new",
    )
    return env


def run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
        env=git_env(),
    )


def timed_clone(argv: list[str], *, cwd: Path | None = None) -> tuple[float, subprocess.CompletedProcess[str]]:
    start = time.perf_counter()
    result = run(argv, cwd=cwd)
    elapsed = time.perf_counter() - start
    return (elapsed, result)


def verify_working_clone(path: Path) -> dict[str, object]:
    head = run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    status = run(["git", "-C", str(path), "status", "--short"]).stdout.strip()
    alternates = path / ".git" / "objects" / "info" / "alternates"
    return {
        "head": head,
        "clean": status == "",
        "uses_alternates": alternates.exists(),
        "alternates_path": str(alternates) if alternates.exists() else None,
    }


def probe_repo(root: Path, repo_name: str, transport: str) -> dict[str, object]:
    url = repo_url(repo_name, transport)
    repo_root = root / repo_name
    direct_root = repo_root / "direct"
    cached_root = repo_root / "cached"
    cache_root = repo_root / "cache"
    direct_root.mkdir(parents=True, exist_ok=True)
    cached_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    direct_target = direct_root / repo_name
    cache_mirror = cache_root / f"{repo_name}.git"
    cached_target = cached_root / repo_name

    direct_seconds, _ = timed_clone(["git", "clone", url, str(direct_target)])
    mirror_seconds, _ = timed_clone(["git", "clone", "--mirror", url, str(cache_mirror)])
    cached_seconds, _ = timed_clone(
        [
            "git",
            "clone",
            "--reference-if-able",
            str(cache_mirror),
            url,
            str(cached_target),
        ]
    )

    direct_info = verify_working_clone(direct_target)
    cached_info = verify_working_clone(cached_target)

    return {
        "repo": repo_name,
        "transport": transport,
        "direct_clone_seconds": round(direct_seconds, 3),
        "mirror_seed_seconds": round(mirror_seconds, 3),
        "cached_clone_seconds": round(cached_seconds, 3),
        "delta_seconds": round(direct_seconds - cached_seconds, 3),
        "direct_clone": direct_info,
        "cached_clone": cached_info,
        "mirror_path": str(cache_mirror),
    }


def main() -> int:
    args = parse_args()
    repos = args.repos or list(PLAYGROUND_REPOS.keys())
    unknown = [repo for repo in repos if repo not in PLAYGROUND_REPOS]
    if unknown:
        raise SystemExit("unknown repos: " + ", ".join(unknown))

    if args.workspace_root:
        root = args.workspace_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        root = Path(tempfile.mkdtemp(prefix="gr2-cache-probe."))
        cleanup = True

    try:
        rows = [probe_repo(root, repo, args.transport) for repo in repos]
        if args.json:
            print(json.dumps({"root": str(root), "results": rows}, indent=2))
        else:
            print(f"root: {root}")
            print("REPO\tTRANSPORT\tDIRECT_S\tMIRROR_S\tCACHED_S\tDELTA_S\tALTERNATES")
            for row in rows:
                print(
                    f"{row['repo']}\t{row['transport']}\t{row['direct_clone_seconds']}\t"
                    f"{row['mirror_seed_seconds']}\t{row['cached_clone_seconds']}\t"
                    f"{row['delta_seconds']}\t{str(row['cached_clone']['uses_alternates']).lower()}"
                )
            print("notes:")
            print("- direct clone is a normal working clone from remote")
            print("- mirror seed is the one-time cache population cost")
            print("- cached clone is a working clone using --reference-if-able from the local mirror")
        return 0
    finally:
        if cleanup:
            shutil.rmtree(root)


if __name__ == "__main__":
    sys.exit(main())
