"""scripts/gr2-review-receipt.sh emits the standardized receipt for a gr2-driven
review lane: the gr commit, create->exit wall time, per-repo and total lane sizes,
an optional SIZE comparison against a --full-ref reference (never a "sparse" claim),
and the CLI-exit-point list.

It is the instrument the exact-work acceptance is MEASURED with (one gr
commit opens one exact multi-repo review; the receipt is the fruit), so its fields
are asserted from a fixture review lane here rather than trusted. The script is bash;
these drive it exactly as a reviewer does — via subprocess against a constructed
review_root — and assert the fields, the NONE path, and the refuse cases.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# repo layout: this file is <repo>/gr2/tests/, the script is <repo>/scripts/.
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gr2-review-receipt.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], text=True, capture_output=True
    )


def _review_root(tmp_path: Path, repo: str = "recall", nbytes: int = 4096) -> Path:
    rr = tmp_path / "review_root"
    (rr / "repos" / repo).mkdir(parents=True)
    (rr / "repos" / repo / "blob.bin").write_bytes(b"x" * nbytes)
    return rr


def test_receipt_names_every_field_from_a_fixture_lane(tmp_path: Path) -> None:
    rr = _review_root(tmp_path)
    ep = tmp_path / "ep.txt"
    ep.write_text("git am to reconstruct the range\nhand-authored sources.json\n")
    r = _run(
        "--gr-sha", "gr:e9b6ac73",
        "--review-root", str(rr),
        "--t0", "1000", "--t1", "1201",  # 201s -> 3m21s
        "--v2-head", "c899978d",
        "--target", "recall dev @ c28f446a",
        "--exit-points", str(ep),
        "--lane", "review-856-review",
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "gr:e9b6ac73" in out
    assert "R1 bound to head: c899978d" in out
    assert "target: recall dev @ c28f446a" in out
    # wall time is computed, not echoed: 201s must render as 3m21s AND the raw seconds.
    assert "3m21s" in out and "(201s)" in out
    assert "review lane total size:" in out
    assert "recall:" in out  # per-repo line for the materialized repo
    # exit points: the count is real and each line is carried verbatim.
    assert "CLI exit points (2" in out
    assert "git am to reconstruct the range" in out
    assert "hand-authored sources.json" in out


def test_total_size_is_measured_not_a_constant(tmp_path: Path) -> None:
    # A hard-coded total (e.g. total_kb=0) passes every field-PRESENCE test; the
    # size must be a real, non-zero measurement. ~512K of bytes cannot render as
    # "0K", so a first digit of 0 on the total line means the value was not
    # measured. (per_repo is measured independently, so this pins the TOTAL line.)
    rr = _review_root(tmp_path, nbytes=512 * 1024)
    r = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "0", "--t1", "1")
    assert r.returncode == 0, r.stderr
    m = re.search(r"total size: (\d+(?:\.\d+)?)([KMG])", r.stdout)
    assert m, f"no total-size line: {r.stdout!r}"
    val, unit = float(m.group(1)), m.group(2)
    # 512K of bytes -> hundreds of K at least; never 0.
    assert not (unit == "K" and val < 100), f"total looks unmeasured (too small): {m.group(0)!r}"


def test_empty_exit_points_reads_as_NONE_not_a_crash(tmp_path: Path) -> None:
    # Regression: `grep -c` prints 0 and exits 1 on no match; a naive `|| echo 0`
    # appended a second 0 and broke the integer test. Empty file -> clean NONE.
    rr = _review_root(tmp_path)
    ep = tmp_path / "empty.txt"
    ep.write_text("")
    r = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "5", "--t1", "65",
             "--exit-points", str(ep))
    assert r.returncode == 0, r.stderr
    assert "CLI exit points: NONE" in r.stdout
    assert "1m00s" in r.stdout


def test_savings_line_only_with_full_ref(tmp_path: Path) -> None:
    rr = _review_root(tmp_path, nbytes=4096)
    full = tmp_path / "full"
    full.mkdir()
    (full / "big.bin").write_bytes(b"y" * (64 * 1024))  # ~16x bigger
    with_full = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "0", "--t1", "1",
                     "--full-ref", str(full))
    assert with_full.returncode == 0, with_full.stderr
    assert "smaller" in with_full.stdout  # a savings line is present
    # a positive comparison prints a percent, never a negative one.
    pct_line = re.search(r"= (-?[\d.]+)% smaller", with_full.stdout)
    assert pct_line and float(pct_line.group(1)) > 0, with_full.stdout
    # never claims "sparse" — the script measures size, not how the tree was cut.
    assert "sparse" not in with_full.stdout
    # ... and absent when no --full-ref is given (no fabricated comparison).
    without = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "0", "--t1", "1")
    assert without.returncode == 0
    assert "smaller" not in without.stdout


def test_savings_says_NOT_smaller_when_tree_exceeds_reference(tmp_path: Path) -> None:
    # The pre-push full-clone case (my recall#856 lane): the review tree is at or
    # above the reference. A naive percent goes negative and renders as "smaller";
    # the label must say NOT smaller and carry no negative percent.
    rr = _review_root(tmp_path, nbytes=512 * 1024)  # bigger
    full = tmp_path / "full"
    full.mkdir()
    (full / "tiny.bin").write_bytes(b"z" * 1024)  # smaller reference
    r = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "0", "--t1", "1",
             "--full-ref", str(full))
    assert r.returncode == 0, r.stderr
    savings_line = next(l for l in r.stdout.splitlines() if "reference" in l)
    assert "NOT smaller" in savings_line, savings_line
    assert "-" not in savings_line.split("=", 1)[1], f"negative percent leaked: {savings_line!r}"
    assert "%" not in savings_line, f"a percent should not print in the not-smaller case: {savings_line!r}"


def test_savings_equal_size_boundary_is_not_smaller(tmp_path: Path) -> None:
    # The equal-size
    # boundary (total_kb == full_kb) was untested; -ge -> -gt would survive and print
    # a false "0.0% smaller" on identical-size trees. Shipped code uses -ge; pin it.
    # Point --full-ref at the review_root ITSELF, so total_kb == full_kb EXACTLY
    # (the only point where -ge and -gt differ; a nested fixture would drift and pass
    # under both). -ge -> NOT smaller; -gt would take the else branch and print
    # "0.0% smaller", so this pins -ge specifically.
    rr = _review_root(tmp_path, repo="recall", nbytes=256 * 1024)
    r = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "0", "--t1", "1",
             "--full-ref", str(rr))
    assert r.returncode == 0, r.stderr
    savings_line = next(l for l in r.stdout.splitlines() if "reference" in l)
    assert "NOT smaller" in savings_line, savings_line
    assert "%" not in savings_line, f"equal size must not print a percent: {savings_line!r}"


def test_refuses_t1_before_t0(tmp_path: Path) -> None:
    rr = _review_root(tmp_path)
    r = _run("--gr-sha", "abc", "--review-root", str(rr), "--t0", "10", "--t1", "5")
    assert r.returncode == 2
    assert "t1 < t0" in r.stderr


def test_refuses_missing_review_root(tmp_path: Path) -> None:
    r = _run("--gr-sha", "abc", "--review-root", str(tmp_path / "nope"),
             "--t0", "1", "--t1", "2")
    assert r.returncode == 2
    assert "not a directory" in r.stderr


def test_refuses_missing_required_arg(tmp_path: Path) -> None:
    rr = _review_root(tmp_path)
    # no --gr-sha
    r = _run("--review-root", str(rr), "--t0", "1", "--t1", "2")
    assert r.returncode == 2
    assert "required" in r.stderr


def test_script_is_executable() -> None:
    # No skipif: a missing or non-executable script is a FAILURE, not a skip. CI
    # invokes it directly, so the executable bit is part of the contract.
    assert SCRIPT.exists(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), "receipt script must be executable (chmod +x)"
