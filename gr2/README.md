# gr2

**gr2 is the workspace layer for multi-repo work.** One repository is just git; when a change spans repositories — a feature slice, a release, several agents touching several repos — gr2 gives you a workspace over them, isolated lanes to work in, and one grouped review/PR set for the slice. It is the Python successor to the Rust `gr` (1.x, called gr1 below); both ship from this repository, and `gr2` is the command for the workspace and the slice.

## Install

```bash
uv tool install --compile-bytecode --pre gitgrip    # the PyPI package is "gitgrip"; it installs `gr` and `gr2`
gr2 --version
```

`--compile-bytecode` writes the `.pyc` files at install time; without it uv leaves them to the first `gr2` call, which then takes about a third of a second longer. pip compiles at install on its own.

Without uv, a venv and pip do the same:

```bash
python3 -m venv ~/gr2-venv && source ~/gr2-venv/bin/activate
pip install --pre gitgrip
gr2 --version
```

`--pre` is required while every gr2 release is an alpha: 1.5.0 and 1.5.1 were pre-alpha builds and are yanked, so without `--pre` there is nothing to install.

Already installed 1.5.0 or 1.5.1? The install commands above leave it in place, because an installed version already satisfies them. Upgrade explicitly: `uv tool upgrade --prerelease allow gitgrip`, or `pip install --pre -U gitgrip`. To run unreleased development code instead, install from a checkout: `pip install -e gr2/` from the repository root.

The package installs two commands. `gr2` always runs gr2. `gr` picks by the nearest workspace marker: in a gr1 workspace (`.gitgrip`) it runs gr1, and anywhere else, including outside any workspace, it runs gr2.

The Rust `gr` (gr1, 1.x) installs with `brew install synapt-dev/tap/gitgrip` or `cargo install gitgrip`, as `gr` and `gitgrip`. You can have both installed. Whichever `gr` comes first on `PATH` answers: gitgrip 1.5 and earlier always run gr1, so if it comes first, type `gr2` for gr2 (`gr2` says so once). In a gr1 workspace, this package's `gr` runs gr1 through `gitgrip`. With no gr1 installed, it serves a workspace already moved with `gr2 workspace migrate-gr1`, and in one not yet moved it says how to move it.

## First five minutes

Every command below was run, in this order, in a clean virtualenv against a throwaway workspace of two repositories. Put two clones side by side in one folder, say `~/ws/repo-a` and `~/ws/repo-b`.

```bash
cd ~/ws
gr2 workspace init ~/ws                 # scans the folder; writes .grip/workspace_spec.toml
gr2 workspace materialize ~/ws --yes    # builds the .grip layout and a default unit
gr2 workspace status ~/ws               # what kind of workspace this is
gr2 store init ~/ws                     # the workspace snapshot store (needed before review)
gr2 store commit -m "first snapshot"    # record the workspace once; lanes start from a recorded snapshot
```

`store commit` reads the workspace from the current directory, which is why the walk starts with `cd ~/ws`; from anywhere else, name it: `gr2 store commit -C ~/ws -m "..."`. It prints nothing on success; each repo it records for the first time gets one note on stderr.

