#!/usr/bin/env bash
# ci-clippy.sh [--print-command]
#
# Runs clippy exactly as CI does, and refuses to run when the clippy it would use is not the
# clippy that belongs to the active rustc.
#
# WHY THE REFUSAL. clippy's version tracks rustc's (rustc 1.99.0 ships clippy 0.1.99). On a host
# where a package manager's `cargo-clippy` is earlier on PATH than the rustup proxy, a toolchain
# pinned by rust-toolchain.toml runs the right rustc beside the WRONG clippy: reproducing a CI lint
# locally then exits 0 clean and reads as "CI flaked" (measured 2026-10-01: Homebrew clippy 0.1.98
# under rustc 1.99.0 passed while CI failed 26 double_must_use errors). rustc and cargo are resolved
# through the rustup proxies and follow the pin; a cargo-clippy that is not a proxy does not.
#
# The flags below are the ONE copy: the CI "Run clippy" step calls this script, so what CI checks
# and what you check cannot drift apart.
#
# Exit: 0 clippy ran clean (or --print-command printed it); 2 version mismatch or the pin not honored
#       (nothing ran);
#       3 a version could not be read; otherwise clippy's own exit.
set -u

print_only=0
[ "${1:-}" = "--print-command" ] && print_only=1

rustc_v=$(rustc --version 2>&1) || { echo "ci-clippy: cannot run rustc: $rustc_v" >&2; exit 3; }
clippy_v=$(cargo clippy --version 2>&1) || { echo "ci-clippy: cannot run cargo clippy: $clippy_v" >&2; exit 3; }
echo "ci-clippy: $rustc_v"
echo "ci-clippy: $clippy_v"

# The pin: when rust-toolchain.toml names a numeric channel, the rustc that runs must be that
# release. A Homebrew rustc earlier on PATH than the rustup proxy ignores the file and answers for
# its own version, so clippy and rustc can agree with each other and both be wrong.
repo_root=$(cd "$(dirname "$0")/.." && pwd)
pinned=""
if [ -f "$repo_root/rust-toolchain.toml" ]; then
  pinned=$(sed -n -E 's/^channel *= *"(1\.[0-9]+\.[0-9]+)".*/\1/p' "$repo_root/rust-toolchain.toml" | head -n 1)
fi
if [ -n "$pinned" ]; then
  active=$(printf '%s\n' "$rustc_v" | sed -n -E 's/^rustc ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')
  if [ "$active" != "$pinned" ]; then
    echo "ci-clippy: REFUSED, rust-toolchain.toml pins $pinned but rustc is ${active:-unreadable}." >&2
    echo "ci-clippy: rustc on PATH: $(command -v rustc || echo none); the pin is only read through the rustup proxies." >&2
    echo "ci-clippy: put \$HOME/.cargo/bin before any other rustc and cargo-clippy and rerun." >&2
    exit 2
  fi
fi

rustc_minor=$(printf '%s\n' "$rustc_v" | sed -n -E 's/^rustc 1\.([0-9]+)\..*/\1/p')
clippy_minor=$(printf '%s\n' "$clippy_v" | sed -n -E 's/^clippy 0\.1\.([0-9]+).*/\1/p')
if [ -z "$rustc_minor" ] || [ -z "$clippy_minor" ]; then
  echo "ci-clippy: cannot read a minor version from the two lines above" >&2
  exit 3
fi
if [ "$rustc_minor" != "$clippy_minor" ]; then
  echo "ci-clippy: REFUSED, rustc is 1.$rustc_minor but clippy is 0.1.$clippy_minor." >&2
  echo "ci-clippy: a clippy that does not match the rustc answers for a different toolchain." >&2
  echo "ci-clippy: cargo-clippy on PATH: $(command -v cargo-clippy || echo none)" >&2
  echo "ci-clippy: put the rustup bin directory (\$HOME/.cargo/bin) before any other cargo-clippy and rerun." >&2
  exit 2
fi

# Allowed lints are common in the codebase; fix them and remove the allows.
cmd=(cargo clippy --workspace --all-features --
  -A clippy::ptr_arg
  -A clippy::too_many_arguments
  -A clippy::if_same_then_else
  -D warnings)

if [ "$print_only" = 1 ]; then
  printf '%s\n' "${cmd[*]}"
  exit 0
fi
exec "${cmd[@]}"
