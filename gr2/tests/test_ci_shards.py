"""GR2_TEST_SHARD="i/n" splits the suite by test file for CI (the root conftest's pytest_collection_modifyitems).

The property CI relies on is a PARTITION: over i = 1..n every collected test lands in exactly one shard, so a
sharded run tests what an unsharded run tests. A value that is not "i/n" with 1 <= i <= n refuses the session
rather than quietly running everything or nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

GR2 = Path(__file__).resolve().parents[1]
# Small, fast files: enough of them that three shards are not all empty, cheap enough to collect four times.
FILES = [
    "tests/test_skill_dump.py",
    "tests/test_api_dump.py",
    "tests/test_cli_first_touch_help.py",
    "tests/test_cli_shape_witnesses.py",
    "tests/test_vocabulary.py",
    "tests/test_lane_show_name.py",
    "tests/test_sync_wait.py",
    "tests/test_readme_verbs_exist.py",
]


def _collect(shard: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GR2_TEST_SHARD"}
    if shard is not None:
        env["GR2_TEST_SHARD"] = shard
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *FILES],
                          cwd=GR2, env=env, capture_output=True, text=True, timeout=300)


def _ids(result: subprocess.CompletedProcess[str]) -> list[str]:
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_shards_partition_the_collected_tests() -> None:
    whole = _ids(_collect(None))
    assert len(whole) > 20, f"the control collected only {len(whole)} tests"
    shards = [_ids(_collect(f"{i}/3")) for i in (1, 2, 3)]
    assert sum(1 for s in shards if s) >= 2, [len(s) for s in shards]
    seen = [test for shard in shards for test in shard]
    assert len(seen) == len(set(seen)), "a test landed in two shards"
    assert sorted(seen) == sorted(whole), sorted(set(whole) ^ set(seen))
    for shard in shards:  # a file is never split across shards
        files = {test.split("::", 1)[0] for test in shard}
        assert not files & {t.split("::", 1)[0] for other in shards if other is not shard for t in other}


def test_a_sharded_run_leaves_no_shard_setting_for_child_processes() -> None:
    probe = "tests/test_ci_shard_env_not_inherited.py"
    shard = f"{zlib.crc32(probe.encode()) % 6 + 1}/6"  # the probe's own shard (the conftest's hash), so the run keeps it
    env = {k: v for k, v in os.environ.items() if k != "GR2_TEST_SHARD"}
    env["GR2_TEST_SHARD"] = shard
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", probe],
                            cwd=GR2, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0 and "1 passed" in result.stdout, result.stdout[-2000:] + result.stderr[-2000:]


@pytest.mark.parametrize("value", ["3", "0/3", "4/3", "a/3", "1/0", "1/"])
def test_a_value_that_is_not_i_of_n_refuses_the_session(value: str) -> None:
    result = _collect(value)
    assert result.returncode != 0
    assert "GR2_TEST_SHARD" in result.stdout + result.stderr, result.stdout[-1000:] + result.stderr[-1000:]
