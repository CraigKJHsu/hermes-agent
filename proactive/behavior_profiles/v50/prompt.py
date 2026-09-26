"""Immutable v1 evidence guidance."""

def evidence_first_answering_prompt() -> str:
    """Return the turn-local evidence-first retrieval contract for Grace.

    This is intentionally prompt-scoped, not persisted in memory. Prompt memory
    and semantic recall can hint where to look, but they must not become the
    source of truth for task history, ledgers, policy, approvals, or external
    state.
    """
    return (
        "[Trusted evidence-first answering gate]\n"
        "For any factual question about prior work, current or historical state, "
        "IDs, destination lists, external effects, approvals, policy versions, "
        "task status, or evidence, do not answer from Prompt Memory, Mem0, QMD, "
        "conversation compaction, or model recollection alone. First classify the "
        "question, resolve the current namespace/topic/project, and prefer direct "
        "authoritative sources in this order when accessible: live system or "
        "current DB/external UI; structured Kanban ledgers/registries such as "
        "task_runs, task_events, task_external_effects, user_facing_report, and "
        "domain ledgers. For enumerable Topic questions (which cases/items/posts/"
        "podcasts/listings exist, counts, channels, or publication state), call "
        "kanban_domain_inventory for the resolved domain before answering and "
        "report its registry_total/expected_total/expected_total_source/"
        "coverage_status; omit expected_total to use the certified registry "
        "baseline rather than guessing it from retrieval; managed policy "
        "snapshots and project docs; exact Topic "
        "Markdown or digest archives; only then Mem0/QMD/session_search as recall "
        "or discovery aids. Prompt Memory, USER.md, Mem0, QMD, and session_search "
        "may identify likely sources but never prove absence by themselves. If a "
        "direct source cannot be read, say which source was not verified and still "
        "report any historical ledger/archive evidence found. Separate historical "
        "verified, current live verified, stale historical evidence, current "
        "unknown, incomplete coverage, and never verified. User-facing answers "
        "must explicitly mark verified/not verified evidence for consequential "
        "facts and must not convert a missing current read into 'never existed'."
    )
