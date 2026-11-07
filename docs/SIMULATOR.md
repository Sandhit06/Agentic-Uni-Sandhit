# Simulator reference

The simulator is the outside world: the dialler, the leads, the people who pick up (or don't), the client's
approvers and an LLM you can ask for prompt fixes. Your controller talks to it **only over HTTP**. Treat
everything in `sim/` and everything under `# ---- hidden ground truth` in a scenario file as unknown to you —
we grade on hidden scenarios with the same schema and different truth.

```
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine     # → http://127.0.0.1:8700/docs
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base   # no-op controller, for comparison
python -m sim grade --run runs/mine --baseline runs/base --out reports/public_01.md
```

A run directory holds exactly one run. The simulator keeps its state in memory: if **it** restarts, that's a new
world. If **your controller** restarts, the world is still there, waiting at the same simulated minute.

---

## 1. Time

- Simulated time starts at `D0 00:00` = `2027-01-04T00:00:00+05:30` and moves only when you call
  `POST /sim/advance`. Nothing happens between calls, so you can take as long as you like to think.
- One tick is 15 simulated minutes. `minutes` must be a positive multiple of 15. Advancing by 1440 runs one
  day. Smaller steps let you react sooner, at the cost of more round-trips.
- Before `D0`, the world has already run for `history_days` under the business's default config. That
  history is available as `history` chunks.
- The scenario ends at `D{days} 00:00`. Further advances return `409 finished`.

## 2. The world

| Concept | What it means |
|---|---|
| **Tenant** | A client (an insurer or lender). Has one or more workflows. |
| **Workflow** | One use case (e.g. renewal reminder) with its own dialling window, capacity, cadence, options, objective and governance policy. Read it from `GET /workflows/{id}`. |
| **Campaign** | One lead batch, uploaded every day at `daily_batch.at_hour`. Some leads are **returning** people seen in earlier campaigns. Their `person_id` is stable. |
| **Lead** | One person enrolled in one campaign (`lead_id`). Features: `lang`, `city_tier`, `age_band`, `value_band`. |
| **Cadence** | Business-owned. `cadence: [2,3,3]` means up to 2 attempts on the campaign's first day, 3 on the second and 3 on the third, then the lead is unreachable. There are at least `min_gap_minutes` between attempts. **You cannot change this.** |
| **Capacity** | `dials_per_hour` within `dialling_window` (business-owned, `[start, end)` hours IST). Each tick the dialler picks up to capacity/4 leads from those eligible. |
| **Outcome** | A dial is `connected` (picked up) or not. A connected call is `engaged` if the person stays to a conclusion, interested or not. An engaged lead is done. |

**KPIs.** These are tracked separately and never blended:
- pickup rate = connected ÷ dials
- engagement rate = engaged ÷ connected
- business outcome = leads concluded ÷ leads uploaded

Each workflow declares a `primary` KPI and a `guardrail` on the other in `objective`.

## 3. What you control: the workflow config

```jsonc
{
  "cohorts": [
    {"id": "senior_ta", "where": {"age_band": ["60+"], "lang": ["ta"]}},
    {"id": "young",     "where": {"age_band": ["18-30"]}}
  ],
  "allocations": {
    "default":   {"welcome_message": {"W1": 1}, "voice": {"V1": 1}, "caller_id": {"D1": 0.5, "D2": 0.5}},
    "senior_ta": {"voice": {"V1": 0.5, "V3": 0.5}}
  },
  "prompt_version": "P1",
  "schedule": {
    "hour_weights": {"default": {"9": 1, "10": 1, "...": 1}, "young": {"18": 3, "19": 3, "20": 3}},
    "lead_preferred_hour": {"nw_renewal:p0001234": 11}
  }
}
```

- **cohorts** — up to 20 and evaluated in order. A lead belongs to the first cohort it matches, otherwise to
  `default`. Each `where` clause is an AND across features and an OR within a feature's list.
- **allocations** — for each dial, the simulator samples `welcome_message`, `voice` and `caller_id`
  independently from the lead's cohort distribution. If a cohort omits a dimension, `default` is used.
  Weights are normalised. Only options listed in the workflow's `options` are allowed.
- **prompt_version** — one per workflow, applied to every call. There is no per-cohort prompt.
- **schedule.hour_weights** — decides *who* the dialler picks in a given hour, not how many calls it makes. A
  lead's weight comes from its cohort's table, or from `default` if its cohort has no table, or is `1` if
  there's no table at all. **If a table is given, any hour it leaves out gets weight 0.** If every eligible lead
  has weight 0, that capacity goes unused. Hours outside the dialling window are rejected.
- **schedule.lead_preferred_hour** — per person (by `person_id`, so it carries across campaigns). In its
  preferred hour a lead's weight is multiplied by 6, and in every other hour by 0.25. At most 200k entries.

**Mid-flight changes.** If `mid_flight: allowed`, a new config affects running campaigns from the next tick.
If `mid_flight: next_campaign_only`, the agent-facing parts (`cohorts`, `allocations`) are locked per campaign
to the version that was active when the campaign was created. `prompt_version` and `schedule` always apply
immediately.

Every call event records the `config_version`, `cohort_id` and `variant` it was dialled with.

## 4. Governance: how a config changes

A workflow's config can change in only three ways:

1. **Supervisor edits.** A client's supervisor can change config at any time, and you'll see it as a
   `config_changed` event with `actor: "supervisor"`. These edits bypass governance.
2. **Approval path.** `POST /approvals` → a simulated human approver decides → `POST /workflows/{id}/apply`.
3. **Auto-apply.** `PUT /workflows/{id}/config`, allowed only where the workflow's policy permits it.

### 4.1 Approval path

`POST /approvals`

```jsonc
{
  "proposal_id": "your-unique-id",          // idempotency key: reusing one → 409 duplicate_proposal_id
  "workflow_id": "nw_renewal",
  "base_version": 4,                        // must equal the current version, else 409 stale_base_version
  "proposed_config": { ...full config... },
  "change_summary": "one line a supervisor understands",
  "diff": { ... },                          // free-form, non-empty
  "affected_leads": 5230,                   // int ≥ 0 — your estimate
  "dry_run": { ... },                       // free-form, non-empty — e.g. replay against a historical cohort
  "impact_score": 0.42,                     // [0,1]
  "rationale": "…",
  "evidence": {"validation_run_id": "val_00003"},   // required for prompt changes
  "expires_in_hours": 12                    // optional, capped at the policy TTL
}
```

The approver (per-workflow `governance` block):
- works only within `approver_hours` and takes a random `approver_latency_minutes` to respond;
- rejects requests with empty `change_summary`/`diff`/`dry_run`/`rationale`, a bad `affected_leads` or a bad
  `impact_score`;
- rejects **prompt changes** unless `evidence.validation_run_id` refers to a validation run for the same
  workflow and prompt with `pass_rate ≥ min_validation_pass_rate`;
- rejects anything beyond `max_requests_per_24h` for a workflow ("batch your changes");
- otherwise approves, except for a `base_reject_rate` of judgement-call rejections;
- doesn't reveal the reasons until the decision is made.

Lifecycle: `pending → approved | rejected | expired`, then `approved → applied | lapsed`.
- A request still undecided at `expires_at` (after `proposal_ttl_hours`) becomes `expired`.
- An approved change must be applied within `apply_window_hours` (`apply_by`), or it `lapses` (410).
- Applying fails with `409 stale_base_version` if the config changed after you proposed, for example because
  of a supervisor edit. Re-propose.
- `POST /workflows/{id}/apply {"approval_id": "apr_00012"}` applies exactly the `proposed_config` that was
  approved.

`GET /approvals/{id}` and `GET /approvals?workflow_id=&status=` let you poll and reconcile after a crash.
Decisions also arrive as `approval_decided` events.

### 4.2 Auto-apply

`PUT /workflows/{id}/config` with `{proposal_id, base_version, proposed_config, rationale, evidence?}`.
It's permitted only if `governance.auto_apply.enabled`, every changed dimension is in `allowed_dims`, and no
cohort's `welcome_message`/`voice`/`caller_id` distribution moves by more than `max_shift` in total-variation
distance. Changed dimensions are drawn from `cohorts, welcome_message, voice, caller_id, prompt_version,
schedule`. Anything else returns `403 auto_apply_not_permitted` with reasons, and the attempt is logged.

## 5. LLM and validation (costs credits)

Each scenario has a credit budget (`GET /sim/state` → `credits_remaining`). Running out returns `402`.

| Endpoint | Cost | Returns |
|---|---|---|
| `POST /llm/suggest-fixes {workflow_id, problem}` | 3 | Up to 3 alternative `prompt_version`s with one-line summaries. Some fixes are worse than the problem. |
| `POST /validation/runs {workflow_id, prompt_version, n}` | ⌈n/100⌉, n ∈ [50, 2000] | `pass_rate` on n synthetic conversations: a noisy measure of **safety** (hallucination). It says nothing about engagement. |

## 6. Events

`POST /sim/advance` returns `new_chunks`. Fetch each one with `GET /events/chunks/{n}` (NDJSON). You can list
them with `GET /events/chunks?kind=events|history&after=n`. Chunk numbers are global and increasing, and
`event_id` is strictly increasing. Volume ranges from ~30k events/day (dev) to ~850k/day (load).

```jsonc
{"type":"campaign_created","ts":"…","tenant_id":"…","workflow_id":"…","campaign_id":"nw_renewal:c0015","leads":1100,"config_version_at_creation":3,"event_id":1}
{"type":"lead","ts":"…","workflow_id":"…","campaign_id":"…","lead_id":"nw_renewal:l00012345","person_id":"nw_renewal:p0004567",
 "features":{"lang":"hi","city_tier":"2","age_band":"46-60","value_band":"mid"},"event_id":2}
{"type":"call","ts":"…","tenant_id":"…","workflow_id":"…","campaign_id":"…","lead_id":"…","person_id":"…","attempt_no":2,
 "campaign_day_index":1,"config_version":3,"cohort_id":"senior_ta",
 "variant":{"welcome_message":"W2","voice":"V3","caller_id":"D1","prompt_version":"P1"},
 "connected":true,"engaged":false,"disposition":"short_hangup","duration_s":14,"turns":2,
 "qa":{"hallucination_flag":false,"flow_adherence":0.94},"event_id":3}
{"type":"config_changed","ts":"…","workflow_id":"…","version":4,"actor":"supervisor|controller_approved|controller_auto",
 "changed_dims":["prompt_version"],"proposal_id":null,"approval_id":null,"event_id":4}
{"type":"approval_decided","ts":"…","approval_id":"apr_00003","workflow_id":"…","status":"approved|rejected|expired","reasons":[],"event_id":5}
```

`disposition` ∈ `no_answer, voicemail, short_hangup, concluded_interested, concluded_not_interested`.
`qa` is only present on connected calls. `hallucination_flag` comes from an imperfect automated detector, so it
has both false positives and false negatives.

## 7. Other endpoints

`GET /sim/state` · `GET /workflows` · `GET /workflows/{id}` · `GET /workflows/{id}/config` ·
`GET /workflows/{id}/config/history` · `GET /health` — the full schema is at `/docs` when the server is running.

## 8. Grading output

`python -m sim grade` compares your run with a baseline run of the same scenario. It reports the KPIs and their
change against baseline for each workflow, how much of the hidden engagement headroom you captured, every
config change you applied and whether it was **harmful** / **neutral** / **beneficial** according to hidden
truth at the moment you applied it, your governance behaviour, how long incidents took to fix, how quickly you
moved off variants that stopped working, and credits used. You can see all of this for public scenarios. Use it
to learn, not to overfit.
