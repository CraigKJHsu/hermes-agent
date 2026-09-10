# Versioned behavior profiles

## Contract

Grace owns business understanding, delegation, review and delivery. This change
belongs to the shared control plane. It adds no publishing authorization.

An exact `(platform, chat_id, thread_id, project)` selection applies only when a
new Objective is inserted. The Objective transaction durably stores a profile
pin and the complete verified Topic policy bytes. Existing Objectives remain
legacy/unresolved until explicitly migrated. Disabling a selection affects only
future Objectives.

Installed `ai_bizweek@v1` and `secondhand_commerce@v1` manifests pin a code bundle,
separate routing snapshots, schema, validator hash and safety compatibility hash.
The pin stores the actual `project_namespace` separately from the business profile
name. An operator may map an exact generated project namespace to `ai_bizweek`;
existing policy/memory namespaces are never renamed.
The v1 bundle contains contract validation, compiler, domain normalization,
review, preflight, workflow, routing and prompt code. Compiler output carries a
pin marker through execution/review cards. Admission, claims, completion,
callbacks and workflow operations validate the authoritative Objective pin.
Worker-supplied metadata cannot select another version.

The full Topic policy snapshot survives later `latest_active` changes. Completion
receipts must match the pinned version/hash; reviewers attest
`pinned_version_verified=true`. Historical policy reads after migration resolve
the immutable migration history, while old generations cannot resume execution.

## Safety boundary and limits

Human authorization, revocation, budgets, external-effect ledger, idempotency,
cancellation, callback leases and CAS remain in the existing control plane.
Behavior pins do not authorize any external effect. Review runtime source
attestation rejects a process whose loaded source snapshot is stale. Pinned
review receipts bind the selected bundle and kernel separately from the physical
process inventory. Adding a profile requires a runtime restart; after restart,
existing pinned reviews and accepted packages keep their selected provenance.
Starting a worker still verifies its own loaded runtime against disk.

This is a transitional extraction: residual shared business decisions in the
controller/DB/executor are fenced by the kernel manifest. Editing a fenced file
blocks pinned work with `behavior.kernel_migration_required`; it must not silently
reinterpret old work. This conservative fence also means some otherwise harmless
shared-source edits require an explicit compatibility release. The hash is a
local integrity/compatibility check, not a signature against a malicious host
administrator. Installed v1 files must never be regenerated after release.

## Shadow

Run in the candidate checkout with its Python dependencies:

```sh
python scripts/shadow_behavior_profiles.py --output /absolute/new/output-directory
```

Each of four representative fixtures runs through actual contract/compiler,
SQLite tasks, review, Objective stages and callback handling under legacy and v1.
The process denies network and subprocess execution. Compare decisions, review
outcomes, stages, packages, task states, approvals and external-effect counts.
Pin metadata and compiled policy instructions intentionally differ. These are
synthetic transport receipts: shadow does not claim real Grace/model execution,
business acceptance or external publication.

## New-Objective canary

Use `python -m hermes_cli.behavior_profiles select selection.json`. Example:

```json
{"platform":"telegram","chat_id":"EXACT_CHAT","thread_id":"EXACT_TOPIC","project":"ai_bizweek","profile_id":"ai_bizweek","version":"v1","expected_revision":0,"reason":"Reviewed new-Objective canary"}
```

The Topic must already have its managed policy binding. Use a second exact
selection for `secondhand_commerce`; there is no wildcard. First verify source
hashes, restart the affected runtime under the normal deployment procedure, and
verify loaded runtime attestation before enabling selection. Exercise a new
zero-external-effect Objective through Grace delegation, execution, independent
review and the original Objective's acceptance. Record task/run IDs and confirm
all pins, policy receipts, callback/stage transitions and effect count zero.
Synthetic shadow alone is not this production canary.

Rollback new admissions with `profile_id:null`, `version:null` and the current
selection revision. Existing pinned work stays pinned; never delete its history.

## Explicit Objective migration

`python -m hermes_cli.behavior_profiles show OBJECTIVE_ID` reports the pin and
its CAS hash. Construct a migration JSON with exact Objective/Topic identity,
`profile_id`, `version`, `expected_revision`, `expected_pin_hash`, `reason`, and
`policy_mode` (`retain` by default, or explicitly `current`). Run:

```sh
python -m hermes_cli.behavior_profiles migrate migration.json
python -m hermes_cli.behavior_profiles migrate migration.json --apply
```

The first command is a dry run. Apply rechecks inside a write transaction. Active
runs/delegations, unfinished versioned cards or undelivered/leased callbacks
block migration. Resolve or cancel them through normal ownership first. A legacy
Objective has unknown historical policy, so `retain` is rejected: an operator
must explicitly accept `current` policy at migration time. Its original project
namespace must be unambiguous in contract history or explicitly supplied as
`project_namespace`; an existing pin always retains its namespace. This does not invent
an original creation version.

Migration changes only the pin, immutable migration row and Objective revision.
It preserves the Objective cursor, stages, tasks, accepted evidence, approvals
and external-effect ledger. New work must use a newly bound contract/generation.
Old-generation cards remain audit evidence and cannot be reclaimed. Verify the
Objective can continue to its original acceptance after migration.

For rollback, specify `rollback_generation` of a recorded migration, matching
target profile/version, and current revision/pin hash. This restores historical
policy with a new monotonically increasing generation; it does not undo external
effects, task history or revisions. Rolling back into unresolved legacy is
intentionally unavailable.
