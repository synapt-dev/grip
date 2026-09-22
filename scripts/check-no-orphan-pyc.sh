#!/usr/bin/env bash
# check-no-orphan-pyc.sh [scan-root]
#
# Refuses a .pyc that has no source, in two ways:
#   (1) a .pyc TRACKED in git — a committed bytecode file is source-less in every
#       fresh checkout and is imported as a GHOST module (pytest collects it, its
#       tests "exist" with no readable source). This is the durable recurrence path
#       and is always meaningful, even on a clean CI checkout.
#   (2) a WORKING-TREE .pyc under scan-root (default: repo root, minus .venv / target /
#       node_modules / .git) whose sibling source .py is absent. Meaningful when run
#       AFTER a test run has populated __pycache__: a source removed while its .pyc
#       lingers shows here.
#
#       Absent HERE is not the same as gone. A branch switch leaves the previous
#       branch's __pycache__ behind, so the source is very often present on another
#       LOCAL BRANCH. That case is reported as STALE and is NOT fatal; only a .pyc
#       whose source is on no local branch is an ORPHAN. When the branch question
#       cannot be answered (scan-root outside a git work tree, or a source path
#       outside the repo root) the message says UNANSWERED rather than implying no.
#
# Why: the release feasibility read found two review tests existing ONLY as stale .pyc
# with no source; the sources have since landed, so this guard keeps the class from
# returning rather than fixing an instance. Wire it into CI AFTER pytest so (2) has a
# populated __pycache__ to scan.
#
# Exit: 0 clean; 1 an orphan or a tracked .pyc was found; 2 usage / not a git tree.
#       A STALE .pyc from another branch alone does NOT change the exit code: it is
#       named on stderr and counted in the OK line, because failing a range for an
#       artifact its bytes never touched is the defect this branch check removes.
set -u
set -o pipefail

ROOT=${1:-}
if [ -z "$ROOT" ]; then
  ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "check-no-orphan-pyc: not a git work tree and no scan-root given" >&2; exit 2; }
fi
[ -d "$ROOT" ] || { echo "check-no-orphan-pyc: scan-root is not a directory: $ROOT" >&2; exit 2; }

status=0

# (1) tracked .pyc anywhere in the repo
tracked=$(git -C "$ROOT" ls-files '*.pyc' 2>/dev/null || true)
if [ -n "$tracked" ]; then
  echo "TRACKED .pyc (a committed bytecode file is source-less in every checkout):" >&2
  printf '%s\n' "$tracked" | sed 's/^/  /' >&2
  status=1
fi

# (2) working-tree orphans: a .pyc in __pycache__ whose sibling source is gone
#
# A source-less .pyc is only a REAL orphan if that source exists on no local
# branch. The common case is the opposite: a branch switch leaves the previous
# branch's __pycache__ behind, so the source is absent HERE and present on the
# branch you just left. Reporting that as fatal reds a range whose bytes never
# touched it (grip#1078), and the reviewer's next move, deleting the artifact,
# is the wrong one.
#
# The question asked of each branch is "does this PATH exist at that branch's
# tip", answered with `cat-file -e <branch>:<path>`. That is NOT commit
# containment and must not be "fixed" into `git branch --contains`: containment
# answers "is this commit OBJECT present", a different question that is wrong in
# both directions here (see the containment clause in config's claude.md). The
# path need not be committed on any branch we did NOT check, which is why an
# unanswerable check says so instead of implying "no branch has it".

GITROOT=$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)
# BOTH SIDES OF THE PREFIX STRIP MUST BE CANONICAL, and this is not theoretical:
# `rev-parse --show-toplevel` returns a RESOLVED path, so on macOS a scan-root
# under /tmp (a symlink to /private/tmp) never matches it and every orphan reads
# as "outside the repo root" — measured, and it was this script's own witness
# that caught it. `pwd -P` puts both on the same footing.
_gsp_real() { [ -n "$1" ] && (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
GITROOT_REAL=$(_gsp_real "$GITROOT")

# Branches whose tip carries $1 (a path relative to the repo root), one per line.
_branches_carrying() {
  _rel=$1
  git -C "$GITROOT" for-each-ref --format='%(refname:short)' refs/heads/ 2>/dev/null |
    while IFS= read -r _br; do
      [ -n "$_br" ] || continue
      git -C "$GITROOT" cat-file -e "$_br:$_rel" 2>/dev/null && printf '%s\n' "$_br"
    done
}

explained=0
orphans=0
while IFS= read -r pyc; do
  [ -n "$pyc" ] || continue
  base=$(basename "$pyc" .pyc)
  stem=${base%%.cpython-*}          # strip .cpython-XY[-pytest-...]; leaves the module name
  srcdir=$(dirname "$(dirname "$pyc")")   # parent of the __pycache__ dir
  [ -f "$srcdir/$stem.py" ] && continue

  # path relative to the repo root, for <branch>:<path>, canonical on both sides
  srcdir_real=$(_gsp_real "$srcdir")
  rel=${srcdir_real#"$GITROOT_REAL"/}/$stem.py
  inside=yes
  [ -n "$GITROOT_REAL" ] && [ "$rel" != "$srcdir_real/$stem.py" ] || inside=no

  on_branches=""
  [ "$inside" = yes ] && on_branches=$(_branches_carrying "$rel" | paste -sd, - 2>/dev/null || true)

  if [ -n "$on_branches" ]; then
    # Not fatal: this clone is simply not on a branch that has the source.
    echo "STALE .pyc from another branch (not an orphan): $pyc" >&2
    echo "  source $rel exists on local branch(es): $on_branches" >&2
    echo "  remove the artifact, or check out one of those branches; the source is not lost." >&2
    explained=$((explained + 1))
  else
    echo "ORPHAN .pyc (no $srcdir/$stem.py): $pyc" >&2
    if [ -z "$GITROOT" ]; then
      echo "  COULD NOT CHECK other branches: $ROOT is not inside a git work tree, so" >&2
      echo "  whether the source survives on another branch is UNANSWERED, not answered no." >&2
    elif [ "$inside" = no ]; then
      echo "  COULD NOT CHECK other branches: $srcdir/$stem.py is outside the repo root" >&2
      echo "  $GITROOT_REAL, so it has no <branch>:<path> spelling. UNANSWERED, not answered no." >&2
    else
      echo "  source $rel is on no local branch." >&2
    fi
    orphans=$((orphans + 1))
    status=1
  fi
done < <(find "$ROOT" \
  \( -path '*/.venv' -o -path '*/target' -o -path '*/node_modules' -o -path '*/.git' \) -prune -o \
  -type f -name '*.pyc' -path '*/__pycache__/*' -print 2>/dev/null)

# A count that is only ever printed when non-zero would let a truncation or a
# find that matched nothing look the same as a clean tree, so both are named.
if [ "$status" -eq 0 ]; then
  echo "check-no-orphan-pyc: OK — no tracked .pyc and no source-less .pyc under $ROOT (0 orphans; $explained stale .pyc from other branches)"
fi
exit "$status"
