# gr2 Frozen-Range Review — Stranger Walkthrough

**Date**: 2026-09-09 | **Tested**: 2026-09-09 by a first-time user against gitgrip 1.5.0 from PyPI

This walkthrough teaches the five-verb frozen-range review workflow by running it. You will reconstruct a code review from a frozen commit range, verify its correctness, and clean up. No prior gr2 experience assumed.

## 1. Install

Create an isolated Python environment outside any agent workspace:

```bash
VENV="$(mktemp -d)/venv"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install -U pip
pip install gitgrip==1.5.0
```

The `VENV` and `TEST_ROOT` variables live in the current shell only, so the entire walkthrough runs in one terminal session without reopening it.

Verify the install:

```bash
which gr2
/var/folders/XX/XXXXXXX/T/tmp.XXXXXX/venv/bin/gr2

python3 -c "import gr2; print(gr2.__file__)"
/var/folders/XX/XXXXXXX/T/tmp.XXXXXX/venv/lib/python3.13/site-packages/gr2/__init__.py
```

(Your `mktemp` path will differ; shown here as an example under `/var/folders`.)

**A note on deprecation**: The wheel includes a `gr2_overlay` shim for backward compatibility. On import, you may see:

```
DeprecationWarning: gr2_overlay is deprecated and will be removed in the release after 1.5.0
```

This is expected. Use `import gr2.overlay` directly; `gr2_overlay` is the deprecated name.

## 2. The Five Verbs

