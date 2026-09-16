# Topic execution compatibility: phase one

> Historical phase-one record. The later versioned Behavior Profile registry,
> Objective pin, safety-kernel generations and explicit migration workflow are
> implemented and documented in `docs/versioned-behavior-profiles.md`. The
> statements below describe the isolated observation baseline only and must not
> be read as current capability status.

This change adds observation and reproducible local replay. It does **not**
implement Behavior Profile isolation, pin old executable behavior, migrate an
Objective, change a policy resolution rule, or grant publication authority.

## Baseline and delivery boundary

The isolated branch is `codex/behavior-observation-phase1`. Its baseline commit
`6cc375385` snapshots the 35 inherited modified/untracked source files on top of
`109e879d9`. Those inherited changes are not part of this phase-one patch.
Review/apply only the diff from that baseline. The live Gateway checkout is not
edited, and no live database, restart, dispatch, publication, or GitHub push is
part of this implementation.

The baseline is today's observed checkout, not a reconstruction of the source
loaded when a historical Objective was originally created. The live incident
timeline and original Objective's recovery remain outside this verification.

## What is recorded

`proactive.behavior_observation` writes structured records through the existing
Python logger. Tests and the replay CLI can capture the same records in a local
sink. Contracts, rendered cards, approval fingerprints, and return values do not
contain this metadata. Raw policies, request text, approval tokens, and exception
messages are not copied into the log; exception text is represented by a hash.
Existing callers still receive the same exception type/message.

Each receipt carries `rule_id`, module `owner`, phase, decision, Objective/stage
and request references when available, input/reason hashes, and these fields:

| Field | Phase-one meaning |
| --- | --- |
| `behavior_profile_id` | `legacy_shared`, an observation label, not a selectable profile |
| `behavior_profile_version` | null: executable behavior has no pinned version |
| `contract_schema_version` | Present after successful normalization; null if not established |
| `validator_set_hash` | Digest of the explicitly listed validator source files, not an exhaustive validator registry |
| `behavior_bundle_hash` | Digest of the explicitly listed shared source files |
| `policy_snapshot_hash` | Digest of the snapshots seen at this boundary; only `policy_snapshot_verified=true` on successful Contract validation attests resolution |
| `safety_kernel_version` | null: no independently versioned kernel exists yet |

The source manifest and its observation time are captured once per observer
process. `source_evidence=listed_files_on_disk_at_first_observation` and
`runtime_version_verified=false` are mandatory caveats: on-disk hashes cannot
prove what every long-lived worker loaded, or freeze live routing YAML, mutable
in-memory values, transitive dependencies, prompts, or tool implementations.
Missing source files are listed with null hashes; the aggregate is then null.

The optional `grace_objective_behavior_observations` table stores a creation-time
sidecar for new Objectives after their business transaction commits, and never
overwrites an existing sidecar. If creation is inside a caller-owned transaction,
the observer emits a `not_persisted` receipt instead of writing to that transaction.
This prevents SQLite storage failures from rolling back business data. A process
crash between commit and observation can leave a sidecar gap: this best-effort
record is not the atomic executable pin required in phase two.
Creation has no resolved Loop Contract, so its
schema/policy fields remain null; later Contract receipts join by Objective ID.
Reopening an old database creates only the empty additive table. Existing
Objectives are not backfilled or relabeled. No execution path reads this table
to decide which behavior to run.

## Coverage and failure semantics

Observed boundaries:

- Contract entry, policy resolution, domain-memory resolution, group destination
  validation, and aggregate Contract schema validation.
- Execution/review card rendering.
- Objective creation and workflow planning.
- Review blocking, callback outcomes, and Objective stage transitions.

Rule IDs name existing guard groups/operation boundaries. They do not pretend to
identify every individual check inside a group. Asset postflight, every routing
guard, and every worker/watcher branch are not individually instrumented yet.
This is intentionally not a promise of complete trace coverage.

`returned` means the existing operation returned; it is not a claim of business
acceptance. Lifecycle receipts include before/after state and the available
workflow revision. Inner transaction receipts mark `transaction_pending=true`;
they must not be treated as durable completion if the outer transaction fails.
Ordinary sink/logging or optional sidecar-storage errors do not change business
decisions. Storage unavailability has its own observation receipt when logging
is available. Total logging failure necessarily leaves an observation gap.

## Replay and regression

Run with the repository's Python environment; the CLI always creates a **new**
output directory, assigns an isolated `HOME`/`HERMES_HOME`, and keeps its evidence:

```sh
python scripts/replay_behavior_observation.py --output /absolute/new/replay-directory
```

