"""Native workspaces carry inclusion declarations, not generated ignore files."""
from pathlib import Path
import pytest
from .test_lane_create_fork_base_cli import _git
from gr2.python_cli import grip_cli
from gr2.python_cli.gitinclude import compile_gitignore


def test_native_store_commits_include_and_regenerates_on_independent_checkout(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    _git(source,'init','-q','-b','main')
    _git(source,'config','user.name','test');_git(source,'config','user.email','test@example.invalid')
    (source/'payload').write_text('member')
    _git(source,'add','payload');_git(source,'commit','-qm','member')
    remote = tmp_path/'member.git';_git(tmp_path,'clone','--bare',str(source),str(remote))
    root = tmp_path/'workspace';root.mkdir()
    child = root/'nested/component';child.parent.mkdir()
    _git(root,'clone',str(remote),str(child))
    (root/'private-local').write_text('excluded')
    grip_cli._native_store_init(root, ['nested/component'])
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text() + 'note\n')
    (root / 'note').write_text('explicitly included')
    grip_cli._regenerate_workspace_gitignore(root)
    _git(root,'config','user.name','test');_git(root,'config','user.email','test@example.invalid')
    assert '.gitinclude' in _git(root,'status','--porcelain')
    assert _git(root,'check-ignore','.gitignore') == '.gitignore'
    _git(root, 'add', 'note')
    assert grip_cli._native_store_commit(root, 'workspace')
    _git(root,'add','-A')
    assert _git(root,'diff','--cached','--name-only') == ''
    assert set(_git(root,'ls-tree','-r','--name-only','HEAD').splitlines()) == {'.gitinclude','grip.toml','nested/component','note'}
    commit = _git(root,'rev-parse','HEAD')
    receiver = tmp_path/'receiver';_git(tmp_path,'clone',str(root),str(receiver))
    assert not (receiver/'.gitignore').exists()
    foreign = tmp_path / 'foreign-ignore'; foreign.write_text('owner bytes')
    (receiver / '.gitignore').symlink_to(foreign)
    receiver_index = (receiver / '.git/index').read_bytes()
    with pytest.raises(grip_cli.NativeStoreRefusal, match='regular file'):
        grip_cli._native_store_checkout(receiver, commit)
    assert foreign.read_text() == 'owner bytes'
    assert (receiver / '.gitignore').is_symlink()
    assert (receiver / '.git/index').read_bytes() == receiver_index
    (receiver / '.gitignore').unlink()  # Fixture-owned symlink only.
    grip_cli._native_store_checkout(receiver, commit)
    generated, notices = compile_gitignore((receiver/'.gitinclude').read_text())
    assert not notices and (receiver/'.gitignore').read_text() == generated
    assert (receiver/'nested/component/payload').read_text() == 'member'
    (receiver/'unlisted').write_text('excluded')
    _git(receiver,'add','-A')
    assert _git(receiver,'diff','--cached','--name-only') == ''
    assert _git(receiver,'check-ignore','.gitignore') == '.gitignore'

    # A forced add bypasses ignores. Refuse the commit and preserve that index.
    _git(root, 'add', '-f', 'private-local')
    index_before = (root / '.git/index').read_bytes()
    with pytest.raises(grip_cli.NativeStoreRefusal, match='private-local is not included'):
        grip_cli._native_store_commit(root, 'must refuse')
    assert (root / '.git/index').read_bytes() == index_before
    assert _git(root, 'rev-parse', 'HEAD') == commit

    # A nested ignore may reinclude content Git sees, but cannot grant authority.
    _git(root, 'update-index', '--force-remove', 'private-local')
    docs = root / 'docs'; docs.mkdir()
    (docs / 'keep.txt').write_text('declared')
    (docs / 'undeclared.txt').write_text('not declared')
    (docs / '.gitignore').write_text('!undeclared.txt\n')
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text() + 'docs/keep.txt\n')
    grip_cli._regenerate_workspace_gitignore(root)
    assert grip_cli._store_git(root, 'check-ignore', '--no-index', 'docs/undeclared.txt', check=False).returncode == 1
    _git(root, 'add', 'docs/undeclared.txt')
    index_before = (root / '.git/index').read_bytes()
    with pytest.raises(grip_cli.NativeStoreRefusal, match='docs/undeclared.txt is not included'):
        grip_cli._native_store_commit(root, 'must refuse nested override')
    assert (root / '.git/index').read_bytes() == index_before
    assert _git(root, 'rev-parse', 'HEAD') == commit
    _git(root, 'update-index', '--force-remove', 'docs/undeclared.txt')
    _git(root, 'add', 'docs/keep.txt')
    assert grip_cli._native_store_commit(root, 'declared file')
    assert 'docs/keep.txt' in _git(root, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()
    assert 'docs/undeclared.txt' not in _git(root, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()

    # Including a directory admits its child ignore file and normal Git behavior.
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text().replace('docs/keep.txt\n', 'docs/\n'))
    (docs / '.gitignore').write_text('ignored.txt\nkeep.txt\n')
    (docs / 'ordinary.txt').write_text('ordinary included file')
    (docs / 'ignored.txt').write_text('must stay untracked')
    (docs / 'keep.txt').write_text('already tracked, still tracked')
    grip_cli._regenerate_workspace_gitignore(root)
    _git(root, 'add', '-A')
    staged = _git(root, 'diff', '--cached', '--name-only').splitlines()
    assert 'docs/.gitignore' in staged and 'docs/ordinary.txt' in staged
    assert 'docs/keep.txt' in staged
    assert 'docs/ignored.txt' not in staged and '.gitignore' not in staged
    assert grip_cli._native_store_commit(root, 'normal child ignore')
    tree = _git(root, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()
    assert 'docs/.gitignore' in tree and 'docs/ordinary.txt' in tree and 'docs/keep.txt' in tree
    assert 'docs/ignored.txt' not in tree and '.gitignore' not in tree

    _git(root, 'config', 'core.ignorecase', 'true')
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text() + '!docs/Secret.txt\n')
    (docs / '.gitignore').write_text((docs / '.gitignore').read_text() + '!secret.txt\n')
    (docs / 'secret.txt').write_text('case variant excluded by declaration')
    grip_cli._regenerate_workspace_gitignore(root)
    assert grip_cli._store_git(root, 'check-ignore', '--no-index', 'docs/secret.txt', check=False).returncode == 1
    _git(root, 'add', 'docs/secret.txt')
    index_before = (root / '.git/index').read_bytes()
    head_before = _git(root, 'rev-parse', 'HEAD')
    with pytest.raises(grip_cli.NativeStoreRefusal, match='docs/secret.txt is not included'):
        grip_cli._native_store_commit(root, 'must refuse case variant')
    assert (root / '.git/index').read_bytes() == index_before
    assert _git(root, 'rev-parse', 'HEAD') == head_before

    # Existing HEAD membership only exempts child Git ignores, never root policy.
    _git(root, 'update-index', '--force-remove', 'docs/secret.txt')
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text().replace('!docs/Secret.txt\n', ''))
    grip_cli._regenerate_workspace_gitignore(root)
    _git(root, 'add', 'docs/secret.txt')
    assert grip_cli._native_store_commit(root, 'declared case alias positive')
    (root / '.gitinclude').write_text((root / '.gitinclude').read_text() + '!docs/Secret.txt\n')
    index_before = (root / '.git/index').read_bytes()
    head_before = _git(root, 'rev-parse', 'HEAD')
    with pytest.raises(grip_cli.NativeStoreRefusal, match='docs/secret.txt is not included'):
        grip_cli._native_store_commit(root, 'must refuse tracked case alias')
    assert (root / '.git/index').read_bytes() == index_before
    assert _git(root, 'rev-parse', 'HEAD') == head_before


