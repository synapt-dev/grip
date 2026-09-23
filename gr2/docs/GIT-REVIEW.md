# `git review` — reviewing a single repository

`git review` is the one-clone front door. No workspace, no manifest, no
reconstruction lane: you are inside an ordinary git clone, and the clone itself is
the code under review. For a review that spans several repositories, use
`gr2 review` instead.

It installs as the console script `git-review`, so git's own `git-<name>`
convention resolves `git review …` inside any repository.

```bash
uv tool install --pre gitgrip   # brings both `gr2` and `git-review` (or: pip install --pre gitgrip)
cd some-clone
git review open          # bind HEAD (and its tree) as the code under review
git review run           # run this repo's own tests; record a receipt
git review status        # what is under review, and how the last run went
git review close         # drop the review and everything run created
```

## What makes a green mean something

`run` reuses the trust properties of `gr2 review run` rather than reimplementing
them:

- **The tracked tree must still equal the tree `open` bound.** A commit sha cannot
  answer "has this working tree been edited since open" — editing a tracked file
  leaves HEAD untouched — so `open` records the head *tree* and `run` compares
  against it. A clone edited after `open` is refused (`tree_drift`), because the run
  would not be about the code under review.
- **No untracked path the run did not create.** An injected `conftest.py` or shadow
  module changes what the tests do without touching the tracked tree, so the tree
  hash cannot see it. Refused as `untracked_drift`.
- **The import must resolve inside the clone.** A stale editable install elsewhere on
  the path would otherwise let a pass be about someone else's checkout.
- **Counts come from the runner's own summary line, never its exit code.** A run that
  collected zero tests, or whose summary cannot be parsed, is a refusal — not a green.

## The repo declares how to run itself

Put a `.review-install` file at the repo root and a reviewer types nothing but the
verb. Five keys are recognised; an unrecognised key is a refusal, not a silent skip.

```ini
package = demo_pkg                              # import name proven to resolve in the clone
install = {venv} -m pip install -e {lane} pytest  # {venv} = the review venv's python, {lane} = the clone
runner  = cargo                                 # optional: pytest (default), cargo, jest, or junit-xml
test    = ./gradlew test --rerun-tasks          # required when runner is not pytest
reports = **/build/test-results/**/*.xml        # optional: JUnit XML glob for junit-xml
```

With no `.review-install`, pass `--package` and/or `--install` on the command line.

For Gradle, Maven, and other tools that write JUnit XML, set `runner = junit-xml`
and make `test` run the project's test command. The default report glob is
`**/build/test-results/**/*.xml`; override it with `reports = ...` or `--reports`.
Only reports the test command creates or changes count. If Gradle skips an
up-to-date task and writes no changed report, the run refuses and tells you to use
`cleanTest` or `--rerun-tasks` rather than reporting a stale result as green.

## Where things are written

Everything lives under the repository's own git directory, so the reviewed working
tree is left as it was found:

| path | what |
|---|---|
| `<git-dir>/grip/review.json` | the open review: repo, base, head, head tree |
| `<git-dir>/grip/venv/` | the review venv (pytest runner only) |
| `<git-dir>/grip/run.json` | the last run's receipt, including a refusal |
| `<git-dir>/grip/run.log` | the runner's full output |

An editable install writes `<pkg>.egg-info/` into the source tree; `run` removes the
ones its own install created and names them in the receipt. A path that was already
untracked before the install is never touched.

## Exit codes

| code | meaning |
|---|---|
| 0 | green |
| 1 | red — the tests ran and something failed |
| 2 | refused — the run could not be trusted at all |

A refusal is deliberately not a red. A red is a fact about the code; a refusal is the
absence of a trustworthy fact, and the two call for different next moves.

## `git review --help` opens a man page

This is git's own behaviour, not a defect in the command: git intercepts `--help`
after a subcommand and hands it to `man git-review`, which does not exist, so you get
`No manual entry for git-review`. **Use `git review -h` or `git review help`** — both
reach the script. `git-review --help`, calling the console script directly, works too.
