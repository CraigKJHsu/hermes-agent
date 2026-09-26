# Versioned behavior profiles

Status: Current operator contract. Volatile selections and accepted canaries are recorded in the MissionCrew G-03 deployment status.

## Contract

Grace owns business understanding, delegation, review and delivery. This change
belongs to the shared control plane. It adds no publishing authorization.

An exact `(platform, chat_id, thread_id, project)` selection applies only when a
new Objective is inserted. The Objective transaction durably stores a profile
pin and the complete verified Topic policy bytes. Objectives created before the
behavior-pin rollout remain legacy/unresolved until explicitly migrated;
Objectives that already have a durable pin are not legacy. Disabling a selection
affects only future Objectives.

Multiple immutable manifest versions may coexist; the exact installed
profile/version/kernel inventory is point-in-time evidence recorded in G-03.
Each version pins a code bundle, separate routing snapshots, schema, validator
hash and safety compatibility hash. Different manifest versions may share one
immutable bundle; manifest version and code-directory name are different
compatibility identities and must not be inferred from each other.
The pin stores the actual `project_namespace` separately from the business profile
name. An operator may map an exact generated project namespace to `ai_bizweek`;
existing policy/memory namespaces are never renamed.
The pinned bundle contains contract validation, compiler, domain normalization,
review, preflight, workflow, routing and prompt code. Compiler output carries a
pin marker through execution/review cards. Admission, claims, completion,
callbacks and workflow operations validate the authoritative Objective pin.
Worker-supplied metadata cannot select another version.

The full Topic policy snapshot survives ordinary later `latest_active` and
Topic-binding changes. For a behavior-pinned Objective, current completion
validates that immutable snapshot; it does not reinterpret the pin from active
policy. Unpinned legacy tasks continue to revalidate `latest_active` and the
Topic binding at review and fail closed with `policy_stale` after a change.

The required deny-only revocation overlay for already pinned work is not yet
source/runtime verified. Until it is implemented and accepted, operators must
cancel every affected Objective and apply the relevant runtime/tool/credential
hard ceiling before activating a revocation; policy activation alone must not be
assumed to stop existing pins. A future overlay may only narrow or block
admission, claim, and completion, never reinterpret the pinned bundle or rewrite
its policy history. Changing the pin or policy snapshot still requires an
explicit idle-boundary migration. Completion receipts must match the pinned
version/hash and reviewers attest `pinned_version_verified=true`. Historical
policy reads after migration resolve immutable migration history, while old
generations cannot resume execution.

## Safety boundary and limits

Human authorization, revocation, budgets, external-effect ledger, idempotency,
cancellation, callback leases and CAS remain in the existing control plane.
Behavior pins do not authorize any external effect. Review runtime source
attestation rejects a process whose loaded source snapshot is stale. Pinned
review receipts bind the selected bundle and kernel separately from the physical
process inventory. Adding a profile requires a runtime restart; after restart,
existing pinned reviews and accepted packages keep their selected provenance.
Starting a worker still verifies its own loaded runtime against disk. Current
Topic selections do not upgrade existing Objective pins.

This is a transitional extraction: residual shared business decisions in the
controller/DB/executor are fenced by the kernel manifest. Editing a fenced file
blocks pinned work with `behavior.kernel_migration_required`; it must not silently
reinterpret old work. This conservative fence also means some otherwise harmless
shared-source edits require an explicit compatibility release. The hash is a
local integrity/compatibility check, not a signature against a malicious host
administrator. Any released bundle or kernel file must never be regenerated in
place; publish a new manifest/kernel generation instead.

## Shadow

From the `hermes-agent` repository root, run in the candidate checkout with the
supported Python environment:

```sh
.venv312/bin/python scripts/shadow_behavior_profiles.py --version EXACT_CANDIDATE_VERSION --output /absolute/new/output-directory
```

Each representative fixture runs through actual contract/compiler, SQLite
tasks, review, Objective stages and callback handling under legacy and the
selected profile version.
A CPython audit hook denies socket APIs (including UDP/DNS) and process APIs
(including direct forks). This is a guard for the trusted Python replay, not an
OS sandbox for hostile native code. Compare decisions, review
outcomes, stages, packages, task states, approvals and external-effect counts.
Pin metadata and compiled policy instructions intentionally differ. These are
synthetic transport receipts: shadow does not claim real Grace/model execution,
business acceptance or external publication.

