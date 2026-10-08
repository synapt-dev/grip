# Event outcome classification

`emit` is strict. A sink failure raises and stops the caller. Sites that run
after an outcome which cannot honestly be reported as failed use
`emit_after_outcome` instead. That helper preserves the outcome and reports the
recording failure on stderr. It is not a general event wrapper.

## Direct call sites

| Site | Event | Position | Policy |
|---|---|---|---|
| `hooks.run_lifecycle_stage` skipped branch | `hook.skipped` | no hook ran | strict |
| `hooks.run_lifecycle_stage` before subprocess | `hook.started` | before command | strict |
| `hooks.run_lifecycle_stage` success branch | `hook.completed` | after command | preserve outcome |
| `hooks.run_lifecycle_stage` failure branch | `hook.failed` | after command | preserve outcome |
| `spec_apply.apply_plan` projection loop | `workspace.file_projected` | after projection | preserve outcome |
| `spec_apply.apply_plan` completion | `workspace.materialized` | after filesystem work | preserve outcome |
| `execops.run_exec` start | `exec.started` | after lease acquisition | preserve outcome |
| `execops.run_exec` success | `exec.completed` | after subprocesses | preserve outcome |
| `execops.run_exec` failure | `exec.failed` | after subprocesses | preserve outcome |
| `failures.resolve_failure_marker` | `failure.resolved` | after marker removal | preserve outcome |
| `pr.create_pr_group` | `pr.created` | after host creation and state save | preserve outcome |
| `pr.merge_pr_group` | `pr.merged` | after host merge and state save | preserve outcome |
| `pr._record_merge_failure` | `pr.merge_failed` | after a host attempt | existing guarded error channel |
| `pr.check_pr_group_status` state change | `pr.status_changed` | before cache mutation | strict |
| `pr.check_pr_group_status` failed checks | `pr.checks_failed` | read-only observation | strict |
| `pr.check_pr_group_status` passed checks | `pr.checks_passed` | read-only observation | strict |
| `pr.record_pr_review` | `pr.review_submitted` | no local work | strict |
| `syncops._emit_sync_event` | dynamic | dispatch only | classified by caller below |
| `app.lane_create` | `lane.created` | after lane materialization | preserve outcome |
| `app.lane_enter` | `lane.entered` | after entry mutation | preserve outcome |
| `app.lane_exit` | `lane.exited` | after exit mutation | preserve outcome |
| `app.lane_lease_acquire` | `lease.acquired` | after lease mutation | preserve outcome |
| `app.lane_lease_release` | `lease.released` | after lease mutation | preserve outcome |
| `app.review_bind` | `review.bound` | after the bind commit is written | preserve outcome |
| `app._emit_review_opened` | `review.opened` | after reconstruction and marker | preserve outcome |
| `app.review_verify` | `review.verified` | read-only observation | strict |
| `app._emit_review_run` | `review.run_completed` | after the run and its receipt | preserve outcome |
| `app._emit_review_run` | `review.run_refused` | after the refusal (and its receipt, when one is written) | preserve outcome |
| `app.review_close_gr` | `review.closed` | after the lane is reclaimed | preserve outcome |

## Dynamic sync callers

The sync wrapper does not determine position. Each caller does.

| Caller outcome | Position | Policy |
|---|---|---|
| cache seeded or refreshed | after cache mutation | preserve outcome |
| shared repo cloned and hooks run | after filesystem and hook work | preserve outcome |
| remote refs fetched | after network and ref mutation | preserve outcome |
| lane repo materialized and hooks run | after filesystem and hook work | preserve outcome |
| dirty repo stashed | after working-tree mutation | preserve outcome |
| dirty repo discarded | after working-tree mutation | preserve outcome |
| lock-held conflict | no mutation | strict |
| lock-held terminal result | no mutation | strict |
| sync started | before planned operations | strict, with lock cleanup on failure |
| active-lease conflict | no mutation | strict |
| dirty-repo conflict | no mutation | strict |
| blocked terminal result | no mutation | strict |
| terminal result after operations | after repo and state mutation | preserve outcome |
