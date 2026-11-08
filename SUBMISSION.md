# Outcome-First Agent Loop — Submission Notes

**Name:** Sandhit Karmakar · **Date:** 2026-11-08 · **Hours actually spent:** around 12hours

## 0. TL;DR

The controller ingests history and live events, learns from population-level experiments, routes changes through workflow governance, responds to caller-ID and prompt-safety incidents, and records an auditable decision trail in SQLite. Public 01 ended at +4.8% aggregate conclusion rate versus baseline; public 02 ended at +13.7%. The final graded runs applied no changes classified as harmful. The load run completed five days at 106 seconds per simulated day including history bootstrap. Per-person call-time optimization and production-grade shared storage/UI remain out of scope.

## 1. Assumptions

1. The simulator HTTP API is the sole source of operational facts available to the controller; scenario truth and run files are never inputs to controller decisions.
2. The simulator clock advances only through `/sim/advance`; the controller may spend wall-clock time between simulated ticks.
3. Workflow objective and governance policy returned by the API are authoritative. Stakeholder recollection cannot expand permissions.
4. Stable lead/person identifiers and observed variant assignments support historical attribution where the API provides them.
5. SQLite is adequate for this single-controller assignment process. Production multi-replica operation needs shared transactional storage and an ownership/lease strategy.
6. The loop prioritizes policy compliance and explainability over short-term lift.
7. The measured load run is representative of this Windows host only; it is not a substitute for the required 4-vCPU/8-GB Linux benchmark.

## 2. Scope decisions

| # | Capability | Status | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | Built | Chunk-based ingestion; aggregate deltas and high-water metadata commit transactionally. Load run completed 9.74M events. | Benchmark on the specified Linux target and add backpressure metrics. |
| C2 | Shared cohorts | Partial | Encoded lead segments are reused in analytics and decisions; view and proposal evidence expose population segments. | Promote to a versioned cohort registry with lifecycle and explicit contract tests. |
| C3 | History bootstrap | Built | History chunks seed assignments/outcomes before live decisions. | None for simulator scope. |
| C4 | Call-time optimization | Partial | Hour profile learning updates schedule weights. Full per-person preferred-hour learning is not delivered. | Repeated-person causal estimates, allocation and leakage controls. |
| C5 | Caller-ID health | Built | Rolling population window detects a degraded DID and rotates/downweights within policy limits. Public 02 flagged-DID share fell from 22.4% on D2 to 2.0% on D3. | Improve confidence bounds and avoid repeated small auto-shifts. |
| C6 | Welcome/voice experiments | Partial | Deterministic two-proportion tests, BH correction, sample floors and a control holdback support experiments. Dimensions are evaluated separately; interactions are not factorially estimated. | Pre-register experiments, robust sequential testing and interaction-aware designs. |
| C7 | Promotion/decay rollback | Built | Public 02 audit contains approved decay rollback proposals and applied config versions. | Add targeted edge-case fixtures and stronger post-promotion monitoring. |
| C8 | Hallucination response | Built | Detect → LLM suggest → validate → request approval → apply → close incident. Public 02 prompt incident closed 60 simulated minutes after detection. | Broaden prompt-quality checks and include measured engagement/guardrail evidence in candidate ranking. |
| C9 | Governance client | Built | Request limits, approver windows, approval polling, apply, rejection, stale, expiry/lapse and uncertain-write reconciliation are implemented. | Add broader transition fault-injection coverage. |
| C10 | Impact/dry run | Partial | Diffs, affected populations and model-based replay are attached; impact is explicitly an estimate, not a counterfactual. | Calibrate estimates out of sample and expose uncertainty intervals. |
| C11 | Supervisor view | Built | Consolidated JSON view, health and Prometheus-style metrics endpoints. | Authenticated production UI and alert delivery. |
| C12 | Audit store | Built | SQLite records config versions, events, proposals, approvals, incidents and decision rationale. | Retention policy, access control and external export. |
| C13 | Crash recovery | Built | Hard-kill/restart evidence includes three pending approvals; restart reaches completion with matching event high-water marks. | Inject at every API/apply boundary. |
| C14 | Loop observability | Partial | `/health`, `/metrics`, audit CLI and run evidence are present. | Production dashboards, alert routing and explicit lag SLOs. |
| C15 | Fair credit/attention allocation | Cut | Per-workflow policy caps are enforced; cross-tenant fairness scheduling is deferred. | Tenant budgets and fair scheduling across workflows. |

