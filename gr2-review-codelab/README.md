# Review a project's tests with `git review`: the ten-minute codelab

**Date: 2026-09-24. Written from a measured cold run, not composed backwards.** The
commands below were executed in order on a fresh, empty home directory (a scratch
`mktemp -d` as `$HOME`) on a machine with no gr1 or gr2 installed, using the wheel
that `uv` pulled from PyPI (`gitgrip 2.0.0a4`). Every time in the wall-clock box at
the end is from that run. If a number changes when you run it, your run is the
newer measurement, not a mistake.

What you end up with: one command that opens a review of any branch in a
repository you already have, runs that repository's own tests in a separate
environment, shows you a green/red summary bound to the exact tree it tested, and
removes everything it created. Total machine time measured: **about 11 seconds.**
The ten-minute budget is for reading this guide, not for waiting.

## 1. Install (measured: 1.4 s)

You need [uv](https://docs.astral.sh/uv/). One command:

```bash
uv tool install --pre gitgrip
```

Measured output (versions move; the shape is what matters):

```text
Installed 2 executables: git-review, gr2
warning: `/home/you/.local/bin` is not on your PATH.
```

That warning is the next step, not an error. `uv` installs the two executables
into a per-user bin directory and tells you where (the paths in this guide are
shortened; your printed path is longer). Either let uv fix your shell once:

```bash
uv tool update-shell
```

then open a new terminal so the PATH change applies. Alternatively, add the
printed directory to your PATH by hand for this session. Then verify you got
the gr2 line, not the older 1.5.1:

```bash
gr2 --version
```

Measured: `2.0.0a4`.

You now have two commands. `gr2` is the full workspace tool; `git-review` is the
one this codelab uses: a four-command review flow that runs inside any git
repository you already have. If you have `git` on your PATH, you can also type
it as `git review …` (git dispatches subcommands by executable name), which is
the form used below.

## 2. A repository to review

Use any git repository with a Python project you have on disk. If you want a
disposable one instead, this 30-second block builds a tiny project, the same
shape thousands of real projects have (a `pyproject.toml`, one module, one
test):

```bash
mkdir -p demo/plain_review_demo && cd demo/plain_review_demo
git init -q -b main
printf '[project]\nname = "plain-review-demo"\nversion = "0.1.0"\n' > pyproject.toml
mkdir plain_review_demo
printf 'def add(a, b):\n    return a + b\n' > plain_review_demo/__init__.py
printf 'from plain_review_demo import add\ndef test_add():\n    assert add(2, 2) == 4\n' > test_demo.py
git add -A && git commit -qm "tiny demo project"
```

These five lines are ordinary project setup a developer does anyway. The review
itself asks nothing about how the repository was made, and needs no second
remote, no bare repo, and no patch files.

## 3. Open the review (measured: 1.2 s)

From inside the repository:

```bash
git review open
```

Measured output:

```text
opened review: plain_review_demo base 1dbccd40d83a head 1dbccd40d83a
  warning: no diverging default branch found; base = HEAD (empty diff)
  stored at /…/.git/grip/review.json
```

Read that warning without alarm: on a single-branch repository this is the
ordinary shape, not a failure. The two short hashes in the `open` line are
the demo commit's own; yours will differ, and that is expected. `open` looks
for a branch that has diverged from
your default branch to use as the review's base; finding none, it reviews the
current commit against itself, which is exactly what you asked for when you
opened a review on a branch you just wrote. If your repository does have a
diverging branch, you will not see the warning, and the base is that branch's
common ancestor.

## 4. Run the tests (measured: 7.9 s on this demo)

The run verb is the one that needs to know about your project, and it says so
rather than guessing. Run bare on a project that declares nothing:

```bash
git review run
```

```text
refused: no_package: no --package given and this repo's .review-install declares
none; a package name is required so the install can be proven to resolve inside
the clone rather than in some other checkout on the path
```

That refusal introduces the two flags every project is different about, and it
stops instead of guessing because a green summary about the wrong import is
worse than a stop:

- `--package` is the **import name** of the code under review. The review
  environment installs the project and then proves it can import this name;
  refusing to guess keeps a green receipt from describing someone else's
  install.
- `--install` is the command that builds the review environment. `{venv}` means
  that environment's Python and `{lane}` means your repository; here, `pip
  install -e` the repo and add pytest, which a plain project needs because pytest
  is only a test dependency.

With the flags, the demo's run is green:

```bash
git review run --package plain_review_demo --install '{venv} -m pip install -e {lane} pytest'
```

Measured output:

```text
GREEN: selected=1 passed=1 failed=0 errors=0 skipped=0
  receipt /…/.git/grip/run.json
```

`GREEN` is bound to the exact tree `open` recorded; the run refuses if the
working tree has drifted from what was opened, so the counts describe the code
you think they describe.

If your project carries a `.review-install` file at its root (two lines naming
the same things), you type less: plain `git review run` and the file supplies
both. grip's own repository does this, which is how its ~1,900-test suite runs
as one bare command.

## 5. Read the result, close it out (measured: 0.5 s)

```bash
git review status
git review close
```

Measured (an excerpt; `status` also prints the review's name, base and head,
plus the empty-diff warning when there is no diverging branch):

```text
last run GREEN at <timestamp>: selected=1 passed=1 failed=0 errors=0
closed review
```

The timestamp is elided rather than shown: the tool prints it with
microseconds and a UTC offset, and every reader's value differs, so a
verbatim example would read as pasted while being the one string you
cannot have.

`status` shows the open review and the last run's counts. `close` removes the
review state, the separate environment it created, and the receipt; today
nothing of the run survives the close (keeping a receipt past close is a known
open item), so capture anything you want to keep before you close. Your
repository is left exactly as it was.

## 6. Other layouts (an example, not a step)

The two substitutions also fit projects whose package lives in a subdirectory
(a `packages/python` layout). Shown as text rather than a runnable step: this
demo's package sits at the repository root, so on it this command is the wrong
one.

```text
git review run --package mypackage --install '{venv} -m pip install -e {lane}/packages/python pytest'
```

## What just happened, in one paragraph

You installed a wheel from PyPI (1.4 s), told `git review` to treat your current
commit as the code under review (1.2 s), and it built an isolated environment,
installed your project there, ran its tests, and bound the green summary to that
exact tree (7.9 s). Nothing about the flow needed a second remote, a hand-built
patch, or any tool beyond git and the two commands above: the review lives
entirely inside your repository's own `.git`.

## Known in this alpha (dated 2026-09-24, measured on this run)

- The counts do not add up when a test is an expected failure: there is no
  `xfailed` column yet, so `selected` exceeds `passed + failed + skipped` by
  that many (tracked).
- An interrupted run leaves no `status` trace until its receipt is written
  (tracked).
- A repository with no `.review-install` stops at `no_package` on the first
  unflagged run; pass `--package` and, when pytest is not a runtime dependency,
  `--install` as above.

---
*Every number in this guide is from the measured run of 2026-09-24 on macOS
(Darwin 25.2.0, git 2.50.1, uv 0.11.7, Python 3.13 via the wheel's own
environment): install 1.4 s, open 1.2 s, run 7.9 s, close 0.5 s, review total
≈ 11 s. The `no_package` refusal and the empty-diff warning were both reproduced
on purpose during the run.*