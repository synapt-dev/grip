#!/usr/bin/env bash
# check-no-orphan-pyc-selftest.sh
#
# Witness for scripts/check-no-orphan-pyc.sh, which shipped with none: its own
# commit added the script and its CI step and no test at all, so every branch in
# it was asserted only by someone having run it by hand once.
#
# The fixture is built under $TMPDIR on purpose. On macOS /tmp is a symlink to
# /private/tmp, and `git rev-parse --show-toplevel` returns the RESOLVED path, so
# a scan-root under the unresolved spelling is exactly the case where a
# prefix-strip against the git root fails. That is not hypothetical: it is the
# bug this self-test caught in the change it witnesses.
#
# ONE CASE AND ONE MUTATION PER DEFECT THIS GUARD HAS ACTUALLY SHIPPED WITH.
# The two 2026-09-22 fixes came from a reviewer's probes, not from the author, so
# each gets its own arm here:
#   F  a DIRECTORY named like the source on another branch   -> ORPHAN (cat-file -t)
#   M2 revert the type check to cat-file -e                  -> F goes red
#   G  a root-level __pycache__ whose source is on a branch  -> STALE (path boundary)
#   M3 revert the boundary to the prefix strip               -> G goes red
#
# Exit: 0 every case passed; 1 a case failed; 2 usage / fixture could not build;
#       3 the harness itself is unsound (a mutation did not land in bytes, or a
#       declared case did not run).
set -u
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$HERE/check-no-orphan-pyc.sh"
[ -f "$SUBJECT" ] || { echo "selftest: subject not found: $SUBJECT" >&2; exit 2; }

fail=0
ran=0
declared=0
note() { printf '  %s\n' "$*"; }

FX=$(mktemp -d "${TMPDIR:-/tmp}/opyc-selftest.XXXXXX") || { echo "selftest: mktemp failed" >&2; exit 2; }
trap 'rm -rf "$FX"' EXIT INT TERM

gitq() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

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

# A repo where a DIRECTORY, not a file, sits at the source's path on `other`.
build_dir_fixture() {
  repo=$1
  mkdir -p "$repo/pkg/__pycache__"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email selftest@example.invalid
  git -C "$repo" config user.name selftest
  mkdir -p "$repo/pkg/ghost.py"
  printf 'not a source\n' > "$repo/pkg/ghost.py/inside.txt"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "a directory named pkg/ghost.py"
  git -C "$repo" branch other
  git -C "$repo" rm -qr pkg/ghost.py
  git -C "$repo" commit -qm "remove the directory from main"
}

# A repo whose source lives ONLY on `other` and whose .pyc sits at the ROOT's
# own __pycache__, so the source path has no directory prefix to strip.
build_root_fixture() {
  repo=$1
  mkdir -p "$repo/__pycache__"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email selftest@example.invalid
  git -C "$repo" config user.name selftest
  printf 'TOP = 1\n' > "$repo/topmod.py"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "add topmod"
  git -C "$repo" branch other
  git -C "$repo" rm -q topmod.py
  git -C "$repo" commit -qm "remove topmod from main"
}

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