Selection principle: finish the safety-critical floor first, then spend effort on attribution, governed experiments, incident response and audit. Avoid claiming autonomy when a workflow policy requires a human decision.

## 3. Stakeholders

The simulator policy is authoritative when it conflicts with stakeholder recollection.

### 01 — Sales
Accepted: concise workflow-level evidence and timely decisions. Declined blended KPIs, blanket speed targets, universal auto-apply, Northwind KYC auto-apply, and cross-client prompt rollout. Kestrel auto-apply is scoped by its workflow policy; it is not blanket permission for every dimension. Northwind KYC policy does not grant the auto-apply permission Sales recalls.

Reply: “We can show workflow-level pickup and engagement separately, with the evidence and approval state behind each change. We will move quickly where that workflow’s policy permits it, but we will not bypass an approver or transfer a prompt result between clients. That keeps the summit story tied to changes the client actually authorized.”

### 02 — Northwind Compliance
Accepted named approval where policy requires it, validation evidence for script changes, auditability and approver-hours-aware routing. The memo’s conditions are stronger than some simulator workflow policies, so the client-facing production contract would need explicit reconciliation before rollout.

Reply: “We attach validation and population evidence to prompt proposals, retain the applied config version and variant attribution, and route approval requests within the workflow’s approver window. The audit record ties a lead to the observed variant and config version. We will honor the configured governance boundary for each workflow and document any stricter client contract before production.”

### 03 — Platform/SRE
Accepted restartability, idempotency, actionable health and dependency restraint. SQLite avoids external stateful infrastructure in the assignment but is not a multi-replica production store. Public 03 completed on this laptop; it was not measured on the specified 4-vCPU/8-GB Linux host.

Reply: “The controller uses transactional SQLite checkpoints, proposal IDs, approval reconciliation and health/metrics endpoints. The five-day public load run completed in 531 seconds with 127 MB controller peak RSS on an 8-core/12-thread, 16-GB Windows laptop. Before claiming the target-host SLO, I would run the same benchmark on 4 vCPU/8 GB and move durable state to Postgres with controller leases.”

### 04 — Kestrel Operations
Accepted supervisor-edit protection, policy-bounded requests, and a consolidated view. Policy controls whether edits can affect a running campaign. Hallucination reduction is not sufficient if engagement is harmed; the prompt fix requires validation and human governance where required.

Reply: “Supervisor changes become a new config version; a proposal based on the old version cannot silently overwrite it. The local view summarizes tests, proposals, changes and health. We only change live campaigns where that workflow permits it, and prompt fixes carry validation evidence through the approval path.”

### 05 — Product
Accepted separate KPIs, shared segment definitions, proposal evidence, and workflow-local promotion. Partial: the segment registry is bounded and per-person call-time optimization is not complete. Impact is historical replay, not a causal guarantee.

Reply: “Pickup and engagement stay separate. Proposals include a diff, affected population, historical dry run and estimated impact. The same encoded segments feed analysis and the view. Per-person preferred-hour learning remains deferred until we have enough repeated-person evidence and a design that limits leakage.”

### 06 — CTO
Accepted explainability, credit restraint, scale-aware design and explicit cuts. The load numbers and crash result are measured and reproducible; target-host throughput and 10× capacity are not claimed.

Reply: “I prioritized a governed, auditable loop over a broader but shallow feature set. Public 01/02 outcomes improved versus baseline with no harmful changes classified in the final runs; the load run processed 9.74M events. The next production risks are target-host throughput, shared-state ownership and statistical robustness under hidden populations.”

## 4. Architecture

```text
Simulator HTTP API
  ├─ history/events chunks ─> analytics + lead/variant joins ─> SQLite checkpoint/audit
  ├─ workflow/config/policy ─> config reconciler
  ├─ approvals <──────────── proposal lifecycle / apply / stale recovery
  └─ prompt suggestion/validation <─ incident response
                                      │
                 statistical planner + caller/schedule/decay checks
                                      │
                    governed config update or policy auto-apply
                                      │
                  supervisor snapshot + health/metrics endpoint
```

On restart, the controller reads simulator state, verifies scenario/high-water continuity, reloads analytics from SQLite, syncs current config, then reconciles pending proposals and approvals. Event aggregates and chunk high-water metadata commit atomically. Writes use proposal IDs and are reconciled after uncertain responses.

