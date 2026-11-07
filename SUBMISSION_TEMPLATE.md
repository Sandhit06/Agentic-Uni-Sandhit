# Outcome-First Agent Loop — Submission

**Name:** · **Date:** · **Hours actually spent (be honest):** · **Language / storage:**

> Copy this file to `SUBMISSION.md`. Keep the headings. Be concrete: numbers, examples, diagrams. If a
> section doesn't apply, say why in a line rather than deleting it.

---

## 0. TL;DR
Five lines at most: what your controller does, the headline results on public_01/02, what you deliberately
didn't build.

## 1. Assumptions
Every assumption you made about the business, the data or the environment, numbered. We'll discuss them.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | … | | | |

Then one paragraph: the principle you used to choose.

## 3. Stakeholders
One subsection per memo (01–06):
- What you accepted, and where it shows up in the system.
- What you pushed back on or declined, and why.
- Any claim in the memo that turned out to be **wrong** against the actual policy or contract.
- **The reply you'd send them** (≤ 120 words, ready to paste).

## 4. Architecture
Diagram (ASCII or Mermaid) from events in to config changes out. Components, state, where the state lives,
and what happens on restart.

## 5. The learning loop
- **Cohorts**: how they're formed, the minimum size and why, and how you avoid fragmenting traffic.
- **Experiment design**: allocation strategy, exploration vs. exploitation, how you handle two dimensions
  (welcome × voice) and interactions between them.
- **Deciding a winner**: test or posterior, stopping rule, multiple comparisons, and your false-winner rate.
- **Decay and rollback**: how you notice a winner stop winning, and how fast.
- **Call time and caller ID**: what you did with history, per-cohort vs. per-person, and how you explore.
- **Guardrail KPI**: how a change that helps the primary KPI but hurts the guardrail gets caught.

## 6. Governance
- Proposal lifecycle as a state machine, including expired, lapsed, stale and re-proposal paths.
- Batching and timing under approver hours, latency, TTL, apply windows and fatigue limits.
- When you use auto-apply and when you voluntarily don't, and why.
- How impact score, affected leads and dry run are computed, and how honest they are.
- What happens when a supervisor edits config mid-experiment.

## 7. Incidents
Detection thresholds and their expected false-positive rate, how you pick a fix, credit spend, and how you
verify a fix worked after applying it. Give a timeline from public_02.

## 8. Scale
Measured on public_03_load: ingest events/sec, end-to-end decision lag, peak memory, wall-clock time per
simulated day. What breaks first at 10×, and how you'd partition the work across workflows and tenants.

## 9. Reliability
Crash-recovery test: method, results, and what state you lose (if any). Idempotency keys. The behaviour when
the simulator returns 4xx/5xx.

## 10. Audit & observability
Show a real query or command that answers *"why did workflow X change at time T, who approved it, and what
happened after?"* using your store, with its output. List the alerts you'd page on and the ones you wouldn't.

## 11. Results
Paste the summary tables from `reports/public_01.md` and `reports/public_02.md`. Interpret them: where you
helped, where you were **harmful** and why, and what you'd change.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|

## 13. Known weaknesses
Honest and specific.

## 14. If this went to production at Formi next month
What you'd do in the first two weeks, in order.
