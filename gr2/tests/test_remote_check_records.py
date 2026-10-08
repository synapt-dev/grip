"""Thin remote-check proof: producer, second-clone reader, moved-head absence."""
import json
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
            result = subprocess.run([sys.executable, "-m", "gr2.python_cli.app", "check", *map(str, args)],
                                    capture_output=True, text=True)
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