## 5. The learning loop

Lead attributes are encoded into bounded candidate segments, with an all-population segment. Calls are attributed to actual observed variants and the lead’s features. Proposal affected counts use a disjoint first-match population partition. `MIN_ARM_N=180` connected calls per arm, a five-day window, two-proportion tests, BH correction at q=0.10, a 6% relative-lift floor and a two-half consistency check govern routine welcome/voice findings. These are design thresholds, not an empirically measured false-winner rate. The controller retains control traffic and runs rollback checks, but the public report’s decay-share table is not proof of a successful rollback in every case.

Pickup and engagement remain separate. The planner uses the workflow primary KPI and the predicted guardrail; routine proposals now stop when the historical replay predicts a guardrail violation. A prompt-safety repair is a separate incident path: it favors validated, grounded prompts and requires governance, but its guardrail replay does not yet estimate prompt-specific engagement effects. That remains a known risk. Cadence, call window and capacity are never changed. Schedule learning changes only eligible-lead weights; per-person preferred-hour optimization is not implemented.

## 6. Governance

The engine persists config versions, proposals and approvals; it reconciles each state through the API, applies approved proposals only, and handles rejection, stale base versions, expiry/lapse and uncertain submissions. It respects request caps and approver windows. Auto-apply uses only workflow-enabled dimensions within the declared shift limit. Proposals carry summary, diff, affected count, replay dry run, impact score and rationale. Supervisor config events trigger a fresh read; stale proposals do not overwrite newer versions. Prompt incident proposals cite validation runs. On apply, the incident is closed using the proposal’s persisted incident ID, including after a restart.

## 7. Incidents

Public 02 caller-ID health detection used a rolling 240-minute population window. The grader recorded the degraded DID share at 22.4% at the D2 10:00 flag and 2.0% on D3. The controller detected it at D2 13:30, with 769 D2 dials in its rolling evidence, and resolved the incident 300 simulated minutes later. Three policy-permitted small shifts were applied; repeated shifts are a known area to consolidate.

The prompt-safety audit detected 13 hallucinations in 192 connected calls (6.77%) against a 4.05% threshold, requested a grounded P3 candidate, validated it at n=200 with a 98.5% pass rate, and applied it under approval. The incident closed 60 simulated minutes after detection. Public 02 used 7 credits, 2 validations and 1 LLM suggestion call. The grader reports hallucinated calls fell from 416 baseline to 153 with the controller. These figures are simulator-specific and do not establish production detection precision.

## 8. Scale

See [`reports/public_03_load.md`](reports/public_03_load.md) and [`evidence/public03-metrics.json`](evidence/public03-metrics.json). On a 12th Gen Intel Core i5-12450H laptop (8 cores/12 logical CPUs, 16 GB RAM), the controller completed five simulated days / 3.24M dials / 9.74M total historical-plus-live events in 531.42 seconds. Mean wall time was 106.28 seconds per simulated day including history bootstrap; peak controller RSS was 127.28 MB. The result is below 10 minutes/day on this host. The required 4-vCPU/8-GB Linux target was not tested. SQLite write volume and the single-process sequential decision loop are expected to constrain 10× scale first; partition by tenant/workflow and use shared transactional storage.

## 9. Reliability

Crash evidence is in [`evidence/crash_recovery.json`](evidence/crash_recovery.json), with reproducible steps in [`scripts/crash_recovery.py`](scripts/crash_recovery.py). A hard kill at simulated minute 720 left a durable checkpoint at minute 720 with three approval requests pending. Restart reconciled approvals and completed public 02 at minute 14,400. Final controller and simulator event IDs matched at 324,336; the audit recorded two controller starts and one clean stop. Proposal outcomes were 15 applied, two rejected and one stale. The API client retries reads and reconciles uncertain advance/write outcomes rather than blindly repeating them.

## 10. Audit & observability

Example query: `python -m solution.controller.audit_cli --db solution/state/public02/controller.sqlite --workflow ks_cancel --limit 60`. Excerpt from the prompt-fix timeline:

