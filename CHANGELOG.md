# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`gr spawn up` interactive fallback.** When tmux is not installed (native
  Windows, a bare container) or `--interactive` is passed, one named agent runs
  in the foreground of the current terminal with the same command, cwd, env, and
  startup prompt a tmux pane would use. `gr spawn up <agent>` also accepts the
  agent name positionally. The foreground launch runs the agent through `bash`,
  so bash must be on PATH (on Windows: run from Git Bash or WSL); with neither
  tmux nor bash available the verb refuses and names bash. A whole-fleet
  `gr spawn up` without a multiplexer — and `--interactive` with no agent named —
  refuses and names the single-agent form. Unix tmux behaviour is unchanged: the
  launch command is now built by one shared path used by both the pane and the
  foreground launch.
- **`gr2`: the platform adapter protocol's changelog ships in the package.** The
  wheel and sdist carry `gr2/PLATFORM_ADAPTER_CHANGELOG.md`, readable from an
  install with `importlib.resources.files("gr2")`. It lists, release by release
  since 2.0.0a1, each method, keyword, field and registration rule an
  out-of-tree adapter implements or receives, and a test fails when the protocol
  gains one with no entry.

### Changed

- **`gr2` runs every git command in its workspace and review modules through
  one shared helper.** Each such call carries `-c maintenance.auto=false -c
  gc.auto=0`, so none starts background maintenance in your repository. A clone
  is never bounded; every other call has a 600-second default, including calls
  that had none before (an expiry is exit 124). Lane workspace cloning loses its
  previous 600-second bound. A merge-tree timeout in `gr2 review merge` is still
  reported as `merge_build_failed`.

### Fixed

- **`gr pr create` opens PRs for the branch you are on, not for every branch
  some repo happens to sit on.** It used to open one PR per branch group under
  the single `-t` title, so a repo (the manifest included) parked on an old
  branch with a commit ahead got a stray PR titled after an unrelated feature.
  Run from inside a repo, it now opens PRs only for repos on that repo's branch
  and lists the others (`not included: X is on 'B', not this branch; pass
  --repo X to include it`). Run from the workspace root with repos on different
  branches, it refuses and names each group. Repos named with `--repo` open
  regardless of branch, and `--repo manifest` now selects the manifest. A
  manifest with uncommitted changes but no commits ahead is no longer PR
  content.

## [gr2 2.0.0a7] - 2026-10-08

### Added

- **`gr2` workspace verbs take the root as `--root/-C`, and the 21 with a fixed number of arguments
  let you leave it out.** Every verb that took `WORKSPACE_ROOT` as its first required argument now
  also accepts `-C <root>` (or `--root <root>`) anywhere on the line; giving the root both ways is
  refused. For the 21 verbs whose argument list has a fixed length (`lane create/enter/exit/resolve/
  show/bind`, `lane lease acquire/release/show`, `review requirements/checkout-pr/create-project`,
  `repo projection-run`, `hooks run`, `config restore`, and the hidden aliases), leaving the
  root out now means the nearest workspace above the current directory: `gr2 lane create default
  feat-x --repos app --branch feat/x` works from the workspace root or any directory below it, and
  from outside any workspace it refuses and names `-C`. The verbs that end in an optional argument
  take `-C`; `exec status` and `exec run` are never inferred, because one word fewer reads two ways
  (`exec status unit lane` is both "root left out" and "root is `unit`"), and when one of them reads
  its first word as a root that is not a directory, or reports a missing argument, the error says to
  pass the root first or name it with `-C <root>`. The pr verbs and `review open` infer the workspace
  in the cases described below, as do `review show` and `review verify`. The explicit form,
  `gr2 <verb> ROOT ...`, is unchanged for every verb.

- **`gr2` emits `review.*` events.** `review bind`, `open`, `verify`, `run` and `close` each
  write an event to the workspace outbox (`review.bound`, `review.opened`, `review.verified`,
  `review.run_completed` / `review.run_refused`, `review.closed`), so a timeline or a hook
  consumer sees reviews the way it sees lanes and PRs. A reconstruction lane's marker now
  records the workspace it was opened from, which is where `run` and `close` write; a lane
  opened by an earlier version emits no run or close events.

- `gr2 pr create`, `pr status`, `pr merge`, `pr checks` and `pr view` take the current review as their subject when given no positional argument. The workspace is the one above the current directory, and the review is the workspace's only review bind, or the one named with `--review gr:<sha>` (zero or several binds are refused by name; `--review` beside a unit is refused). Any positional keeps the existing `ROOT UNIT [LANE]` grammar. Inside a reconstruction lane (`review open`), bare `pr checks` and `pr view` read the PR group marked for that reconstruction, even when the lane sits beneath another workspace or the author workspace has since acquired another bind; explicit root, unit and `--review` selectors still win, and a malformed marker refuses. `pr create` opens one PR per member, only for the reviewed bytes: each member's checkout must be at its reviewed commit and the branch the PR opens from must be at that commit on the remote (read with a 30 second bound, and only when git resolves the remote to the same github.com repository the PR opens on). A branch that is behind, ahead or not pushed is refused by name, as is a second PR group for the same review. `pr merge` pins every member to its reviewed commit unless `--match-head-commit` names other pins, and uses the workspace's configured merge method when it has one. Review-mode pr verbs support github.com remotes only.

- `gr2 check run` and `gr2 check show` record and read exact-head checks through an ordinary Git remote. `check run -- <cmd>` runs the command in a disposable checkout, verifies HEAD before and after, and publishes its observation at `refs/dev.synapt.grip/__checks__/v1`; `check show` fetches a fresh snapshot and returns `pass`, `fail` or `absent` for the required check names (default `test`, repeat `--require`). A different commit is `absent` even when its tree is identical. Concurrent writers keep each other's observations; conflicting pass and fail observations both survive and refuse readiness. `check run` exits 1 when the command fails and 2 when it cannot execute or publish; `check show` exits 2 on a failed or unmeasurable read. No test output or environment contents are uploaded, and the records are attestations by writers trusted with remote access, not signed proofs.

- `gr2 review merge [REVIEW] [--from BRANCH] [--into BRANCH] [--check NAME]` merges a bound review into plain Git remotes with no hosting platform. It verifies the bind locally, then requires for every member the review record on the remote, the feature branch still at the reviewed head, the target branch still at the reviewed base, and a passing exact-head check (default name `test`). If any member fails that preflight nothing is pushed; otherwise it builds every `--no-ff` merge first and pushes each with a lease on its base, re-reading each feature head just before its push. Each member must be its own Git worktree inside the workspace, with its git directory, objects and refs inside the workspace too; a `.git` pointing elsewhere or symlinked object, ref or log paths are refused. Fetches and pushes run with automatic maintenance disabled. The receipt is always JSON, and the exit status comes from the remote state after the run: 0 every member merged, 3 none merged (refused), 4 partial or unknown. A push whose acknowledgement is lost is re-read rather than assumed, and the pushes are separate, so the merge is not atomic across repositories.

- `gr2 store migrate-reviews ROOT [--json]` gives each legacy review bind a field tree twin at the versioned ref. Legacy refs, commits and trees are left as they are, the twin keeps the legacy commit's author, committer, dates and message so a rerun gives the same id (reported as `present`), and a bind is converted only when it verifies as written and its twin passes every rule the readers apply. A refused bind is reported with its reason and the run continues; a receipt under `.grip/receipts/` records every row, and the command exits 4 when any bind was refused.

- The check and merge-receipt record formats ship as offline protobuf schema packages (`dev.synapt.grip.check.v1alpha1`, `dev.synapt.grip.merge.v1alpha1`) alongside the review and approval ones, pinned by hash. Existing JSON formats, refs and CLI behaviour are unchanged.

- `gr2 review publish` and `gr2 review receive` move a native review bind between workspaces. `publish gr:<full-id> --remote <dest>` pushes the bind's review ref to an https URL or an absolute local path and checks that the remote now holds that exact id. `receive gr:<full-id> --remote <source>` takes the expected full commit id from you (it is never supplied by a marker), checks that the remote advertises that ref at that id, fetches the commit, checks its schema and content, and only then creates the local review ref; `show`, `verify` and the listing refuse a ref whose target does not match its id. Both verbs are marked may-change in the API stability file, and `publish` is not yet meant for shared or public remotes.

- A new native workspace root (`store init`) now records a `.gitinclude` declaration (seeded with `grip.toml` and the member paths) and generates a self-excluding `.gitignore` from it, so the root is ignore-by-default and tracks only what is declared. `store commit` refuses at 4 a path the declaration does not include (`<path> is not included by .gitinclude; update the declaration before store commit`) or one Git ignores, and `store checkout` regenerates `.gitignore` from the selected commit's declaration. Child `.gitignore` files inside an included directory keep their normal effect. An existing root without a `.gitinclude` keeps its owner's `.gitignore` untouched.

- A `gr` resolver module decides which half answers a command: run it with
  `python -m gr2.python_cli.gr_resolver`. The nearest marker above the current directory
  decides: a `.gitgrip` entry means gr1; `grip.toml` beside a `.git`, or a workspace spec under the grip
  directory, means gr2; no marker means gr2. A directory holding both goes to gr1 and prints one stderr line
  saying it also holds a gr2 workspace. The chosen half is run from `PATH` with `GR_RESOLVED` set; a second
  resolution refuses with exit 70, and a half that is not installed refuses in one sentence with exit 69.
  `GR2_QUIET_CONTEXT` silences only the informational line, never a refusal. The same table also has a Rust
  implementation in the gr1 crate (a library, an example and conformance tests over the shared table); it is not
  wired to the `gr` binary yet, so installing gr1 adds no resolver either. Invoked under the name `gr1` or
  `gr2` it runs that half and resolves nothing. `--which` prints `<kind> <marker> <binary>` and names any other
  `gr` on `PATH` on stderr. The package declares no `gr` console script, so installing it adds no `gr` to
  `PATH`; the entry point ships separately.

- The `gr` resolver table gains the gr1-only install case: with no workspace marker and gr2 not installed, the Rust
  half runs gr1 and prints one line naming the install (`pip install --pre gitgrip`); a gr2 workspace with gr2
  missing still refuses with exit 69. `gr2` now clears `GR_RESOLVED` when it starts, so a process it runs can run
  `gr` again instead of being refused as a second resolution.

- `gr2 review run` reads each member's install order through ecosystem plugins. The built-in Python plugin
  reads `[project].dependencies` as before, and any `grip-ecosystem-<name>` executable on your own `PATH` may
  claim members of other ecosystems and order them. Plugins are read from `PATH` only, never from a member's
  directory, so the change under review cannot choose its own install order. A plugin that fails (crash,
  non-zero exit, timeout, invalid JSON, an answer over the byte bound, or a unit id that does not start with
  `<plugin>:`) refuses the lane as `plugin_failure`, naming the plugin; it never falls back to the marker order.
  The request reaches a plugin through a temporary file rather than a pipe, and a plugin that never stops writing
  is killed, with its child processes, as soon as its answer passes the bound. A dependency cycle between
  members still refuses as `dependency_cycle` and prints the loop in member names; `--order` still overrides.
  A reference Cargo plugin ships in `gr2/examples/grip-ecosystem-cargo`.

### Changed

- New gr2 review binds record the binder's configured Git name. Show and verify
  expose it; older records remain readable with their original ids. The review
  schema adds author at field 5, and the unsigned approval schema and descriptors
  ship as pinned offline resources.

- gr2 PR creation defaults to draft at the request, group and CLI layers. Use
  `--no-draft` or Python `draft=False` for explicit non-draft creation. Body-only
  sibling edits do not publish drafts.

- External platform factories can register in-process or use the
  `gr2.platform_adapters` entry-point group. Only the selected plugin is loaded.
  Duplicate names and plugin errors refuse clearly, with built-in GitHub names reserved.

- Platform adapter contract version 1 documents `pr_view`, `edit_pr_body`,
  `PRStatus.head_oid` and keyword `merge_pr(expected_head=...)`. Older adapters
  lacking merge-pin support must upgrade rather than drop pins. Single-member
  create-only adapters remain supported, while multi-member groups preflight
  body-edit capability before any creation. Provider review votes are display
  evidence, not inputs to the current local review-requirement check.

- `gr2 review check` replaces `review requirements` for compiled reviewer requirements. The old spelling
  is a hidden alias with a warning on stderr, registered for removal at beta. Both keep the same
  payload, root inference and exit status, including exit 0 with `satisfied: false`.

