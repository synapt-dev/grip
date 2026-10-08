#!/usr/bin/env python3
"""Counted approval guard mutations, applied only in subprocess module memory.

Run from gr2 with the candidate tree on PYTHONPATH. The pristine file must pass
first. Each mutant selects its own witness, and never writes the source module.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

GUARDS = [
    ("workspace_policy_floor", "required_approvals", "return max(required, given or 0)", "return given if given is not None else required", "test_cli_count_cannot_lower_workspace_policy"),
    ("requested_count_type", "required_approvals", "if given is not None and (type(given) is not int or given < 0):", "if False:", "test_invalid_requested_count_refuses"),
    ("optional_policy_file", "required_approvals", "except FileNotFoundError:", "except FileExistsError:", "test_missing_policy_file_defaults_zero_and_merges"),
    ("workspace_approver", "approve", 'name = _git(workspace, "config", "user.name")', 'name = _git(Path.cwd(), "config", "user.name")', "test_approve_uses_workspace_identity_from_a_member_cwd"),
    ("zero_policy_skips_count", "review_merge", "if required:", "if True:", "test_zero_required_skips_an_unmeasurable_approval_chain"),
    ("merge_quorum", "review_merge", 'if approval_receipt["count"] < required:', "if False:", "test_default_merge_reads_workspace_policy_and_counts_two_approvers"),
    ("member_repo", "_context", "if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo or not merge_gate._store_inside(repo, root):", "if False:", "test_context_refuses_member_that_is_not_its_own_repo"),
    ("head_tree", "_context", "if actual_tree != tree:", "if False:", "test_context_refuses_a_head_tree_mismatch"),
    ("branch_repo", "current_branch", "if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo:", "if False:", "test_branch_resolver_refuses_nested_member"),
    ("branch_unique", "current_branch", "if len(names) != 1:", "if False:", "test_branch_resolver_refuses_different_member_branches"),
    ("reconcile_stability", "_reconcile", "if set(measured.values()) != {tip}:", "if False:", "test_reconcile_refuses_a_changed_post_publish_measurement"),
    ("writer_identity", "approve", "if not name.strip():", "if False:", "test_missing_approver_refuses_before_remote_measurement"),
    ("reconcile_lease", "_reconcile", 'f"--force-with-lease={ref}:{old or \'\'}"', '"--force"', "test_reconcile_lease_keeps_a_racing_remote_link"),
    ("bind_unknown_payloads", "_bind_record", "wire = fd.to_protobuf(workspace, tree)", "wire = fd.encode(fd.read_record(workspace, tree))", "test_first_prev_hashes_full_bind_with_bytes_and_nested_unknowns"),
    ("bind_byte_payloads", "_bind_record", "base64.b64encode(payload)", "base64.b64encode(b'')", "test_first_prev_hashes_full_bind_with_bytes_and_nested_unknowns"),
    ("writer_bind_root", "approve", "previous = _bind_record(workspace, rid) if tip is None else _read_link(workspace, tip)", "previous = root if tip is None else _read_link(workspace, tip)", "test_first_prev_hashes_full_bind_with_bytes_and_nested_unknowns"),
    ("reader_bind_root", "_walk", "previous = _bind_record(workspace, rid) if parents[0] == rid else _read_link(workspace, parents[0])", "previous = root if parents[0] == rid else _read_link(workspace, parents[0])", "test_first_prev_hashes_full_bind_with_bytes_and_nested_unknowns"),
    ("git_failure", "_git", "if p.returncode:", "if False:", "test_git_failure_is_not_an_empty_success"),
    ("bind_verification", "_context", 'if verified.get("tree_matches") is not True:', "if False:", "test_bind_verification_failure_stops_before_any_remote"),
    ("workspace_store", "_context", "if merge_gate._toplevel(workspace) != workspace or not merge_gate._store_inside(workspace, workspace):", "if False:", "test_workspace_store_is_checked_before_bind_reads"),
    ("policy_table", "required_approvals", "if not isinstance(policy, dict):", "if False:", "test_policy_table_must_be_a_table"),
    ("policy_integer", "required_approvals", "if type(required) is not int or required < 0:", "if False:", "test_invalid_or_signed_policy_refuses"),
    ("policy_signed_type", "required_approvals", 'if "require_signed" in policy and type(policy["require_signed"]) is not bool:', "if False:", "test_invalid_or_signed_policy_refuses"),
    ("policy_signed", "required_approvals", 'if policy.get("require_signed"):', "if False:", "test_invalid_or_signed_policy_refuses"),
    ("unique_current_bind", "current_review", "if len(hits) != 1:", "if False:", "test_reader_refuses_several_matching_binds"),
    ("remote_option", "_remote_tips", 'if remote.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in remote):', "if False:", "test_option_shaped_remote_stops_before_git"),
    ("remote_multiple", "_remote_tips", "if len(hits) > 1:", "if False:", "test_duplicate_remote_advertisement_refuses"),
    ("remote_fetch_identity", "_remote_tips", 'if _git(workspace, "rev-parse", temporary) != tip:', "if False:", "test_tip_fetch_must_match_its_advertisement"),
    ("duplicate_json", "_unique_pairs", "if key in out:", "if False:", "test_duplicate_json_keys_are_refused_directly"),
    ("plain_tree", "_read_link", 'if len(entries) != 1 or not re.fullmatch(r"100644 blob [0-9a-f]{40}\\tapproval\\.json", entries[0]):', "if False:", "test_malformed_tree_refuses"),
    ("canonical_json", "_read_link", "if not isinstance(record, dict) or text != canonical(record):", "if False:", "test_malformed_json_refuses"),
    ("required_fields", "_validate_link", 'if not required.issubset(record) or set(record) - required - {"sig", "keyring_tip"}:', "if False:", "test_malformed_link_fields_refuse[unknown-missing or unknown fields]"),
    ("schema_verdict", "_validate_link", 'if record["schema"] != SCHEMA or record["verdict"] != "approve":', "if False:", "test_malformed_link_fields_refuse[schema-schema or verdict]"),
    ("exact_pins", "_validate_link", 'if any(record[k] != root[k] for k in ("review_id", "author", "members")):', "if False:", "test_malformed_link_fields_refuse[head-bind or head pins]"),
    ("identity_shape", "_validate_link", 'if not isinstance(identity, dict) or set(identity) != {"name", "key_id"}:', "if False:", "test_malformed_link_fields_refuse[approver_shape-approver fields]"),
    ("identity_name", "_validate_link", 'if not isinstance(name, str) or not name or name != name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):', "if False:", "test_malformed_link_fields_refuse[name_empty-invalid approver name]"),
    ("reader_self", "_validate_link", "if name in authors:", "if False:", "test_malformed_link_fields_refuse[range_author-self_approval]"),
    ("unsigned_only", "_validate_link", 'if "sig" in record or identity["key_id"] != "" or record.get("keyring_tip", "") != "":', "if False:", "test_malformed_link_fields_refuse[empty_signature-approval_signed_unsupported]"),
    ("digest_shape", "_validate_link", 'if not isinstance(record["prev"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["prev"]):', "if False:", "test_malformed_link_fields_refuse[digest-invalid prev digest]"),
    ("timestamp_shape", "_validate_link", 'if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]+)?Z", stamp):', "if False:", "test_malformed_link_fields_refuse[timestamp-invalid created_at]"),
    ("timestamp_date", "_validate_link", 'datetime.fromisoformat(stamp.removesuffix("Z") + "+00:00")', "pass", "test_malformed_link_fields_refuse[date-invalid created_at]"),
    ("bind_not_link", "_walk", "if tip == rid:", "if False:", "test_approval_ref_cannot_point_to_the_bind"),
    ("chain_bound", "_walk", "if len(seen) >= 10000:", "if False:", "test_chain_length_limit_refuses"),
    ("one_parent", "_walk", "if len(parents) != 1:", "if False:", "test_a_link_must_have_one_parent"),
    ("previous_hash", "_walk", 'if record.get("prev") != digest(previous):', "if False:", "test_broken_prev_refuses_without_moving_target"),
    ("one_tip", "count_approvals", "if len(set(tips.values())) != 1:", "if False:", "test_count_refuses_divergence_without_writes"),
    ("tip_stability", "count_approvals", "if current != tips:", "if False:", "test_count_refuses_a_tip_that_moves_during_the_walk"),
    ("writer_self", "approve", "if name in authors:", "if False:", "test_author_is_refused_before_remote_measurement"),
    ("fork_refusal", "_reconcile", "if len(longest) != 1:", "if False:", "test_forked_remote_tips_refuse_without_overwriting_either"),
    ("append_lease", "approve", 'f"--force-with-lease={ref}:{old or \'\'}"', '"--force"', "test_lease_race_refuses_then_a_fresh_attempt_appends"),
]

PLUGIN = '''import ast, importlib, json, os
from pathlib import Path
def pytest_sessionstart(session):
    spec=json.loads(os.environ['APPROVAL_MUTATION'])
    module=importlib.import_module('gr2.python_cli.'+spec['module'])
    source=Path(module.__file__).read_text()
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name==spec['function'])
    lines=source.splitlines(keepends=True)
    part=''.join(lines[node.lineno-1:node.end_lineno])
    assert part.count(spec['target'])==1
    changed=part.replace(spec['target'],spec['replacement'])
    assert changed.count(spec['target'])==0
    candidate=''.join(lines[:node.lineno-1])+changed+''.join(lines[node.end_lineno:])
    exec(compile(candidate,module.__file__,'exec'),module.__dict__)
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--only", help="One guard id, for a replay")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    subject = root / "gr2/python_cli/approvals.py"
    source = subject.read_text()
    before = hashlib.sha256(subject.read_bytes()).hexdigest()
    subjects = {name: root / ("gr2/python_cli/" + name + ".py") for name in ("approvals", "merge_gate")}
    source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in subjects.items()}
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.pop("APPROVAL_MUTATION", None)
    pristine = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_review_approvals.py"], cwd=root, env=env, capture_output=True, text=True)
    if pristine.returncode:
        print(pristine.stdout + pristine.stderr)
        raise SystemExit("pristine failed; no mutants run")
    print("PRISTINE", pristine.stdout.splitlines()[-1], flush=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="approval-mutants-") as td:
        (Path(td) / "approval_mutant_plugin.py").write_text(PLUGIN)
        for ident, function, target, replacement, test in GUARDS:
            if args.only and ident != args.only:
                continue
            module = "merge_gate" if function == "review_merge" else "approvals"
            candidate_source = subjects[module].read_text()
            node = next(n for n in ast.parse(candidate_source).body if isinstance(n, ast.FunctionDef) and n.name == function)
            part = "".join(candidate_source.splitlines(keepends=True)[node.lineno-1:node.end_lineno])
            count = part.count(target)
            if count != 1:
                raise SystemExit(f"{ident}: target occurrence count {count}, expected1")
            assert part.replace(target, replacement).count(target) == 0
            spec = dict(module=module, function=function, target=target, replacement=replacement)
            mutated_env = {**env, "PYTHONPATH": td + os.pathsep + env["PYTHONPATH"], "APPROVAL_MUTATION": json.dumps(spec)}
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "approval_mutant_plugin",
                                  "tests/test_review_approvals.py::" + test], cwd=root, env=mutated_env, capture_output=True, text=True)
            killed = run.returncode == 1 and "FAILED tests/test_review_approvals.py::" in run.stdout
            results.append({"id": ident, "module": module, "function": function, "target": target, "replacement": replacement,
                            "target_count_before": 1, "target_count_after": 0, "witness": test,
                            "status": "KILLED" if killed else "SURVIVED_OR_INVALID", "exit": run.returncode,
                            "output": run.stdout + run.stderr})
            print(ident, results[-1]["status"], flush=True)
    after = hashlib.sha256(subject.read_bytes()).hexdigest()
    assert after == before, "source changed during mutation run"
    assert source_hashes == {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in subjects.items()}, "source changed during mutation run"
    result = {"subject": str(subject), "source_sha256": before, "source_unchanged": True,
              "source_hashes": source_hashes, "pristine": pristine.stdout, "mutants": results}
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(0 if results and all(row["status"] == "KILLED" for row in results) else 1)


if __name__ == "__main__":
    main()