`--source-root /absolute/baseline-checkout` runs the same fixture driver against
the pre-change source. Compare the two `semantics.json` files. Raw results are
also retained in `raw-semantics.json`; receipts are in `observations.jsonl`.
Only the sandbox home path and its derived fingerprint are normalized. Actual
fingerprints are checked against the actual normalized Contract before deriving
the separately named `canonical_contract_fingerprint`. A generated approval
token is represented as `$APPROVAL_TOKEN` in the semantic result.

Fixtures are representative historical samples, with provenance recorded in
`tests/fixtures/behavior_observation/topics.json`. They are not live DB exports.
The Audio Brief discrepancy was supplied by the user. The runner does not ask
an LLM to rediscover that discrepancy. Accepted reviews use a clearly synthetic
transport receipt, following existing callback tests; this proves the local
receipt/lifecycle handling, not the quality of a new model review.

| Replay case | Expected result |
| --- | --- |
| SoloBizAi corrected content | Review accepted; move to `publish`, waiting for a scoped approval |
| SoloBizAi Audio Brief says 30 times | Dependency rejection returns the **same** execution to `ready`, review to `todo`; Objective stays at `prepare`, with no accepted callback or approval |
| Secondhand preflight sample | Accepted local evidence; wait for approval without publication |
| Secondhand mismatched destination URL | Contract rejected before task creation |

All cases preserve package data and create zero external effects. Pending
approval fixtures are never consumed. This follows the current correction path;
it deliberately does not replace a valid review rejection with successful
completion, or falsely report every rejection as `Objective.status=blocked`.

The non-interference test mutates each domain's existing schema definition in
turn: that domain's normalization must change, while the other domain's complete
replay result remains identical. This is a demonstrated cross-Topic regression
guard for those definitions, **not** proof that arbitrary shared-code changes
can no longer affect other Topics. Phase two must add equivalent coverage for
the extracted validators, compilers, routes, and stage transitions.

Additional checks cover observation failure, privacy, nested capture isolation,
sidecar restart/readback, additive schema/no legacy backfill, caller transaction
rollback, and an injected sidecar ROLLBACK that must not undo Objective creation.
Run the canonical test wrapper, not raw pytest:

```sh
HERMES_TEST_VENV=/absolute/venv scripts/run_tests.sh -j 2 \
  tests/proactive/test_behavior_observation.py \
  tests/proactive/test_loop_contract.py \
  tests/proactive/test_grace_task_compiler.py \
  tests/proactive/test_grace_objectives.py \
  tests/proactive/test_objective_workflow.py \
  tests/proactive/test_policy_registry.py \
  tests/hermes_cli/test_kanban_db.py
```

## Proposed production application and rollback — not executed

1. Reconcile the then-current live checkout against the recorded baseline. Apply
   only this phase-one patch in a staging checkout, preserving intervening work.
2. After explicit approval of the database/restart scope, take a consistent
   SQLite backup including committed WAL data and verify its readability.
3. Rehearse the additive table creation on that copy. Compare existing Objective,
   stage, callback, approval, run, and external-effect rows before/after. Existing
   business rows must be unchanged and the new sidecar table initially empty.
4. Deploy the reviewed patch and restart only the approved service(s). Normal
   database initialization creates the optional table. Verify actual runtime
   source/load evidence separately from these observational file hashes.
5. Check a newly created controlled Objective's sidecar and joined receipts.
   Verify an existing Objective remains unbackfilled, and verify Grace's normal
   delegation/review/continuation path before declaring the deployed phase done.

Rollback restores the exact pre-deployment source release and restarts the same
approved services. Leave the additive table and logs in place for audit; older
code does not read them. Do not restore an older business database over newer
tasks/effects, delete Objectives, undo publications, or move profile pointers as
part of this rollback. There is no profile pointer in phase one.

Phase two requires a separate approval: immutable behavior bundles, complete
version propagation, legacy compatibility decisions, shadow evaluation, Topic
canary, and explicit migration. None is enabled by this patch.

## Grace verification handoff after an approved deployment

> Grace，請先確認這次只新增執行規則觀測，尚未切換 Behavior Profile。請由你帶隊，用核准的隔離案例核對新 Objective 的觀測紀錄、審查結果、stage 與續接狀態，並確認舊 Objective 沒有被回填或換版。Audio Brief 的 30 倍錯誤仍須退回修正；不得為驗證而發布、消耗授權或重跑正式業務。請交付實際證據與尚未驗證的範圍，不要只以測試通過或 callback 已送達宣告業務完成。