- **`gr2 lane create` takes the repos and the branch from the workspace when you leave them out.** With no `--repos`
  it makes the lane over every repo the workspace declares (the spec's repos) and says how many clones that is; with no `--branch` the branch is the lane name, verbatim.
  Each value gr2 filled in is one stderr line (`gr2: repos=a,b (every repo of the workspace spec; this makes 2
  clones)`, `gr2: branch=demo (the lane name)`), hushed by `GR2_QUIET_CONTEXT=1`; a typed value wins and prints
  nothing. A defaulted branch is checked against each repo's remote under ONE 20 s budget: when `demo` already
  exists there at a commit other than the tip the lane forks from, `lane create` refuses and names both ways out
  (`--branch demo` to continue that work, or another lane name); a remote that cannot be asked is named as not
  checked and the lane is still made (that line is printed even when `GR2_QUIET_CONTEXT` is set, because a safety
  check that did not run is not information to hush). A branch left over from earlier, already-merged work sits at
  an older commit than the tip the lane forks from and reads as pushed work too; pass `--branch <name>` to reuse it
  or pick another lane name. If a workspace file cannot be read, a defaulted branch is refused (name it with `--branch`),
  because the check cannot be made; a typed `--repos` entry the workspace files do not declare is named as not checked. `lane create` on a root that has no workspace spec (a root made by `store init` holds
  only `grip.toml`) writes the spec from its members, announces that step, and builds the lane. If creation refuses
  before a usable lane exists, it rolls back the spec this call wrote. With neither a spec nor members it still
  refuses before writing anything and names `gr2 workspace init`. `--repos` stays
  required with `--bind`.

- **`gr2 add` can stage a whole lane, and will not stage the workspace root by accident.** `gr2 add --lane .` stages
  in every repo of the entered lane (the unit and lane are read from where you are and named on stderr).
  `gr2 add .` inside a lane's repo still stages that repo only. Standing at the workspace root while a lane is
  entered, `add` without `--lane` or `--repo-path` now refuses (exit 2) and says how to stage the lane, one repo,
  or the root on purpose (`--repo-path .`); with no lane entered it behaves as before.

- **`gr2 review` takes the bind and the directory from the workspace when you leave them out, and `review show` reads
  a bind without opening it.** `review bind` with no row given binds every member whose checkout is ahead of its pin
  (`--members a,b` narrows; a member with no pin is named as not bound; it refuses when no member is ahead) and prints
  the rows chosen on stderr BEFORE it binds anything. Behind or unrelated checkouts are named and skipped.
  Typed `--title`, `--body`, `--path` and `--ref` require an explicit row and are refused during inference,
  including explicitly empty text and an explicitly typed default ref. `review open` with no target opens the workspace's one review
  bind, saying so on stderr; several binds are listed and none is chosen, none is refused naming `review bind`.
  `review open gr:<sha>` with no `--lane-dir` reconstructs into `<workspace>.review/<first 8 of the sha>` beside the
  workspace, named in full on stderr, and an existing directory is refused rather than reused; `--enter` is accepted
  and implied. `gr2 review show <workspace> <gr:sha>` (read-only, `--json`) prints each member's repository, base and
  head, title, body and the files its range changes, and refuses a commit that is not a bind the way `verify` does.

- **`gr2 lane show`, `lane enter`, `lane exit`, `lane resolve` and `lane create` no longer make you type what the
  workspace already holds.** Inside a workspace the root is found from the current directory, and the unit from
  the lane you have entered (or the workspace's only unit); each value gr2 filled in is named on stderr in one line
  with where it came from (`gr2: unit=default (the only unit with an entered lane)`; the root is named only when it
  is not the current directory), and `lane show --json` carries the same pairs under `context`. Set
  `GR2_QUIET_CONTEXT=1` to silence the lines. `--unit NAME` names the unit; a word that is both a workspace and a
  unit is refused with both readings spelled out (`-C <word>` for the root, `--unit <word>` for the unit). Calls
  that name everything behave exactly as before.

- **The actor of a lane verb is never made up.** `--actor` is now optional on `lane enter`, `lane exit` and
  `lane resolve` and on the hidden `review exit-gr`: when it is left out gr2 reads `GR2_ACTOR` (set it to the label
  your session should carry, for example `agent:atlas`), and at a terminal falls back to `human:<git user.name>`;
  otherwise it refuses, exit 4, in one sentence. `review exit-gr` no longer defaults to `agent:cli`. An event whose
  actor was filled in carries `actor_source`, so it cannot be read as a typed name.

- **`gr2` review binds live in the workspace root's own `.git`, under `refs/dev.synapt.grip/__reviews__/`, and
  no second repo is made.** On a native root (`store init`) a `review bind` used to create
  `<root>/.grip/.git` on first use and write the bind there. The bind is now a parentless commit in the
  root's `.git`, published as the last step by a create-only ref, so a refused bind leaves no ref and
  nothing under `.grip/`; it does not appear in `git log`, `git branch`, `git tag` or `git status`
  (it does in `git log --all` and `git for-each-ref`), and it is not carried by `git push`,
  `git fetch` or `git clone` (it IS carried by `git push --mirror` and `git clone --mirror`, which copy
  every ref: a bind holds the frozen range of a head that is not on any remote yet, so do not mirror
  a workspace root to a remote you would not publish that range to). Newly written binds use the field tree
  layout at `__reviews__/v1/<id>` (below). A bind that an older gr2 left in `.grip/.git` moves into refs
  automatically the first time a bind-touching verb runs, keeping its id and its tree, after each one is checked against the old
  store; the old store is renamed `.grip/legacy-store.git`, and the move prints one line saying what
  moved. `store init` now adds `/.grip/` to the root's own `.git/info/exclude`, since a bind no longer
  does. A commit that has no `refs/dev.synapt.grip/__reviews__/` ref is refused as not bound. `workspace init`
  no longer makes `.grip/.git`, and `review bind` / `review create-project` on a workspace root with no
  store set up the native store themselves (`store init`'s own, with its own refusals) and say so on
  stderr, so there is no `store init` step to remember. A `.grip/.git` that holds no bind and no
  snapshot counts as no store and is moved aside to `.grip/legacy-store.git`, with one line. A
  `.grip/.git` with real alpha state (a snapshot or a bind) is refused with `alpha_root` naming
  `gr2 store migrate`, which can legitimately refuse, and nothing is written. **Breaking:** an id is a
  bound review only if it has its ref, and `workspace init` leaves no `.grip/.git`.

- **`gr2 review run` derives a multi-repo lane's install order from the members' own declared
  dependencies.** A member installs after every other member of the lane whose distribution
  name appears in its `[project].dependencies` (names compared after PEP 503 normalisation).
  Among members that are ready, the lane marker's order (sorted by member key) still decides,
  so a lane whose members do not depend on each other runs in the order it always did. Before,
  the order was the marker's alone unless `--order` named one, so a member that depends on
  another installed correctly only when its key sorted after the other's. `--order KEY,KEY`
  stays the explicit override and bypasses the derivation. Two new refusals, both raised before
  any venv exists and naming the members: `dependency_cycle` (the loop is printed) and
  `member_name_clash` (two members declare one distribution). A member with no readable
  `pyproject.toml`, or one that is not a `[project]` table with a list of requirement strings,
  declares nothing and keeps its marker position, and only
  `[project].dependencies` counts, not optional dependencies. The receipt gains `order_source`
  (`dependencies` or `explicit`, null for a refusal before the order is chosen); the printed `order:`
  line is unchanged.

- New review binds are written as field tree records (one Git tree entry per protobuf field), verified as written before any ref exists, and published create-only at `refs/dev.synapt.grip/__reviews__/v1/<id>` instead of the unversioned ref. Local review readers (`review show`, `verify`, `run`, `open`) read both that layout and the older named-subtree layout, at either ref spelling. Transport is stricter: `review publish` and `review receive` require a field tree under the v1 spelling and refuse an older-layout record there; under the legacy spelling either layout is accepted. A bind carrying both spellings is listed once, and a nested review ref of a version this release does not know is skipped with a warning on stderr instead of failing every command that lists binds. A valid spelling never masks another spelling that names a different commit. `review receive` with no `--ref` looks up the v1 ref first and tries the legacy ref only when v1 is absent; a fault, a malformed answer or a target mismatch on v1 refuses rather than falling back, and `--ref` accepts either spelling and keeps the one given. `review verify --json` on a field tree with an entry it may not hold exits 2 with the entry named and prints nothing on stdout (a legacy-layout bind with the same drift still prints `"tree_matches": false`). A remote that holds only some other ref ending in the v1 spelling now refuses with `cannot_measure_review_ref` rather than falling back.

- Every review member key must be one plain directory name: not `.`, `..` or `.git`, not the lane's shared `.venv`, not a `.grip-review*` name, and containing no separator, NUL or newline (other dot names such as `.github` remain valid). Binds that break this rule are refused on read and on write. In a multi-member lane each member's run log is now `.grip-review-run.log.<key>`; logs named the old way in existing receipts are still carried out by `review close`. `review open` checks every member's key and carried range before reconstructing the first.

- `gr2 lane exit` refuses a lane with uncommitted work (staged, unstaged or untracked) in any of its repositories. It exits 2, names each repository, prints a command you can paste, and leaves the lane entered with its work in place; before this, exit silently ran `git stash push -u` in each such repository. Pass `--dirty stash` to keep the old behaviour: each stash is named in the JSON output (`stashed`: repository, path, stash sha, restore command) and on stderr, and the restore command is `git stash apply --index <sha>`. If an `on_exit` hook leaves uncommitted work, exit refuses the same way. Once the lane is found, a refusal prints a JSON receipt with `"status": "refused"`; a stash or hook that fails after earlier repositories were stashed refuses with `exit_step_failed` and names every stash already made, and an unknown `--dirty` value refuses with `unknown_dirty_mode`. A script that relied on exit stashing now meets the refusal and needs `--dirty stash`.

- `gr2 review close` (and its hidden alias `close-gr`) on a reconstruction lane refuses with exit 2, and keeps the lane, its allocation record and its run evidence, when any member repository holds a staged, unstaged or untracked change, a stash, or a commit that no remote has and the reviewed HEAD does not contain (by branch, tag or reflog). The review's own marker, run receipt and logs do not count; a runner's own outputs (such as cargo's `target/` and `Cargo.lock`) do not count when the run receipt names that runner. At the root of a multi-member lane, any entry that is not a member, the shared `.venv`, or a file the review writes counts as work. Files git ignores are not examined, a retry of a close that already began removing the lane does not check again, and a git command that cannot read a member refuses the close. Closing a project review or an ordinary PR-head lane is unchanged.

- `gr2 check run`, `check show` and `review merge` default what the repository already answers; every flag still overrides, and each verb prints what it resolved on stderr (credentials in a remote URL are masked). For `check`, `--remote` is the current branch's configured upstream remote, else `origin` (a remote name resolves to its URL), and `--head` is `HEAD`; with no upstream and no `origin`, or a configured upstream remote that is missing, they refuse naming `--remote` and exit 2. Note that `check run` publishes its observation to that remote. For `review merge`, the review is the one bind whose member heads all equal each member's current `HEAD` (inside an opened review the marker supplies only the workspace), `--from` is the members' common branch, and `--into` is the remote's default branch, but only when it is the one branch still at the reviewed base (a stale branch left at the base is never chosen). When the repository does not settle a value (zero or several matching binds, members on different branches, a member path that is not its own repository, no default branch alone at the reviewed base), it refuses with the merge receipt shape and exit 3 and pushes nothing.

- `gr2 review run` on a multi-member lane now tests downstream by default. Every workspace member the lane does not bind is brought to the commit the workspace pins for it, and the members that depend on a changed member are installed and tested against the changed code; members the changed ones need are installed but not tested. A red downstream member makes the lane red without stopping the run. The verdict ends with a line saying what was tested together (`downstream: ran (changed a; downstream b; upstream c)`, `downstream: skipped (...)` or `downstream: not examined (...)`). In the receipt, each `members[]` entry gains `role` (changed, upstream, downstream) and `tested`, the counts and result cover tested members only, and the lane carries a `downstream` object whose `status` is always present: `ran`, `skipped`, `not_examined` (with the reason) or `refused`. A single-repo lane is not examined (the clone is the lane directory, so other members cannot be placed beside it) and its receipt and verdict line now say `not examined`. Pass `--no-downstream` to test only the lane's own members; the receipt then records `skipped`. `--order` also records `skipped`, since it names the run's members by hand. Pytest members only: a downstream or upstream member that declares another runner is refused as `member_runner_unsupported`.

- Where `review run` reads the members' pins from, and how it refuses. The members and their pins come from the workspace spec's `[[repos]]` and from the root's `grip.toml` `[[members]]` (url under `[members.remotes] origin`), so a root made by `store init` alone now runs downstream too. A pin counts only as a full lowercase 40-hex commit; a branch name, a short sha or an uppercase sha is not a pin. When both files exist, members are matched by name: two different usable pins refuse the lane as `downstream_pin_conflict` (naming the member, both pins and both files), a usable pin in one file is used, and a member only in `grip.toml` is added. A member with no usable pin, no url, or a pin no source has refuses as `downstream_unpinned`, a `grip.toml` or spec file that is malformed TOML, not UTF-8 or unreadable refuses as `downstream_unreadable` naming the file, and each of these refusals names `--no-downstream` as the way out. All of them exit 2, before any venv or clone, and the receipt records `downstream: {status: refused, code, reason}`.

- `review run` now plans downstream from a probe and clones only the members the plan selects. For each unchanged member it fetches just the pinned commit and reads that commit's root-level files (a root-level file over 2 MiB, and symlinks, are skipped); a manifest below the root is not visible at planning time. Members in no role leave nothing in the lane and are listed in the receipt as `not_selected`; the rest get a full checkout at the pin. A pin is looked for in the workspace's own checkout of the member (`<workspace>/<name>`), then the repo cache, then the member's url, and a refusal names every source that was tried. The receipt records the source of each probed member under `downstream.sources`, and when a member that took part came from the local checkout the verdict line says `<member> taken from the local checkout, not checked against its remote`, so a green over an unpushed commit does not read as published.

- `gr2 review run` takes `LANE_DIR` as optional. Run from inside a reconstruction opened by `review open`, it uses the one enclosing review marker; an explicit path still wins and an invalid explicit path refuses without falling back. With no enclosing reconstruction it refuses (`no_review_context`), and with more than one it refuses (`ambiguous_review_context`) and asks for an explicit `LANE_DIR`; both exit 2.

- `gr2 review show` and `gr2 review verify` can be called with no target from inside a workspace: they use the workspace's sole review bind and say so on stderr (`gr2: target=gr:<id> (the only review bind in this workspace)`). With no binds or several binds they refuse and name the explicit form (`review show <root> gr:<sha>`). `ROOT TARGET`, `-C ROOT TARGET` and target-only calls all work; a `gr:` id or a bare hex id stays a target even when a directory has that name; a positional root together with `-C`, an invalid target or surplus arguments are refused.

- `gr2 review open` with no root now finds the workspace root from the current directory, as the other workspace verbs do, when it is given no target or one target that is not a directory. The three-word PR-head form (`ROOT UNIT REPO PR`) is unchanged and never inferred.

- On a native workspace root (a `grip.toml` beside the root's own `.git`), `gr2 lane create` materializes the whole member set of one workspace commit: the root's current HEAD, or the commit named by the new `--workspace-commit`. Member names, paths and pins come from that commit, so a member can sit at a nested path; a `--repos` list that is not the complete member set is refused (`a native workspace lane materializes the complete selected member set`), and an existing lane that selects a different commit is preserved unchanged and refused. `lane create` also tells you when it fills in the repos from the selected commit rather than the spec.

- New materialized lanes are checked out at a visible path, `agents/<unit>/lanes/<lane>` (members under `repos/<name>`, or at their own paths on a native root), instead of beside the lane's state under `.grip/state/lanes/`; the lane's record names the location, and lanes created before this keep theirs.

- Review safety records (the receipt that `commit`, `push` and `close` consult before touching a review checkout) now live in Git metadata for the calling worktree (`grip-review.json` and a coordinate-only `grip-review.pointer` under the checkout's own git directory), with the workspace copy under `.grip/state/reviews/<unit>/<lane>/<member>.json` as compatibility evidence. Two linked worktrees of one repository keep independent records, and a bound lane in a linked worktree writes and reads its receipt correctly. Receipt and pointer are published together and rolled back together; if rollback itself fails the lane is left refused with `review publication is incomplete; explicit recovery at <path>` until the recorded worktree, Git and workspace identities and the live contents are reconciled. A direct `review open` that names a workspace now requires the managed member location for its checkout; an arbitrary destination is refused when the safety record is published, which can be after the clone was made.

- Review cleanup keeps its evidence. Before a reconstruction, PR-head lane or disposable project review is deleted, `review close` and the project EXIT keep the safety receipt, the marker and the run receipt and log outside the directory being removed, so a failed removal can be diagnosed and retried. Deletion is authorized only by the workspace's own allocation for that lane: old standalone reconstruction markers are not adopted automatically (save the evidence and reopen), existing project roots are not allocated automatically (explicit legacy adoption needs independent workspace evidence), and a linked worktree, a symlinked Git directory, or a changed HEAD, origin, selection or physical identity is refused and may need operator recovery. The coordinate-less legacy close path keeps its previous behaviour.

- The `store` verbs `init`, `commit`, `check`, `push`, `status`, `materialize`, `migrate`, `log`, `diff` and
  `checkout` now refuse a workspace whose state has moved to a newer gr2, instead of answering "run store
  init" (which would start a second, divergent workspace beside the real one). A workspace counts as moved when
  the root's `HEAD` tracks `.grip-moved`, or the root holds the ref `refs/dev.synapt.grip/__state__/v1`; an
  untracked or merely staged marker does not count. The refusal exits 4 with one sentence: this workspace's
  state has moved to a format this gr2 cannot read; use a newer gr2. A plain root behaves as before;
  `workspace init` and `store migrate-reviews` are not guarded.

- The `gr2` source tree keeps `python_cli` and `prototypes` under `gr2/gr2/`, so the directory name is the
  import name. No import name changes; a bare tree with `PYTHONPATH` set to the `gr2` directory now imports
  `gr2.python_cli` without an editable install.

### Fixed

- **A store root nested under an initialised workspace is its own workspace root.**
  The implied root (what every `gr2` verb that takes an optional workspace root
  uses when none is given, `gr2 review bind` and `gr2 workspace status` among
  them) was the nearest ancestor holding
  `.grip/workspace_spec.toml`. A native store root (a `grip.toml` beside a root
  `.git`, what `gr2 store init` makes) has no such file, so an outer initialised
  workspace won: a bind run from inside the store root exited 0, printed a `gr:`
  id, and wrote the bind commit into the outer workspace, where a read against
  the root you were standing in said `no_rows`. The root is now the nearest
  ancestor of either kind, the current directory included. A directory under the
  outer workspace only still finds the outer one, an explicit root argument still
  wins, and `workspace status` asks the same resolver instead of keeping its own
  copy of the walk.

- `gr2 review bind` no longer lets a remote that git reads as an option run a command. A remote such as `--upload-pack=<command>`, given by `--remote` or read from a `grip.toml` in a workspace someone else prepared, used to reach `git ls-remote` and execute before bind refused it as unreadable. Bind now refuses a remote that begins with `-` or holds a control character (`invalid_field`, naming the member and field, never the value) before any git call, and its `ls-remote` calls put `--` before the remote.

- The in-repo `.review-install` file can be written as TOML or as the older bare `key = value` lines. A quoted `package = "x"` no longer keeps its quotes in the import name, and a quoted `install = "{venv} -m pip install -q ."` is shell-split into a command instead of being run as one program name. A `[section]` header, a line that is not `key = value`, and a list or table value are now refused as `bad_hint` naming the file and line (they were silently skipped); an unrecognised key is refused as before and a repeated key keeps the last value. A bare value that is one quoted string reads as that string, so a program path with a space is written `install = "'/opt/my tools/pip' install ."`. `{venv}` is the lane venv's python executable, not its directory.

- A red `review run` under `FORCE_COLOR=1`, a caller's own `--color=yes`, or a CI that colours its logs now still reports its failed test ids and counts; the parser strips terminal escape sequences (including OSC 8 hyperlinks, which used to put a wrong id in the receipt) before reading pytest's output. The lane's log file keeps the raw output.

- A remote with URL credentials can no longer reach a review record by any route. `review bind` (including `--remote`), a project review pin and a workspace commit refuse a member remote that carries URL userinfo (an SSH login is allowed) before git is run, naming the member and never the URL (`remote_credentials`); `store init` uses the same rule. `review publish` checks the record again before pushing: a record with credentials is refused, and a record goes to an https destination only when every member remote is portable (an https, http, ssh or git URL, or an scp-like `host:path`); a relative or absolute path, `file://`, a bare remote alias or a transport helper is refused as `local_path_remote`, while the same record still publishes to a local destination.

- The tmux launch no longer misreads a working directory that contains spaces or a literal `|` when it observes the panes it started, checks that the panes it finds are exactly the expected ones, and keeps the original launch error when its rollback cannot be verified (the failure note says the rollback failed or remains unverified). A path containing a newline is still unsupported.

- `gr2 review run` keeps pytest failed-test ids and counts intact when the output carries terminal control
  sequences: charset escapes (`ESC ( B`), truncated OSC payloads, and carriage returns or CRLF no longer end up
  inside a `FAILED` id or stop the summary from parsing. A truncated OSC payload is discarded up to the end of its
  line, the next escape or the end of output, and a blank `FAILED` row no longer consumes the following summary
  line as its id.

## [gr2 2.0.0a6] - 2026-10-01

### The store is a git repository: `gr2 store` has its full verb set

`gr2 store` now keeps the workspace record in the root's own git repository: a
`grip.toml` naming each member (path, origin url, pin) and one gitlink per
member in the root's tree. The group gains `commit`, `check`, `push`, `status`,
`materialize` and `migrate` beside `init`, `log`, `diff` and `checkout`:

- `store init` initializes the store at the current directory or the root you
  name. It finds members from `--member <path>` (repeatable) first, then from the
  paths the root's `.grip/workspace_spec.toml` declares, and only then by scanning
  the root's direct children, so a root whose members sit one level down
  (`core/config`, `team-b/config`) initializes when `--member` or its spec names
  them; with neither, only the root's direct children are scanned and it still
  refuses with `no sibling git repositories found to store`. A path that is not a checkout, escapes the root,
  resolves to the root itself, aliases another member, or collides with another
  path on one member name is refused at 4, and every problem is reported in one
  refusal. A member's name comes from its normalised path (`./core/config` is
  `core-config`).
- `store init` records the branch each member is on and its upstream on `origin`,
  where it used to write `main` and `origin/main` for every member. A member on
  `core/main` is now checked against `origin/core/main`, so the first `store
  commit` of a workspace whose members are namespaced branches no longer refuses
  at 3. A branch tracking a remote other than `origin` records `origin/<branch>`,
  the only remote a fresh clone has. An existing `grip.toml` is not rewritten.
- `store commit -m <message>` records each member's pin, refusing at 3 a pin its
  upstream does not contain. A commit with nothing new to record is now a no-op
  that exits 0 and prints `Nothing to record: every pin already matches the
  root's last commit.`; `--json` reports `"status": "unchanged"`. It used to exit
  5 with `git command failed`.
- `store check` verifies the root's gitlinks and each member's coverage against
  its live upstream, fetching first. `store push` checks, then pushes only the
  root branch, never a member branch.
- `store status` prints every member's pin, working HEAD and state, and the
  gitlink when it disagrees with the pin (the `--json` rows always carry it). The
  table always prints; the verb then exits 4 when a gitlink disagrees with its
  pin, and 5 when a member cannot be measured.
- `store materialize` checks out each pin from its declared origin, and
  `store checkout <commit>` does the same at a root commit.
- `store migrate` converts an existing `.grip/.git` store into one native root
  commit (`--dry-run` prints the plan). Each pin is checked against the branch the
  member is on, the same upstream `store init` records, and a pin that is not
  covered refuses at 3 and leaves no native store behind.

The group's exit codes are stated in its help: 0 ok, 2 usage, 3 refused on
coverage or cleanliness, 4 refused as inconsistent or beta, 5 cannot measure; a
verb that could not complete also exits 5, with its message prefixed `cannot
complete this store verb:`. `status`, `check` and `log` on a root with no commit
yet refuse at 5 with `no root commit yet; run store commit`. The store group is
marked may-change in the published surface (below) while it settles.

What an existing workspace now meets: `store log`, `diff`, `checkout` and
`snapshot` act on the current directory and no longer take a workspace argument,
so a script that passes one to `log` or `snapshot` is refused at the parser
(exit 2). `diff` takes two optional root commits and `checkout` a root commit
instead, so a workspace path given to either is read as a root commit and refused
at 5, the code for what the store cannot resolve; a script keyed on exit 2 will
not see it. `store init` still accepts a
root, now optionally, but no longer creates `.grip/.git`; `store migrate`
converts one you already have.
`store snapshot` is now a hidden alias of `store commit`, removed at beta: it
makes the native root commit, requires `-m`, and no longer accepts `--repos`,
`--type`, `--sprint` or `--overlay-dir`. The hidden `grip` group remains an
alias of `store`.

### Store verbs read members where they actually are

- `store log` on a root that had its own history before it became a store
  (an adopted root) used to fail whole at 5 on the first commit without a
  `grip.toml`. The log now stops at that boundary and shows every store commit
  after it.
- `grip.toml` declares a member's `path`, but earlier alphas placed members by
  name. When nothing is at the declared path and a checkout sits at the member's
  name, `status`, `check`, `commit`, `checkout` and `materialize` now read that
  checkout where it is and say so once on stderr, naming both locations. Nothing
  is moved and nothing is cloned over it. A name that would resolve outside the
  root, onto another member's path or name, or inside another member's tree is
  never read, so one member's pin cannot be written from another's checkout.
- `store materialize` used to clone a second working copy at the declared path
  while the member's checkout sat at its name, and `store checkout` left such a
  member unrestored; both exited 0. They now resolve the member the same way as
  the other verbs. When neither location holds a checkout, `checkout` refuses at
  5 naming the member, instead of letting git act on the store root.
- `store checkout` used to detach the root and then read the member pins, so a
  member renamed since the target revision left the root moved, no member moved,
  and nothing printed. It now resolves every pin before it writes anything and
  refuses by name with nothing moved. A git failure after the root is detached
  names which members moved and which did not.

### `gr2 pr view`, and a safer `gr2 pr merge`

`gr2 pr view <workspace_root> <owner_unit> [lane_name] [--json] [--repo <member>]`
prints the member pull requests of one change, keyed on the lane. Members come
from the lane's PR group record, or from the lane record when no group exists
yet; `--json` names which source answered. A member with no PR, or whose read
failed, is listed rather than dropped, and `--repo` naming a non-member refuses
and lists the members.

`gr2 pr merge` gains `--match-head-commit [REPO=]SHA`, repeatable. Every pinned
member's live head is compared before the first merge, so a push that landed
after review refuses the whole group with both commits named and nothing merged;
the pin is also passed to the host's merge. A pin must be a full 40-character
lowercase sha, must cover every member of the group (a bare SHA only for a
one-member group), and must name members of the group; each fault is refused
before anything merges. Without the flag a merge behaves as before.

The merge path's other failures now reach you as sentences: a member with no
verification target, a group file whose entries cannot be read, and a group
that lists one repo twice. A group with no members used to exit 0 with
`merged: []`, the same shape as a completed merge; it is now refused. A file in
the group directory that is not a JSON object is skipped when looking up a group.

### Migrating from gr1 and validating a spec

- `gr2 workspace migrate-gr1` declared every unit at `agents/<unit>/home`. It now
  emits the location gr1 uses: `.` for `worktree = "main"`, and `../<worktree>`
  for a desk beside the root (a `/` in the worktree becomes `-`, as gr1 does).
  `gr2 apply` adopts such a desk instead of re-cloning it: members are found at
  the `path` their spec declares rather than at their name, and a unit at the
  root or beside it gets its `unit.toml` under `.grip/state/units/<unit>/`, never
  inside the desk. A member found only at its name is read where it is, with one
  line naming both locations. Two units resolving to one path are refused.
- Unit paths and member paths are contained: an absolute path, `~`, a backslash,
  `../../x`, `../x/y`, and a path through a symlink are refused with the unit
  named.
- `gr2 spec validate` checks both `grip.toml` and `.grip/workspace_spec.toml` when
  a root carries both. A spec that parses but fails validation now exits 4
  instead of 1; a file that is not valid TOML still exits 1.
- `repos[].path` is now contained too. An absolute path used to point outside the
  root, where a clone would be planned and applied. Newly refused at this
  coordinate: `./m`, `m/`, `m/.`, `a//m`, any absolute path (even inside the
  root), a leading `~`, a backslash, NUL, and a path through a symlink. Generated
  specs never use these spellings; a hand-written one may.
- A refused gr1 member path is a sentence through `migrate-gr1` as well, where it
  was a traceback. A path that leaves the gripspace says members live under the
  gripspace root and to move the member inside or leave it out; a path with a
  `..` segment that stays inside (`a/../b`) gets its own sentence.

### `gr2 review`

- `review bind` on a fresh native store root refused with advice to run
  `store init`, the verb just run. It now creates the review store on first use,
  adds `/.grip/` to the root's `.git/info/exclude` (an adopted root's own
  `.gitignore` is not touched), and removes what it created if the bind fails.
  `review verify` on a root with nothing bound names `bind`.
- `review run` runs a lane that binds several repositories: every member installs
  into one shared `<lane>/.venv`, each member's tests run in its own directory,
  and one verdict is printed for the lane. Integrity is checked for every member
  before the venv exists. Each member's install and package come from its own
  `.review-install`; `--package` and `--install` on such a lane are refused.
  `--order KEY,KEY` sets the install order. A refusal stops the run and the
  receipt names the members not run.
- `review run --runner junit-xml` retired a report rewritten at the same size
  within one mtime tick as stale. The report's content is now part of its
  identity.
- A reconstruction lane is now marked `.grip-review-open.json`, and help and
  refusal messages say `review open` and `review close`. A lane opened by an
  earlier release (`.grip-open-gr-reconstruct.json`) is still reclaimed by
  `review close` and read by `review run`.
- Review receipts are kept outside lane state, so reopening a lane does not
  collide with its receipt.
- `review run` re-checks the lane after the install step and again after the
  tests, not only before the venv. The tracked tree of every member must still be
  the bound tree, and an untracked path that is new since the first check refuses
  with `untracked_drift`, naming the path, the member in a multi-repo lane, and
  the fix. What a run may add is unchanged except for one allowance: the lane's
  own files, `.venv/`, and anything under a `__pycache__`, `.pytest_cache`,
  `.mypy_cache` or `*.egg-info` segment are admitted as before, and `build/` is
  now admitted only when it is new since the install step began (what
  `pip install <dir>` leaves behind); an untracked `build/` already in the tree
  before the run still refuses at the first check. A repo whose tests write an untracked artifact into the
  tree (a coverage file, say) now refuses unless the repo ignores that path in its
  `.gitignore`.

### Verb names and version lines

- `gr2 repo hooks` and `gr2 repo hook-run` are now `gr2 hooks show` and
  `gr2 hooks run`, beside `hooks trust`, `revoke` and `status`. `gr2 lane current`
  is now `gr2 lane show`. The old spellings still work as hidden aliases until
  release.
- `gr2 --version` used to print the version recorded at install time. It now
  prints `<version>+g<sha>` for an editable checkout, `<version> released` for a
  regular install, `<version>+unknown` when an editable checkout's commit cannot
  be read, and `unknown` when the distribution is not installed for the running
  interpreter. A broken or missing `git` never makes it fail.
- `git review` shares its name with the Gerrit `git-review` tool. `git review
  --help` now opens by saying which tool it is and names the other one, and
  `git review --version` (also `-V` and `version`) answers instead of exiting 2.
- `gr2 store log --help` no longer prints references a reader cannot follow, and
  the clone and project-file executors' wrong-kind refusals say what each
  executor applies.

### A published surface: `gr2/api/cli.api` and `gr2/api/deprecations.toml`

`gr2/api/cli.api` is a committed, generated listing of the gr2 command line,
grouped by command: every verb, flag and positional, the store group's exit
codes, the JSON keys of the store verbs, and the layout rows (`grip.toml` and the
two reserved ref namespaces). Each line carries a marker: `stable`, `may-change`,
`reserved`, or `hidden`. The store group and its JSON keys are `may-change`.

`gr2/api/deprecations.toml` records each deprecated name against one milestone
(`alpha`, `beta` or `release`) instead of a version. Registered today: the
hidden aliases above, the `review open-gr`, `close-gr`, `exit-gr` and
`open-project` aliases, the hidden `grip` group, `store snapshot`, the pre-rename
review marker, the `.grip/.git` store home, and `gr2_overlay`. The
`gr2_overlay` shim's deadline read "the release after 1.5.0", which had already
passed; it now says the beta milestone.

### Other fixes

- `.gitinclude` compares names case-insensitively for its self-protection and
  conflict checks, so on a case-insensitive filesystem `!.GITINCLUDE` no longer
  untracks the declaration and an include differing from its ignore only by case
  is reported.
- The event outbox's reader is now a peek: `read_events()` and
  `read_events_detailed()` no longer advance a consumer's cursor, and
  `ack_events()` is the only call that does. The channel bridge acknowledges each
  event after posting it, so a post that fails loses no event; delivery is
  at-least-once, and `event_id` is on every event for deduplication. An event the
  formatter cannot format is skipped, named on stderr, and acknowledged. A caller
  that relied on a read to consume events must now call `ack_events()`.

## [gr2 2.0.0a5] - 2026-09-25

### Adopting a superproject: `gr2 workspace init --from-superproject`

A new entry point adopts an existing superproject rather than a directory of
repos. `--from-superproject` requires that the root's tree pin members and refuses
otherwise. The member set now comes from the root's declaration and nowhere else,
so a member not on disk after a plain `git clone` without `--recurse-submodules`
is counted and named `not materialized` instead of dropping the whole superproject
line. The gate is the gitlink: a `.gitmodules` alone declares nothing, because git
resolves members from the tree's 160000 entries.

### Members land on the commit the root declares

`gr2 workspace materialize` cloned a superproject member onto its default branch
tip, not onto the commit the root's gitlink pins, so a member whose branch had
moved sat on the wrong commit while the verb exited 0. A member is now checked out
at its declared pin and the output names it (`cloned example1@065be099e95a`); a
pin the clone cannot reach is refused with an error naming the pin, the member,
and the command to try by hand, never falling back to the default tip. The clone
stages in a sibling directory and renames into place, so a refusal cannot leave a
repository at the tip. `gr2 sync run` now lands a re-created root member at its pin
too.

### A member path holds a repo, an empty placeholder, or neither

Several verbs asked whether a member path was a repository by asking git whether
the path was inside a work tree, which is true for any directory inside a
checkout; on a superproject every member path is an empty placeholder inside the
root, so the answer came from the enclosing repository. `gr2 spec validate` never
fired `repo_path_conflict` and validated clean with a plain directory at a
declared repo path, and `lane create` used the root's empty placeholder as its
source. Each site now asks whether the path is ITSELF a repository root, and an
empty directory at a declared repo path is exempt, because a plain `git clone` of
a superproject leaves the mount points empty until `submodule update --init`.

`gr2 store snapshot` and `gr2 store checkout` read through the same way, and the
store verbs resolve each member to the state it actually holds. Snapshot used to
record the ROOT's HEAD as every member's head, and to refuse with `Dirty repos
detected` for clean members whenever the root carried its own untracked `.grip/`
and `agents/`, the default state after adoption; checkout would have acted on the
placeholder paths and could have moved the root repository. Now a repo root is
read as-is, an empty placeholder falls to the unit's materialized copy of that
member, and a path that is neither refuses with the verb that fixes it.

### A refused `lane create` leaves no lane any verb accepts

A `lane create` that refused at materialization wrote its lane document first and
exited 1, leaving a lane that `lane enter`, `lane exit`, and `exec run` all
accepted; `exec run` then reported the repos missing, two steps from the create
that refused. That lane is now removed. A refusal landing after every repo's fork
base is recorded is different: the checkout exists and `review create-project`
succeeds on that lane, so it is KEPT, its stderr says so together with both ways
forward (continue with `review create-project`, or remove the named path), and the
command still exits non-zero. A two-repo lane whose second source never
materialized carries a fork base for the first repo only, and is removed rather
than promised as recoverable.

### Hooks report what did not run

`lane enter` ran a trusted lifecycle hook, printed its warn payload, and then
reported `"status": "ok"` with exit 0, so the verb's success did not depend on the
entry work having happened. The exit code stays 0 under the warn tier, and the
failure is now recorded instead: the status reads `warned` and `hook_failures`
names the hook and its rc. A hooks table whose stage keys are top-level
(`[[on_enter]]` instead of `[[lifecycle.on_enter]]`) used to build an empty hook
set and let every verb succeed while the author's hook intent vanished; the parser
now refuses at load and names the expected shape. A refused or blocked projection
row now carries `lifecycle_hooks_withheld`, naming the consented `on_materialize`
hooks that never ran, plus `lifecycle_hooks_withheld_detail`. `gr2 hooks trust`
shows an ESCAPE line for a row that resolves outside the member tree and binds it
by design; the bind output now warns how many rows will be refused at run time.

### A grouped PR set is legible on the forge

`gr2 pr create` produced a set in which every PR carried the lane name as its title
and the same one-line body naming no sibling, so a reviewer landing on one PR had
no path to the others, and the recorded `pr_number` was null while the url on the
same row carried the number. The number is now parsed from the URL `gh pr create`
prints, and a URL carrying no `/pull/<digits>` is refused. `--title` overrides the
lane-name default, which makes every PR in a set read identically; `--body` and
`--body-file` set the group body, are mutually exclusive, and the pair is refused
with exit 2. `--body-file` reads the body from a file, so the body reaches `gh`
through a file rather than on argv. The default body now names the group and lists
its member repos, and every body gets every sibling's URL; a failed sibling edit
exits non-zero with the group printed.

### Where `gr2 review bind --remote` resolves

`review bind` reads `--remote` with git inside the workspace's store, and the
reconstruction clone cannot resolve a user-repo remote name either, so only a URL
or an absolute path works end to end. The `--remote` option help now says so, and
the first refusal is no longer how you find out.

## [gr2 2.0.0a4] - 2026-09-24

### Member repo hooks: consent before they run, and where they may write

A member repo's `.gr2/hooks.toml` can carry two kinds of things gr2 executes for
you: lifecycle commands (they run when the workspace materializes and when
lanes are cut, entered, or exited) and file projections (copy or link a file
from the member into the workspace). Through 2.0.0a3 both ran with no prompt:
cutting a lane executed commands written by whoever authored the member repo,
and a projection could write outside that repo entirely — into the workspace
root, inside the workspace's `.git`, or through a symlink to anywhere on the
machine. (Plain `git clone` never runs repository code, so a superproject user
moving to gr2 had gone from "cloning is safe" to "cutting a lane executes the
repo".)

2.0.0a4 gates both behind two local mechanisms:

**Consent.** A member's hooks run only after an explicit local record exists.
`gr2 hooks trust <member>` shows everything being consented to before anything
is written: the hash of the member's hooks table, every lifecycle command, and
every projection with its RESOLVED destination (symlinks followed) and any
escape flags. The record — `<workspace>/.grip/consent/<member-path>.json`,
naming the host user and the exact hash — lapses the moment the hook text
changes, so a repo cannot change its commands under a standing yes. Unbound
members skip and report: the verb completes, prints the commands that did not
run, and names the bind command; `gr2 hooks status` and `gr2 status` (the
same table as `gr2 repo status`) keep printing the unbound state. `gr2 hooks
revoke` removes the record. A row
whose destination cannot be resolved while you are reviewing the hooks is
refused at bind time — the record only ever binds rows the screen showed you.

**Confinement.** Consent answers whether a repo may act; confinement answers
where. Four rules, checked before a projection's parent directory exists and
refused-and-reported even when consent is granted:

1. every projection destination resolves (symlinks followed) inside the
   workspace root;
2. never under any `.git` component, anywhere in the workspace, and never
   under the workspace's own `.grip` directory (gr2's consent records and
   workspace spec live there);
3. never inside another member repo's tree;
4. a `[[files.link]]` target and a `[[files.copy]]` source must resolve inside
   the member's own tree.

Every boundary comparison is NFC-normalized and casefolded on both sides,
because macOS volumes treat `.GRIP`, `.Grip` and `.grip` as one directory by
default (APFS can be formatted case-sensitive).

**First materialization.** A member's hooks table arrives with the clone, so
the first materialization of a not-yet-consented member skips and records the
fact; the next materialize where that member is bound runs its hooks once. No
re-clone is ever needed.

Limitations, carried openly:

- a `{unit_root}` destination is refused at trust time: it renders differently
  per lane and per unit, so the trust screen cannot show you the value you
  would be consenting to. Until a follow-up renders it per declared unit, the
  bindable destination forms are the workspace-deterministic ones
  (`{workspace_root}`, `{repo_root}`).
- the trust screen resolves and shows every projection's destination, but it
  does not yet flag a copy **source** outside the member's own tree. Sources
  are still confined — a projection whose source escapes the member is refused
  at runtime — the screen simply does not yet show that row as an escape.
- git's wider rules for alternate spellings of `.git` — ignorable code points
  on HFS+, and trailing dots, spaces and 8.3 short names on NTFS — are open
  follow-ups beyond the casefold normalization above.

## [1.5.1] - 2026-09-10

**Scope.** This release promotes `v1.5.0..<dev tip>` — **15 commits, 7 merge commits, and 7
first-parent units**, measured with `git rev-list --count`, `git rev-list --merges --count`, and
`git rev-list --first-parent --count` over `v1.5.0..origin/dev` (tag `v1.5.0` = 9894260d), with the
empty range `v1.5.0..v1.5.0` returning 0 as the control. The release-prep commit carrying this
entry sits one beyond that range and is excluded; this bump's own merge into `dev` and the
`dev`→`main` promote merge add to a tag-range figure a reader measuring `v1.5.0..v1.5.1` would see.
Rust source changed since 1.5.0 — three source files (`src/cli/commands/spawn.rs`,
`src/cli/dispatch.rs`, `src/core/gripspace.rs`) and two test files
(`tests/spawn_callsite_argorder.rs`, `tests/gripspace_include_rev_dirty.rs`) — so this is a
substantive patch release, not a metadata-only bump.

**gr2 review-run hardening (#1050, #1051).** A review lane can now run more than once: the run log
is allow-listed so `review run` no longer refuses the second run as untracked drift, and a run
refused before pytest writes a refusal receipt so `close-gr` can reclaim the lane. Import resolution
is isolated on two axes — `-I` keeps the lane's cwd from shadowing its own package as a namespace
directory, and a scrubbed environment (every `PYTHON*` variable dropped) is shared by the import
checks and the pytest run, so a rogue package on `PYTHONPATH` can neither fool the check nor be
imported by the run while the check certifies the lane.

**gr spawn arg ordering (#1046, #1047, #1048).** `gr spawn up` now applies tool-level args to the
launch command, with a call-site witness pinning that tool args compose before agent args; the
witness is gated to `cfg(unix)` so it does not red the Windows build.

**gripspace includes (#1045).** A rev-pinned gripspace-include clone that is dirty no longer drops
its included repos: the resolver keeps them instead of silently taking the shorter list.

**CI (#1049).** The clippy doc-comment lint on `assemble_launch_parts` is cleared and the Windows
job is required in the CI aggregate, so the aggregate context that gates merges cannot go green
without Windows.

## [1.5.0] - 2026-09-07

**Scope.** This release promotes `v1.4.0..12b08f1e` — the immutable sha `12b08f1e` (the
`dev` tip at freeze), not the moving `dev` ref: **70 commits, 35 merge commits, and 8
first-parent units**, measured with `git rev-list --count`, `git rev-list --merges
--count`, and `git rev-list --first-parent --count` over that exact range, with the empty
range `v1.4.0..v1.4.0` returning 0 as the control. The release-prep commit carrying this
entry sits one beyond that range and is excluded.

**At the tag.** The release tag `v1.5.0` sits three commits beyond `12b08f1e`, so a reader
measuring `v1.4.0..v1.5.0` gets **73 commits / 37 merges**: the promoted range plus this
bump commit, its merge into `dev`, and the `dev`→`main` promote merge. The promoted-range
counts above are the substantive figure; the tag-range figure is stated so a reader who
recomputes at the tag is not surprised.

**Why 8 first-parent units expand to 70 commits.** The first-parent line is short because
unit 1 is a promote merge (#1035, `promote/dev-to-main-review-verbs`, now also `main`'s
tip) that folds the entire review-verb, review-flow, `prune`, `target`, fixes, and CI
stack — the 27 PRs in the `#1004`..`#1034` span, which were 27 first-parent units before
the promote collapsed them — onto its second-parent side. The remaining 7 first-parent
units are the post-promote gr2 packaging and review-run fixes, #1036–#1042. So the work
below is enumerated by PR, not by first-parent unit; the two diverge here by design.

**Zero of the 92 changed files touch the published Rust CLI (`src/`)**, and neither
`Cargo.toml` nor `Cargo.lock` changed in the range — measured with `git diff --name-only
v1.4.0..12b08f1e -- src/ Cargo.toml Cargo.lock` (0 files) and by directory (85 under `gr2/`, 2 under
`.github/`, 2 under `scripts/`, 2 in the repo root, 1 under `tests/`). The one Rust file
that changed, `tests/release_pipeline_contract.rs`, is a test-only integration test and
does not enter the compiled binary. So the Rust binary published to crates.io at the tag
is **byte-identical to 1.4.0**; this is the first grip release that changes no shipped
Rust code. The minor bump reflects new user-facing verbs in the gr2 overlay, not a change
to the crate. (This bump commit itself is the only edit to `Cargo.toml`/`Cargo.lock`.)

### Carried by the promote merge (#1035 — PRs #1004–#1034)

**The three review verbs (the R2 "Exact Work" closing fruit).** A frozen-range Python
review through gr2 now runs end to end with no raw-shell exit point:
- `review bind --from-range` (#1032) ingests a frozen `range.patch` directly.
- `review close-gr` (#1033) is a verb-owned teardown for an `open-gr --enter` lane.
- `review run` (#1034) folds the reviewer's in-lane `venv` + install + `pytest` into one
  verb, with a two-sided tree binding (tracked-tree equality plus an untracked-drift scan)
  and an import-under-lane check, so a green is always about the bound tree; counts come
  from pytest's summary line and a green requires `passed >= 1`.

**Review-flow infrastructure (10 PRs).** Receipt emitter (#1008); pre-push head
materialization via the blobless+sparse path (#1009); reconstruct a pre-push head from a
carried range (#1010); committer-faithful reconstruction so the reconstructed sha equals
the pinned head (#1011); run reviewed tests inside the lane, recorded in the receipt
(#1017); multi-repo verification from per-repo spec commands (#1019); provision the lane's
own venv (#1020); end-to-end carry-range reconstruction test (#1022); install declared
test-extras into the lane venv (#1023); print the refusal reason on default output (#1031).

**prune verb stack (4 PRs).** `prune` — merged-branch cleanup by patch-id + squash,
never containment (#1025); default target resolves dev before origin/HEAD, not silently
main (#1026); help text (#1027); prefer the gripspace stored `[settings].target` (#1028).

**target verb (2 PRs).** `target set/show/unset` — the writer for the stored PR target
prune reads (#1029); atomicity witness — the original spec survives a failed replace (#1030).

**Fixes (5 PRs).** Lane create records the fork base so review create-project works
(#1004); workspace status flags a hand-made linked worktree (#1006); pin pr-merge `--json`
assertions to stdout across click versions (#1015); record lane fork_base even when a
projection hook blocks (#1016); pin that source-transport does not change review identity
(#1021).

**CI and test scaffolding (3 PRs).** Raise the Windows test-job timeout 15→30 (#1005);
guard against a `.pyc` with no source (#1007); lift the version-safe CliRunner shim into
one shared helper (#1018).

### Post-promote fixes (first-parent units #1036–#1042)

- **rmtree teardown refuses on partial failure (#1036).** A lane/review teardown that
  cannot fully remove its tree refuses rather than leaving a half-deleted directory;
  witnessed by `test_rmtree_or_refuse.py`.
- **`review run` install hint (#1037).** A reviewer whose in-lane install is missing an
  extra now gets a printed `.review-install` hint instead of a bare failure.
- **`review run` undeclared-extra guard (#1038).** An extra named in `.review-install`
  but not declared by the project is caught before the install runs.
- **gr2 overlay import-package rename (#1039).** `gr2_overlay` → `gr2/overlay`, so the
  installed import package matches the distribution layout.
- **gr2 packaging (#1040).** A PEP 517 build backend that sources the version from
  `Cargo.toml` (`gr2/_build/version_from_cargo.py`), a `MANIFEST.in`, and release-workflow
  wiring to build and publish the overlay.
- **`review run` doors (#1041).** Entry/exit guarding so `review run` and `close-gr`
  refuse to act outside a bound lane; `test_review_run.py` + `test_review_close_gr.py`.
- **release-pipeline contract: stale `needs:` (#1042).** The Rust contract test asserts
  every publish job declares `needs: ci`, closing the gap the standing-stop lift opened.

**Not in this range.** No spawn or graceful-shutdown fix landed in `v1.4.0..dev`.

## [1.4.0] - 2026-09-05

**Scope.** This release promotes `v1.3.1..dde8c816`: **144 commits and 35 first-parent
units**, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range, with the empty range `v1.3.1..v1.3.1` returning 0 as the control.
The release-prep commit carrying this entry sits one beyond that range and is excluded.

Units are classified by diffing each against its **first parent** (a merge commit has no
canonical diff, and `--name-only` returns nothing on a merge). **Five of the 35 units
touch the published Rust CLI (`src/`)**: gripspace-name validation for Windows path
separators, two MCP cancellation fixes (a stdout leak and a concurrent-cancel class), the
refusal of `clone_strategy: worktree`, and the CI-green catch-up promotion. The remaining
30 are the gr2 Python overlay and CI/release-pipeline hardening, which do not change the
shipped Rust binary.

**gr2 overlay (the bulk of this release).** The R2 "Exact Work" review chain: project
review over an immutable multi-repo pin, `review bind`/`open-gr` reconstruction with a
tree-equality assertion, the `review create-project` producer verb and the materialize
path (`open-project --enter`), and review-ephemeral lanes cloned blobless + sparse from a
persistent per-host mirror. The fork-base ruling: a review's per-repo base is the recorded
fork point, never `HEAD^`. Lane lifecycle safety, lane-aware commit, workspace kinds, and
`workspace convert-clone` with a no-worktree materialization playbook.

**Release pipeline.** `release.yml` now gates on CI: `build` needs `ci`, and both
`release` and `publish-crates` need `[ci, build]`; `cargo publish` runs `--locked` with
`--no-verify` removed. CI runs on `dev`, with no-fail-fast reporting.

## [1.3.1] - 2026-08-30

**Scope.** This release promotes `v1.3.0..3a8df911`: **22 commits and 5 first-parent
units**, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range, with the empty range `v1.3.0..v1.3.0` returning 0 as the control.
The release-prep commit carrying this entry sits one beyond that range and is excluded.

Units are classified by diffing each against its **first parent**, because a merge commit
has no canonical diff. **Four of the five units change the published Rust CLI**
(`src/mcp/server.rs`, `src/cli/commands/push.rs`, `src/cli/commands/pr/create.rs`,
`src/cli/commands/pr/merge.rs`); the fifth touches `tests/` only and changes no shipped
behaviour. There are no gr2 units in this range.

Every entry below is a correction to a command that previously reported one thing and did
another. None adds a capability.

### Fixed
- **`gr pr create` and `gr pr merge` resolve the base from the request and the platform**,
  not from the manifest's stored target. A stale stored target silently opened and merged
  PRs against the wrong base; an unreadable base ref is now reported instead of silently
  clearing the merge.
- **`gr pr` exit status agrees with the failure it just printed.** The command could print a
  platform failure and still exit 0, so a caller reading `$?` and a caller reading the
  output reached opposite conclusions. `--json` now keeps exit 0 by design and carries
  pass/fail in the payload's `success` field, which is set at the production call site
  rather than only in the serialization helper.
- **`gr push` names every repository it actually pushed**, with its ref and commit. The
  summary previously named only the repositories with nothing to push, so the operator saw
  the inverse of what happened; a repository with nothing to push is not listed.
- **The gr1 MCP server speaks newline-delimited stdio**, matching the transport its clients
  use.

### Internal
- MCP harness response reads are bounded, so a harness that never receives a reply fails
  with a timeout instead of hanging. Tests only; no shipped behaviour changes.

## [1.3.0] - 2026-08-25

**Scope.** This release promotes `v1.2.0..c095d5d`: 6 commits and 3 first-parent
units, measured with `git rev-list --count` and `git rev-list --first-parent --count`
over that exact range. All three units change the published Rust CLI.

### Added
- **Codex startup prompts use the native developer boundary.** `gr spawn` loads each
  configured `startup_prompt` and supplies it as `developer_instructions`, while recall
  continuity remains a separate optional user payload. Missing configured prompts fail
  closed. Generated launch scripts are constructed on mode-0600 inodes and atomically
  replace prior files.

### Fixed
- **Unknown repository filters are refused** by `gr add`, `gr commit`, and `gr push`
  instead of matching zero repositories and reporting success.
- **Pruning protects the remote default branch** in addition to the current and target
  branches.

## [1.2.0] - 2026-08-21

**Scope, stated first because the range and the artifact are not the same thing.**

The work promoted in this release is the range **`v1.1.0..6192e8c5`** — from the 1.1.0 tag to the
`dev` tip at freeze — which is **42 commits / 14 first-parent units**. The release-prep commit
that carries this entry sits one beyond that and is deliberately excluded; counting it makes the
range 43 / 15. Both numbers are true of different ranges, so the range is named rather than left
to inference.

Units are classified by diffing each against its **first parent**. A merge commit has no
canonical diff, and `git show <merge> --name-only` returns nothing for these, so the method is
part of the claim.

**Five of those 14 units are in the published `gitgrip` crate; nine are not.** gr2 is a separate
Python surface at version `0.1.0` that is not distributed, so its nine units — the propagation
prototypes, the append/torn-line work, the native daily verbs, and workspace-spec-from-topology —
are present in this repository and absent from anything `cargo install gitgrip` gives you. The
entries below cover the five gr1 units only. Read the git range if you want the gr2 work.

### Fixed
- **`gr pr merge` and `gr checkout` exit nonzero when part of a batch fails** — a multi-repo
  operation that failed in some repos and succeeded in others previously exited 0, so a caller
  or CI step reading the exit code saw success over a partial failure.

  **This does not close the exit-code-honesty class, and should not be read as doing so.**
  The class is tracked in `grip#886`, which records 9+ known instances; two are fixed here.
  **`gr push` — the most consequential instance, the one that can silently lose work — is
  untouched by this release.**
- **`gr link --apply` reports a stale source instead of composing it silently** and
  **gripspace pins are checked for freshness**.

  **Two measured holes in this remain open** (`grip#891`), and this feature ships with **no
  user-facing documentation** — `README.md` has no `gr link` section, only a one-line table
  mention. Link freshness is **not** guaranteed by this release; what changed is that one class
  of stale composition now reports rather than proceeding quietly.

### Known gaps at this release
- The new exit-code semantics (`EXIT_REFUSED=2`, operational failure `1`, success `0`) are not
  documented in `README.md` for either `gr checkout` or `gr pr merge`.
- `CONTRIBUTING.md` does not mention gr2 anywhere, despite **9 of the 14 first-parent units in
  `v1.1.0..6192e8c5` touching only `gr2/`** — the same first-parent classification used
  throughout this entry. A contributor following that document has no path to discovering gr2
  exists or how to set up its separate Python environment.

  **Two earlier drafts of this line carried a commit-level statistic; both are withdrawn, and
  the second is the more instructive.** The first said gr2 was "roughly a third of this
  repository's active commit volume" — no range, no metric, nothing to reproduce. The second
  replaced it with "22 of 42 commits (52%), measured by first-parent diff," which named a range
  and a method and was **still wrong, because the number did not come from the method it
  named**: 22 is `git rev-list --count -- gr2/`, whose path-history simplification silently drops
  eight merge commits that *do* touch `gr2/` against their first parent. Under the stated method
  the figure is 30 of 42 (71%). A sourced number can be more misleading than an unsourced one,
  because citing a method invites trust the number has not earned. The unit measure above needs
  no footnote, so it is the only one kept.

## [1.1.0] - 2026-08-13

These entries sat under an `[Unreleased]` heading until 2026-08-21. **That heading was false:**
this work shipped in `v1.1.0`, tagged 2026-08-13 and published to crates.io. Verified by
checking the feature's own symbols into the `v1.1.0` tree rather than by commit archaeology,
with a negative control. A reader trusting the old heading would have believed a shipped
feature was still pending.

### Fixed
- **`gr pr merge` no longer defaults to squash** (#829) — with no `--method`, the command queried the host for its allowed methods and took the first of squash > merge > rebase, so on any repo permitting squash the tool actively chose it. A workspace whose policy is merge-commit-only got squashes from its own tooling, and on a private repo — where hosting rulesets are unavailable on most plans — nothing downstream could reject the result. The default is now a real merge commit, configurable via `settings.merge_method` in the manifest. Choosing is the workspace's job; the host is asked only whether the choice is permitted.
- **An unavailable merge method is refused, not substituted** (#829) — if the chosen method is not permitted by the host, `gr pr merge` says so and stops rather than quietly using a different one. Silent substitution is the original defect in a politer form: the operator asks for one strategy, another happens, and the only way to find out is to count parents afterwards.
- **`gr pr merge` verifies the merge commit has two parents** (#829) — after a `merge`, the resulting commit is checked against the LOCAL repository rather than the platform's response, because the API reports that a merge happened and not which strategy produced it. Squash and rebase are exempt: they produce single-parent commits by design, and warning on them would make the check noise.

### Changed
- **`gr pr merge` gates are individually waivable, and what is waived is printed at the moment of the merge** (#837) — `--force` was a single boolean that suppressed the approval, checks and mergeability gates together. In a workspace whose ratification convention is review *comments* rather than formal platform approvals, the approval gate can never pass, so `--force` became mandatory on every merge — and took the other gates with it, silently. A gate that must always be bypassed does not gate; it trains the bypass. New `--skip-gate <approval|checks|mergeable>` waives exactly one, repeatable; `--force` still waives all but now names what it suppressed. An unknown gate name is an error rather than a silent no-op, because a misspelled waiver that quietly waives nothing reads exactly like a gate that passed.
- **`gr pr merge` prints the resolved target and method at the moment of the irreversible act** (#837) — `owner/repo#N branch -> base method=Merge`, printed durably immediately before the merge call rather than from the plan beforehand or the result afterwards. `gr pr merge` takes no PR number: it merges whatever PR the current branch owns, so the operator's belief about the target and the command's resolution of it are separate facts. Everything that goes wrong with this command goes wrong in that gap.

## [1.0.1] - 2026-07-23

### Fixed
- **`gr pr edit`/`review`/`merge` refuse unscoped multi-repo actions** (#773) — when a caller doesn't pass `--repo` and more than one repo has a matching open PR for its checked-out branch, gr now fails closed and lists exactly what would have been touched instead of silently fanning out. A repo's checked-out branch can be a stale, forgotten one from unrelated past work, so "N repos matched" was never the same thing as "the user meant N repos" — this closed two real incidents (grip#770, grip#771) where a body meant for one PR landed on someone else's unrelated one.
- **`gr checkout add` produces a self-discoverable child workspace** (#775) — unified the workspace discovery walk so a child checkout's manifest is found the same way regardless of nesting, removed a checkout/manifest-repo collision, and made malformed or reconstructed checkout metadata fail closed rather than silently carrying authority it shouldn't have.
- **`gr pr merge --wait` detects no-checks-configured and returns immediately** (#776) — previously could hang waiting on checks that were never going to run; now closes the false-absence and mutation-silence gaps in the re-poll guard.

## [1.0.0] - 2026-04-17

gr1 reaches stable. This release marks the feature-complete gr1 CLI before gr2 takes over as the primary workspace management system.

### Summary

gr1 ships as a production-ready multi-repo workspace tool with:

- **Manifest-based configuration**: declarative YAML workspace definitions with composable gripspace inheritance, named remotes, and fork workflow support
- **Synchronized operations**: branch, checkout, add, commit, push, pull, rebase, sync, and diff across all repos in parallel
- **Linked PR workflow**: create, review, merge, and track pull requests that span multiple repos with all-or-nothing merge strategy and auto-merge support
- **Griptrees**: worktree-based parallel workspaces for simultaneous multi-branch development with per-repo upstream tracking
- **Multi-platform support**: GitHub, GitLab, Azure DevOps, and Bitbucket with platform-specific adapters and rate limiting
- **Agent orchestration**: `gr spawn` for multi-agent session management with tmux, graceful shutdown, and configurable restart policies
- **Developer tooling**: grep, prune, gc, cherry-pick, verify, release, benchmarks, shell completions, MCP server, and agent context generation
- **Security hardening**: path traversal protection, credential sanitization, mutex poison recovery, thread panic propagation

27 releases. 60+ commands. 4 hosting platforms. Production-tested across the synapt multi-agent team since January 2026.

### Deprecation Notice

**gr1 is now in maintenance mode.** gr2 (Python-first workspace orchestration) is the active development target. gr2 introduces declarative workspace specs, execution plans, lane-aware commands, and a migration path from gr1 gripspaces.

See `gr2/docs/GR2-MVP.md` for the gr2 feature set and `gr2/docs/GR1-GR2-MIGRATION-PLAYBOOK.md` for migration instructions.

New features will land in gr2. gr1 will receive only critical bug fixes.

## [0.20.0] - 2026-04-14

### Added
- **`gr spawn down` graceful shutdown** (#567)
  - Three-phase shutdown: send `/exit` to agents, poll `pane_dead`, force-kill remaining
  - `pane_exit_state()` with tri-state return (`Option<bool>`) for proper tmux error handling
  - Per-agent shutdown via `--agent <name>` flag
  - Configurable timeout via `--timeout` flag (default 10s)
  - Full error reporting on all tmux operations

- **Python-first gr2 workspace orchestration** (#566)
  - Complete Python gr2 CLI with typer: spec, plan, apply, exec, review, workspace, repo, lane, lease commands
  - Cache-backed materialization for workspace apply
  - Structured hook runtime with dataclasses and lifecycle stages
  - Review checkout-pr with remote refetch on existing branches
  - gr1 detect and migration commands

## [0.19.0] - 2026-04-14

### Added
- **gr2 team-workspace model** — declarative spec, plan, and apply lifecycle
  - `gr2 init` creates team workspace structure (agents/, repos/, .grip/)
  - `gr2 spec show/validate` for workspace spec management
  - `gr2 plan` diffs workspace spec into an execution plan
  - `gr2 apply` materializes repos via git clone into unit workspaces (#514)
  - `gr2 apply --autostash` automatically stashes and restores dirty repos (#534)
  - Partial unit convergence: detects and clones missing repos in existing units (#539)
  - `gr2 team add/list/remove` for agent workspace management
  - `gr2 repo add/list/remove` for repo registry
  - `gr2 unit add/list/remove` for unit registry
  - Guard checks with dirty state detection via `git status --porcelain`
  - Stash state audit trail in `.grip/state/stash.toml`
- **Checkout lifecycle commands** (#489) — `gr checkout --create`, `--orphan`
- **Cache-backed checkout creation** (#485) — checkout creates from local cache when available
- **Machine-level manifest repo caches** (#484) — shared manifest caches across workspaces
- **`gr migrate in-place`** (#458) — upgrade existing workspaces without re-cloning
- **E2E demo script** (#454) — automated release verification

### Changed
- **gr2 binary removed from main crate** — gr2 development continues as a standalone Python CLI; Rust gr2 code retained as library
- CI: Windows tests run in non-blocking lane (#487)
- Stripped non-OSS prompts from migrate flow (#510)

### Fixed
- Spawn model passthrough from agents.toml (#474)
- Root manifest creation during migrate in-place (#467)
- Auto-reclone spaces/main when not a git repo (#470)
- Migrate linked worktrees into griptrees (#466)
- Preserve .env at gripspace root during worktree repair
- Stable pane IDs for dashboard targeting (#453)
- Benchmark CI fixture literals

## [0.17.1] - 2026-03-12

### Added
- **`gr issue` commands** (#239)
  - `gr issue list` — list issues with state, label, and assignee filters
  - `gr issue create -t "title"` — create issues with body, labels, and assignees
  - `gr issue view <number>` — view full issue details
  - `gr issue close <number>` — close an issue (with PR guard)
  - `gr issue reopen <number>` — reopen an issue (with PR guard)
  - Per-repo targeting with `--repo` flag; auto-selects single-repo workspaces
  - `--json` output for all subcommands
  - MCP server integration via `gitgrip_issue` tool
  - Pagination to fill requested `--limit` despite GitHub's mixed issues/PRs endpoint
  - URL-encoded query parameters for labels and assignees
- **`gr pr edit` command** (#257)
  - Update PR title and/or body across linked PRs
  - `gr pr edit --title "new title"` to update title
  - `gr pr edit --body "new body"` to update body
  - Updates all open PRs on the current branch across repos
  - JSON output support with `--json`
- **`gr pr create` multi-branch support** (#225)
  - Repos on different branches are now grouped by branch with PRs created per group
  - `--repo` filter for targeting specific repos
- **`gr push` skip repos with no unique commits** (#372)
  - New branches with no unique commits are skipped instead of causing push errors

### Fixed
- **`gr add` partial staging** (#374)
  - Stage files individually so one missing path doesn't block the rest
  - Warn instead of error when a pathspec doesn't match in a repo

## [0.17.0] - 2026-03-11

### Added
- **Init wizard with auto-detection** (#361)
  - `gr init --from-dirs` now auto-detects language, package manager, and toolchain commands for 9 languages (Rust, TypeScript, JavaScript, Python, Go, Ruby, Java, C++, C)
  - Interactive multi-step wizard: repo selection, post-sync hooks, agent context targets, manifest review
  - Auto-populates per-repo `agent:` config (language, build, test, lint, format commands)
  - Generates workspace scripts (`build-all`, `test-all`, per-repo variants)
  - Generates `workspace.hooks.post_sync` for repos with install commands (e.g., `pnpm install`, `uv sync`)
  - Generates `workspace.agent` with description, conventions, and workflows
  - TTY auto-detection: wizard runs automatically on TTY, non-interactive when piped
  - `--no-interactive` flag to explicitly skip the wizard
  - YAML section comments in generated manifests
- **`gr restore` command** (#359)
  - Unstage files with `gr restore --staged`
  - Discard working tree changes with `gr restore`
  - `--repo` filtering support
- **Per-repo `--repo` filtering on all commands** (#358)
- **`gr target` command** for managing PR base branches (#336)
- **MCP stdio server** for agent tool access (#335)
  - CLI passthrough tools and resource endpoints
  - Cancellation support and bounded output capture
- **`gr pr list` and `gr pr view` commands** (#202)
  - `gr pr list` — list PRs across all repos with `--state`, `--repo`, `--limit` filters
  - `gr pr view [number]` — view PR details, reviews, and body (auto-detects from current branch)
  - `list_pull_requests` platform trait method with GitHub implementation

- **`gr pr checks` improvements** (#238)
  - `--repo` flag to filter checks to a specific repository
  - Skip reference repos from checks output
  - Deduplicate stale check runs (prefer terminal states over pending for same context)

### Fixed
- Deep-merge gripspace repo config with local overrides (#356)
- Status typechanges, in-progress operations, and tree list detection (#355)
- Azure PR diff auth and `--no-delete-branch` flag (#345)
- Surface per-repo errors in `gr pr create/merge` summaries (#344)
- Manifest repo handling in branch and sync (#357)

### Changed
- Extract command dispatch from `main.rs` into `src/cli/dispatch.rs` (#352)
- Introduce option structs for complex function signatures (#350)
- Change `&PathBuf` to `&Path` across all function signatures (#349)
- Extract shared HTTP client creation for platform adapters (#348)
- Remove duplicated code and unused test helpers (#347)
- Wire up rate limiting across platform adapters (#351)

## [0.16.0] - 2026-02-20

### Breaking Changes
- **Manifest v2 schema** (#332)
  - `default_branch` renamed to `revision` (aligns with git-repo terminology)
  - `target` is now a branch name only (e.g. `develop`), not `remote/branch` format
  - New `sync_remote` field: remote for fetch/rebase (default: `origin`)
  - New `push_remote` field: remote for push (default: `origin`)
  - New top-level `remotes` section for named remotes with base fetch URLs
  - Repos can use `remote: upstream` instead of explicit `url` (URL derived from remote base + repo name + `.git`)
  - Default version bumped to 2
  - v1 manifests auto-migrate: `default_branch` aliased to `revision`, `target: upstream/develop` split into `target: develop` + `sync_remote: upstream`

### Added
- **Named remotes** — declare remotes at manifest top level with base URLs for URL derivation
- **`ensure_remote_configured`** — `gr sync` auto-configures declared remotes (e.g. upstream) in repos
- **Fork workflow support** — separate `sync_remote` (fetch from upstream) and `push_remote` (push to fork)

## [0.15.0] - 2026-02-18

### Added
- **Per-repo workflow `target` branch** (#329, #330)
  - New `target` field on repos and settings for specifying the workflow target branch (PRs, pull, rebase, prune, sync)
  - Supports `remote/branch` format (e.g. `origin/develop`, `upstream/main`) for fork workflows
  - Resolution chain: `repo.target` → `settings.target` → `repo.default_branch` → `settings.default_branch` → `"main"`
  - `default_branch` is now optional at repo level, inheritable from `settings.default_branch`
  - `gr repo add --target` flag for setting target on new repos
  - `gr status` column renamed from "vs main" to "vs target"
  - Full backward compatibility: existing manifests without `target` behave identically

## [0.14.1] - 2026-02-13

### Fixed
- **`gr pr merge --method squash`** no longer switches repos to a branch named "squash" (#251)
  - Merge method now uses clap `ValueEnum` for type-safe parsing
  - Invalid values are rejected at CLI level with `[possible values: merge, squash, rebase]`
  - Removed ambiguous `-m` short flag (use `--method` instead)
- **`gr init --from-dirs`** now detects the remote's default branch instead of using the current local branch (#310)
  - Checks `origin/HEAD` symref, then remote tracking branches, then local branches
  - Previously picked up feature branches as `default_branch` in the generated manifest

### Changed
- Improved test coverage from 57.84% to 59.56% with 90+ new unit tests across 14 files (#325)

## [0.14.0] - 2026-02-13

### Added
- **`gr sync --rollback`** - Rollback all repos to their state before the last sync (#323)
  - Pre-sync snapshot saved to `.gitgrip/sync-state.json`
  - Restores each repo's branch and HEAD commit
- **GitBackend/GitRepo traits** - Testable git abstraction layer for swappable implementations (#322)
- **OutputSink trait** - Testable output abstraction with `TerminalSink` and `BufferSink` (#321)
- **Platform capability matrix** - Documentation and code showing which operations each platform supports (#320)
- **GitHub Pages landing page** (#307)

### Changed
- **WorkspaceContext** consolidates workspace_root, manifest, quiet, verbose, json, sink, and git_backend into a single struct passed to commands (#318)
- **Trimmed tokio features** from `"full"` to `["rt-multi-thread", "macros", "process", "time"]` reducing compile-time dependencies (#319)
- Moved repo-local skill files to gripspace for single-source management (#311)

### Fixed
- **Thread panic handling** - JoinSet tasks now propagate panics instead of silently dropping them (#315)
- **Mutex poison recovery** - Git status cache recovers from poisoned locks instead of panicking (#315)
- **Branch `--json` output** - JSON mode now works correctly for branch operations (#316)
- **Git lock detection** - Detects stale `.git/index.lock` files and provides actionable guidance (#316)
- **GHE auto-merge** - GitHub Enterprise auto-merge now works correctly (#316)
- **Windows stack overflow** on debug builds resolved (#314)
- **GitHub platform test failures** resolved (#312)

## [0.13.0] - 2026-02-11

### Added
- **`gr agent` command** - AI coding tool context discovery and workspace operations (#289)
  - `gr agent context` — Full workspace context with repos, build commands, and conventions
  - `gr agent context --repo <name>` — Single repo context
  - `gr agent build <repo>` / `gr agent test <repo>` — Run build/test for a specific repo
  - All subcommands support `--json` for machine consumption
- **`gr agent generate-context`** - Multi-tool context generation from a single source (#290)
  - Define context once in manifest, generate for Claude, OpenCode, Codex, Cursor, and raw formats
  - `{repo}` placeholder in dest paths generates per-repo skill files
  - `compose_with` appends additional files to generated context
  - Runs automatically during `gr sync`; standalone with `gr agent generate-context`
  - `--dry-run` flag to preview without writing
- **`gr verify`** - Boolean pass/fail assertions for CI and scripting (#284)
  - `--clean` — Assert all repos have no uncommitted changes
  - `--on-branch <name>` — Assert all repos are on a specific branch
  - `--synced` — Assert repos are up to date with remote
  - Returns exit code 0/1 for scripting; supports `--json` output
- **`gr release`** - Automated release workflow (#287)
  - Bumps version, updates changelog, creates PR, tags, and creates GitHub release
  - `--dry-run` for preview
- **`--json` global flag** - Machine-readable JSON output on all commands (#282)
  - Structured output for `gr status`, `gr diff`, `gr pr status`, `gr verify`, `gr link --status`, and more
- **`--wait` flag for `gr pr merge`** - Poll CI checks before merging (#285)
  - Configurable timeout with `--timeout <seconds>` (default: 300s)
  - Visual spinner showing elapsed time and check status
- **Post-sync hooks** - Run commands automatically after `gr sync` (#286)
  - `post_sync` hooks in workspace manifest with optional `condition` triggers
  - `file_changed` condition to only run when specific files change
- **Agent manifest config** - Define agent-relevant metadata per-repo in manifest (#288)
  - `agent.description`, `agent.language`, `agent.build`, `agent.test`, `agent.lint`
  - Workspace-level `agent.conventions` for cross-repo coding standards

### Fixed
- **`gr pr merge` silent failure** - Now verifies PR state after merge API call to catch cases where GitHub reports success but PR wasn't actually merged (#283)
- **`gr pr merge --wait` timeout** - Timeout now properly bails with error instead of silently allowing merge to proceed (#305)
- **`gr pr merge --wait` early exit** - Checks loop now exits early when all checks have definitively resolved (no more pending)
- **Gripspace repo groups** - Repos inherited from included gripspaces can now be added to groups (#294)

## [0.12.3] - 2026-02-10

### Added
- **Auto-apply links on sync** - `gr sync` now automatically applies linkfiles and copyfiles after syncing repos, eliminating the need to manually run `gr link --apply` (#279)

## [0.12.2] - 2026-02-10

### Added
- **ASCII logo** - Running `gr` with no subcommand now displays a colored ASCII art logo and wordmark (#244)

## [0.12.1] - 2026-02-10

### Fixed
- **Gripspace-inherited repos now visible to all commands** - `gr status`, `gr repo list`, `gr branch`, and all other commands now see repos inherited from gripspace includes, not just `gr sync` (#273)

### Changed
- **Renamed internal manifest loader** - `load_workspace()` → `load_gripspace()`, `resolve_workspace_manifest_path()` → `resolve_gripspace_manifest_path()` for naming consistency with gripspace terminology

## [0.12.0] - 2026-02-09

### Added
- **Gripspace includes** - Composable manifest inheritance via `gripspaces:` directive (#270)
  - Clone external gripspace repositories and merge their repos, scripts, env, hooks, linkfiles, and copyfiles into the local workspace
  - Recursive resolution with DAG-aware cycle detection (max depth 5)
  - Local manifest values always win on conflict
  - `gr sync` and `gr init` resolve and clone gripspaces automatically
  - `gr status` shows gripspace clone status with revision and dirty state
- **Composefile support** - Generate files by concatenating parts from gripspaces and/or local manifest (`composefile:` directive)
  - Processed on `gr sync` and `gr link --apply`
  - Parts can reference gripspace content or local manifest content
- **URL normalization** - SSH and HTTPS URLs to the same repo are recognized as equivalent for space reuse (`git@github.com:user/repo` ↔ `https://github.com/user/repo`)
- **Manifest paths module** - Consistent path resolution across all commands (`manifest_paths.rs`)

### Changed
- **Unified directory layout** - `.gitgrip/spaces/` is now the single directory for all space content (gripspaces and manifest), replacing the previous split between `gripspaces/` and `spaces/`
- Reserved space names (`main`, `local`) are auto-suffixed to avoid conflicts with the manifest space

### Security
- Gripspace name validation with allowlist (`[a-zA-Z0-9._-]`), rejecting `.`, `..`, and path traversal
- Windows absolute path and UNC path rejection in path boundary checks
- Gripspace manifest validation via `validate_as_gripspace()` (allows empty repos, validates all other constraints)
- Failed clone cleanup — partial directories are removed on clone error
- Untrusted gripspace path hardening across all validators

## [0.11.3] - 2026-02-09

### Changed
- **Griptree branch tracking at creation** - `gr tree add` now sets each repo's griptree branch to track its upstream default (`origin/main`, `origin/dev`, etc.) (#267)
- **Griptree sync self-healing** - `gr sync` now repairs branch upstream tracking when on the griptree base branch, using per-repo mapping from `griptree.json` (#267)

### Documentation
- Updated workflow docs to prefer `gr checkout --base` after merge cleanup
- Updated command docs to include `gr tree return` and `gr sync --reset-refs`

### Testing
- Added integration coverage for upstream tracking during `gr tree add`
- Added integration coverage for upstream tracking repair during `gr sync`

## [0.11.2] - 2026-02-09

### Fixed
- **Griptree registry resolution** - `gr tree list` now resolves griptrees from the main workspace when run inside a griptree (#263)
  - Also applies to `gr tree remove`, `gr tree lock`, and `gr tree unlock`
  - Added regression coverage for list/remove/lock from griptree workspaces
- **Worktree-safe ref reset** - `gr sync --reset-refs` now falls back to detached checkout when target branch is locked by another worktree (#265)
  - Prevents false failures when refs are shared across multiple worktrees
  - Keeps hard reset behavior and adds explicit fallback status output

## [0.11.1] - 2026-02-08

### Fixed
- **Reference repo sync alignment** - `gr sync --reset-refs` now checks out the correct upstream branch before hard-resetting (#259)
  - Warns before discarding uncommitted changes or unpushed commits
  - Properly aligns reference repos to upstream branch (e.g., `origin/dev`) instead of staying on wrong branch
  - New `checkout_branch_at_upstream` git helper with worktree conflict detection

### Added
- `gr tree return` command to switch back to main workspace (#254)
- `gr sync --reset-refs` flag to hard-reset reference repos to upstream (#255)

## [0.11.0] - 2026-02-07

### Added
- **Griptree upstream tracking** - Per-repo upstream branch configuration for griptrees (#246)
  - `gr tree add` now auto-detects and records upstream for each repo in `griptree.json`
  - `gr sync` uses per-repo upstream mapping when on griptree base branch
  - `gr rebase --upstream` uses griptree upstream mapping instead of hardcoded `origin/main`
  - `gr checkout --base` - new flag to checkout the griptree base branch across all repos
  - Upstream validation with clear error messages for malformed refs
- **Pull command** - `gr pull` for pulling changes across repos (#234)
  - `--rebase` flag for rebase-based pulls
  - `--sequential` flag for ordered output
  - `--group` flag for group-scoped pulls
- **Terminal UI improvements** - verbose mode and debug output (#232)
- **Rate limiting** - Infrastructure for platform API rate limiting (#153)
- **`--verbose` global flag** - Shows external commands being executed (#204)

### Changed
- `gr rebase --upstream` now uses griptree config for per-repo upstream resolution
- Refactored `InitOptions` and `BranchOptions` into dedicated structs (#222)
- Simplified `check_repo_for_changes` in PR merge (#221)
- Consolidated duplicate manifest helper functions (#220)

### Fixed
- **Production readiness hardening** (#247)
  - Replaced `process::exit(1)` with proper error propagation in commit command
  - Poisoned mutex recovery in git status cache (4 locations)
  - Safe `SystemTime` fallback in retry jitter
  - Descriptive `expect()` for hardcoded progress bar templates
  - Improved path traversal detection with depth-tracking segment walk
  - Credential sanitization in git command logging
  - Symlink destination validation within workspace boundaries
  - HTTP client fallback logging in GitLab, Azure DevOps, and Bitbucket adapters
- Fixed HTTP client recursion in platform adapters (#242)

### Testing
- 75+ new integration tests covering edge cases and error scenarios
- Pull error handling tests (#235)
- Unit tests for rate_limit, types, bench, and PR commands (#228)
- PR merge command integration tests (#206)

## [0.10.0] - 2026-02-05

### Added
- **Parallel sync** - `gr sync` now runs in parallel by default for faster syncing
  - Use `--sequential` flag for sequential sync (previous behavior)
- **Checkout create flag** - `gr checkout -b <branch>` creates and switches to branch in one command
  - Creates branch if it doesn't exist, checks out if it does
- **Manifest schema command** - `gr manifest schema` displays manifest specification
  - `--format yaml` (default), `--format json`, or `--format markdown`
- **Group management** - Interactive repo grouping commands
  - `gr group add <group> <repos...>` - add repos to a group
  - `gr group remove <group> <repos...>` - remove repos from a group
  - `gr group create <name>` - shows how to create groups

### Changed
- **Consistent manifest handling** - Manifest repo now included in all operations:
  - `gr sync` - syncs manifest repo first
  - `gr branch` - creates/deletes branches in manifest repo
  - `gr checkout` - checks out manifest repo with other repos
  - `gr push` - pushes manifest repo if it has changes
  - `gr diff` - shows manifest repo changes
- Added `get_manifest_repo_info()` helper in `src/core/repo.rs` for reusable manifest repo handling

### Fixed
- Manifest repo was inconsistently handled across commands (fixes #210, #214)

## [0.9.0] - 2026-02-05

### Added
- `gr pr merge --update` flag - automatically update branch from base when behind, then retry merge
- `gr pr merge --auto` flag - enable auto-merge so PRs merge when all required checks pass
- `BranchBehind` error variant - detects when PR branch is behind base and provides actionable hint
- `BranchProtected` error variant - detects branch protection rule violations and suggests `--auto` or `--admin`
- `update_branch` platform trait method with GitHub implementation (PUT update-branch API)
- `enable_auto_merge` platform trait method with GitHub implementation (via `gh` CLI)
- wiremock tests for branch-behind, branch-protected, update-branch success, and update-branch conflict scenarios

### Changed
- `merge_pull_request` in GitHub adapter now uses raw HTTP instead of octocrab for proper error classification
  - octocrab swallowed HTTP response bodies, making it impossible to distinguish error types
  - Now correctly parses 405 (branch behind), 403 (branch protected), and other failure modes
- CI workflow no longer uses path filters - runs on every push/PR to main (#175)

### Fixed
- `gr pr merge` "not mergeable" message now suggests `--update` when branch may be behind base

## [0.8.0] - 2026-02-04

### Added
- `gr prune` command - delete local branches merged into the default branch
  - Dry-run by default, `--execute` to actually delete
  - `--remote` flag to also prune remote tracking refs (`git fetch --prune`)
  - Reports summary of pruned branches across repos
- `gr grep` command - cross-repo search using `git grep`
  - Prefixes results with repo name for easy identification
  - `-i` flag for case-insensitive search
  - `--parallel` flag for concurrent search across repos
  - Supports pathspec filtering (`gr grep "pattern" -- "*.rs"`)
- Test harness with 40+ integration tests (Phases 0-3)
  - `WorkspaceBuilder` fixture for creating temporary workspaces with bare remotes
  - git_helpers module for test git operations
  - wiremock-based platform mocks for GitHub/GitLab/Azure
  - Tests for branch, checkout, sync, add, commit, push, status, forall, griptree, PR, and error scenarios

### Fixed
- `gr pr create` now includes repos without remote tracking branches
  - Previously `has_commits_ahead()` returned false when base ref was missing, silently skipping repos
  - Now assumes the branch has changes when neither remote nor local base ref exists
- Griptree worktree name parsing bug fix for names with path separators

### Improved
- Error messages across 5 key files with actionable recovery suggestions:
  - Push errors: interpreted messages for non-fast-forward, auth failure, network issues
  - PR create: clearer branch reference errors with guidance
  - Init: recovery suggestions for existing directory and missing manifest
  - Run: suggests `gr run --list` for missing scripts
  - Push: suggests `gr sync` for missing remote targets

## [0.7.1] - 2026-02-03

### Fixed
- `gr pr merge --force` now properly bypasses `all-or-nothing` merge strategy (#180)
  - Previously would stop on first failed merge even with `--force` flag
  - Now continues merging remaining PRs when one fails with `--force`
  - Shows warning for failed merges instead of hard stop
- `gr pr create` now detects uncommitted changes in manifest repo (#178)
  - Previously only checked for commits ahead of default branch
  - Now detects staged and unstaged changes as well
  - Properly handles manifest-only PR creation

### Documentation
- Updated skill documentation with complete manifest schema
- Added workflow patterns section (accidental main branch commits, single-repo operations)
- Documented `reference` repos and `platform` configuration options
- Added IMPROVEMENTS.md entries for discovered friction points

## [0.7.0] - 2026-02-02

### Changed (Breaking)
- `gr forall` now defaults to running commands only in repos with changes
  - Use `--all` flag for previous behavior (run in all repos)

### Added
- `gr branch --move` flag to move commits from current branch to a new branch
  - Creates new branch at HEAD, resets current branch to remote, checkouts new branch
- `gr branch --repo <names>` flag to operate on specific repos only

### Fixed
- Platform API timeouts now have explicit configuration (10s connect, 30s read/write)
  - Faster failure detection and clearer error messages
- Worktree branch conflicts now show helpful error messages with guidance
  - Explains the git limitation and suggests alternatives

## [0.6.0] - 2026-02-02

### Fixed
- Griptree branches now base off repo's default branch instead of HEAD
- Griptree worktrees now use griptree branch name, not current workspace branch
- Reference repo sync failures no longer block griptree creation (warning only)
- Automatic link application after griptree creation
- Manifest repo links (copyfile/linkfile) now properly processed
- Worktree cleanup on griptree removal using git2 prune
- Rollback on partial griptree creation failure
- Clone fallback when specified branch doesn't exist on remote

### Added
- Legacy griptree discovery in `gr tree list`
- Worktree tracking metadata (worktree_name, worktree_path, main_repo_path)
- state.json initialization in new griptrees

## [0.5.8] - 2026-02-02

### Added
- `gr pr create` now supports `-b/--body` flag for non-interactive PR description
- Griptree manifest worktree support - each griptree can have its own manifest worktree
- Branch tracking for griptrees - tracks original branch per repo for proper merge-back
- Reference repo sync - reference repos auto-sync with upstream before worktree creation
- `gr add` and `gr commit` now handle manifest worktree changes automatically
- `gr status` displays manifest worktree status as separate entry
- Griptree worktrees now prioritize repo's current branch instead of griptree branch
- Comprehensive test coverage for manifest worktree functionality (10 new tests)
- Documentation in IMPROVEMENTS.md for tracking completed and pending features
- Worktree conflict troubleshooting guide added to CONTRIBUTING.md
- Documentation for IMPROVEMENTS.md merge conflict behavior
- PLAN document for griptree repo branch implementation

### Changed
- Manifest loading prioritizes griptree's own manifest, falls back to main workspace
- IMPROVEMENTS.md reorganized to show completed vs pending features clearly

## [0.5.7] - 2026-02-01

## [0.5.6] - 2026-02-01

### Added
- Full Bitbucket API integration with PR create/merge/status/comment support
- `gr pr create` supports `--dry-run` for preview without creating actual PRs
- `gr pr create` supports `--push` flag to push branches before creating PRs
- Shell completions for bash, zsh, fish, elvish, powershell via `gr completions <shell>`

### Changed
- `gr sync` now succeeds when on a branch without upstream configured
- `gr push` now shows which repos failed and why
- Better CI status visibility in PR checks output
- Improved sync error messages showing which repos failed

### Fixed
- PR merge now recognizes passing GitHub Actions check runs correctly
- `gr repo add` YAML insertion correctly places repos under `repos:` section
- Griptree creation now writes `.griptree` pointer file for workspace detection
- Windows CI: Fixed libgit2-sys linking by adding advapi32.lib

## [0.5.5] - 2026-02-01

### Added
- Telemetry, tracing, and benchmarks infrastructure for performance monitoring
  - Optional telemetry feature flag
  - Correlation IDs for request tracing
  - Git operation metrics (fetch, pull, push timing)
  - Platform API metrics
- CI now triggers for markdown file changes (enables doc-only PRs)

### Fixed
- `gr sync` now succeeds when on a branch without upstream configured
  - Fetches from origin to update refs instead of failing
  - Reports "fetched (no upstream)" status
- Windows CI: Fixed libgit2-sys linking by adding advapi32.lib via build.rs and RUSTFLAGS
- `gr repo add` YAML insertion now correctly places repos under `repos:` section
- Griptree creation now writes `.griptree` pointer file for workspace detection

### Changed
- CI summary job added for branch protection compatibility

## [0.5.4] - 2026-02-01

### Added
- Reference repos feature - mark repos as read-only with `reference: true` in manifest
  - Reference repos are excluded from `gr branch`, `gr checkout`, `gr push`, and PR operations
  - Reference repos still sync with `gr sync` and appear in `gr status` with `[ref]` indicator
  - Useful for upstream dependencies, reference implementations, or docs you only read
- `gr status` now shows `[ref]` suffix for reference repos

## [0.5.3] - 2026-01-31

### Added
- `gr status` now shows "vs main" column with commits ahead/behind default branch
  - `↑N` for commits ahead of main
  - `↓N` for commits behind main
  - `-` when on the default branch
  - `✓` when feature branch is in sync with main
- Summary line shows count of repos ahead of main

## [0.5.2] - 2026-01-31

### Fixed
- Git operations now work correctly in griptree worktrees
  - Changed all git CLI calls to use `repo.workdir()` instead of `repo.path().parent()`
  - Fixes "fatal: this operation must be run in a work tree" errors for `gr sync`, `gr add`, `gr commit`, etc.
- Release workflow now uses `--allow-dirty` for crates.io publish to handle Cargo.lock changes

### Added
- Shell completions via `gr completions <shell>` (bash, zsh, fish, elvish, powershell)
- GitLab E2E PR workflow tests with Bearer token authentication
- `get_workdir()` helper function for worktree-compatible path resolution

## [0.5.1] - 2026-01-31

### Fixed
- `gr` commands now work from griptree directories by detecting `.griptree` marker file
  - Reads `mainWorkspace` field from `.griptree` and delegates to parent workspace
  - Fixes "fatal: this operation must be run in a work tree" errors when running from griptrees

## [0.5.0] - 2026-01-31

### Added
- `gr init --from-dirs` command to create workspace from existing local directories
  - Auto-scans current directory for git repositories
  - `--dirs` flag to scan specific directories only
  - `--interactive` flag for YAML preview and editing before save
  - Discovers remote URLs and default branches automatically
  - Handles duplicate names with auto-suffixing
  - Initializes manifest directory as git repo with initial commit

## [0.4.2] - 2026-01-29

### Fixed
- Griptree worktrees now use manifest paths (e.g., `./codi`) instead of repo names
- `gr` commands now work correctly from within griptree directories

## [0.4.1] - 2026-01-29

### Changed
- Renamed command from `gr griptree` to `gr tree` to avoid "gitgrip griptree" duplication
- Standalone references use "griptree" branding (e.g., "Create a griptree")
- Commands use `gr tree` (e.g., `gr tree add`, `gr tree list`)
- Config file remains `.griptree`

## [0.4.0] - 2026-01-29

### Added
- `gr tree` commands for worktree-based multi-branch workspaces (griptrees)
  - `gr tree add <branch>` - create parallel workspace on a branch
  - `gr tree list` - show all griptrees
  - `gr tree remove <branch>` - remove a griptree
  - `gr tree lock/unlock <branch>` - protect griptrees from removal
- `GitStatusCache` class for caching git status calls within command execution
- CI workflow with build/test/benchmarks on Node 18, 20, 22
- Griptree documentation graphics (`assets/griptree-concept.svg`, `assets/griptree-workflow.svg`)

### Changed
- **Performance:** Parallelized `push`, `sync`, and `commit` commands using `Promise.all()`
  - 3.4x speedup on `status` operation
  - 1.8x speedup on `branch-check` operation

## [0.3.1] - 2026-01-29

### Added
- `gr repo add <url>` command - add new repositories to workspace
  - Parses GitHub, GitLab, Azure DevOps URLs automatically
  - Updates manifest.yaml preserving comments
  - Clones repo and syncs to current workspace branch
  - Options: `--path`, `--name`, `--branch`, `--no-clone`

### Fixed
- `gr sync` no longer discards local commits on unpushed feature branches
  - Now checks if branch was ever pushed before auto-switching
  - Warns if local-only commits would be lost

## [0.2.4] - 2026-01-28

### Removed
- Removed backward compatibility for `.codi-repo/` directories
- Removed `gr migrate` command (no longer needed)

### Fixed
- Fixed PR linking - manifest PRs now include linked PR table with cross-references

## [0.2.3] - 2026-01-28

### Fixed
- Fixed PR linking - manifest PRs now include linked PR table with cross-references

## [0.2.2] - 2026-01-28

### Changed
- Updated branding with new emerald/green color scheme
- New icon design showing grip concept with central hub and three branches
- Updated README with centered banner and npm badges

## [0.2.1] - 2026-01-28

### Changed
- Renamed from `codi-repo` to `gitgrip`
- CLI command changed from `cr` to `gr`
- Directory changed from `.codi-repo/` to `.gitgrip/`
- Skill renamed from `codi-repo` to `gitgrip`
- All documentation updated to use new naming

### Added
- Backward compatibility for legacy `.codi-repo/` directories

## [0.2.0] - 2026-01-28

### Changed
- Initial release as `gitgrip` (renamed from codi-repo)

## [0.1.2] - 2026-01-27

### Added
- `gr forall` command - run commands in each repository (like AOSP repo forall)
- `gr add` command - stage changes across all repositories
- `gr diff` command - show diff across all repositories
- `gr commit` command - commit staged changes across all repositories
- `gr push` command - push current branch across all repositories
- `gr branch --repo` flag - create branches in specific repos only
- `--timing` flag for performance debugging
- `gr bench` command for benchmarking

### Fixed
- `gr pr create` now only checks branch consistency for repos with changes
- `gr pr status/merge` find PRs by checking each repo's own branch
- `gr sync` automatically recovers when manifest's upstream branch was deleted

## [0.1.1] - 2026-01-27

### Added
- Manifest repo (`.gitgrip/manifests/`) automatically included in commands
- `gr status` shows manifest in separate section
- `gr branch --include-manifest` flag

### Fixed
- Various stability improvements

## [0.1.0] - 2026-01-27

### Added
- Initial release
- `gr init` - initialize workspace from manifest
- `gr sync` - pull latest from all repos
- `gr status` - show status of all repos
- `gr branch` - create/list branches across repos
- `gr checkout` - checkout branch across repos
- `gr pr create` - create linked PRs
- `gr pr status` - show PR status
- `gr pr merge` - merge all linked PRs
- `gr link` - manage copyfile/linkfile entries
- `gr run` - execute workspace scripts
- `gr env` - show workspace environment variables
- Manifest-based configuration (AOSP-style)
- Linked PR workflow with all-or-nothing merge strategy
