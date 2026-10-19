"""The root conftest removes GR2_TEST_SHARD from the environment once it has read it.

A test that runs pytest in a child process passes its environment on; if the setting stayed, the child's run
would be sharded too and could deselect the very test it was started to run. tests/test_ci_shards.py runs
this file in its own shard, so the setting is present at the start of that run.
"""
from __future__ import annotations

import os


def test_the_shard_setting_is_not_left_in_the_environment() -> None:
    assert "GR2_TEST_SHARD" not in os.environ