For a full-mode workspace, where each member is recorded as a field-commit (the skill's Recipe 2), make the store with `gr2 store init --mode full ~/ws` in place of the `store init` line above. A store keeps the mode it was made with: `--mode` on an existing store that declares another mode is refused (exit 4).

A **unit** is one worker — you, or one agent — and `default` is created for you. A **lane** is one piece of work spanning repos, with its own clones, so two lanes never step on each other:

```bash
gr2 lane create ~/ws default feat-x --repos repo-a,repo-b --branch feat/x
gr2 lane enter  ~/ws default feat-x --actor human:you
```

`lane create` prints the lane's record, then, per repo, the absolute directory you work in (`lane enter` prints the same directories as `repo_paths`):

```
~/ws/.grip/state/lanes/default/feat-x/lane.toml
repo-a: ~/ws/agents/default/lanes/feat-x/repo-a
repo-b: ~/ws/agents/default/lanes/feat-x/repo-b
```

Edit there — not in your original clones. Stage and commit across the lane's repos:

```bash
gr2 add . --repo-path ~/ws/agents/default/lanes/feat-x/repo-a
gr2 commit -m "my change" --workspace-root ~/ws --owner-unit default
```

`commit` with the lane flags commits every lane repo that has staged changes and names the repos it skipped. If every repo was skipped it exits non-zero with one sentence naming where it looked — and, when it can see them, where your staged changes actually are. Run one command across every lane repo, then turn the lane into one reviewable object:

```bash
gr2 exec run ~/ws default --actor human:you -- git status --short   # one command, every lane repo
gr2 review create-project ~/ws default feat-x              # prints gr:<sha>: one object to review
gr2 lane push ~/ws default feat-x                          # push every lane repo, each verified on its remote
gr2 lane show ~/ws default                                 # where am I?
gr2 lane exit ~/ws default --actor human:you
gr2 lane list ~/ws                                         # each lane: removable, or keep and why
gr2 lane remove ~/ws default feat-x                        # end it: its clones and record go
```

`lane remove` refuses while the lane is entered or leased, or was made by `lane bind`, while any of its repos holds uncommitted work, a stash, or a commit no remote has (on any branch or tag), or while its checkout holds anything that is not one of its repos; it names each one and removes nothing. `exec run` runs whatever follows `--` in each lane repo, with your own `PATH`: a test runner works there only if it is installed where your shell finds it. `review create-project` refuses a lane repo holding any uncommitted or untracked file, so whatever a command leaves behind (`__pycache__/` after a pytest run, say) has to be in the repo's `.gitignore`. `exec run` and `lane exit` require `--actor`. Every group and verb takes `--help` or `-h`, and a bare group prints its help. `gr2 --help` lists the everyday commands; `gr2 help -a` adds the plumbing that scripts, CI and other gr2 verbs call, each of which still runs by its full name.

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
| `workspace` | init, init-from-topology, materialize, status, convert-clone, detect-gr1, migrate-gr1, retire-gr1, migrate-lane-state, bootstrap-gr1, gitinclude |
| `spec` | show, validate |
| `lane` | create, enter, exit, show, resolve, bind, lease, list, push, remove |
| (top level) | branch, add, commit, change, push, prune, status, plan, apply, update |
| `sync` | status, run, wait |
| `pr` | create, status, checks, merge, view |
| `check` | run, show, set |
| `proto` | json |
| `review` | open, close, checkout-pr, read-pr, run, check, pin, publish, receive, verify, show, stamp, merge, update, repin, create-project, bind, approve, rebind, stamps, merged, blockers, list |
| `exec` | status, run |
| `repo` | status, projection-run |
| `hooks` | trust, revoke, status, show, run |
| `store` | init, commit, check, push, status, log, diff, checkout, materialize, migrate, migrate-reviews |
| `target`, `config` | stored PR target; config overlays |

`gr2 change` commits and branches every changed member in one command, at the same safety as git itself. It refuses before any write when a member cannot take the change, and on a failure or an interrupt it undoes the members it touched, in reverse order.

Every `store` verb names its workspace with `-C/--root <root>`, else reads the current directory.

`review bind`, `review approve` and `review rebind` remain as aliases of `pin`, `stamp` and `repin`.

The words these commands use (lane, pin, stamp, repin, gate, review) are defined in [docs/GLOSSARY.md](docs/GLOSSARY.md).

## Stamps (unsigned approvals)

Inside a workspace with one pinned review matching every member's current head,
a reviewer stamps it as the workspace's Git `user.name`:

```bash
gr2 review stamp
gr2 review merge
```

A reviewer whose own workspace does not hold the reviewed repos (another team's
gripspace) receives the review, opens a lane, and stamps from the lane (or from one
repo directory in it). The stamp uses the store of the workspace the lane was opened
from and the repos checked out in the lane, and records the git `user.name` of that
workspace, not of the lane:

```bash
gr2 review receive <their-workspace> gr:<id> --remote <member-remote>
gr2 review open <their-workspace> gr:<id> --lane-dir <lane>
gr2 review stamp <lane>
```

The author recorded by the pin, and anyone who authored a commit in its range,
cannot stamp it. A stamp records an approval; approver names are self-declared and receipts say `signed: false`.
Repeated stamps by the same name count once. A moved head needs a new pin
and fresh stamps. Signing is not implemented by these commands. `review stamps` still counts
stamps on a pin whose feature branch has moved, and says so per member (`moved_since_pin`, `null` when
unreadable) with one stderr line, since `blockers` and `merge` will refuse it.

The workspace's `grip.toml` sets the number required before merging:

```toml
[approvals]
required = 2
```

The default is zero; `review merge --approvals N` can raise the count for that
run, but cannot lower the workspace policy. With zero required, the merge gate
skips approval counting. When approvals are required, broken records or different
chain tips across member remotes refuse before this run pushes a merge. After a refused stamp, rerun
`review stamp` to read the current chain and retry with a lease.

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

A bare `gr2 review open <workspace>` uses the workspace's sole pinned review. Its default directory is `<workspace>.review/<sha8>` in the workspace's parent directory, and the command announces that path on stderr. Give `--lane-dir` to choose another location.

New pinned reviews record the author's Git `user.name` from the workspace root.
Set it with `git config user.name 'Your Name'` there if Git has no configured name.
`review show --json` and `review verify --json` include this author; older pinned reviews
remain readable without an author field, and reading them does not invent one.

## Run a review's tests

`gr2 review run` runs the reviewed tree's tests inside the review's lane, and only after it proves the lane equals the pinned tree. What to install and what to test comes from the reviewed repo itself: a tracked `.review-install` file at the repo root, read whenever the flags are left out. For a Python repo tested with pytest it needs two lines: the package the run must import from inside the lane, and an install that brings pytest:

```
# .review-install
package = mypkg
install = {venv} -m pip install -e {lane} pytest
```

`{venv}` is the lane venv's Python executable, not its directory, and `{lane}` is the lane's checkout of the repo; both are filled in per token. Without an `install` line the run uses `<lane venv python> -m pip install -e <lane>`, which installs your package and no test runner, so a pytest repo without the line refuses (`pytest_not_installed`). Either way the repo must be pip-installable (a `pyproject.toml` or `setup.py` declaring that package). For another language, declare the runner and its command instead, for example `runner = cargo` and `test = cargo test`. The recognised keys are `install`, `package`, `runner`, `test` and `reports`; any other key is refused (`bad_hint`). A repo with no `.review-install` must pass `--package` and/or `--install`, and with neither the run refuses (`no_package`). `gr2 review run --help` has the full list.


## Transfer a native review

Publish an existing pinned review and receive it in another native workspace:

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
local review ref. A valid same-ID receive is idempotent. A conflicting existing ref
is refused. Object downloads may remain after refusal. The owned temporary ref
is removed separately, and cleanup failure is reported without replacing a
primary failure.

Receive also fetches each member's pinned head from the remote it read the review
from, one `heads` row per member. A member whose recorded remote is another one
(a review published to a staging remote) is `not_this_remote`. `gr2 review open`
builds the lane from the range the review carries, and the row's `next` names that
command. `gr2 review show --diff` and `gr2 review stamp` still need the head: they
refuse until the author pushes it to the member's own remote and it is fetched.

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
What changed in that protocol, release by release, ships in the package: read it from
an install with `importlib.resources.files("gr2") / "PLATFORM_ADAPTER_CHANGELOG.md"`,
or in this repository at [gr2/PLATFORM_ADAPTER_CHANGELOG.md](gr2/PLATFORM_ADAPTER_CHANGELOG.md).
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

A review over several members is checked as one set. `gr2 check set <workspace> <review> -- <command>`
reconstructs every member at its pinned head side by side (`GR2_SET_ROOT`, `GR2_REVIEW_ID` and `GR2_SET_MEMBERS`
name them), runs the command once, and publishes one `set` observation per member remote, carrying the review and the
identity of the set. With no `--check`, `gr2 review merge` on a multi-member review requires that record AND each
member's `test` at its head: a member whose recorded `test` is red is refused and nothing lands, and a member with no
`test` record of its own is decided by the passing set (members that import each other cannot pass a test alone), so
one `check set` and the approvals are enough to merge. `--check test` names `test` explicitly and only a `test` record
decides it (one that `check set --name test` wrote for this review counts); its refusal prints that `check set`. On a
one-member review the default `test` is also decided by a passing `set` record for this review at that head. A `set`
record for another review or other heads never counts. The requirement comes from the
review, not from the person merging: on a multi-member review `--no-set` is refused, and so is any `--check` name other
than `test` and `set`. `review pin` refuses a multi-member review whose rows do not each carry a `source` or a `range`,
because `check set` could never run on it. `check show --require set --review <id>` reads a `set` record for another
review as stale.

A failed command is recorded and `check run` exits 1. Publication or execution
refusals exit 2. `check show` returns `pass`, `fail` or `absent` in its JSON;
failed or unmeasurable reads exit 2. Conflicting pass and fail observations both
survive and refuse readiness. Rerunning a check does not erase its earlier
failure at that commit. Concurrent writes union observations with bounded
compare-and-swap retries. An unmeasurable push acknowledgement is reported as
indeterminate unless a fresh remote read confirms the observation.

## Merge a pinned review into plain Git remotes

```sh
gr2 review merge
```

This merges the review pinned at the members' current heads, from their current branch, into the
branch on each remote that still sits at the reviewed base, and prints what it resolved. If several
branches sit there, it asks for `--into`. It merges only if every member's remote feature
branch is still the reviewed head, the target is still the reviewed base, and the exact-head check
passes. If any member fails that preflight, nothing is pushed. Merges are separate pushes, not atomic
across repos: a member that moves after an earlier member was pushed is refused, and the run exits 4.

Exit codes:
- 0: all merged;
- 3: none merged (refused);
- 4: partial or unknown.

A review id, `--from` and `--into` override the defaults. Zero or several matching pinned reviews, or members on
different branches, refuse and name the value to pass.

These records are attestations by writers trusted with remote access, not signed
proof against a writer who can forge Git objects. Test output and environment
contents are not uploaded. Read this custom ref through gr2; plain `git notes`
cannot read notes outside `refs/notes/`.
