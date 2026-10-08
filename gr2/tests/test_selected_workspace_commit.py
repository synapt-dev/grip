"""A native selected commit owns the member set, paths, pins and displayed paths."""
import json
from pathlib import Path
import pytest
from .conftest import make_cli_runner
from .test_lane_create_fork_base_cli import _git
from gr2.python_cli import app, grip_cli
from gr2.prototypes import lane_workspace_prototype as lanes

runner = make_cli_runner()

@pytest.fixture
def selected_workspace(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    _git(source,'init','-q','-b','main')
    _git(source,'config','user.name','test'); _git(source,'config','user.email','test@example.invalid')
    (source/'payload').write_text('first'); _git(source,'add','payload'); _git(source,'commit','-qm','first')
    first = _git(source,'rev-parse','HEAD')
    (source/'payload').write_text('second'); _git(source,'commit','-qam','second')
    second = _git(source,'rev-parse','HEAD')
    remote=tmp_path/'remote.git'; _git(tmp_path,'clone','--bare',str(source),str(remote))
    ws=tmp_path/'workspace';ws.mkdir();_git(ws,'init','-q','-b','main')
    _git(ws,'config','user.name','test');_git(ws,'config','user.email','test@example.invalid')
    rows = [
        [{'name':'engine','path':'nested/src/runtime','pin':first,'ref':'main','remote':str(remote)}],
        [{'name':'engine','path':'changed/core','pin':second,'ref':'main','remote':str(remote)},
         {'name':'extra','path':'nested/tools/extra','pin':first,'ref':'main','remote':str(remote)}],
    ]
    commits=[]
    for members in rows:
        grip_cli._write_native_members(ws,members)
        (ws/'.gitinclude').write_text('grip.toml\n'+''.join(m['path']+'\n' for m in members))
        grip_cli._regenerate_workspace_gitignore(ws)
        _git(ws,'read-tree','--empty');_git(ws,'add','grip.toml','.gitinclude')
        for member in members:
            _git(ws,'update-index','--add','--cacheinfo',f"160000,{member['pin']},{member['path']}")
        _git(ws,'commit','-qm','workspace');commits.append(_git(ws,'rev-parse','HEAD'))
    (ws/'.grip').mkdir()
    (ws/'.grip/workspace_spec.toml').write_text('schema_version=1\nworkspace_name="test"\n[[repos]]\nname="ambient"\npath="wrong/path"\nurl="'+str(remote)+'"\n[[units]]\nname="a"\npath="agents/a/home"\nrepos=["ambient"]\n[[units]]\nname="b"\npath="agents/b/home"\nrepos=["ambient"]\n')
    return ws,remote,commits,rows


def test_selected_commit_checkout_preserves_key_coordinate(selected_workspace,monkeypatch):
    ws,remote,commits,rows=selected_workspace
    legacy=ws/'engine';_git(ws,'clone',str(remote),str(legacy))
    before=(_git(legacy,'rev-parse','HEAD'),(legacy/'.git/index').read_bytes(),(legacy/'payload').read_bytes())
    monkeypatch.chdir(ws)
    for commit,members in zip(commits,rows):
        result=runner.invoke(app.app,['store','checkout',commit,'--json'])
        assert result.exit_code==0,result.output
        assert {m['name'] for m in json.loads(result.stdout)['members']}=={m['name'] for m in members}
        for member in members:
            assert _git(ws/member['path'],'rev-parse','HEAD')==member['pin']
    assert before==(_git(legacy,'rev-parse','HEAD'),(legacy/'.git/index').read_bytes(),(legacy/'payload').read_bytes())


def test_native_lane_display_enter_and_alternates_follow_selected_commit(selected_workspace):
    ws,remote,commits,rows=selected_workspace
    for unit,commit,members in zip(['a','b'],commits,rows):
        result=runner.invoke(app.app,['lane','create',str(ws),unit,'feature','--branch','feature/shared','--workspace-commit',commit])
        assert result.exit_code==0,result.output
        output=result.stdout.splitlines(); root=ws/f'agents/{unit}/lanes/feature'
        assert _git(root,'rev-parse','HEAD')==commit
        enter=runner.invoke(app.app,['lane','enter',str(ws),unit,'feature','--actor','test'])
        assert enter.exit_code==0,enter.output
        current=lanes.load_current_lane_doc(ws,unit)['current']['repo_paths']
        assert set(current)=={m['name'] for m in members}
        for member in members:
            path=root/member['path']
            assert f"{member['name']}: {path}" in output
            assert f"{member['name']}: {root/'repos'/member['name']}" not in output
            assert current[member['name']]==str(path)
            assert _git(path,'rev-parse','HEAD')==member['pin']
            assert _git(path,'branch','--show-current')=='feature/shared'
            assert (path/'.git').is_dir()
            assert (path/'.git/objects/info/alternates').read_text().strip()==str(root/'.grip/cache/repos'/f"{member['name']}.git"/'objects')


@pytest.mark.parametrize("stage", ["cache seeding", "clone materialization"])
def test_post_detach_failure_preserves_primary_and_existing_checkout(selected_workspace, monkeypatch, capsys, stage):
    ws, remote, commits, rows = selected_workspace
    legacy = ws / 'engine'
    _git(ws, 'clone', str(remote), str(legacy))
    before = (_git(legacy, 'rev-parse', 'HEAD'), (legacy/'.git/index').read_bytes(), (legacy/'payload').read_bytes())
    primary = OSError('injected materialization failure')
    def fail(*args, **kwargs):
        raise primary
    if stage == 'cache seeding':
        monkeypatch.setattr(grip_cli.gitops, 'ensure_repo_cache', fail)
    else:
        from gr2.python_cli import clone_exec
        monkeypatch.setattr(clone_exec, 'materialize_lane_clone', fail)
    with pytest.raises(OSError) as caught:
        grip_cli._native_store_checkout(ws, commits[0])
    assert caught.value is primary
    assert _git(ws, 'rev-parse', 'HEAD') == commits[0]
    diagnostic = capsys.readouterr().err
    assert 'THIS WORKSPACE IS PART-APPLIED' in diagnostic
    assert f'engine: {stage} failed' in diagnostic
    assert commits[0] in diagnostic
    assert any(diagnostic.strip() == note for note in primary.__notes__)
    assert before == (_git(legacy, 'rev-parse', 'HEAD'), (legacy/'.git/index').read_bytes(), (legacy/'payload').read_bytes())


def test_post_detach_diagnostic_failure_does_not_replace_primary(selected_workspace, monkeypatch):
    ws, _, commits, _ = selected_workspace
    primary = OSError('original cache failure')
    def fail_cache(*args, **kwargs):
        raise primary
    def fail_diagnostic(*args, **kwargs):
        raise OSError('stderr failure')
    monkeypatch.setattr(grip_cli.gitops, 'ensure_repo_cache', fail_cache)
    monkeypatch.setattr(grip_cli.typer, 'echo', fail_diagnostic)
    with pytest.raises(OSError) as caught:
        grip_cli._native_store_checkout(ws, commits[0])
    assert caught.value is primary
    assert _git(ws, 'rev-parse', 'HEAD') == commits[0]
    assert any('cache seeding failed' in note for note in primary.__notes__)


def test_lane_create_preserves_detached_native_root_on_cache_failure(selected_workspace, monkeypatch):
    ws, _, commits, _ = selected_workspace
    primary = OSError('original cache failure')
    def fail(*args, **kwargs):
        raise primary
    monkeypatch.setattr(grip_cli.gitops, 'ensure_repo_cache', fail)
    result = runner.invoke(app.app, ['lane','create',str(ws),'a','partial','--workspace-commit',commits[0]])
    assert result.exception is primary
    root = ws/'agents/a/lanes/partial'
    assert _git(root, 'rev-parse', 'HEAD') == commits[0]
    assert lanes.lane_file(ws, 'a', 'partial').exists()
    assert 'THIS WORKSPACE IS PART-APPLIED' in result.output
    assert any(str(root) in note for note in primary.__notes__)


def test_lane_commit_uses_selected_nested_member_paths(selected_workspace):
    from gr2.python_cli.commit import commit_lane
    ws, _, commits, rows = selected_workspace
    result = runner.invoke(app.app, ['lane','create',str(ws),'a','commit-paths','--workspace-commit',commits[1]])
    assert result.exit_code == 0, result.output
    root = ws/'agents/a/lanes/commit-paths'
    expected = {}
    for member in rows[1]:
        path = root/member['path']
        _git(path,'config','user.name','test');_git(path,'config','user.email','test@example.invalid')
        (path/'payload').write_text('new '+member['name'])
        _git(path,'add','payload')
        expected[member['name']] = path
    receipt = commit_lane(ws, 'a', 'commit selected paths', lane_name='commit-paths')
    assert {r.repo for r in receipt.results} == set(expected)
    assert all(r.status == 'committed' for r in receipt.results)
    for row in receipt.results:
        assert row.commit_sha == _git(expected[row.repo], 'rev-parse', 'HEAD')
        assert _git(expected[row.repo], 'show', 'HEAD:payload') == 'new '+row.repo
    assert not (root/'repos').exists()
    empty = commit_lane(ws, 'a', 'no changes', lane_name='commit-paths')
    assert empty.all_skipped
    assert empty.lane_repo_dir == str(root)


def test_review_context_uses_current_lane_head_membership(selected_workspace):
    from gr2.python_cli.review_records import _validate_context_repo, ReviewRecordLocationError
    ws, _, commits, rows = selected_workspace
    result = runner.invoke(app.app, ['lane','create',str(ws),'a','advance','--workspace-commit',commits[0]])
    assert result.exit_code == 0, result.output
    root = ws/'agents/a/lanes/advance'
    raw_before = lanes.lane_file(ws, 'a', 'advance').read_bytes()
    grip_cli._native_store_checkout(root, commits[1])
    assert lanes.lane_file(ws, 'a', 'advance').read_bytes() == raw_before
    added = root/rows[1][1]['path']
    _validate_context_repo(ws, 'a', 'advance', 'extra', added)
    with pytest.raises(ReviewRecordLocationError):
        _validate_context_repo(ws, 'a', 'advance', 'engine', root/rows[0][0]['path'])
    with pytest.raises(ReviewRecordLocationError):
        _validate_context_repo(ws, 'a', 'advance', 'extra', root/'repos/extra')
    with pytest.raises(ReviewRecordLocationError):
        _validate_context_repo(ws, 'a', 'advance', 'removed', added)


def test_snapshot_metadata_comes_from_committed_native_members(selected_workspace):
    from gr2.python_cli.workspace_snapshot import resolve_lane_repos
    from gr2.python_cli.project_review import pins_from_lane
    ws, remote, commits, rows = selected_workspace
    result = runner.invoke(app.app, ['lane','create',str(ws),'a','snapshot','--workspace-commit',commits[1]])
    assert result.exit_code == 0, result.output
    for ambient in ['absent', 'same-key-conflict']:
        if ambient == 'same-key-conflict':
            (ws/'.grip/workspace_spec.toml').write_text('schema_version=1\nworkspace_name="decoy"\n[[repos]]\nname="engine"\npath="wrong/path"\nurl="wrong-remote"\n')
        actual = resolve_lane_repos(ws, 'a', 'snapshot')
        pins = pins_from_lane(ws, 'a', 'snapshot')
        assert {(p.key, p.repo, p.path, p.head, p.base) for p in pins} == {
            (r['key'], r['remote'], r['path'], r['commit'], r['base']) for r in actual
        }
        assert {r['key'] for r in actual} == {m['name'] for m in rows[1]}
        for row in actual:
            member = next(m for m in rows[1] if m['name'] == row['key'])
            assert row['path'] == member['path']
            assert row['remote'] == str(remote)
            assert row['commit'] == row['base'] == member['pin']
