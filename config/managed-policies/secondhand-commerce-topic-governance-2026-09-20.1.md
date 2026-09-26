# Secondhand Commerce Topic Governance

## Scope

This policy governs Grace and ClawOps work in the secondhand_commerce Topic, including Facebook Marketplace listing identity, historical group-destination recovery, group-status readback, and staged relisting workflows.

## Source And Evidence Rules

- Treat direct task evidence, Kanban task_runs/task_events/task_external_effects, commerce_group_ledger, commerce_group_coverage, attached artifacts, and verified live readbacks as authoritative evidence.
- Prompt Memory, Mem0, QMD, session_search, and compact Topic memory may be used for discovery, but they do not prove completion, absence, current Facebook state, or external effects by themselves.
- Preserve listing ID conflicts explicitly. Do not substitute one Marketplace listing ID for another without a fresh source-listing readback or explicit KJ approval.
- Classify each consequential claim as historical verified, current live verified, stale historical evidence, current unknown, incomplete coverage, not verified, or blocked.

## Read-Only Group Status Reports

- A secondhand_commerce_group_status or read-only recovery task must not mutate Facebook state.
- Allowed read-only outputs include named destination rows, destination IDs or URLs, readable group names, status labels, evidence text, observed_at or verified_at timestamps, coverage counts, and precise blockers.
- Every row must include either external_effects=[] for the current read-only phase or a precise blocker. Do not invent rows to reach an expected total.
- report.complete means the originating user outcome is complete. If the original objective expects 20 destinations and only 18 are verified, complete must be false even if the phase-1 audit is useful.

## Group Candidate Qualification

- A candidate must be either a specialist buy-and-sell group whose visible scope explicitly permits the exact listed item category, or a general multi-category secondhand marketplace whose visible scope does not exclude the item.
- A community without visible buy-and-sell permission may be retained only as conditional.
- An adjacent specialist group may be ready only when its visible rules explicitly permit the listed item category; otherwise it is conditional.
- Exclude specialist marketplaces for unrelated categories. Search-result proximity, a similar product word, or generic terms such as equipment do not establish product fit.
- ready requires a verified numeric group ID and canonical URL, Taiwan or served-region evidence, visible commerce permission, and no visible category or rule conflict. Missing commerce or category-fit evidence must be conditional or excluded, never ready.
- Reports must include candidate_count, ready_count, conditional_count, and excluded_count. The counts must reconcile with the listed rows, and every excluded row must include its group ID when known and an exclusion reason.

## External Action Boundaries

- Facebook actions such as checkbox selection, Post, Publish, Submit, Share, Join, Edit, Upload, Comment, React, Message, or relisting require fresh, explicit, task-scoped KJ approval naming the exact source listing and exact destination groups.
- Read-only inventory, formatting, candidate discovery, or status reports must never close an originating external-action objective such as relisting to original groups.
- A staged workflow must preserve the durable objective and objective_ref across phases. Phase-1 read-only acceptance is intermediate evidence only.

## Grace Review Requirements

- Grace review must verify the pinned policy snapshot and include policy_receipts in kanban_complete metadata for each policy snapshot.
- For latest_active policy snapshots, review receipts must set latest_active_verified=true.
- Grace may accept a fail-closed or incomplete phase only as the correct phase outcome; it must not convert that into completion of the larger external-action objective.
- If required policy snapshots, receipts, task evidence, or coverage are missing or stale, Grace must block with the exact missing item instead of asking KJ to judge technical policy state. user_facing_report is required only when the admitted Loop Contract explicitly declares user_facing_delivery.required=true; when the contract omits or forbids that delivery, Grace must not request or validate user_facing_report.