# F: a DIRECTORY at the source's path must NOT excuse the orphan.
DR="$FX/dirrepo"; build_dir_fixture "$DR"
declared=$((declared + 1)); ran=$((ran + 1))
printf 'GHOSTPYC' > "$DR/pkg/__pycache__/ghost.cpython-313.pyc"
out=$(bash "$SUBJECT" "$DR" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'ORPHAN .pyc' \
   && printf '%s' "$out" | grep -q 'is on no local branch' \
   && ! printf '%s' "$out" | grep -q 'STALE'; then
  note "F pass: a directory named like the source is not a source; the orphan stays fatal"
else
  note "F FAIL (rc=$rc) — a TREE at that path excused a real orphan: $out"; fail=1
fi

# G: a root-level __pycache__ must still get the branch check.
RR="$FX/rootrepo"; build_root_fixture "$RR"
declared=$((declared + 1)); ran=$((ran + 1))
printf 'TOPPYC' > "$RR/__pycache__/topmod.cpython-313.pyc"
out=$(bash "$SUBJECT" "$RR" 2>&1); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'STALE .pyc from another branch' \
   && printf '%s' "$out" | grep -q 'local branch(es): other'; then
  note "G pass: a root-level __pycache__ gets the branch answer, not a false 'outside the repo root'"
else
  note "G FAIL (rc=$rc) — the boundary was computed wrongly: $out"; fail=1
fi

# --- mutations ---------------------------------------------------------------
# Each is asserted to have landed IN BYTES before its exit is read, and the
# harness refuses (exit 3) when it cannot land. One mutation per defect, and each
# must red only its own arm.

mutate() {
  # mutate <out-path> <old-file> <new-file>
  python3 - "$SUBJECT" "$1" "$2" "$3" <<'PY'
import pathlib, sys
subject, out, oldf, newf = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
src = pathlib.Path(subject).read_text()
old = pathlib.Path(oldf).read_text()
new = pathlib.Path(newf).read_text()
if src.count(old) != 1:
    sys.exit(f"anchor count {src.count(old)}")
pathlib.Path(out).write_text(src.replace(old, new))
PY
}

# M2: revert the type check to the existence-only form.
cat > "$FX/m2.old" <<'OLD'
      [ "$(git -C "$GITROOT" cat-file -t "$_br:$_rel" 2>/dev/null)" = blob ] &&
OLD
cat > "$FX/m2.new" <<'NEW'
      git -C "$GITROOT" cat-file -e "$_br:$_rel" 2>/dev/null &&
NEW
declared=$((declared + 1)); ran=$((ran + 1))
if mutate "$FX/mut-catfile.sh" "$FX/m2.old" "$FX/m2.new"; then
  out=$(bash "$FX/mut-catfile.sh" "$DR" 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'STALE'; then
    note "M2 pass: reverting the type check to cat-file -e reds the directory arm (F)"
  else
    note "M2 FAIL (rc=$rc) — the type-check witness does not discriminate: $out"; fail=1
  fi
else
  echo "selftest: HARNESS UNSOUND — the cat-file mutation did not land; nothing measured" >&2
  exit 3
fi

# M3: revert the boundary computation to the trailing-slash prefix strip.
cat > "$FX/m3.old" <<'OLD'
  inside=no
  rel="$srcdir_real/$stem.py"
  case "$srcdir_real" in
    "$GITROOT_REAL")   inside=yes; rel="$stem.py" ;;
    "$GITROOT_REAL"/*) inside=yes; rel="${srcdir_real#"$GITROOT_REAL"/}/$stem.py" ;;
  esac
OLD
cat > "$FX/m3.new" <<'NEW'
  inside=yes
  rel=${srcdir_real#"$GITROOT_REAL"/}/$stem.py
  [ "$rel" != "$srcdir_real/$stem.py" ] || inside=no
NEW
declared=$((declared + 1)); ran=$((ran + 1))
if mutate "$FX/mut-boundary.sh" "$FX/m3.old" "$FX/m3.new"; then
  out=$(bash "$FX/mut-boundary.sh" "$RR" 2>&1); rc=$?
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'outside the repo root'; then
    note "M3 pass: reverting the boundary to the prefix strip reds the root-level arm (G)"
  else
    note "M3 FAIL (rc=$rc) — the boundary witness does not discriminate: $out"; fail=1
  fi
else
  echo "selftest: HARNESS UNSOUND — the boundary mutation did not land; nothing measured" >&2
  exit 3
fi

if [ "$ran" -ne "$declared" ]; then
  echo "selftest: HARNESS UNSOUND — declared $declared case(s), ran $ran" >&2
  exit 3
fi

if [ "$fail" -eq 0 ]; then
  echo "selftest: OK — $ran/$declared cases passed, each mutation reddening only its own arm"
fi
exit "$fail"
