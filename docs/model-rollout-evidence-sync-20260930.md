# Model rollout evidence repair — 2026-09-30

Scope: preserve exact controller acceptance criteria and bounded attachment hashes/Gateway claims; provide a guarded file SHA-256 tool to read-only reviewers; prevent background board scans from redirecting delegation; reject retry of finalized review callbacks. Preserve old blocked cards, history, and retry counts.

The installed default Hermes profile uses `tool_output.max_line_length: 6000` (previously 2000). The original Telegram DOM evidence has a maximum line length of 5827 characters. This uses existing configuration; no extra file-read API is introduced. Live credential/config files and raw Telegram evidence are excluded from Git.

Live validation on 2026-09-29: DevOps Integration Agent (`clawops-dev`, task `t_bb3c0edb`, run 2667) completed execution; independent Grace Review (`default`, task `t_ef3c01b8`, run 2668) accepted; Objective `go_user_cc06911c61d1175a4983d6bd` completed at stage `r5_existing_evidence_narrow_review_and_delivery_r3`. Grace delivered the report in General at 23:33 Asia/Taipei. The prior `_r2` terminal-blocked record remains.

Acceptance is limited to effective configuration matching Gateway-reported Astra/Sol/Luna and captured UI-visible basic send/response pairs across nine Topics (19 messages). Provider raw actual model, hidden overrides, fallback switching, and complete Topic business workflows remain unverified. The separate OpenClaw patch preserves provider response.model when present without overwriting the requested model; it does not upgrade old Gateway receipts into provider proof.

The isolated GitHub repair branch seals these three changed kernel sources in kernel v9003/profile v9004 (the next unused version after local v9003). Existing manifests and live DB selections remain unchanged; applying this source package requires explicit profile migration/reload before execution. Attachment size is measured with its verified hash, not copied from DB metadata.
