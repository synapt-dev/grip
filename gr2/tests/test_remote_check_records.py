"""Thin remote-check proof: producer, second-clone reader, moved-head absence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from gr2.python_cli import check_records


def test_check_crosses_bare_remote_and_stays_at_exact_head():
    with tempfile.TemporaryDirectory(prefix="exact-head-proof-") as root:
        root = Path(root)
        remote, writer, reader = (root / name for name in ("remote.git", "writer", "reader"))

        def git(repo, *args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(writer)], check=True, capture_output=True)
        git(writer, "config", "user.name", "Check proof")
        git(writer, "config", "user.email", "check-proof@example.invalid")
        (writer / "input").write_text("H\n")
        git(writer, "add", "input")
        git(writer, "commit", "-m", "First proof input")
        head = git(writer, "rev-parse", "HEAD")
        git(writer, "push", str(remote), "HEAD:refs/heads/main")
        subprocess.run(["git", "clone", "--branch", "main", str(remote), str(reader)],
                       check=True, capture_output=True)
        index_before = git(writer, "write-tree")

        def cli(*args, code=0):
            prefix = [os.environ["GR2_CHECK_PROOF_CLI"]] if os.environ.get("GR2_CHECK_PROOF_CLI") else [sys.executable, "-m", "gr2.python_cli.app"]
            environment = dict(os.environ)
            if os.environ.get("GR2_CHECK_PROOF_CLI"):
                environment.pop("PYTHONPATH", None)
                interpreter = str(Path(prefix[0]).parent / "python")
                identity = subprocess.run([interpreter, "-c", "import gr2.python_cli.check_records as c; print(c.__file__)"],
                                          env=environment, cwd=root, capture_output=True, text=True, check=True).stdout.strip()
                assert "/site-packages/" in identity, identity
                print("installed CLI subject:", identity)
            result = subprocess.run([*prefix, "check", *map(str, args)],
                                    capture_output=True, text=True, env=environment, cwd=root)
            assert result.returncode == code, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        produced = cli("run", writer, "--remote", remote, "--head", head, "--name", "test", "--",
                       sys.executable, "-c", "from pathlib import Path; assert Path('input').read_text() == 'H\\n'")
        seen = cli("show", reader, "--remote", remote, "--head", head, "--json")
        observation = produced["observation"]
        assert observation == dict(v=1, head=head, observed_head=head, name="test", result="pass",
                                   exit_code=0, observation_id=observation["observation_id"])
        assert seen == dict(status="pass", record_id=produced["record_id"],
                            snapshot_oid=produced["snapshot_oid"], head=head, member_key="repo",
                            records=[observation], reason="required_checks_pass")
        assert git(writer, "write-tree") == index_before
        assert git(writer, "status", "--porcelain") == ""
        assert git(remote, "show-ref", "--verify", check_records.CHECK_REF).split()[0] == produced["snapshot_oid"]
        assert git(reader, "for-each-ref", "refs/dev.synapt.grip/__check_transfers__") == ""
        (writer / "input").write_text("H2\n")
        git(writer, "commit", "-am", "Move proof head")
        head2 = git(writer, "rev-parse", "HEAD")
        git(writer, "push", str(remote), "HEAD:refs/heads/main")
        git(reader, "fetch", str(remote), "refs/heads/main")
        absent = cli("show", reader, "--remote", remote, "--head", head2, "--json")
        assert absent == dict(status="absent", record_id=None, snapshot_oid=produced["snapshot_oid"],
                              head=head2, member_key="repo", records=[], reason="required_check_absent")
        print(json.dumps({"source": check_records.__file__, "H": head, "H2": head2,
                          "written": produced, "read_H": seen, "read_H2": absent}, sort_keys=True))
    assert not root.exists()

import pytest


@pytest.fixture
def repositories():
    with tempfile.TemporaryDirectory(prefix="check-hardening-") as directory:
        root = Path(directory)
        remote = root / 'remote.git'
        repo = root / 'writer'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        check_records._git(repo, 'config', 'user.name', 'Check proof')
        check_records._git(repo, 'config', 'user.email', 'check-proof@example.invalid')
        check_records._git(repo, 'commit', '--allow-empty', '-m', 'H')
        head = check_records._git(repo, 'rev-parse', 'HEAD').decode().strip()
        check_records._git(repo, 'push', str(remote), 'HEAD:refs/heads/main')
        yield repo, str(remote), head, root
    assert not root.exists()


def receipt(head, name='test', code=0, **extra):
    return dict(v=1, head=head, observed_head=head, name=name,
                result='pass' if code == 0 else 'fail', exit_code=code, **extra)


def read(repo, remote, head, names=('test',)):
    return check_records.read_remote_check(remote, dict(path=repo, key='unit', remote=remote), head, names)


def test_remote_race_unions_conflict_other_head_and_unknown_fields(repositories, monkeypatch):
    repo, remote, head, root = repositories
    other = root / 'other'
    subprocess.run(['git', 'clone', '--branch', 'main', remote, str(other)], check=True, capture_output=True)
    for key, value in [('user.name', 'Other'), ('user.email', 'other@example.invalid')]:
        check_records._git(other, 'config', key, value)
    check_records._git(other, 'commit', '--allow-empty', '-m', 'H2')
    head2 = check_records._git(other, 'rev-parse', 'HEAD').decode().strip()
    check_records.publish_observation(other, remote, receipt(head2, diagnostic={'future': 7}))
    original = check_records._git
    races = []

    def race(path, *args, **kwargs):
        if args[0] == 'push' and path == repo and not races:
            races.append(True)
            check_records.publish_observation(other, remote, receipt(head, code=1, future='keep'))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(check_records, '_git', race)
    check_records.publish_observation(repo, remote, receipt(head))
    seen = read(repo, remote, head)
    assert races == [True]
    assert seen['status'] == 'fail'
    assert len(seen['records']) == 2
    assert next(r for r in seen['records'] if r['result'] == 'fail')['future'] == 'keep'
    # This head commit need not exist in the reader's object store.
    blob = check_records._entries(repo, seen['snapshot_oid'])[head2[:2] + '/' + head2[2:]]
    assert check_records._records(original(repo, 'cat-file', 'blob', blob), head2)[0]['diagnostic'] == {'future': 7}
    parents = original(repo, 'show', '-s', '--format=%P', seen['snapshot_oid']).split()
    assert len(parents) >= 2


def test_local_cas_race_retries_without_losing_winner(repositories, monkeypatch):
    repo, remote, head, _ = repositories
    original = check_records._git
    fired = []

    def race(path, *args, **kwargs):
        if args[:2] == ('update-ref', check_records.CHECK_REF) and not fired:
            fired.append(True)
            winner = check_records._commit_union(repo, check_records._union(repo, (), head,
                check_records._line(receipt(head, name='other'))), ())
            original(repo, 'update-ref', check_records.CHECK_REF, winner, '0' * len(head))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(check_records, '_git', race)
    check_records.publish_observation(repo, remote, receipt(head))
    assert fired and {r['name'] for r in read(repo, remote, head)['records']} == {'test', 'other'}


def test_applied_push_with_lost_ack_is_reconciled(repositories, monkeypatch):
    repo, remote, head, _ = repositories
    original = check_records._git
    pushes = []

    def lost_ack(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if args[0] == 'push':
            pushes.append(True)
            raise check_records.CheckRefused('lost acknowledgement')
        return result

    monkeypatch.setattr(check_records, '_git', lost_ack)
    check_records.publish_observation(repo, remote, receipt(head))
    assert pushes == [True] and read(repo, remote, head)['status'] == 'pass'


def test_failed_push_and_failed_reconciliation_is_indeterminate(repositories, monkeypatch):
    repo, remote, head, _ = repositories
    original = check_records._git
    pushed = []

    def outage(path, *args, **kwargs):
        if args[0] == 'push':
            pushed.append(True)
            raise check_records.CheckRefused('transport unavailable')
        if args[0] == 'ls-remote' and pushed:
            raise check_records.CheckRefused('remote unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(check_records, '_git', outage)
    with pytest.raises(check_records.CheckRefused, match='publication_indeterminate'):
        check_records.publish_observation(repo, remote, receipt(head))
    assert pushed == [True]


def test_retry_exhaustion_is_bounded(repositories, monkeypatch):
    repo, remote, head, _ = repositories
    original = check_records._git
    pushes = []

    def rejection(path, *args, **kwargs):
        if args[0] == 'push':
            pushes.append(args)
            raise check_records.CheckRefused('rejected')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(check_records, '_git', rejection)
    with pytest.raises(check_records.CheckRefused, match='conflict_exhausted'):
        check_records.publish_observation(repo, remote, receipt(head))
    assert len(pushes) == 4
    assert all('--force-with-lease=' + check_records.CHECK_REF + ':' in p for p in pushes)


def test_duplicate_and_missing_policy_and_transport_failure(repositories, monkeypatch):
    repo, remote, head, _ = repositories
    first = check_records.publish_observation(repo, remote, receipt(head))
    second = check_records.publish_observation(repo, remote, receipt(head))
    assert first['record_id'] == second['record_id']
    assert len(read(repo, remote, head)['records']) == 1
    missing = read(repo, remote, head, ('lint',))
    assert missing['status'] == 'absent' and missing['record_id'] == first['record_id']
    assert read(repo, remote, head, ())['status'] == 'fail'
    original = check_records._git

    def outage(path, *args, **kwargs):
        if args[0] == 'fetch':
            raise check_records.CheckRefused('fetch failed')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(check_records, '_git', outage)
    assert read(repo, remote, head)['status'] == 'fail'
    assert read(repo, remote, head)['records'] == []


@pytest.mark.parametrize('change', [dict(v=2), dict(v=True), dict(exit_code=True), dict(observed_head='wrong'),
                                   dict(name=''), dict(result='pass', exit_code=1)])
def test_invalid_record_refuses(repositories, change):
    repo, remote, head, _ = repositories
    with pytest.raises(check_records.CheckRefused):
        check_records.publish_observation(repo, remote, {**receipt(head), **change})
    assert read(repo, remote, head)['status'] == 'absent'


def test_duplicate_keys_utf8_and_noncanonical_refuse():
    for blob in [b'{"v":1,"v":1}\n', b'\xff\n', b'{ "v":1 }\n']:
        with pytest.raises((check_records.CheckRefused, ValueError, UnicodeError)):
            check_records._records(blob, 'a' * 40)


def test_execution_moves_head_refuses_publication(repositories):
    repo, remote, head, _ = repositories
    with pytest.raises(check_records.CheckRefused, match='execution_head_changed'):
        check_records.run_check(repo, remote, head, 'test',
            ['git', '-c', 'user.name=Proof', '-c', 'user.email=proof@example.invalid',
             'commit', '--allow-empty', '-m', 'Moved'])
    assert read(repo, remote, head)['status'] == 'absent'


def test_same_tree_new_commit_still_absent(repositories):
    repo, remote, head, _ = repositories
    check_records.publish_observation(repo, remote, receipt(head))
    check_records._git(repo, 'commit', '--allow-empty', '-m', 'Same tree, different commit')
    head2 = check_records._git(repo, 'rev-parse', 'HEAD').decode().strip()
    assert check_records._git(repo, 'rev-parse', head+'^{tree}') == check_records._git(repo, 'rev-parse', head2+'^{tree}')
    assert read(repo, remote, head2)['status'] == 'absent'


def test_corrupt_remote_refuses_without_replacing_it(repositories):
    repo, remote, head, _ = repositories
    blob = check_records._git(repo, 'hash-object', '-w', '--stdin', data=b'not JSON\n').decode().strip()
    tree = check_records._git(repo, 'mktree', data=f'100644 blob {blob}\tunrecognized\n'.encode()).decode().strip()
    corrupt = check_records._git(repo, 'commit-tree', tree, data=b'Corrupt fixture\n').decode().strip()
    check_records._git(repo, 'push', remote, corrupt+':'+check_records.CHECK_REF)
    assert read(repo, remote, head)['status'] == 'fail'
    with pytest.raises(check_records.CheckRefused):
        check_records.publish_observation(repo, remote, receipt(head))
    assert check_records._snapshot(repo, remote) == corrupt


def test_duplicate_keys_guard_is_independent():
    with pytest.raises(check_records.CheckRefused, match='duplicate_record_field'):
        check_records._pairs([('v', 1), ('v', 1)])


def test_failed_execution_is_recorded_and_pass_cannot_erase_it(repositories):
    repo, remote, head, _ = repositories
    failed = check_records.run_check(repo, remote, head, 'test', [sys.executable, '-c', 'raise SystemExit(7)'])
    assert failed['observation']['result'] == 'fail'
    assert failed['observation']['exit_code'] == 7
    check_records.run_check(repo, remote, head, 'test', [sys.executable, '-c', 'pass'])
    seen = read(repo, remote, head)
    assert seen['status'] == 'fail' and len(seen['records']) == 2
    assert {r['result'] for r in seen['records']} == {'pass', 'fail'}


@pytest.mark.parametrize('duplicate', ['leaf', 'fanout', 'none'])
def test_duplicate_git_tree_paths_refuse_without_replacing_remote(repositories, duplicate):
    repo, remote, head, _ = repositories

    def raw_tree(entries):
        raw = b''.join(mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
                       for mode, name, oid in entries)
        return check_records._git(repo, 'hash-object', '-w', '-t', 'tree', '--literally', '--stdin', data=raw).decode().strip()

    failed = check_records._git(repo, 'hash-object', '-w', '--stdin',
        data=check_records._line(receipt(head, code=1)) + b'\n').decode().strip()
    passed = check_records._git(repo, 'hash-object', '-w', '--stdin',
        data=check_records._line(receipt(head)) + b'\n').decode().strip()
    suffix, prefix = head[2:], head[:2]
    leaves = [('100644', suffix, passed)]
    if duplicate == 'leaf':
        leaves.insert(0, ('100644', suffix, failed))
    fanout = raw_tree(leaves)
    roots = [('40000', prefix, fanout)]
    if duplicate == 'fanout':
        roots.insert(0, ('40000', prefix, raw_tree([('100644', suffix, failed)])))
    root = raw_tree(roots)
    snapshot = check_records._git(repo, 'commit-tree', root, data=b'Tree path fixture\n').decode().strip()
    check_records._git(repo, 'push', remote, snapshot + ':' + check_records.CHECK_REF)
    seen = read(repo, remote, head)
    if duplicate == 'none':
        assert seen['status'] == 'pass'
        assert seen['record_id'] == passed
        assert len(seen['records']) == 1
    else:
        assert seen['status'] == 'fail' and seen['reason'] == 'duplicate_checks_tree_path'
        with pytest.raises(check_records.CheckRefused, match='duplicate_checks_tree_path'):
            check_records.publish_observation(repo, remote, receipt(head))
        assert check_records._snapshot(repo, remote) == snapshot
