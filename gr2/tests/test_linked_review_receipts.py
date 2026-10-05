"""Bound receipt consumers on ordinary and linked checkouts, with local Git only."""
import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import commit, push, review_records as records
from gr2.prototypes import lane_workspace_prototype as lanes


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def world(tmp_path):
    ordinary = tmp_path / 'ordinary'
    ordinary.mkdir()
    git(ordinary, 'init', '-q')
    git(ordinary, 'config', 'user.name', 'Test')
    git(ordinary, 'config', 'user.email', 'test@example.invalid')
    (ordinary / 'file').write_text('base\n')
    git(ordinary, 'add', '.')
    git(ordinary, 'commit', '-qm', 'base')
    base = git(ordinary, 'rev-parse', 'HEAD')
    (ordinary / 'file').write_text('head\n')
    git(ordinary, 'commit', '-qam', 'head')
    head = git(ordinary, 'rev-parse', 'HEAD')
    repos = [ordinary]
    for name in ('linked-one', 'linked-two'):
        repo = tmp_path / name
        git(ordinary, 'worktree', 'add', '-q', '-b', name, str(repo), head)
        repos.append(repo)
    workspace = tmp_path / 'workspace'
    for repo in repos:
        doc = lanes.lane_file(workspace, 'owner', repo.name)
        doc.parent.mkdir(parents=True)
        doc.write_text('lane_kind = "bound"\nrepos = ["member"]\nbound_worktree = '
                       + json.dumps(str(repo)) + '\nbound_head = ' + json.dumps(head) + '\n')
    return workspace, repos, base, head


def coordinates(world, repo):
    return records.review_record_paths(world[0], 'owner', repo.name, 'member', repo)


def bind(world, repo):
    return lanes.bind_bound_lane(world[0], 'owner', repo.name, base=world[2], allow_local=True)


def test_three_checkouts_bind_and_validate_before_stub_push(world, monkeypatch):
    pointers = []
    calls = []
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda repo, **kw: calls.append(repo))
    for repo in world[1]:
        receipt = bind(world, repo)
        paths = coordinates(world, repo)
        pointer = records.review_record_pointer_path(repo)
        pointers.append(pointer)
        assert pointer.read_text() == str(paths.current) + '\n'
        assert records.lane_paths_for_repo(repo) == paths
        assert json.loads(paths.current.read_text()) == receipt.to_dict()
        assert (receipt.base, receipt.head, receipt.lane_kind) == (world[2], world[3], 'bound')
        assert not paths.legacy.exists()
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)
    assert calls == world[1]
    assert len(set(pointers)) == 3


@pytest.mark.parametrize('content', [None, '[]', '{bad', '{"lane_kind":"review-ephemeral"}'])
def test_missing_malformed_disposable_refuses_before_push(world, monkeypatch, content):
    repo = world[1][1]
    bind(world, repo)
    paths = coordinates(world, repo)
    if content is None:
        paths.current.unlink()
    else:
        paths.current.write_text(content)
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda *a, **kw: pytest.fail('push reached'))
    with pytest.raises(SystemExit):
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)


def test_legacy_disposable_cannot_hide_under_canonical_bound(world, monkeypatch):
    repo = world[1][1]
    record = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    for module, error in ((commit, commit.CommitError), (push, push.PushError)):
        module._refuse_review_ephemeral_repo(repo)  # nondisposable positive control
        paths.legacy.write_text(json.dumps({**record, 'lane_kind': 'review-ephemeral'}))
        with pytest.raises(error):
            module._refuse_review_ephemeral_repo(repo)
        paths.legacy.unlink()
    paths.legacy.write_text(json.dumps({**record, 'lane_kind': 'review-ephemeral'}))
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda *a, **kw: pytest.fail('push reached'))
    with pytest.raises(SystemExit):
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)


def test_legacy_bound_read_only_fallback(world, monkeypatch, capsys):
    repo = world[1][2]
    record = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    paths.current.unlink()
    paths.legacy.write_text(json.dumps(record))
    previous = paths.legacy.read_bytes()
    calls = []
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda *a, **kw: calls.append(a))
    lanes.pr_create_bound_lane(world[0], 'owner', repo.name)
    assert len(calls) == 1
    assert 'legacy review record' in capsys.readouterr().out
    assert paths.legacy.read_bytes() == previous
    assert not paths.current.exists()