Set up a test workspace with two bare git repositories (the remotes) and two working clones (where you'll make changes):

```bash
TEST_ROOT="$(mktemp -d)/gr2-test-workspace"
mkdir -p "$TEST_ROOT/remotes"

# Create bare repos
git init --bare "$TEST_ROOT/remotes/repo1.git"
git init --bare "$TEST_ROOT/remotes/repo2.git"

# Clone them
git -C "$TEST_ROOT" clone file://"$TEST_ROOT"/remotes/repo1.git repo1
git -C "$TEST_ROOT" clone file://"$TEST_ROOT"/remotes/repo2.git repo2

# Add initial content
for n in 1 2; do
  git -C "$TEST_ROOT/repo$n" config user.email "test@example.com"
  git -C "$TEST_ROOT/repo$n" config user.name "Test User"
  echo "Repo $n initial content" > "$TEST_ROOT/repo$n/README.md"
  git -C "$TEST_ROOT/repo$n" add .
  git -C "$TEST_ROOT/repo$n" commit -m "repo$n: initial"
  git -C "$TEST_ROOT/repo$n" push -u origin main
done
```

Now make a change in repo1 that we'll review. Give it a minimal Python package structure so review run can install and test it:

```bash
# Create package structure
mkdir -p "$TEST_ROOT/repo1/gr2_test"
echo "# gr2_test package" > "$TEST_ROOT/repo1/gr2_test/__init__.py"
echo "def hello(): return 'world'" >> "$TEST_ROOT/repo1/gr2_test/__init__.py"

# Create a test
mkdir -p "$TEST_ROOT/repo1/tests"
cat > "$TEST_ROOT/repo1/tests/test_hello.py" << 'TEST'
from gr2_test import hello
def test_hello():
    assert hello() == "world"
TEST

# Create pyproject.toml
cat > "$TEST_ROOT/repo1/pyproject.toml" << 'PROJ'
[project]
name = "gr2-test"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = []
PROJ

# Commit the changes
git -C "$TEST_ROOT/repo1" add .
git -C "$TEST_ROOT/repo1" commit -m "feat: add gr2_test package with test"
```

Create the frozen range (the artifact that carries the change):

```bash
BASE=$(git -C "$TEST_ROOT/repo1" rev-parse origin/main)
HEAD=$(git -C "$TEST_ROOT/repo1" rev-parse main)
FREEZE_OUT="$TEST_ROOT/frozen-range"
mkdir -p "$FREEZE_OUT"

git -C "$TEST_ROOT/repo1" format-patch "$BASE" --stdout > "$FREEZE_OUT/range.patch"
git -C "$TEST_ROOT/repo1" log --format=fuller "$BASE..HEAD" > "$FREEZE_OUT/metadata.fuller.txt"
echo "test: review of gr2_test package addition" > "$FREEZE_OUT/title.txt"
echo "Test review of package structure and test addition." > "$FREEZE_OUT/body.txt"
```

Now run the five verbs:

### Verb 1: grip init

Initialize the workspace for review:

```bash
gr2 grip init "$TEST_ROOT"
```

**Output**:
```
Initialized .grip/ at <TEST_ROOT>
```

(Your mktemp path differs; the output shows your TEST_ROOT's expanded path.)

### Verb 2: review bind

Bind a review commit from the frozen range. **Critical**: specify `--ref refs/heads/main` to match your remote's branch (the default is `refs/heads/dev`; if your remote carries `main`, you must say so or bind will refuse with "base_not_live_head: expected '<sha>', observed ''").

```bash
gr2 review bind "$TEST_ROOT" \
  --from-range "$TEST_ROOT/frozen-range/range.patch" \
  --repo repo1 \
  --remote "$TEST_ROOT/remotes/repo1.git" \
  --base "$BASE" \
  --head "$HEAD" \
  --ref refs/heads/main
```

(Your `$BASE` and `$HEAD` values will differ; they come from the `rev-parse` commands in the setup block above.)

**Output**:
```
gr:66e685d19dbb7f93e1441cbf2ad88155114acc96
```

(Your commit hash will differ; that's the review bind commit, which you'll reference next.)

### Verb 3: review open-gr --enter

Reconstruct the review lane from the bind commit. This clones the remote at the base, applies the frozen range, and asserts the tree matches the head:

```bash
# paste the bind commit printed by Verb 2
BIND_COMMIT="gr:<paste-yours>"
LANE_DIR="$TEST_ROOT/review-lane"

gr2 review open-gr "$TEST_ROOT" "$BIND_COMMIT" \
  --lane-dir "$LANE_DIR" --enter
```

**Output**:
```
lane: <TEST_ROOT>/review-lane
bound_head: b0cb66aa83066775e1eb733859d48735eafc21c6
reconstructed_head: f80c0ebeaba66b19ad0d6c3a903c0fcb42cd84c7
tree_match: True
```

(Your mktemp path differs; the output shows your TEST_ROOT's expanded path. The reconstructed_head differs from bound_head because git apply re-creates the commit; the tree is what matters, and tree_match: True means the content is correct.)

### Verb 4: review run

Run tests inside the lane with the package installed. Both `--install` (the command to install the package into the lane) and `--package` (the module name to verify) are required:

```bash
LANE_DIR="$TEST_ROOT/review-lane"

gr2 review run "$LANE_DIR" --install "{venv} -m pip install -e {lane} pytest" --package gr2_test
```

**Output**:
```
green: selected=1 passed=1 failed=0 skipped=0 xfailed=0 errors=0
bound_head_tree: 69f816abccb137ede7dbcaf8e53956c96105e0ae
install resolved: <TEST_ROOT>/review-lane/gr2_test/__init__.py
```

The `--install` command is executed in the lane's venv (use `{venv}` and `{lane}` substitution tokens as shown). The `--package` flag names the module that must be importable. Pytest must be included in the install command for the test run to proceed.

### Verb 5: review close-gr

Reclaim the lane (delete the reconstruction, keep the record):

```bash
gr2 review close-gr "$LANE_DIR"
```

**Output**:
```
reclaimed <TEST_ROOT>/review-lane (gr:66e685d19dbb7f93e1441cbf2ad88155114acc96)
```

(The bind commit is the same as Verb 2's output because this is one continuous run: the package was set up before binding, so all five verbs operate on the same review artifact.)

## 3. What will refuse, and why

One situation will refuse as you run the verbs:

**review run without both --package and --install** (symptom: "no_package: no --package given and the lane's .review-install declares none")

Cause: gr2 must prove the reviewed package resolves and imports correctly in the lane. Both are required: pass `--package` with the module name, and `--install` with the command to install the package into the lane's venv (as shown in Verb 4). The install command must include pytest so tests can run.

## 4. Commands Outside the Five Verbs

The stranger's walk executed **9 shell commands** of setup (counting for-loops and heredocs as single commands each):
- 3 commands to initialize git repos and push
- 4 commands to create package structure, pyproject.toml, and test
- 2 commands to freeze the range

Then, **zero commands outside the five gr2 verbs** to complete the review workflow.
