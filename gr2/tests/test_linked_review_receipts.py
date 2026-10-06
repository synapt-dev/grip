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
        doc.write_text('owner_unit = "owner"\nlane_name = ' + json.dumps(repo.name) + '\nlane_kind = "bound"\nrepos = ["member"]\nbound_worktree = '
                       + json.dumps(str(repo)) + '\nbound_head = ' + json.dumps(head) + '\n')
    return workspace, repos, base, head


def coordinates(world, repo):
    return records.review_record_paths(world[0], 'owner', repo.name, 'member', repo)


def bind(world, repo):
    return lanes.bind_bound_lane(world[0], 'owner', repo.name, base=world[2], allow_local=True)


def test_three_checkouts_bind_and_validate_before_stub_push(world, monkeypatch):
    pointers = []
    active = []
    siblings = {}
    calls = []
    monkeypatch.setattr(lanes._push, 'push_current_branch', lambda repo, **kw: calls.append((repo, git(repo, 'rev-parse', 'HEAD'))))
    for repo in world[1]:
        # Each worktree has distinct payload and head, so sharing only G while
        # leaving P per-worktree cannot silently overwrite equivalent bytes.
        (repo / 'file').write_text(repo.name + '\n')
        git(repo, 'commit', '-qam', repo.name)
        head = git(repo, 'rev-parse', 'HEAD')
        definition = lanes.lane_file(world[0], 'owner', repo.name)
        doc = lanes.load_lane_doc(world[0], 'owner', repo.name)
        definition.write_text(lanes.serialize_toml({**doc, 'bound_head': head}))
        receipt = bind(world, repo)
        paths = coordinates(world, repo)
        pointer = records.review_record_pointer_path(repo)
        pointers.append(pointer)
        active.append(paths.current)
        assert pointer.read_text() == str(paths.context) + '\n'
        assert records.lane_paths_for_repo(repo) == paths
        assert json.loads(paths.current.read_text()) == receipt.to_dict()
        assert (receipt.base, receipt.head, receipt.lane_kind) == (world[2], head, 'bound')
        assert not paths.legacy.exists()
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)
        assert all(path.read_bytes() == raw for path, raw in siblings.items())
        siblings.update({paths.current: paths.current.read_bytes(), pointer: pointer.read_bytes()})
    assert calls == [(repo, git(repo, 'rev-parse', 'HEAD')) for repo in world[1]]
    assert len({head for _, head in calls}) == 3
    assert len(set(pointers)) == 3
    assert len(set(active)) == 3


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
        paths.legacy.parent.mkdir(parents=True, exist_ok=True)
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
    paths.legacy.parent.mkdir(parents=True, exist_ok=True)
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
        paths.current.unlink()
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
    recovery = Path(message.split('prior bytes at ', 1)[1])
    assert recovery.read_bytes() == old_payload
    assert pointer.read_bytes() == old_pointer
    assert list(paths.current.parent.glob('.review-*')) == [recovery]


def test_existing_non_git_directory_refuses_and_future_clone_coordinate_is_safe(tmp_path):
    future = tmp_path / 'future'
    assert records.legacy_review_record_path(future) == future / '.git' / 'grip-review.json'
    future.mkdir()
    # A known ordinary coordinate remains available for interrupted close.
    # This does not validate Git ownership or grant a deletion permission.
    assert records.legacy_review_record_path(future) == future / '.git' / 'grip-review.json'


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


def test_explicit_rebind_synchronizes_existing_workspace_evidence(world):
    repo = world[1][1]
    old = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    paths.legacy.parent.mkdir(parents=True)
    paths.legacy.write_text(json.dumps(old, separators=(',', ':')) + '\n')
    new = {**old, 'base': old['head']}
    records.write_review_record(paths, new)
    assert records.read_review_record_at(paths) == (new, paths.current)
    assert json.loads(paths.legacy.read_bytes()) == new


