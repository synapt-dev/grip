# gr2

**gr2 is the workspace layer for multi-repo work.** One repository is just git; when a change spans repositories — a feature slice, a release, several agents touching several repos — gr2 gives you a workspace over them, isolated lanes to work in, and one grouped review/PR set for the slice. It is the Python successor to the Rust `gr` (1.x, called gr1 below); both ship from this repository, and `gr2` is the command for the workspace and the slice.

## Install

```bash
uv tool install --pre gitgrip    # the PyPI package is "gitgrip"; the command is "gr2"
gr2 --version
```

Without uv, a venv and pip do the same:

```bash
python3 -m venv ~/gr2-venv && source ~/gr2-venv/bin/activate
pip install --pre gitgrip
gr2 --version
```

`--pre` is required while every gr2 release is an alpha: 1.5.0 and 1.5.1 were pre-alpha builds and are yanked, so without `--pre` there is nothing to install.

Already installed 1.5.0 or 1.5.1? The install commands above leave it in place, because an installed version already satisfies them. Upgrade explicitly: `uv tool upgrade --prerelease allow gitgrip`, or `pip install --pre -U gitgrip`. To run unreleased development code instead, install from a checkout: `pip install -e gr2/` from the repository root.

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
gr2 lane show ~/ws default                                 # where am I?
gr2 lane exit ~/ws default --actor human:you
```

`exec run` and `lane exit` require `--actor`. Every group and verb takes `--help`; `gr2 --help` lists them all.

## What works today, and what is not there yet

Alpha 2 is a working local workspace layer. Verified by running the walk above:

- `workspace` (init, materialize, status, migrate-gr1 from a gr1 gripspace), `lane` (create, enter, exit, current, lease), `store` (init, commit, log, diff, checkout), `exec run`, `repo status`, `sync status`, `review create-project`.
- Lanes are independent clones: two lanes, or two agents, never share refs, an index, or a working tree.
- A lane commit that commits nothing anywhere refuses loudly instead of reporting success.

Not there yet:

- The full walk with **remote** PRs (`gr2 push`, `gr2 pr create`) is documented in `--help` but was not exercised in a clean environment for this README; treat those two as alpha.
- `gr2 --help` and some verb output still say "prototype" in places; the wording is being cleaned up as verbs land.
- The Rust `gr` (gr1) still owns `spawn`, `fleet`, and `manifest` until their gr2 ports land. Everything else — workspace sync, lanes, the multi-repo slice, its review — is gr2's job; if a verb is missing or wrong there, that is a bug worth reporting, not a reason to reach for gr1.

## Commands

| Group | Verbs |
|---|---|
| `workspace` | init, init-from-topology, materialize, status, convert-clone, detect-gr1, migrate-gr1, migrate-lane-state, bootstrap-gr1, gitinclude |
| `spec` | show, validate |
| `lane` | create, enter, exit, show, resolve, bind, lease |
| (top level) | branch, add, commit, push, prune, status, plan, apply |
| `sync` | status, run |
| `pr` | create, status, checks, merge, view |
| `check` | run, show |
| `review` | open, close, checkout-pr, run, check, bind, publish, receive, verify, show, merge, rebind, create-project |
| `exec` | status, run |
| `repo` | status, projection-run |
| `hooks` | trust, revoke, status, show, run |
| `store` | init, commit, check, push, status, log, diff, checkout, materialize, migrate, migrate-reviews |
| `target`, `config` | stored PR target; config overlays |

## Overlay substrate

gr2 also carries the config-overlay substrate (capture, compose, and materialize configuration layers over a workspace): see [docs/OVERLAY-SUBSTRATE.md](docs/OVERLAY-SUBSTRATE.md).

## Development

```bash
pip install -e "gr2[dev]"    # from the repository root
cd gr2 && pytest             # the gr2 suite (tests + overlay tests)
cd gr2 && ruff check .       # lint, same config as CI
```

The Rust development binary is **`gr2-dev`** — built by `cargo build -p gr2-cli --release`, `publish = false`, and not shipped with `cargo install gitgrip`. It answered to the name `gr2` until 2026-09-30, so a branch written before that rename goes red on `cargo test -p gr2-cli` with `CARGO_BIN_EXE_gr2 is unset`; the repair is `cargo_bin("gr2-dev")`, from which Cargo derives `CARGO_BIN_EXE_gr2-dev`.

Migration: use `gr2 review check` for compiled review requirements. `review requirements`
remains a hidden alias with a warning on stderr until beta. Both report `satisfied`
in the same JSON payload, and a missing reviewer remains a reported status with exit 0.

A bare `gr2 review open <workspace>` uses the workspace's sole review bind. Its default directory is `<workspace>.review/<sha8>` in the workspace's parent directory, and the command announces that path on stderr. Give `--lane-dir` to choose another location.

New review binds record the binder's Git `user.name` from the workspace root.
Set it with `git config user.name 'Your Name'` there if Git has no configured name.
`review show --json` and `review verify --json` include this author; older binds
remain readable without an author field, and reading them does not invent one.


## Transfer a native review

Publish an existing bound review and receive it in another native workspace:

```bash
gr2 review publish gr:<full-review-id> --remote https://example.org/workspace.git
gr2 review receive gr:<full-review-id> --remote https://example.org/workspace.git
gr2 review show gr:<full-review-id>
gr2 review verify gr:<full-review-id>
gr2 review open gr:<full-review-id> --lane-dir /absolute/fresh-review
```

The root defaults to the current workspace. Use `-C /absolute/workspace` to override
it. Transport requires an independently supplied full 40-character lowercase
review ID and an explicit HTTPS URL or absolute local remote path. It does not
infer the expected ID from the remote. A review is published at
`refs/dev.synapt.grip/__reviews__/v1/<full-review-id>`; `--ref`, when supplied, must be
exactly that, or the older `refs/dev.synapt.grip/__reviews__/<full-review-id>`. Invalid explicit input is
refused without falling back to context.

Publish transfers only that review ref and confirms its returned target. Receive
compares both the advertised and actual fetched raw target with the expected ID,
requires a commit and recomputable review content, then creates the canonical
local bind ref. A valid same-ID receive is idempotent. A conflicting existing ref
is refused. Object downloads may remain after refusal. The owned temporary ref
is removed separately, and cleanup failure is reported without replacing a
primary failure.

The review commit carries its trees, blobs and range patches. It does not make
its recorded base or original member head reachable as Git parents. Keep each
member's base reachable at its recorded remote for reconstruction. `verify`
checks content consistency, not current remote state, provenance or approval.
`open` checks reconstructed member trees. Commit identity can differ from the
original head. Receiving content grants no local approval, allocation, selection
or cleanup authority. Opening a reconstruction remains a separate operation.


## External PR adapters

External packages can expose a zero-argument factory through the
`gr2.platform_adapters` entry-point group. See [the adapter contract](docs/PR-LIFECYCLE.md#51-adapter-protocol-and-capabilities)
for registration, optional operation capabilities and merge head-pin requirements.
Select the installed adapter with `gr2 pr create ... --platform NAME` on a
materialized lane. The bound-lane path currently pushes and returns its receipt.

PRs start as drafts by default. `--draft` is explicit draft creation, while
`--no-draft` explicitly creates non-drafts. The same default applies to
`CreatePRRequest` and `create_pr_group`. Existing Python callers can retain
non-draft creation with `draft=False`. Plugins execute trusted Python from the
existing environment. The CLI never installs or downloads them.

## Checks on a plain Git remote

Run a command at an exact full commit id in a disposable checkout, then publish
its exit status to the repository's remote:

```sh
gr2 check run ./repo -- python -m pytest
gr2 check show ./repo --json
```

With no flags, the remote is the current branch's upstream (else `origin`) and the head is `HEAD`,
as a full commit id. gr2 prints what it resolved on stderr. `--remote` (a URL, an absolute path or a
remote's name), `--head` and `--name` override them. With no upstream and no `origin`, it refuses and
asks for `--remote`.

Absolute paths to local bare remotes also work. Observations live at
`refs/dev.synapt.grip/__checks__/v1`, keyed by commit id. `check show` fetches a
fresh snapshot; a different commit has no check even when its tree is identical.
Supplied `--require` names replace the default `test` requirement. For both
checks, use `--require test --require lint`.

The read JSON contains `status`, `record_id` (the head's record-set blob id or
null), `snapshot_oid`, `head`, `member_key`, `records`, and `reason`. Each
observation retains its fields and adds a SHA-256 `observation_id`.

A failed command is recorded and `check run` exits 1. Publication or execution
refusals exit 2. `check show` returns `pass`, `fail` or `absent` in its JSON;
failed or unmeasurable reads exit 2. Conflicting pass and fail observations both
survive and refuse readiness. Rerunning a check does not erase its earlier
failure at that commit. Concurrent writes union observations with bounded
compare-and-swap retries. An unmeasurable push acknowledgement is reported as
indeterminate unless a fresh remote read confirms the observation.

## Merge a bound review into plain Git remotes

```sh
gr2 review merge
```

This merges the review bound at the members' current heads, from their current branch, into the
branch on each remote that still sits at the reviewed base, and prints what it resolved. If several
branches sit there, it asks for `--into`. It merges only if every member's remote feature
branch is still the reviewed head, the target is still the reviewed base, and the exact-head check
passes. If any member fails that preflight, nothing is pushed. Merges are separate pushes, not atomic
across repos: a member that moves after an earlier member was pushed is refused, and the run exits 4.

Exit codes:
- 0: all merged;
- 3: none merged (refused);
- 4: partial or unknown.

A review id, `--from` and `--into` override the defaults. Zero or several matching binds, or members on
different branches, refuse and name the value to pass.

These records are attestations by writers trusted with remote access, not signed
proof against a writer who can forge Git objects. Test output and environment
contents are not uploaded. Read this custom ref through gr2; plain `git notes`
cannot read notes outside `refs/notes/`.
