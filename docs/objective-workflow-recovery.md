# Objective workflow recovery

From the `hermes-agent` repository root, use
`.venv312/bin/python -m hermes_cli.objective_workflow show OBJECTIVE_ID` to read the
authoritative stage order, revision and publication progress. This operator CLI
uses the configured Hermes home/board. Grace should delegate internal planning
to the ops worker; it does not need browser credentials or registry SQL.

Use `.venv312/bin/python -m hermes_cli.objective_workflow plan plan.json` to submit a forward
stage plan. Required JSON fields: `objective_id`, `expected_revision`,
`platform`, `chat_id`, `thread_id`, `required_stage_keys`, `current_stage_key`,
and `reason`. Optional fields: `title`, `acceptance_criteria`, `next_action`,
and `workflow`. The following is a complete `plan.json` document:

```json
{
  "objective_id": "go_EXACT_OBJECTIVE_ID",
  "expected_revision": 1,
  "platform": "telegram",
  "chat_id": "EXACT_CHAT_ID",
  "thread_id": "EXACT_THREAD_ID",
  "required_stage_keys": ["prepare", "preflight", "publish", "terminal"],
  "current_stage_key": "preflight",
  "reason": "Add the reviewed secondhand publication workflow",
  "workflow": {
    "project": "EXACT_PROJECT_NAMESPACE",
    "source_listing_id": "12345",
    "expected_destinations": 20,
    "excluded_destination_ids": ["111"],
    "historical_evidence": []
  }
}
```

Replace every `EXACT_*` value, `expected_revision`, and the stage keys with the
values returned by `show`; the final stage key must remain the Objective's
declared terminal stage. The workflow project must match the Objective's exact
project namespace. Do not reuse the example IDs.

An ops card already bound to the Objective must use `request-plan` instead of
`plan`, and include `origin_execution_task_id` in this same top-level JSON
document. The request is bound to that card's active execution run, delegation,
stage, Topic, and Objective revision. It does not change the spine. The gateway
applies it only after that exact run completes and its paired Grace review
accepts it, then the accepted callback can bind the newly declared current
stage. Set `current_stage_key` to that new successor stage. A blocked or rejected
run, stale revision, mismatched review receipt, or unrelated in-flight
delegation leaves the request unapplied.

Listing IDs and excluded group IDs must be strings of ASCII digits, even in
JSON. `excluded_destination_names` may additionally preserve historical names
whose numeric IDs have not yet been recovered. `historical_evidence` can include
terminal blocked pairs with `use=blocker_context_only`; these retain failure
evidence without pretending the review accepted it.

The plan preserves bound stages and evidence. An omitted stage can be removed
only if it is still planned and has no delegation, task links or evidence; its
previous row remains in the revision snapshot. Bound stages retain
their relative order; the terminal stage remains last. Admitted work, active runs and stale
revisions reject the plan. Validation, writes, readback and the previous-state
revision snapshot share one transaction. Any failure rolls back the entire
plan. Never build a spine by looping over `ensure_grace_objective_stage`.
To undo an incorrect plan, inspect its revision snapshot and submit a corrected
plan against the current revision; do not delete history or restore the live DB.

Historical accepted pairs are pinned as context only. Their original task
contracts, callbacks and approvals remain unchanged. Fresh writable contracts
must name the same objective and an exact run-bound, Grace-accepted preflight.
Use `facebook_group_publish.mode=accepted_preflight` and `preflight_source`
containing the execution/review IDs. The controller resolves those IDs and pins
the evidence; callers cannot supply their own trusted chooser evidence.

The forward flow is source/chooser preflight, candidate qualification and gap
research, membership preparation when needed, fresh accepted chooser scope,
exact approval, publication, verification, gap reconciliation, and final
acceptance. All successor cards carry the same `objective_ref`; repairs and
intermediate reviews cannot close the parent. A stage may need a bounded retry
when evidence changes. Its retry retains all prior effects.

Candidate suitability, membership, chooser selectability, submission, and
visible publication are distinct states. Always report them separately. A
candidate total of 20 does not satisfy a publication target of 20. The objective
progress projection counts only verified `publish_existing_listing` effects for
the exact source listing with `publication_state=published`, a canonical group
name, a matching Facebook group post permalink and structured visible-publication
readback. Each effect must name its originating native execution run, with an
accepted review explicitly bound to that run. Later retries do not approve old
unreviewed effects or discard earlier accepted run bindings. The originating
contract must authorize that exact listing and destination through the resolved
publication route. Legacy imported effects are context and replay protection,
never publication-completion evidence or consumers of this objective's new-publication capacity;
historical exclusions do not count. Excluding an objective-native submission never
releases its occupied capacity. Pending/ambiguous effects reserve destination
capacity until reconciled, preventing an eventual overshoot of the target. They require readback
and cannot be replayed as a fresh publish. Join evidence remains available but
never counts as publication. Update reconciliation through the existing
external-effect APIs, not raw SQL. A read-only verification stage can return
`acceptanceEvidence.publication_reconciliation` rows with exact `group_id`,
`source_listing_id`, `status` (published or rejected), `visible`, `pending_review`,
`post_url`, and `observed_at`. Once its latest run receives an exact-run accepted
review, the projection folds that observation over the existing submission.
It never creates another external effect. Confirmed rejection releases capacity
for a different new destination; the original group remains protected from replay.

Before approval, the controller rejects an unplanned workflow, mismatched
listing/project, a route contradicted by the contract, excluded destinations,
duplicate submissions, and requests exceeding the remaining target. Reservation
re-resolves the exact accepted execution/review runs and evidence hash under the
approval transaction and serializes in-flight objective work.
Changed accepted preflight evidence invalidates a sealed publication contract.
At execution, refresh live identities and eligibility. Partial execution is
allowed only when the exact contract permits it; otherwise stop and narrow the
next contract. Never substitute another group or use Create new listing to
repost an existing listing. Reconcile an uncertain submit before retrying.

Validation from the `hermes-agent` repository root:

```sh
HERMES_TEST_VENV="$PWD/.venv312" \
  scripts/run_tests.sh tests/proactive/test_objective_workflow.py
```

This selects the current Python 3.12 test environment and exercises the real
SQLite APIs and CLI in isolated Hermes homes, including rollback, revision
conflict, retained history, exact review binding, restart readback and
closure/replay gates.