@pytest.mark.parametrize('failed_destination', ['pointer', 'workspace'])
def test_participating_workspace_publication_failure_restores_all_bytes(world, monkeypatch, failed_destination):
    repo = world[1][1]
    old = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    pointer = records.review_record_pointer_path(repo)
    paths.legacy.parent.mkdir(parents=True)
    paths.legacy.write_text(json.dumps(old, separators=(',', ':')) + '\n')
    # Same valid coordinate, distinct bytes from canonical publication.
    pointer.write_bytes(pointer.read_bytes() + b'\n')
    before = {p: p.read_bytes() for p in (paths.current, pointer, paths.legacy)}
    failed = pointer if failed_destination == 'pointer' else paths.legacy
    replace = records.os.replace
    faults = []
    def fail_once(source, target):
        if Path(target) == failed and not faults:
            faults.append(str(target))
            raise OSError('named participating publication failure')
        return replace(source, target)
    monkeypatch.setattr(records.os, 'replace', fail_once)
    with pytest.raises(OSError, match='named participating publication failure'):
        records.write_review_record(paths, {**old, 'base': old['head']})
    assert len(faults) == 1
    assert {p: p.read_bytes() for p in before} == before


def test_swapped_linked_context_refuses_each_guard(world):
    one, two = world[1][1:]
    bind(world, one)
    bind(world, two)
    pointer = records.review_record_pointer_path(one)
    original = pointer.read_bytes()
    pointer.write_bytes(records.review_record_pointer_path(two).read_bytes())
    for module, error in ((commit, commit.CommitError), (push, push.PushError)):
        with pytest.raises(error) as caught:
            module._refuse_review_ephemeral_repo(one)
        assert 'different bound worktree' in exception_chain(caught.value)
    pointer.write_bytes(original)
    commit._refuse_review_ephemeral_repo(one)
    push._refuse_review_ephemeral_repo(one)


def exception_chain(error):
    reasons = []
    while error is not None:
        reasons.append(str(error))
        error = error.__cause__ or error.__context__
    return "\n".join(reasons)


def test_bound_project_mode_conflict_never_uses_bound_return(world):
    import tomllib
    repo = world[1][1]
    bind(world, repo)
    definition = lanes.lane_file(world[0], 'owner', repo.name)
    document = tomllib.loads(definition.read_text())
    definition.write_text(lanes.serialize_toml({**document, 'creation_source': 'project-review'}))
    with pytest.raises(records.ReviewRecordLocationError) as caught:
        records.lane_paths_for_repo(repo)
    assert 'conflicting or unsupported ownership modes' in exception_chain(caught.value)


@pytest.mark.parametrize('overrides', [
    {'lane_kind': 'unknown'},
    {'lane_kind': 'materialized', 'bound_worktree': 'contradictory'},
])
def test_invalid_mode_at_valid_materialized_coordinate_refuses(world, overrides):
    workspace = world[0]
    repo = lanes.lane_dir(workspace, 'owner', 'materialized') / 'repos' / 'member'
    repo.parent.mkdir(parents=True)
    subprocess.check_call(['git', 'clone', '-q', str(world[1][0]), str(repo)])
    definition = lanes.lane_file(workspace, 'owner', 'materialized')
    document = {'owner_unit': 'owner', 'lane_name': 'materialized', 'repos': ['member']}
    definition.write_text(lanes.serialize_toml(document))
    paths = records.review_record_paths(workspace, 'owner', 'materialized', 'member', repo)
    record = {'repo': 'local:' + str(world[1][0]), 'base': world[2], 'head': world[3], 'lane_kind': 'materialized'}
    records.write_review_record(paths, record)
    # Omitted-kind compatibility and absent-definition standalone both work.
    assert records.lane_paths_for_repo(repo) == paths
    definition.unlink()
    assert records.lane_paths_for_repo(repo) == paths
    definition.write_text(lanes.serialize_toml({**document, **overrides}))
    with pytest.raises(records.ReviewRecordLocationError) as caught:
        records.lane_paths_for_repo(repo)
    assert 'conflicting or unsupported ownership modes' in exception_chain(caught.value)


