#!/usr/bin/env bash
# ci-clippy-selftest.sh
#
# Witness for scripts/ci-clippy.sh. It compiles nothing: rustc and cargo are shims that print the
# versions a case asks for, so every branch of the guard runs in a second.
#
# CASES (each expects an exit code and a line):
#   A  rustc 1.99.0, clippy 0.1.99, no pin     -> 0, prints CI's flags (-D warnings among them)
#   B  rustc 1.99.0, clippy 0.1.98             -> 2 (a clippy that does not match the rustc)
#   C  pin 1.99.0, rustc 1.99.0, clippy 0.1.99 -> 0
#   D  pin 1.99.0, rustc 1.98.1, clippy 0.1.98 -> 2 (the Homebrew case: they agree, the pin is not honored)
#   E  clippy prints nothing readable          -> 3
#   F  `cargo clippy` itself fails             -> 3
#   G  pin is a name (stable), not a release   -> 0 (nothing to compare)
# MUTATIONS (each is applied to a COPY, and the count of its target in the copy must move 1 -> 0, or
# the harness is unsound and this exits 3): the case named must go red.
#   M1  invert the rustc/clippy comparison     -> A and B go red
#   M2  switch the pin check off               -> D goes red
#   M3  drop -D warnings from the flags        -> A goes red
# Exit: 0 all green; 1 a case or mutation expectation failed; 3 the harness is unsound.
set -u
here=$(cd "$(dirname "$0")" && pwd)
src="$here/ci-clippy.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/ci-clippy-selftest.XXXXXX") || exit 3
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
cat > "$T/bin/rustc" <<'S'
#!/usr/bin/env bash
echo "${FAKE_RUSTC:-rustc 1.99.0 (fake)}"
S
cat > "$T/bin/cargo" <<'S'
#!/usr/bin/env bash
[ "${FAKE_CARGO_RC:-0}" != 0 ] && { echo "error: no such command: clippy" >&2; exit "$FAKE_CARGO_RC"; }
echo "${FAKE_CLIPPY:-clippy 0.1.99 (fake)}"
S
chmod +x "$T/bin/rustc" "$T/bin/cargo"

fail=0
# run <script> <pin-or-empty> -- sets RC and OUT; the script runs from a private tree so its own
# rust-toolchain.toml is the one the case names, never the repo's.
run() {
  local script=$1 pin=$2 tree="$T/tree.$RANDOM"
  mkdir -p "$tree/scripts"; cp "$script" "$tree/scripts/ci-clippy.sh"
  [ -n "$pin" ] && printf '[toolchain]\nchannel = "%s"\n' "$pin" > "$tree/rust-toolchain.toml"
  OUT=$(PATH="$T/bin:$PATH" "$tree/scripts/ci-clippy.sh" --print-command 2>&1); RC=$?
}
expect() { # name want-rc grep-pattern(optional)
  if [ "$RC" != "$2" ] || { [ -n "${3:-}" ] && ! printf '%s' "$OUT" | grep -qF -- "$3"; }; then
    echo "RED   $1: rc=$RC (want $2)${3:+, want line: $3}"; fail=1; return 1
  fi
  echo "green $1"; return 0
}

caseA() { FAKE_RUSTC="rustc 1.99.0" FAKE_CLIPPY="clippy 0.1.99" run "$1" "";      expect A 0 "-D warnings"; }
caseB() { FAKE_RUSTC="rustc 1.99.0" FAKE_CLIPPY="clippy 0.1.98" run "$1" "";      expect B 2 "REFUSED"; }
caseC() { FAKE_RUSTC="rustc 1.99.0" FAKE_CLIPPY="clippy 0.1.99" run "$1" "1.99.0"; expect C 0; }
caseD() { FAKE_RUSTC="rustc 1.98.1" FAKE_CLIPPY="clippy 0.1.98" run "$1" "1.99.0"; expect D 2 "pins 1.99.0 but rustc is 1.98.1"; }
caseE() { FAKE_RUSTC="rustc 1.99.0" FAKE_CLIPPY="garbled"       run "$1" "";      expect E 3; }
caseF() { FAKE_RUSTC="rustc 1.99.0" FAKE_CARGO_RC=1             run "$1" "";      expect F 3; }
caseG() { FAKE_RUSTC="rustc 1.99.0" FAKE_CLIPPY="clippy 0.1.99" run "$1" "stable"; expect G 0; }
for c in A B C D E F G; do case$c "$src"; done

# --- mutations ---------------------------------------------------------------------------------
mutate() { # name target replacement
  local m="$T/mut.$1.sh" n
  cp "$src" "$m"
  n=$(grep -cF -- "$2" "$m"); [ "$n" = 1 ] || { echo "UNSOUND $1: target occurs $n times before"; exit 3; }
  python3 - "$m" "$2" "$3" <<'P'
import sys
p,a,b=sys.argv[1:4]; s=open(p).read(); open(p,"w").write(s.replace(a,b,1))
P
  n=$(grep -cF -- "$2" "$m"); [ "$n" = 0 ] || { echo "UNSOUND $1: target still occurs $n times after"; exit 3; }
  MUT="$m"
}
expect_red() { # mutation-name case-fn...
  local name=$1; shift; local c
  for c in "$@"; do
    local before=$fail; fail=0
    "case$c" "$MUT" >/dev/null 2>&1; local rc=$?
    fail=$before
    if [ "$rc" = 0 ]; then echo "RED   $name: case $c stayed GREEN under the mutation (the guard cannot fail)"; fail=1
    else echo "green $name: case $c goes red"; fi
  done
}
mutate M1 '[ "$rustc_minor" != "$clippy_minor" ]' '[ "$rustc_minor" = "$clippy_minor" ]'; expect_red M1 A B
mutate M2 'if [ -n "$pinned" ]; then' 'if false; then';                                  expect_red M2 D
mutate M3 '  -D warnings)' ')';                                                           expect_red M3 A

[ "$fail" = 0 ] && { echo "ci-clippy-selftest: all cases and mutations as expected"; exit 0; }
echo "ci-clippy-selftest: FAILED"; exit 1