@pytest.mark.parametrize('prior', [False, True])
def test_pointer_failure_preserves_pair_or_leaves_no_new_payload(world, monkeypatch, prior):
    repo = world[1][1]
    old = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    pointer = records.review_record_pointer_path(repo)
    old_pointer = pointer.read_bytes()
    old_payload = paths.current.read_bytes()
    if not prior:
        paths = records.review_record_paths(world[0], 'owner', 'fresh', 'member', repo)
    replace = records.os.replace
    def fail_pointer(source, target):
        if Path(target) == pointer:
            raise OSError('pointer publication failed')
        return replace(source, target)
    monkeypatch.setattr(records.os, 'replace', fail_pointer)
    with pytest.raises(OSError, match='pointer publication failed'):
        records.write_review_record(paths, {**old, 'head': world[2]})
    assert pointer.read_bytes() == old_pointer
    if prior:
        assert paths.current.read_bytes() == old_payload
    else:
        assert not paths.current.exists()
    assert not list(paths.current.parent.glob('.review-*'))


def test_failed_compensation_keeps_recoverable_prior_payload(world, monkeypatch):
    repo = world[1][1]
    old = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    pointer = records.review_record_pointer_path(repo)
    old_pointer, old_payload = pointer.read_bytes(), paths.current.read_bytes()
    replace = records.os.replace
    published = False
    def fail_twice(source, target):
        nonlocal published
        if Path(target) == pointer:
            raise OSError('pointer fault')
        if Path(target) == paths.current:
            if published:
                raise OSError('rollback fault')
            published = True
        return replace(source, target)
    monkeypatch.setattr(records.os, 'replace', fail_twice)
    with pytest.raises(records.ReviewRecordLocationError) as caught:
        records.write_review_record(paths, {**old, 'head': world[2]})
    message = str(caught.value)
    assert 'pointer fault' in message and 'rollback fault' in message
    recovery = Path(message.split('preserved for recovery at ', 1)[1])
    assert recovery.read_bytes() == old_payload
    assert pointer.read_bytes() == old_pointer
    assert list(paths.current.parent.glob('.review-*')) == [recovery]


def test_existing_non_git_directory_refuses_and_future_clone_coordinate_is_safe(tmp_path):
    future = tmp_path / 'future'
    assert records.legacy_review_record_path(future) == future / '.git' / 'grip-review.json'
    future.mkdir()
    with pytest.raises(records.ReviewRecordLocationError):
        records.legacy_review_record_path(future)


def test_close_never_deletes_bound_linked_author_checkout(world):
    from gr2.python_cli.review import ReviewError, close_review_lane
    repo = world[1][1]
    bind(world, repo)
    # Even a linked worktree beneath the supplied ownership boundary is not an
    # owned clone. Its Git-aware receipt does not grant recursive-delete rights.
    with pytest.raises(ReviewError, match='owned .git directory'):
        close_review_lane(lane_repo_root=repo, review_lane_root=repo.parent,
                          workspace_root=world[0], owner_unit='owner',
                          lane_name=repo.name, member='member')
    assert repo.is_dir() and (repo / '.git').is_file()
    assert git(repo, 'rev-parse', 'HEAD') == world[3]
    with pytest.raises(ReviewError, match='not strictly beneath'):
        close_review_lane(lane_repo_root=repo, review_lane_root=repo,
                          workspace_root=world[0], owner_unit='owner',
                          lane_name=repo.name, member='member')


@pytest.mark.parametrize('replacement', [
    {'repo': 'local:/different'}, {'head': 'f' * 40}, {'base': 'f' * 40},
    {'extra': 'field'}, {'base': []},
])
def test_receipt_identity_and_range_mismatch_refuse_before_push(world, monkeypatch, replacement):
    repo = world[1][2]
    record = bind(world, repo).to_dict()
    coordinates(world, repo).current.write_text(json.dumps({**record, **replacement}))
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda *a, **kw: pytest.fail('push reached'))
    with pytest.raises(SystemExit, match='identity/range'):
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)


@pytest.mark.parametrize('target', ['payload', 'pointer'])
def test_symlink_publication_refuses_without_changing_prior_pair(world, target):
    repo = world[1][1]
    record = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    pointer = records.review_record_pointer_path(repo)
    path = paths.current if target == 'payload' else pointer
    prior = path.read_bytes()
    saved = path.with_suffix('.saved')
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(records.ReviewRecordLocationError, match='symlink'):
        records.write_review_record(paths, record)
    assert path.is_symlink() and saved.read_bytes() == prior
