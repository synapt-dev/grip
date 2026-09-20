"""Runner summary parsers for `review run --test "<cmd>"`.

The runner contract: counts always come from the runner's OWN summary line, never
its exit code, and a summary the parser cannot read is a REFUSAL with the raw tail —
never a silent zero-green. One small parser per runner; each returns a normalized
dict ``{passed, failed, errors, skipped, selected}`` or ``None`` when no summary line
is present (the caller refuses).

Parsers are written against real runner output:
  * pytest — delegated to ``review_run.parse_pytest_summary``.
  * cargo  — ``test result: ok. 1 passed; 0 failed; 0 ignored; …``, emitted ONCE PER
             test binary (unit + integration + doc), so the counts are SUMMED across
             every ``test result:`` line (the last line is usually doc-tests ``0
             passed`` — taking only the last would drop a real green).
  * jest   — ``Tests:       1 failed, 2 passed, 3 total`` (on stderr; the caller
             merges stdout+stderr before parsing).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# cargo: one summary line per test binary; sum across all of them.
_CARGO_RESULT_RE = re.compile(
    r"test result:\s+\w+\.\s+(\d+) passed;\s+(\d+) failed;\s+(\d+) ignored"
)
# jest: the "Tests:" tally line; each outcome is "<n> <word>", total is separate.
_JEST_TESTS_RE = re.compile(r"^Tests:\s+(.+)$", re.MULTILINE)
_JEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|todo|total)")

JUNIT_XML_DEFAULT_REPORTS = "**/build/test-results/**/*.xml"


class JunitXmlReportError(ValueError):
    """A report file cannot safely provide a test result."""


def find_fresh_junit_xml_reports(
    repo_dir: Path, reports: str, run_started_at: float
) -> list[Path]:
    """Return only report files written after this run started.

    A Gradle test task may be up-to-date and leave yesterday's XML behind. Those
    files cannot certify the command we just ran, so freshness is part of the
    runner contract rather than a best-effort filter in the parser.
    """
    return sorted(
        path for path in repo_dir.glob(reports)
        if path.is_file() and path.stat().st_mtime >= run_started_at
    )


def _junit_count(suite: ET.Element, name: str, report: Path) -> int:
    raw = suite.get(name, "0")
    try:
        value = int(raw)
    except ValueError as exc:
        raise JunitXmlReportError(
            f"malformed JUnit XML in {report}: testsuite {name}={raw!r} is not an integer"
        ) from exc
    if value < 0:
        raise JunitXmlReportError(
            f"malformed JUnit XML in {report}: testsuite {name}={raw!r} is negative"
        )
    return value


def parse_junit_xml_reports(reports: list[Path]) -> dict:
    """Sum every JUnit ``testsuite`` element across fresh report files.

    Reports may have a single ``testsuite`` root or a ``testsuites`` wrapper. A
    malformed file or impossible outcome count is an error, never a partial sum.
    """
    totals = {"selected": 0, "failed": 0, "errors": 0, "skipped": 0}
    for report in reports:
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError) as exc:
            raise JunitXmlReportError(
                f"malformed JUnit XML in {report}: {exc}"
            ) from exc
        suites = list(root.iter("testsuite"))
        if not suites:
            raise JunitXmlReportError(
                f"malformed JUnit XML in {report}: no testsuite elements"
            )
        for suite in suites:
            tests = _junit_count(suite, "tests", report)
            failures = _junit_count(suite, "failures", report)
            errors = _junit_count(suite, "errors", report)
            skipped = _junit_count(suite, "skipped", report)
            if failures + errors + skipped > tests:
                raise JunitXmlReportError(
                    f"malformed JUnit XML in {report}: outcomes exceed tests in a testsuite"
                )
            totals["selected"] += tests
            totals["failed"] += failures
            totals["errors"] += errors
            totals["skipped"] += skipped
    totals["passed"] = (
        totals["selected"] - totals["failed"] - totals["errors"] - totals["skipped"]
    )
    return {
        "passed": totals["passed"],
        "failed": totals["failed"],
        "errors": totals["errors"],
        "skipped": totals["skipped"],
        "selected": totals["selected"],
    }


def parse_cargo_summary(output: str) -> dict | None:
    """Sum every ``test result:`` line (unit, integration, doc). None when no such line
    exists — a compile error makes cargo exit non-zero with no result line, so it refuses
    rather than reporting a false zero.

    Every ``test result:`` line MUST parse: the count of `test result:` lines is compared
    to the count the regex matched, and a mismatch returns None (the caller refuses as
    unparseable). Otherwise a result line the regex could not read (an unexpected token, a
    future cargo format) would be SILENTLY DROPPED from the sum — two binaries reporting
    `3 passed` and `2 passed` would sum to 3, a confidently wrong green, not a zero."""
    result_lines = [ln for ln in output.splitlines() if ln.strip().startswith("test result:")]
    if not result_lines:
        return None
    matches = _CARGO_RESULT_RE.findall(output)
    if len(matches) != len(result_lines):
        return None  # a `test result:` line the parser could not read -> refuse, never silently sum
    passed = failed = ignored = 0
    for p, f, ig in matches:
        passed += int(p)
        failed += int(f)
        ignored += int(ig)
    return {
        "passed": passed,
        "failed": failed,
        "errors": 0,  # cargo has no separate "error" outcome; a compile error -> None above
        "skipped": ignored,
        "selected": passed + failed + ignored,
    }


def parse_jest_summary(output: str) -> dict | None:
    """Read jest's ``Tests:`` tally line. None when it is absent (jest that failed to
    start prints no tally).

    Every counted outcome must sum to the total or the line refuses: when a total is
    present and passed + failed + skipped (todo counted as skipped) != total, return None
    (the caller refuses with the raw tail). Otherwise an outcome jest names that this parser
    does not know — ``Tests: 2 flaky, 2 passed, 4 total`` — would be SILENTLY DROPPED and
    land as a green with two tests unaccounted for (the jest sibling of the cargo
    count-mismatch); a total-only line (``4 total``) refuses the same way rather than reading
    as a red about nothing."""
    m = _JEST_TESTS_RE.search(output)
    if m is None:
        return None
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    total = None
    for n, word in _JEST_COUNT_RE.findall(m.group(1)):
        if word == "total":
            total = int(n)
        elif word == "todo":
            counts["skipped"] += int(n)
        else:
            counts[word] = int(n)
    accounted = counts["passed"] + counts["failed"] + counts["skipped"]
    if total is not None and accounted != total:
        return None  # an unknown outcome was dropped, or a bare total -> refuse, never a partial green
    selected = total if total is not None else accounted
    return {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": 0,
        "skipped": counts["skipped"],
        "selected": selected,
    }


def _normalize_pytest(output: str) -> dict | None:
    from .review_run import parse_pytest_summary

    s = parse_pytest_summary(output)
    if s is None:
        return None
    return {
        "passed": s["passed"],
        "failed": s["failed"],
        "errors": s["errors"],
        "skipped": s["skipped"],
        "selected": s["selected"],
    }


# runner name -> (parser, default test command). The command is what `.review-install`
# `test = …` or `--test` overrides; the parser is chosen by the `runner` key.
RUNNERS = {
    "pytest": _normalize_pytest,
    "cargo": parse_cargo_summary,
    "jest": parse_jest_summary,
    # junit-xml reads files written by the command, so review_run dispatches it
    # directly instead of treating stdout as a summary source.
    "junit-xml": lambda _output: None,
}

# Each runner's OWN created outputs, so a second `review run` on an un-gitignored lane
# does not refuse the first run's artifacts as untracked drift (the language-specific
# analogue of the pytest path's .venv/receipt exemptions). `names` are exact repo-root
# filenames; `tops` are top-level directory prefixes (trailing slash).
RUNNER_CREATED_PATHS = {
    "cargo": {"names": frozenset({"Cargo.lock"}), "tops": ("target/",)},
    "jest": {"names": frozenset(), "tops": ("node_modules/", "coverage/")},
    # Gradle can create build/ below every module. `tops` only permits a root prefix,
    # so this runner alone gets the narrower segment allowance. .gradle and .kotlin
    # are root directories written by Gradle/Kotlin tooling.
    "junit-xml": {
        "names": frozenset(),
        "tops": (".gradle/", ".kotlin/"),
        "segments": frozenset({"build"}),
    },
}


def parse_runner_summary(runner: str, output: str) -> dict | None:
    """Dispatch to the named runner's parser. Unknown runner raises KeyError (the
    caller validates the name against RUNNERS first and refuses an unknown one)."""
    return RUNNERS[runner](output)
