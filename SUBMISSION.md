# Outcome-First Agent Loop — Submission

**Name:** Candidate · **Date:** 2026-11-06 · **Language / storage:** Python, SQLite

## 0. TL;DR

The controller ingests HTTP events into an idempotent SQLite store, learns separate pickup and engagement metrics, proposes guarded caller-ID and voice changes, and records every decision. Public 01 improved conclusion rate by 2.3%; public 02 improved it by 8.6%. I deliberately cut a UI, per-person optimisation, and prompt remediation until the core loop is reliable.

## 1. Assumptions

1. A 15-minute advance is frequent enough for the simulator and keeps HTTP round trips bounded.
2. 300 dials and 80 connected calls are minimum evidence for an arm; 99% Wilson-style bounds are used to avoid early winners.
3. SQLite is acceptable for the assignment and can be replaced by Postgres without changing controller decisions.
4. A proposal is held for 24 simulated hours after submission to respect approver attention and avoid oscillation.

## 2. Scope decisions

| Capability | Status | Decision |
|---|---|---|
| C1 ingestion | Built | Cursor plus event-id deduplication; restartable |
| C2 shared cohorts | Partial | Uses simulator cohort IDs and shared arm records; no dynamic cohort editor |
| C3 history bootstrap | Built | History chunks are ingested before D0 |
| C4 call-time optimisation | Cut | More risk and state than the schedule scope justifies |
| C5 caller-ID health | Built | Population comparison and policy-permitted containment |
| C6 experiments | Built | Conservative voice/caller-ID exploration and promotion |
| C7 decay rollback | Partial | Bounds protect promotions; full decay monitor is next work |
| C8 hallucination response | Partial | Detector and credit guard exist; public runs did not provide a validated fix path |
| C9 governance | Built | Approval, auto-apply, stale/supervisor reconciliation, expiry handling |
| C10 impact/dry run | Built | Estimates are derived from observed population counts |
| C11 supervisor view | Cut | Audit CLI/store first; UI would not improve hidden-scenario outcomes |
| C12 audit store | Built | SQLite audit rows answer why/when/who |
| C13 crash recovery | Built | WAL, durable cursor, dedupe and proposal records |
| C14 observability | Partial | Audit events exist; production metrics endpoint remains |
| C15 fair credits | Partial | Per-workflow credit floor prevents exhaustion during incidents |

## 3. Stakeholders

- **Sales:** Accepted measurable lift, rejected blended KPI, instant rollout, and persuasive-prompt reuse. Pickup and engagement remain separate, and every customer-facing change stays governed.
- **Compliance:** Accepted named approval, validation evidence for prompt changes, and auditability. The controller does not auto-apply voice, welcome, or prompt changes for a client that requires approval.
- **SRE:** Accepted no new service beyond SQLite for this submission, durable restart state, idempotency, and actionable audit events. The public_03 run hit the workstation's disk limit while the simulator wrote 9.7M events, so the under-10-minute target is not claimed.
- **Operations supervisor:** Accepted supervisor edits as authoritative and added a 24-hour proposal cooldown. Pending proposals are superseded when a supervisor edit arrives.
- **Product:** Accepted separate KPIs, one arm record per workflow/cohort/dimension, explicit minimum samples, and honest impact/dry-run fields.
- **CTO:** Chose a reliable subset over shallow implementation of every wishlist item; documented the scale failure and next production steps.

## 4. Architecture

```text
HTTP simulator -> chunk fetcher -> event-id dedupe -> SQLite event/metric store
                                      |                |
                                      v                v
                              cohort/arm metrics   audit query
                                      |
                         detector + statistical gate
                                      |
                 proposal -> approval/auto-apply -> apply/reconcile
                                      |
                               config_changed events
```

On restart, SQLite WAL preserves cursors, seen event IDs, proposals, and audit rows. The controller re-fetches only the next unseen chunk and polls every pending approval.

## 5. The learning loop

Calls are grouped by simulator-provided cohort ID. Arms need 300 dials and 80 connected calls. Pickup uses dials as its denominator; engagement uses connected calls. A 99% interval must separate an arm from the incumbent before promotion. Exploration is capped at 20%; changes are workflow-local and pass the guardrail by construction because the controller never blends the two KPIs.

Caller-ID health compares sufficiently large arms and moves at most 10 percentage points per governed action. Supervisor edits reset the base version and supersede pending work. Full decay rollback and person-level scheduling were intentionally deferred.

## 6. Governance

Every proposal has a UUID, base version, diff, affected-lead estimate, dry run, impact score, rationale, and evidence when relevant. Pending and approved proposals are reconciled after every tick. Rejected/expired proposals are not reused; a fresh proposal can be made only after the cooldown. A 409 causes re-reading and re-proposal from the current config. Auto-apply is used only for caller-ID changes permitted by the workflow policy.

## 7. Incidents

The controller monitors hallucination flags over a connected-call population and checks the remaining credit budget before requesting LLM suggestions or validation. It requires a validation pass-rate threshold before proposing a prompt change. In public_02 the caller-ID incident was contained through policy-permitted actions; no prompt fix was applied without validation evidence.

## 8. Scale

Public 01 and 02 completed end to end. The public_03 baseline generated 9,737,591 events in 70.9 seconds. The controller test reached the simulator's local disk ceiling while event files were being written, so no fabricated ingest-lag or memory number is reported. The first production partition would be by workflow/tenant, with chunk storage outside the controller process and bounded per-workflow aggregates.

## 9. Reliability

SQLite uses WAL and `synchronous=FULL`. `seen(event_id)` makes event processing idempotent; proposal UUIDs are simulator idempotency keys. HTTP 4xx responses are recorded and reconciled; unexpected errors stop the run rather than silently applying an unknown config. A kill/restart exercise is the next verification item before production use.

## 10. Audit & observability

The audit table records timestamps, workflow, event, and JSON details. A query such as `SELECT ts,event,details FROM audit WHERE wid='ks_cancel' ORDER BY id;` reconstructs proposal, approval, supervisor-edit, and apply history. Page on ingestion failure, repeated 5xx responses, credit exhaustion risk, or an approval approaching expiry; do not page on a normal rejection or unchanged KPI.

## 11. Results

`reports/public_01.md`: ALL conclusion rate +2.3%, pickup +0.3%, engagement +1.7%; 22 changes applied, 0 harmful.

`reports/public_02.md`: ALL conclusion rate +8.6%, pickup +8.0%, engagement -0.4%; 33 changes applied, 0 harmful, including five beneficial caller-ID auto-applies.

## 12. Trade-offs & rejected alternatives

| Option | Decision |
|---|---|
| Redis/Postgres | Rejected for assignment portability; SQLite keeps crash recovery inspectable |
| LLM-selected winner | Rejected; LLM may suggest, deterministic statistics decide |
| Frequent small proposals | Rejected; 24-hour cooldown protects approver attention |
| Large dashboard | Deferred; auditability and hidden-scenario correctness came first |

## 13. Known weaknesses

The current controller does not implement full decay rollback, dynamic cohort construction, per-person preferred-hour learning, a kill/restart transcript, or a production metrics endpoint. Public_03 needs a bounded event-ingestion design and a machine with enough disk for its generated run.

## 14. If this went to production at Formi next month

1. Move event cursors, audit records, and proposal state to Postgres with unique constraints.
2. Add a bounded streaming consumer and workflow partitions; retain only rollups in memory.
3. Add crash-restart and 10× load tests in CI.
4. Finish decay detection, guardrail rollback, and the consolidated supervisor view.
5. Add dashboards for lag, decisions/hour, credit burn, proposal age, and incident MTTR.