## New-Objective canary

Use `.venv312/bin/python -m hermes_cli.behavior_profiles select selection.json`.
Example shape:

```json
{"platform":"telegram","chat_id":"EXACT_CHAT","thread_id":"EXACT_TOPIC","project":"EXACT_PROJECT_NAMESPACE","profile_id":"ai_bizweek","version":"EXACT_INSTALLED_VERSION","expected_revision":0,"reason":"Reviewed new-Objective canary"}
```

Replace `EXACT_*` and `expected_revision` with the exact installed version and
current selection revision read from the CLI and G-03 before applying; the
example is not directly executable. The Topic must already have its
managed policy binding. Use a second exact selection for
`secondhand_commerce`; there is no wildcard. First verify source
hashes, restart the affected runtime under the normal deployment procedure, and
verify loaded runtime attestation before enabling selection. Exercise a new
zero-external-effect Objective through Grace delegation, execution, independent
review and the original Objective's acceptance. Record task/run IDs and confirm
all pins, policy receipts, callback/stage transitions and effect count zero.
Synthetic shadow alone is not this production canary.

Before installation, verify each exact candidate in its candidate checkout:

```sh
.venv312/bin/python -m hermes_cli.behavior_profiles health --profile ai_bizweek --version EXACT_CANDIDATE_VERSION
.venv312/bin/python -m hermes_cli.behavior_profiles health --profile secondhand_commerce --version EXACT_CANDIDATE_VERSION
```

Candidate health does not open a board or inspect its current selections. Replace
`EXACT_CANDIDATE_VERSION` explicitly; the shadow command also requires a version.
Health reports all kernel source mismatches and returns a nonzero exit status on
any failure. Enabling a selection verifies its bundle, kernel and calling process
before changing the selection revision. Disabling a selection remains available
when the selected version is unhealthy.

After installation and the affected runtime restarts, inspect the selected
versions and every nonterminal pinned Objective with the read-only board check:

```sh
.venv312/bin/python -m hermes_cli.behavior_profiles health
```

Use `--board EXACT_BOARD` before `health` for a named board. This command never
creates or migrates a board. Its runtime digest attests only the calling process;
a fresh CLI cannot prove that a running gateway or dispatcher loaded the release.
Verify those processes through their normal runtime attestation before enabling
the new exact Topic selections. Review the board health report again after
selection changes. An older Objective keeps its old pin and may still fail;
resolve it through the explicit migration procedure below, preserving its policy,
stages and audit history. Never overwrite an old manifest or silently rebind it
to make health pass. Record approved unresolved Objectives separately from the
new-Objective canary; do not report whole-board health as passed while one fails.

Shared `browser_readonly` routing also requires the deployment's existing HubOps
configuration at `../docs/projects/hub-ops/agent-registry.yaml` and
`../docs/projects/hub-ops/routing-rules.yaml`, outside this Hermes repository.
The trusted `worker_profiles.clawops.browser_readonly.allowed_urls` list must
authorize the exact target URL; missing or invalid authority fails closed.
For an isolated worktree, supply copies at the same parent-relative location and
record their source and copy hashes. These external configuration files are a
deployment prerequisite and are not part of the Hermes commit.

A pinned profile must carry the same URL authority inside its immutable route
snapshot, and its immutable routing bundle must copy `allowed_urls` into both
the resolved assignment and backend role card. Profile v50 introduces route
snapshot v2 for this reason while retaining safety kernel v49. The snapshot
preserves the two URLs historically admitted by the prior pinned compiler; it
does not inherit later deployment-only URLs such as localhost targets.

Rollback new admissions with `profile_id:null`, `version:null` and the current
selection revision. Existing pinned work stays pinned; never delete its history.

## Explicit Objective migration

`.venv312/bin/python -m hermes_cli.behavior_profiles show OBJECTIVE_ID` reports the pin and
its CAS hash. Construct a migration JSON with exact Objective/Topic identity,
`profile_id`, `version`, `expected_revision`, `expected_pin_hash`, `reason`, and
`policy_mode` (`retain` by default, or explicitly `current`). Run:

```sh
.venv312/bin/python -m hermes_cli.behavior_profiles migrate migration.json
.venv312/bin/python -m hermes_cli.behavior_profiles migrate migration.json --apply
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