def test_workspace_symlink_preflight_performs_no_replacement(world, monkeypatch, tmp_path):
    repo = world[1][1]
    old = bind(world, repo).to_dict()
    paths = coordinates(world, repo)
    paths.legacy.parent.mkdir(parents=True)
    foreign = tmp_path / 'foreign'
    foreign.write_bytes(b'foreign\n')
    paths.legacy.symlink_to(foreign)
    calls = []
    monkeypatch.setattr(records.os, 'replace', lambda *args: calls.append(args))
    with pytest.raises(records.ReviewRecordLocationError, match='symlink'):
        records.write_review_record(paths, old)
    assert calls == [] and foreign.read_bytes() == b'foreign\n'


def pending_publication(world, monkeypatch, *, evidence=False):
    """Valid A->B->H with same H and two different nonempty reviewed ranges."""
    repo = world[1][1]
    (repo / 'file').write_text('publication-head\n')
    git(repo, 'commit', '-qam', 'publication head')
    head = git(repo, 'rev-parse', 'HEAD')
    definition = lanes.lane_file(world[0], 'owner', repo.name)
    doc = lanes.load_lane_doc(world[0], 'owner', repo.name)
    definition.write_text(lanes.serialize_toml({**doc, 'bound_head': head}))
    old = lanes.bind_bound_lane(world[0], 'owner', repo.name, base=world[3], allow_local=True).to_dict()
    paths = coordinates(world, repo)
    pointer = records.review_record_pointer_path(repo)
    if evidence:
        paths.legacy.parent.mkdir(parents=True)
        paths.legacy.write_text(json.dumps(old, separators=(',', ':')) + '\n')
        pointer.write_bytes(pointer.read_bytes() + b'\n')
    targets = [paths.current, pointer] + ([paths.legacy] if evidence else [])
    before = {p: p.read_bytes() for p in targets}
    faults = []
    replace = records.os.replace
    published = False
    def fail_twice(source, target):
        nonlocal published
        if Path(target) == pointer:
            faults.append('pointer')
            raise OSError('named pointer publication fault')
        if Path(target) == paths.current:
            if published:
                faults.append('G rollback')
                raise OSError('named G compensation fault')
            published = True
        return replace(source, target)
    with monkeypatch.context() as fault:
        fault.setattr(records.os, 'replace', fail_twice)
        with pytest.raises(records.ReviewRecordLocationError) as caught:
            records.write_review_record(paths, {**old, 'base': world[2]})
    assert faults == ['pointer', 'G rollback']
    assert 'named pointer publication fault' in str(caught.value)
    assert 'named G compensation fault' in str(caught.value)
    backup = Path(str(caught.value).split('prior bytes at ', 1)[1])
    assert backup.read_bytes() == before[paths.current]
    assert paths.publication_pending.is_file()
    assert json.loads(paths.current.read_bytes()) == {**old, 'base': world[2]}
    return repo, paths, pointer, old, before


def assert_pending_consumers(world, monkeypatch, state):
    repo, paths, _, old, _ = state
    calls = []
    effects = []
    with monkeypatch.context() as observer:
        observer.setattr(lanes._push, 'push_current_branch', lambda *a, **kw: calls.append(a))
        observer.setattr(records.os, 'replace', lambda *a: effects.append(a))
        observer.setattr(records.os, 'link', lambda *a: effects.append(a))
        consumers = [lambda: records.lane_paths_for_repo(repo),
                     lambda: records.read_review_record_at(paths),
                     lambda: records.read_review_records_for_guard(paths),
                     lambda: commit._refuse_review_ephemeral_repo(repo),
                     lambda: push._refuse_review_ephemeral_repo(repo),
                     lambda: lanes.pr_create_bound_lane(world[0], 'owner', repo.name),
                     lambda: records.write_review_record(paths, old)]
        for consumer in consumers:
            with pytest.raises((records.ReviewRecordLocationError, commit.CommitError, push.PushError, SystemExit)) as caught:
                consumer()
            assert 'publication is incomplete' in exception_chain(caught.value)
    assert calls == [] and effects == []


