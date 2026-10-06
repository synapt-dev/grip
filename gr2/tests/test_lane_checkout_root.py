"""Recorded checkout coordinates preserve existing lanes and isolate new units."""
from pathlib import Path
import pytest
from .conftest import make_cli_runner
from .test_lane_create_fork_base_cli import _workspace as _review_workspace, _git
from .test_lane_create_refusal_leaves_nothing import _unmaterialized_workspace
from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app

runner = make_cli_runner()

def _workspace(tmp_path, repos):
    # These witnesses exercise legacy spec-backed lanes. The shared review
    # fixture adds an empty native marker without a workspace commit.
    ws, tips = _review_workspace(tmp_path, repos)
    (ws / 'grip.toml').unlink()
    return ws, tips

def create(ws, unit='atlas', lane='feature', **kwargs):
    return runner.invoke(app.app, ['lane', 'create', str(ws), unit, lane,
        '--repos', 'app', '--branch', 'same/branch', *kwargs.get('extra', [])])

def enter(ws, unit, lane):
    result = runner.invoke(app.app, ['lane', 'enter', str(ws), unit, lane, '--actor', unit])
    assert result.exit_code == 0, result.output
    return lanes.load_current_lane_doc(ws, unit)['current']['repo_paths']['app']

def test_two_units_same_branch_use_cached_independent_visible_checkouts(tmp_path):
    ws, _ = _workspace(tmp_path, ['app'])
    with (ws/'.grip/workspace_spec.toml').open('a') as f:
        f.write('\n[[units]]\nname="second"\npath="agents/second/home"\nrepos=["app"]\n')
    paths = []
    for unit in ['atlas', 'second']:
        result = create(ws, unit)
        assert result.exit_code == 0, result.output
        doc = lanes.load_lane_doc(ws, unit, 'feature')
        assert doc['checkout_root'] == f'agents/{unit}/lanes/feature'
        repo = ws/doc['checkout_root']/'repos/app'
        assert enter(ws, unit, 'feature') == str(repo)
        assert (repo/'.git').is_dir()
        assert _git(repo, 'branch', '--show-current') == 'same/branch'
        assert (repo/'.git/objects/info/alternates').read_text().strip() == str(ws/'.grip/cache/repos/app.git/objects')
        paths.append(repo)
    a, b = paths
    b_head = _git(b, 'rev-parse', 'HEAD')
    b_index = (b/'.git/index').read_bytes()
    _git(a, 'config', 'user.name', 'test'); _git(a, 'config', 'user.email', 'test@example.invalid')
    (a/'a-only').write_text('a'); _git(a, 'add', 'a-only'); _git(a, 'commit', '-qm', 'isolated')
    assert _git(b, 'rev-parse', 'HEAD') == b_head and (b/'.git/index').read_bytes() == b_index
    assert not (b/'a-only').exists()
    # The owning commit consumer follows the same recorded coordinate.
    from gr2.python_cli.commit import _lane_repo_targets
    assert _lane_repo_targets(ws, 'atlas', 'feature', lanes.load_lane_doc(ws, 'atlas', 'feature')) == [('app', a)]

def test_old_document_ignores_unrelated_visible_checkout(tmp_path):
    ws, _ = _workspace(tmp_path, ['app'])
    assert create(ws).exit_code == 0
    doc = lanes.load_lane_doc(ws, 'atlas', 'feature'); doc.pop('checkout_root'); doc.pop('fork_base')
    doc['lane_name'] = 'old'
    metadata = lanes.lane_dir(ws, 'atlas', 'old'); metadata.mkdir(parents=True)
    (metadata/'lane.toml').write_text(lanes.serialize_toml(doc))
    sibling = ws/'agents/atlas/lanes/old/repos/app'; sibling.mkdir(parents=True)
    (sibling/'keep').write_text('existing')
    app._materialize_lane_repos(ws, 'atlas', 'old')
    assert enter(ws, 'atlas', 'old') == str(metadata/'repos/app')
    assert (sibling/'keep').read_text() == 'existing'