```text
2027-01-07T13:15+05:30 approval_requested  apr_00005, P2 -> P3, 6.8% hallucination rate
2027-01-07T13:15+05:30 validation_run      P3, n=200, pass_rate=0.985, val_00001
2027-01-07T14:45+05:30 approval_event     apr_00005 status=approved
2027-01-07T14:45+05:30 approval_applied   apr_00005 config_version=7
2027-01-07T14:45+05:30 incident_fixed     incident=2, MTTR=90 minutes, prompt=P3
2027-01-07T15:00+05:30 config_event       actor=controller_approved, version=7
```

The public API exposes the approval ID and decision status/reason, not a named approver identity; the audit store cannot invent one. The SQLite file is retained locally but ignored by Git; the JSON crash/load evidence and grade reports are portable. `/health` and `/healthz`, `/metrics`, and `/supervisor` expose controller health, counters and a consolidated workflow snapshot. Page on stale ingestion/high heartbeat age, repeated API failures, exhausted credits before an active incident can be validated, or a stuck approved proposal; do not page on a routine experiment result or a single call outcome.

## 11. Results

### Public 01

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.361 | +8.4% | 0.242 | +1.6% | 0.519 | +5.7% |
| nw_kyc | 0.370 | +1.9% | 0.242 | -0.1% | 0.605 | +2.5% |
| nw_renewal | 0.431 | +2.7% | 0.265 | +0.5% | 0.557 | +2.0% |
| ALL | 0.386 | +4.8% | 0.250 | +0.9% | 0.549 | +3.6% |

Six changes were applied: four beneficial, two neutral, zero harmful. There were seven approval requests: six approved and one declined; no expiries or apply conflicts.

### Public 02

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.347 | +40.3% | 0.234 | +27.5% | 0.509 | +6.9% |
| nw_kyc | 0.370 | -1.2% | 0.235 | -2.8% | 0.604 | +2.0% |
| nw_renewal | 0.431 | +0.9% | 0.260 | -0.5% | 0.556 | +1.4% |
| ALL | 0.380 | +13.7% | 0.244 | +9.7% | 0.544 | +2.7% |

Fourteen changes were applied: five beneficial, nine neutral, zero harmful. There were 13 approval requests, 11 approved, two declined and three policy-permitted auto-applies. Northwind KYC’s primary pickup fell 2.8% relative to baseline while engagement improved 2.0%; investigate that workflow’s pickup regression before widening autonomy. Aggregate pickup and engagement are separate, not combined into one KPI.

These are stochastic simulator runs against their paired no-op baselines, not confidence intervals or guaranteed lift. Full grader tables are in [`reports/public_01.md`](reports/public_01.md) and [`reports/public_02.md`](reports/public_02.md).

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| One blended KPI | Simple executive display | Hides pickup vs. engagement; keep both separate. |
| LLM chooses winning variant | Fast prototype | Not deterministic or statistically accountable; LLM only suggests incident candidates. |
| Cross-client promotion | Reuses apparent winners | Client populations/policies differ; promotion remains workflow-local. |
| Per-person schedule learning | Product headline | Needs repeated-person evidence and leakage controls; deferred. |
| New Redis/Kafka dependency | Could scale ingest | Adds operations burden; local SQLite for assignment, Postgres/partitioning for production. |
| Fully factorial welcome × voice test | Measures interactions | Traffic fragmentation and multiple-comparison burden; current dimensions are screened separately. |

## 13. Known weaknesses

- The public 03 run was not measured on the required Linux target.
- The crash run did not interrupt an outstanding approval.
- Variant arm minimums and BH thresholds need hidden-scenario calibration; observed public harms were zero in the final runs but that is not a false-winner guarantee.
- Prompt repair dry runs do not estimate prompt-specific engagement impact.
- Per-person call-time optimization, cross-tenant credit fairness and an authenticated production UI are incomplete.
- SQLite is single-node state; the supervisor HTTP view is local and unauthenticated.
- Repeated caller-ID auto-shifts can generate churn; consolidate based on a stronger minimum evidence window.

## 14. If this went to production at Formi next month

1. Run shadow decisions on hidden-like, independently seeded populations; review all guardrail misses and false winners.
2. Move state to Postgres with migrations, leases, backups, retention and access control.
3. Kill the controller at each API and transaction boundary, including with pending approvals and auto-apply writes.
4. Measure throughput and memory on the specified 4-vCPU/8-GB target with concurrent workflows.
5. Add authenticated supervisor UX, actionable alert routing, tenant fairness budgets and prompt-quality/engagement guardrails.
6. Enable autonomy gradually by dimension and policy, with rollback SLOs and a human review gate.