def assert_publication_recovered(world, monkeypatch, state):
    repo, paths, _, old, before = state
    records.recover_review_publication(paths)
    assert not paths.publication_pending.exists()
    assert {p: p.read_bytes() for p in before} == before
    assert records.read_review_record_at(paths) == (old, paths.current)
    commit._refuse_review_ephemeral_repo(repo)
    push._refuse_review_ephemeral_repo(repo)
    calls = []
    with monkeypatch.context() as recorder:
        recorder.setattr(lanes._push, 'push_current_branch', lambda target, **kw: calls.append(target))
        lanes.pr_create_bound_lane(world[0], 'owner', repo.name)
    assert calls == [repo]


def observe_recovery_effects(observer, effects):
    unlink = Path.unlink
    observer.setattr(records.os, 'replace', lambda *args: effects.append(('replace', args)))
    def record_unlink(target, *args, **kwargs):
        effects.append(('unlink', str(target)))
        return unlink(target, *args, **kwargs)
    observer.setattr(Path, 'unlink', record_unlink)


def test_pending_publication_refuses_without_pointer(world, monkeypatch):
    state = pending_publication(world, monkeypatch)
    _, paths, pointer, _, before = state
    pointer.unlink()
    assert_pending_consumers(world, monkeypatch, state)
    effects = []
    with monkeypatch.context() as observer:
        observe_recovery_effects(observer, effects)
        with pytest.raises(records.ReviewRecordLocationError, match='target content changed'):
            records.recover_review_publication(paths)
    assert effects == [] and paths.publication_pending.exists()
    pointer.write_bytes(before[pointer])
    assert_publication_recovered(world, monkeypatch, state)


@pytest.mark.parametrize('damage', ['json', 'symlink', 'extra-target', 'foreign-content', 'boolean-identity', 'float-identity'])
def test_invalid_publication_recovery_has_no_restoration(world, monkeypatch, damage, tmp_path):
    state = pending_publication(world, monkeypatch)
    _, paths, _, _, _ = state
    pending = paths.publication_pending
    original = pending.read_bytes()
    live = paths.current.read_bytes()
    if damage == 'json':
        pending.write_text('{bad')
    elif damage == 'symlink':
        pending.unlink()
        pending.symlink_to(tmp_path / 'absent-pending')
    elif damage == 'foreign-content':
        paths.current.write_bytes(live + b'\n')
    else:
        doc = json.loads(original)
        if damage == 'extra-target':
            doc['targets'][str(tmp_path / 'foreign')] = {'old': None, 'new': ''}
        elif damage == 'boolean-identity':
            doc['repo_identity'] = [True, False]
        else:
            integers = doc['repo_identity']
            doc['repo_identity'] = [float(value) for value in integers]
            assert doc['repo_identity'] == integers  # Type is the only changed fact.
        pending.write_text(json.dumps(doc))
    def damaged_snapshot():
        return {str(p): ('symlink', str(p.readlink())) if p.is_symlink()
                else ('bytes', p.read_bytes()) if p.exists() else ('absent', None)
                for p in (pending, paths.current, records.review_record_pointer_path(state[0]), paths.legacy)}
    damaged = damaged_snapshot()
    assert_pending_consumers(world, monkeypatch, state)
    effects = []
    with monkeypatch.context() as observer:
        observe_recovery_effects(observer, effects)
        with pytest.raises(records.ReviewRecordLocationError, match='publication recovery refused'):
            records.recover_review_publication(paths)
    assert effects == [] and damaged_snapshot() == damaged
    if pending.is_symlink():
        pending.unlink()
    pending.write_bytes(original)
    paths.current.write_bytes(live)
    assert_publication_recovered(world, monkeypatch, state)


