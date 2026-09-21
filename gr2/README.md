# gr2

**gr2 is the workspace layer for multi-repo work.** One repository is just git; when a change spans repositories — a feature slice, a release, several agents touching several repos — gr2 gives you a workspace over them, isolated lanes to work in, and one grouped review/PR set for the slice. It is the Python successor to the Rust `gr` (1.x, called gr1 below); both ship from this repository, and `gr2` is the command for the workspace and the slice.

## Install

```bash
python3 -m venv ~/gr2-venv && source ~/gr2-venv/bin/activate
pip install --pre gitgrip        # the PyPI package is "gitgrip"; the command is "gr2"
gr2 --version
```

`--pre` is required while 2.x is an alpha — without it pip installs 1.5.1, which is gr1. To run unreleased development code instead, install from a checkout: `pip install -e gr2/` from the repository root.

The Rust `gr` (gr1, 1.x) installs with `brew install synapt-dev/tap/gitgrip` or `cargo install gitgrip`. The two do not collide; you can have both installed.

## First five minutes

Every command below was run, in this order, in a clean virtualenv against a throwaway workspace of two repositories. Put two clones side by side in one folder, say `~/ws/repo-a` and `~/ws/repo-b`.

```bash
cd ~/ws
gr2 workspace init ~/ws                 # scans the folder; writes .grip/workspace_spec.toml
gr2 workspace materialize ~/ws --yes    # builds the .grip layout and a default unit
gr2 workspace status ~/ws               # what kind of workspace this is
gr2 store init ~/ws                     # the workspace snapshot store (needed before review)
```

A **unit** is one worker — you, or one agent — and `default` is created for you. A **lane** is one piece of work spanning repos, with its own clones, so two lanes never step on each other:

```bash
gr2 lane create ~/ws default feat-x --repos repo-a,repo-b --branch feat/x
gr2 lane enter  ~/ws default feat-x --actor human:you
```

Both commands print, per repo, the absolute directory you work in:

```
repo-a: ~/ws/.grip/state/lanes/default/feat-x/repos/repo-a
repo-b: ~/ws/.grip/state/lanes/default/feat-x/repos/repo-b
```

Edit there — not in your original clones. Stage and commit across the lane's repos:

```bash
gr2 add . --repo-path ~/ws/.grip/state/lanes/default/feat-x/repos/repo-a
gr2 commit -m "my change" --workspace-root ~/ws --owner-unit default
```

`commit` with the lane flags commits every lane repo that has staged changes and names the repos it skipped. If every repo was skipped it exits non-zero with one sentence naming where it looked — and, when it can see them, where your staged changes actually are. Run one command across every lane repo, then turn the lane into one reviewable object:

```bash
gr2 exec run ~/ws default --actor human:you -- pytest -q   # one command, every lane repo
gr2 review create-project ~/ws default feat-x              # prints gr:<sha>: one object to review
gr2 lane current ~/ws default                              # where am I?
gr2 lane exit ~/ws default --actor human:you
```

`exec run` and `lane exit` require `--actor`. Every group and verb takes `--help`; `gr2 --help` lists them all.

## What works today, and what is not there yet

Alpha 2 is a working local workspace layer. Verified by running the walk above:

- `workspace` (init, materialize, status, migrate-gr1 from a gr1 gripspace), `lane` (create, enter, exit, current, lease), `store` (init, snapshot, log, diff, checkout), `exec run`, `repo status`, `sync status`, `review create-project`.
- Lanes are independent clones: two lanes, or two agents, never share refs, an index, or a working tree.
- A lane commit that commits nothing anywhere refuses loudly instead of reporting success.

Not there yet:

- The full walk with **remote** PRs (`gr2 push`, `gr2 pr create`) is documented in `--help` but was not exercised in a clean environment for this README; treat those two as alpha.
- `gr2 --help` and some verb output still say "prototype" in places; the wording is being cleaned up as verbs land.
- The Rust `gr` (gr1) still owns `spawn`, `fleet`, and `manifest` until their gr2 ports land. Everything else — workspace sync, lanes, the multi-repo slice, its review — is gr2's job; if a verb is missing or wrong there, that is a bug worth reporting, not a reason to reach for gr1.

## Commands

| Group | Verbs |
|---|---|
| `workspace` | init, init-from-topology, materialize, status, convert-clone, detect-gr1, migrate-gr1, migrate-lane-state, bootstrap-gr1 |
| `spec` | show, validate |
| `lane` | create, enter, exit, current, resolve, bind, lease |
| (top level) | branch, add, commit, push, prune |
| `sync` | status, run |
| `pr` | create, status, checks, merge |
| `review` | open, close, checkout-pr, run, requirements, bind, verify, rebind, create-project |
| `exec` | status, run |
| `repo` | status, hooks, hook-run, projection-run |
| `store` | init, snapshot, log, diff, checkout |
| `target`, `config`, `plan`, `apply` | stored PR target; config overlays; show/apply the materialization plan |

## Overlay substrate

gr2 also carries the config-overlay substrate (capture, compose, and materialize configuration layers over a workspace): see [docs/OVERLAY-SUBSTRATE.md](docs/OVERLAY-SUBSTRATE.md).

## Development

```bash
pip install -e "gr2[dev]"    # from the repository root
cd gr2 && pytest             # the gr2 suite (tests + overlay tests)
cd gr2 && ruff check .       # lint, same config as CI
```
