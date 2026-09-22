#!/usr/bin/env bash
# check-no-orphan-pyc-selftest.sh
#
# Witness for scripts/check-no-orphan-pyc.sh, which shipped with none: its own
# commit added the script and its CI step and no test at all, so every branch in
# it, including the branch-answer added on 2026-09-22, was asserted only by
# someone having run it by hand once.
#
# The fixture is built under $TMPDIR on purpose. On macOS /tmp is a symlink to
# /private/tmp, and `git rev-parse --show-toplevel` returns the RESOLVED path, so
# a scan-root under the unresolved spelling is exactly the case where a
# prefix-strip against the git root fails. That is not hypothetical: it is the
# bug this self-test caught in the change it witnesses.
#
# Exit: 0 every case passed; 1 a case failed; 2 usage / fixture could not build;
#       3 the harness itself is unsound (mutation did not land, or a declared
#       case did not run).
set -u
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$HERE/check-no-orphan-pyc.sh"
[ -f "$SUBJECT" ] || { echo "selftest: subject not found: $SUBJECT" >&2; exit 2; }

fail=0
ran=0
declared=0
note() { printf '  %s\n' "$*"; }

# --- build the fixture -------------------------------------------------------
FX=$(mktemp -d "${TMPDIR:-/tmp}/opyc-selftest.XXXXXX") || { echo "selftest: mktemp failed" >&2; exit 2; }
trap 'rm -rf "$FX"' EXIT INT TERM

build_fixture() {
  repo=$1
  mkdir -p "$repo/pkg/__pycache__"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email selftest@example.invalid
  git -C "$repo" config user.name selftest
  printf 'GHOST = 1\n' > "$repo/pkg/ghost.py"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "add ghost"
  git -C "$repo" branch other                 # 'other' keeps pkg/ghost.py
  git -C "$repo" rm -q pkg/ghost.py
  git -C "$repo" commit -qm "remove ghost from main"
}

# --- cases -------------------------------------------------------------------
# A: the source is on another local branch -> STALE, and NOT fatal on its own.
# B: the source is on no local branch      -> ORPHAN, fatal.
# C: both together                         -> fatal, and the two are distinguished.
# D: scan-root outside any git work tree   -> UNANSWERED wording, fatal.
# E: no .pyc at all                        -> clean, and the count line says zero.

R="$FX/repo"; build_fixture "$R"

declared=$((declared + 1)); ran=$((ran + 1))
printf 'GHOSTPYC' > "$R/pkg/__pycache__/ghost.cpython-313.pyc"
out=$(bash "$SUBJECT" "$R" 2>&1); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'STALE .pyc from another branch' \
   && printf '%s' "$out" | grep -q 'local branch(es): other'; then
  note "A pass: stale-from-another-branch is named with its branch and is not fatal (rc=0)"
else
  note "A FAIL (rc=$rc): $out"; fail=1
fi

declared=$((declared + 1)); ran=$((ran + 1))
printf 'GONEPYC' > "$R/pkg/__pycache__/gone.cpython-313.pyc"
out=$(bash "$SUBJECT" "$R" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'source pkg/gone.py is on no local branch'; then
  note "C pass: both present -> fatal, and a genuine orphan says so in words"
else
  note "C FAIL (rc=$rc): $out"; fail=1
fi

declared=$((declared + 1)); ran=$((ran + 1))
rm -f "$R/pkg/__pycache__/ghost.cpython-313.pyc"
out=$(bash "$SUBJECT" "$R" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'ORPHAN .pyc' \
   && ! printf '%s' "$out" | grep -q 'STALE'; then
  note "B pass: the genuine orphan stays fatal once the stale one is gone"
else
  note "B FAIL (rc=$rc): $out"; fail=1
fi

declared=$((declared + 1)); ran=$((ran + 1))
rm -f "$R/pkg/__pycache__/gone.cpython-313.pyc"
out=$(bash "$SUBJECT" "$R" 2>&1); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qE '\(0 orphans; 0 stale'; then
  note "E pass: clean tree, and the OK line reports the zero counts rather than nothing"
else
  note "E FAIL (rc=$rc): $out"; fail=1
fi

N="$FX/nogit"; mkdir -p "$N/pkg/__pycache__"
printf 'GONEPYC' > "$N/pkg/__pycache__/gone.cpython-313.pyc"
declared=$((declared + 1)); ran=$((ran + 1))
out=$(bash "$SUBJECT" "$N" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'UNANSWERED, not answered no'; then
  note "D pass: outside a git work tree the branch question is UNANSWERED, not answered no"
else
  note "D FAIL (rc=$rc): $out"; fail=1
fi

# --- mutation: neuter the branch answer, case A must go red -------------------
declared=$((declared + 1)); ran=$((ran + 1))
MUT="$FX/mutated.sh"
sed 's|^  \[ "\$inside" = yes \] && on_branches=.*|  on_branches=""   # MUTATED|' "$SUBJECT" > "$MUT"
if [ "$(grep -c 'MUTATED' "$MUT")" -ne 1 ]; then
  echo "selftest: HARNESS UNSOUND — the mutation did not land in bytes; nothing below is measured" >&2
  exit 3
fi
printf 'GHOSTPYC' > "$R/pkg/__pycache__/ghost.cpython-313.pyc"
out=$(bash "$MUT" "$R" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'ORPHAN .pyc'; then
  note "M pass: with the branch answer removed the stale case reports ORPHAN and exits 1"
else
  note "M FAIL (rc=$rc) — the witness does not discriminate: $out"; fail=1
fi

if [ "$ran" -ne "$declared" ]; then
  echo "selftest: HARNESS UNSOUND — declared $declared case(s), ran $ran" >&2
  exit 3
fi

if [ "$fail" -eq 0 ]; then
  echo "selftest: OK — $ran/$declared cases passed, and the mutation turns case A red"
fi
exit "$fail"