@pytest.mark.parametrize('fault_target', ['pointer', 'pending-clear'])
def test_partial_publication_recovery_retains_pending_until_retry(world, monkeypatch, fault_target):
    state = pending_publication(world, monkeypatch, evidence=True)
    _, paths, pointer, _, before = state
    retained = paths.publication_pending.read_bytes()
    replace, unlink = records.os.replace, Path.unlink
    faults, restored = [], []
    def failing_replace(source, target):
        if fault_target == 'pointer' and Path(target) == pointer:
            faults.append('pointer restore')
            raise OSError('named later restoration fault')
        restored.append(str(target))
        return replace(source, target)
    def failing_unlink(target, *a, **kw):
        if fault_target == 'pending-clear' and target == paths.publication_pending:
            faults.append('pending clear')
            raise OSError('named pending finalization fault')
        return unlink(target, *a, **kw)
    with monkeypatch.context() as fault:
        fault.setattr(records.os, 'replace', failing_replace)
        fault.setattr(Path, 'unlink', failing_unlink)
        with pytest.raises(records.ReviewRecordLocationError, match='named .* fault'):
            records.recover_review_publication(paths)
    assert len(faults) == 1 and str(paths.current) in restored
    assert paths.publication_pending.read_bytes() == retained
    assert_pending_consumers(world, monkeypatch, state)
    assert_publication_recovered(world, monkeypatch, state)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize('changed', ['worktree', 'git-dir'])
def test_publication_recovery_refuses_replaced_physical_owner(world, monkeypatch, changed):
    import shutil
    state = pending_publication(world, monkeypatch)
    repo, paths, _, _, _ = state
    original_pending = paths.publication_pending.read_bytes()
    target = repo if changed == 'worktree' else paths.current.parent
    saved = target.with_name(target.name + '-saved')
    detached = target.with_name(target.name + '-replacement')
    target.rename(saved)
    shutil.copytree(saved, target)
    effects = []
    with monkeypatch.context() as observer:
        observe_recovery_effects(observer, effects)
        with pytest.raises(records.ReviewRecordLocationError, match='owner or Git identity changed'):
            records.recover_review_publication(paths)
    assert effects == [] and paths.publication_pending.read_bytes() == original_pending
    target.rename(detached)
    saved.rename(target)
    assert_publication_recovered(world, monkeypatch, state)


def test_publication_recovery_refuses_another_linked_worktree(world, monkeypatch):
    state = pending_publication(world, monkeypatch)
    _, paths, _, _, _ = state
    sibling = world[1][2]
    sibling_paths = coordinates(world, sibling)
    sibling_paths.publication_pending.write_bytes(paths.publication_pending.read_bytes())
    effects = []
    with monkeypatch.context() as observer:
        observe_recovery_effects(observer, effects)
        with pytest.raises(records.ReviewRecordLocationError, match='owner or Git identity changed'):
            records.recover_review_publication(sibling_paths)
    assert effects == [] and paths.publication_pending.exists()
    sibling_paths.publication_pending.unlink()
    assert_publication_recovered(world, monkeypatch, state)


@pytest.mark.parametrize('redirect', ['replacement', 'symlink'])
def test_publication_recovery_refuses_changed_workspace_parent(world, monkeypatch, redirect):
    state = pending_publication(world, monkeypatch, evidence=True)
    _, paths, _, _, _ = state
    parent = paths.legacy.parent
    retained = paths.publication_pending.read_bytes()
    original_w = paths.legacy.read_bytes()
    saved, foreign = parent.with_name('saved-parent'), parent.with_name('foreign-parent')
    parent.rename(saved)
    foreign.mkdir()
    (foreign / paths.legacy.name).write_bytes(original_w)
    if redirect == 'replacement':
        foreign.rename(parent)
    else:
        parent.symlink_to(foreign, target_is_directory=True)
    effects = []
    with monkeypatch.context() as observer:
        observe_recovery_effects(observer, effects)
        with pytest.raises(records.ReviewRecordLocationError) as caught:
            records.recover_review_publication(paths)
    reason = 'destination parent identity changed' if redirect == 'replacement' else 'ancestor is a symlink'
    assert reason in exception_chain(caught.value)
    assert effects == [] and paths.publication_pending.read_bytes() == retained
    if redirect == 'replacement':
        parent.rename(foreign)
    else:
        parent.unlink()
    saved.rename(parent)
    assert_publication_recovered(world, monkeypatch, state)