def test_generated_ignore_cannot_be_explicitly_included():
    text, notices = compile_gitignore('grip.toml\n.gitignore\n')
    assert any(n.line == '.gitignore' and 'generated' in n.reason for n in notices)
    assert text.splitlines()[-1] == '/.gitignore'


@pytest.mark.parametrize('kind', ['output-symlink', 'declaration-symlink', 'dangling-declaration', 'output-directory', 'tracked-output'])
def test_generation_refuses_owner_path_conflicts_without_changes(tmp_path, kind):
    root = tmp_path / 'root'; root.mkdir(); _git(root, 'init', '-q')
    declaration = root / '.gitinclude'; declaration.write_text('grip.toml\n')
    foreign = tmp_path / 'foreign'; foreign.write_text('owner bytes')
    output = root / '.gitignore'
    if kind == 'output-symlink':
        output.symlink_to(foreign)
    elif kind in ('declaration-symlink', 'dangling-declaration'):
        declaration.unlink()
        declaration.symlink_to(foreign if kind == 'declaration-symlink' else tmp_path / 'missing')
    elif kind == 'output-directory':
        output.mkdir()
    else:
        output.write_text('owner ignore\n'); _git(root, 'add', '.gitignore')
    before_index = (root / '.git/index').read_bytes() if (root / '.git/index').exists() else None
    with pytest.raises(grip_cli.NativeStoreRefusal):
        grip_cli._regenerate_workspace_gitignore(root)
    assert foreign.read_text() == 'owner bytes'
    assert before_index == ((root / '.git/index').read_bytes() if (root / '.git/index').exists() else None)
    if kind == 'tracked-output':
        assert output.read_text() == 'owner ignore\n'
    if kind == 'dangling-declaration':
        assert not (tmp_path / 'missing').exists() and declaration.is_symlink()


def test_failed_atomic_publish_preserves_owner_bytes_and_primary(tmp_path, monkeypatch):
    import os
    root = tmp_path / 'root'; root.mkdir(); _git(root, 'init', '-q')
    (root / '.gitinclude').write_text('grip.toml\n')
    (root / '.gitignore').write_text('previous generated bytes')
    primary = OSError('replace failed')
    def refuse(*args):
        raise primary
    monkeypatch.setattr(os, 'replace', refuse)
    with pytest.raises(OSError) as exc:
        grip_cli._regenerate_workspace_gitignore(root)
    assert exc.value is primary
    assert (root / '.gitignore').read_text() == 'previous generated bytes'
    assert not list(root.glob('.gitignore-*'))