def test_bound_document_ignores_both_unrelated_siblings(tmp_path):
    ws, _ = _workspace(tmp_path, ['app']); src = ws/'repos/app'
    assert create(ws, lane='bound', extra=['--bind', str(src)]).exit_code == 0
    for parent in [ws/'agents/atlas/lanes/bound', lanes.lane_dir(ws, 'atlas', 'bound')]:
        sibling = parent/'repos/app'; sibling.mkdir(parents=True); (sibling/'keep').write_text('existing')
    assert enter(ws, 'atlas', 'bound') == str(src)
    for parent in [ws/'agents/atlas/lanes/bound', lanes.lane_dir(ws, 'atlas', 'bound')]:
        assert (parent/'repos/app/keep').read_text() == 'existing'

def test_preexisting_checkout_is_refused_without_metadata_or_data_loss(tmp_path):
    ws, _ = _workspace(tmp_path, ['app']); root = ws/'agents/atlas/lanes/feature'
    root.mkdir(parents=True); (root/'keep').write_text('existing')
    result = create(ws)
    assert result.exit_code != 0 and 'existing checkout path' in result.output
    assert (root/'keep').read_text() == 'existing'
    assert not lanes.lane_file(ws, 'atlas', 'feature').exists()

def test_cleanup_failure_preserves_same_primary_error(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path, ['app']); primary = RuntimeError('original materialization failure')
    def fail(*a, **kw): raise primary
    def fail_cleanup(*a, **kw): raise OSError('secondary cleanup failure')
    monkeypatch.setattr(app, '_materialize_lane_repos', fail)
    monkeypatch.setattr(app, '_remove_lane_artifacts', fail_cleanup)
    result = create(ws)
    assert result.exception is primary
    assert any('secondary cleanup failure' in note for note in primary.__notes__)
    assert lanes.lane_file(ws, 'atlas', 'feature').exists()

def test_refused_unmaterialized_source_removes_only_attempt_roots(tmp_path):
    ws = _unmaterialized_workspace(tmp_path)
    sibling = ws/'agents/atlas/lanes/sibling'; sibling.mkdir(parents=True); (sibling/'keep').write_text('existing')
    result = runner.invoke(app.app, ['lane','create',str(ws),'atlas','feature','--repos','repo-a','--branch','same/branch'])
    assert result.exit_code != 0 and 'not a repository' in result.output
    assert not lanes.lane_dir(ws, 'atlas', 'feature').exists()
    assert not (ws/'agents/atlas/lanes/feature').exists()
    assert (sibling/'keep').read_text() == 'existing'

def test_fork_base_save_failure_preserves_materialization_primary(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path, ['app'])
    primary = RuntimeError('original hook failure')
    def fail_hook(*a, **kw): raise primary
    def fail_record(*a, **kw): raise OSError('secondary fork-base failure')
    # Force the actual post-clone hook path, then fail its final recording.
    monkeypatch.setattr(app, 'load_repo_hooks', lambda repo: object())
    monkeypatch.setattr(app, 'run_materialize_hook_block', fail_hook)
    monkeypatch.setattr(lanes, 'record_fork_base', fail_record)
    result = create(ws)
    assert result.exception is primary
    assert any('secondary fork-base failure' in note for note in primary.__notes__)


def test_existing_lane_is_not_deleted_when_materialization_refuses(tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path, ['app'])
    import argparse
    ns = argparse.Namespace(workspace_root=ws, owner_unit='atlas', lane_name='existing',
        type='feature', repos='app', branch='same/branch', source='manual', default_commands=[], bind=None)
    assert lanes.create_lane(ns) == 0
    # A prior accepted document with no fork base is not this attempt's metadata.
    doc_path = lanes.lane_file(ws, 'atlas', 'existing'); before = doc_path.read_bytes()
    marker = doc_path.parent/'keep'; marker.write_text('preexisting')
    primary = RuntimeError('refused')
    def fail(*a, **kw): raise primary
    monkeypatch.setattr(app, '_materialize_lane_repos', fail)
    result = runner.invoke(app.app, ['lane','create',str(ws),'atlas','existing','--repos','app','--branch','same/branch','--source','manual'])
    assert result.exception is primary
    assert doc_path.read_bytes() == before and marker.read_text() == 'preexisting'