def ordinary_coordinate_less_close_fixture(world):
    workspace = world[0]
    root = lanes.lane_dir(workspace, 'owner', 'legacy-close')
    repo = root / 'repos' / 'member'
    repo.parent.mkdir(parents=True)
    subprocess.check_call(['git', 'clone', '-q', str(world[1][0]), str(repo)])
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'file').write_text('same-head-close\n')
    git(repo, 'commit', '-qam', 'close head')
    paths = records.review_record_paths(workspace, 'owner', 'legacy-close', 'member', repo)
    record = {'repo': 'local:' + str(world[1][0]), 'base': world[3],
              'head': git(repo, 'rev-parse', 'HEAD'), 'lane_kind': 'materialized'}
    records.write_review_record(paths, record)
    return root, repo, paths, record


def test_coordinate_less_close_without_pending_removes_owned_clone(world):
    from gr2.python_cli.review import close_review_lane
    root, repo, paths, record = ordinary_coordinate_less_close_fixture(world)
    assert not paths.publication_pending.exists()
    close_review_lane(lane_repo_root=repo, review_lane_root=root)
    assert not repo.exists()
    assert world[1][0].is_dir() and git(world[1][0], 'rev-parse', 'HEAD') == world[3]


@pytest.mark.parametrize('pending_kind', ['file', 'dangling-symlink'])
def test_coordinate_less_close_refuses_pending_publication(world, monkeypatch, pending_kind):
    from gr2.python_cli import review
    root, repo, paths, record = ordinary_coordinate_less_close_fixture(world)
    pointer = records.review_record_pointer_path(repo)
    replace = records.os.replace
    published = False
    faults = []
    def double_fault(source, target):
        nonlocal published
        if Path(target) == pointer:
            faults.append('pointer')
            raise OSError('coordinate-less pointer fault')
        if Path(target) == paths.current:
            if published:
                faults.append('rollback')
                raise OSError('coordinate-less G rollback fault')
            published = True
        return replace(source, target)
    with monkeypatch.context() as injection:
        injection.setattr(records.os, 'replace', double_fault)
        with pytest.raises(records.ReviewRecordLocationError, match='coordinate-less pointer fault') as caught:
            records.write_review_record(paths, {**record, 'base': world[2]})
    assert faults == ['pointer', 'rollback']
    backup = Path(str(caught.value).split('prior bytes at ', 1)[1])
    assert json.loads(paths.current.read_bytes()) == {**record, 'base': world[2]}
    pending_bytes = paths.publication_pending.read_bytes()
    if pending_kind == 'dangling-symlink':
        retained = paths.publication_pending.with_name('retained-pending.json')
        paths.publication_pending.rename(retained)
        paths.publication_pending.symlink_to(repo / '.git' / 'absent-pending')
        assert retained.read_bytes() == pending_bytes and not paths.publication_pending.exists()
    before = {p: p.read_bytes() for p in (paths.current, pointer, backup)}
    attempts = []
    monkeypatch.setattr(review.shutil, 'rmtree', lambda target: attempts.append(str(target)))
    with pytest.raises(review.ReviewError, match='publication is incomplete'):
        review.close_review_lane(lane_repo_root=repo, review_lane_root=root)
    assert attempts == [] and repo.is_dir()
    assert {p: p.read_bytes() for p in before} == before
    if pending_kind == 'file':
        assert paths.publication_pending.read_bytes() == pending_bytes
    else:
        assert paths.publication_pending.is_symlink() and retained.read_bytes() == pending_bytes
