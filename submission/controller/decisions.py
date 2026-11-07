"""Detection, experiment design and target-config planning.

All winner decisions here are deterministic functions of observed counts and
the public config.  An LLM is only ever used for incident prompt suggestions,
never for choosing a winner.
"""
from __future__ import annotations

import copy
import math

from .configs import ConfigError, changed_dims, dist, normalise_config
from .features import N_COMBO, combo_features, matches, specificity
from .stats import bh, shrink_rate, wilson, z_two_proportion

# Tunables.  Kept explicit so they can be defended and changed in one place.
MIN_ARM_N = 180          # connected calls per arm before an arm is eligible
MIN_REL_LIFT = 0.06      # 6% relative lift required to promote a variant
WINNER_SHARE = 0.85      # traffic to a proven winner
CONTROL_SHARE = 0.15     # permanent holdback for decay detection
EXPLORE_SHARE = 0.12     # exploration share before any winner is known
VARIANT_WINDOW_DAYS = 5
ROLLBACK_WINDOW_DAYS = 2
CALLER_WINDOW_DAYS = 2
MIN_CALLER_DIALS = 100


def _seg_where(an, wid: str, seg: int) -> dict:
    return an.segments[wid][seg]["where"]


def _seg_id(an, wid: str, seg: int) -> str:
    return an.segments[wid][seg]["id"]


def _current_alloc(cfg: dict, where: dict, dim: str) -> dict:
    """Allocation for a segment under a config: first cohort whose where
    exactly equals the segment's where, else default."""
    for c in cfg.get("cohorts") or []:
        if c.get("where") == where:
            return (cfg["allocations"].get(c["id"]) or {}).get(dim) or cfg["allocations"]["default"][dim]
    return cfg["allocations"]["default"][dim]


def _top_option(alloc: dict) -> str:
    return max(alloc.items(), key=lambda kv: kv[1])[0]


def _rates(an, wid, dim, options, seg, current_day, window):
    out = {}
    for o in options:
        c, e = an.v_counts(wid, dim, o, seg, current_day, window)
        out[o] = (c, e)
    return out


def find_variant_findings(an, wf, wid, cfg, current_day,
                          window=VARIANT_WINDOW_DAYS, min_n=MIN_ARM_N,
                          min_rel=MIN_REL_LIFT) -> list[dict]:
    """Test every candidate option against the current incumbent in every
    candidate segment.  BH-FDR controls false winners across the workflow."""
    tests = []
    for dim in ("welcome_message", "voice"):
        options = list(wf["options"][dim])
        if len(options) < 2:
            continue
        for seg in range(len(an.segments[wid])):
            where = _seg_where(an, wid, seg)
            alloc = _current_alloc(cfg, where, dim)
            incumbent = _top_option(alloc)
            rates = _rates(an, wid, dim, options, seg, current_day, window)
            inc_c, inc_e = rates[incumbent]
            if inc_c < min_n:
                continue
            p_inc = inc_e / inc_c
            for o in options:
                if o == incumbent:
                    continue
                c, e = rates[o]
                if c < min_n:
                    continue
                p_o = e / c
                if p_o <= p_inc:
                    continue
                rel = (p_o - p_inc) / max(p_inc, 1e-9)
                if rel < min_rel:
                    continue
                z, p = z_two_proportion(e, c, inc_e, inc_c)
                tests.append({
                    "wid": wid, "dim": dim, "seg": seg, "where": where,
                    "incumbent": incumbent, "option": o, "rel": rel, "p": p,
                    "n_opt": c, "n_inc": inc_c, "rate_opt": p_o, "rate_inc": p_inc,
                    "kind": "promote",
                })
    if not tests:
        return []
    flags = bh([t["p"] for t in tests], alpha=0.10)
    # Consistency check across two halves of the window: a winner must not be
    # carried entirely by one noisy slice.
    out = []
    half = max(1, window // 2)
    for t, ok in zip(tests, flags):
        if not ok:
            continue
        seg = t["seg"]
        # recent half vs older half
        c1, e1 = an.v_counts(wid, t["dim"], t["option"], seg, current_day, half)
        c0, e0 = an.v_counts(wid, t["dim"], t["option"], seg,
                             current_day - half, window - half)
        ci1, ei1 = an.v_counts(wid, t["dim"], t["incumbent"], seg, current_day, half)
        ci0, ei0 = an.v_counts(wid, t["dim"], t["incumbent"], seg,
                               current_day - half, window - half)
        if min(c1, ci1, c0, ci0) >= 60:
            r1 = (e1 / c1) - (ei1 / ci1)
            r0 = (e0 / c0) - (ei0 / ci0)
            if r1 <= 0 or r0 <= 0:
                continue
        conf = min(1.0, (t["n_opt"] + t["n_inc"]) / 1200.0)
        t["confidence"] = round(0.5 + 0.5 * conf, 3)
        out.append(t)
    # Keep the strongest challenger per (dim, segment).
    best: dict[tuple, dict] = {}
    for t in out:
        key = (t["dim"], t["seg"])
        if key not in best or t["rel"] > best[key]["rel"]:
            best[key] = t
    return list(best.values())


def find_rollbacks(an, wf, wid, cfg, current_day,
                   window=ROLLBACK_WINDOW_DAYS, min_n=120) -> list[dict]:
    """Notice a promoted winner that stopped winning.

    We compare the current top option in each segment with every other arm over
    a short window.  A significant *drop* becomes a rollback finding to the
    best alternative.
    """
    out = []
    for dim in ("welcome_message", "voice"):
        options = list(wf["options"][dim])
        if len(options) < 2:
            continue
        for seg in range(len(an.segments[wid])):
            where = _seg_where(an, wid, seg)
            alloc = _current_alloc(cfg, where, dim)
            top = _top_option(alloc)
            if alloc.get(top, 0) < 0.5:
                continue
            exp = an.exposure_minute(wid, dim, top)
            if exp is None or (current_day * 1440 + 720) - exp < 1440:
                continue
            rates = _rates(an, wid, dim, options, seg, current_day, window)
            tc, te = rates[top]
            if tc < min_n:
                continue
            p_top = te / tc
            best_alt, best_rate, best_p = None, p_top, 1.0
            for o in options:
                if o == top:
                    continue
                c, e = rates[o]
                if c < min_n:
                    continue
                p_o = e / c
                if p_o <= p_top:
                    continue
                rel = (p_o - p_top) / max(p_top, 1e-9)
                if rel < 0.05:
                    continue
                z, p_upper = z_two_proportion(e, c, te, tc)
                # two-sided so a drop in either direction is considered
                p = min(1.0, 2 * p_upper)
                if p < 0.10 and p_o > best_rate:
                    best_alt, best_rate, best_p = o, p_o, p
            if best_alt is not None:
                out.append({
                    "wid": wid, "dim": dim, "seg": seg, "where": where,
                    "incumbent": top, "option": best_alt,
                    "rel": (best_rate - p_top) / max(p_top, 1e-9),
                    "p": best_p, "n_opt": rates[best_alt][0], "n_inc": tc,
                    "rate_opt": best_rate, "rate_inc": p_top,
                    "kind": "rollback", "confidence": 0.9,
                })
    return out


def find_caller_health(an, wf, wid, cfg, current_day,
                       window=CALLER_WINDOW_DAYS, min_dials=MIN_CALLER_DIALS,
                       degraded_override: list[str] | None = None) -> dict:
    """Estimate pickup by caller ID and propose a safer target distribution.

    Load matters: a DID's health decays as it receives more traffic, so
    concentrating all calls on one "best" DID is self-defeating.  The prior is
    kept unless a DID is *clearly* degraded (less than 60% of the workflow rate
    with a Wilson upper bound still below 90%).  When that happens the flagged
    DID is down-weighted and the freed traffic is spread with a per-DID cap; an
    unobserved DID is used only as spare capacity, never as a speculative
    promotion.
    """
    options = list(wf["options"]["caller_id"])
    prior = dict(cfg["allocations"]["default"]["caller_id"])
    prior = {o: float(prior.get(o, 0.0)) for o in options}
    rates = {}
    total_d = total_c = 0
    for o in options:
        d, c = an.p_counts(wid, o, 0, current_day, window)
        rates[o] = (d, c)
        total_d += d
        total_c += c
    global_rate = total_c / total_d if total_d else 0.5
    degraded_override = set(degraded_override or [])
    degraded = []
    for o in options:
        d, c = rates[o]
        if o in degraded_override and d >= 20:
            degraded.append(o)
            continue
        if d < min_dials:
            continue
        r = shrink_rate(c, d, global_rate, k=60)
        lo, hi = wilson(c, d)
        if r < 0.60 * global_rate and hi < 0.90 * global_rate:
            degraded.append(o)
    def _norm(d):
        s = sum(d.values())
        return {o: w / s for o, w in d.items() if w > 1e-9}
    if not degraded:
        # Healthy DIDs are left exactly where they are.  Short-window rate
        # differences between healthy DIDs are noise, and chasing them causes
        # the load-induced health collapse we are trying to avoid.
        return {"weights": _norm(prior), "degraded": [], "rates": rates,
                "global_rate": global_rate, "reason": "ok"}

    healthy = [o for o in options if o not in degraded]
    base = {o: (prior[o] if prior.get(o, 0) > 0 else 0.0) for o in healthy}
    if sum(base.values()) <= 1e-9:
        observed = [o for o in healthy if rates[o][0] >= min_dials]
        base = {o: (1.0 if o in observed else 0.0) for o in healthy}
    base = _norm(base)
    weights = {o: 0.0 for o in options}
    weights.update(base)
    for o in degraded:
        weights[o] = 0.02
    has_spare = any(prior.get(o, 0.0) <= 0 for o in healthy)
    caps = {}
    for o in options:
        if o in degraded:
            caps[o] = 0.08
        elif prior.get(o, 0.0) > 0:
            caps[o] = 0.55 if has_spare else 1.0
        else:
            caps[o] = 0.40
    # Move excess traffic to options with capacity, giving unobserved DIDs the
    # spare role only after all already-used healthy DIDs have room.
    for _ in range(8):
        excess = sum(max(0.0, weights[o] - caps[o]) for o in options)
        if excess <= 1e-9:
            break
        for o in options:
            weights[o] = min(weights[o], caps[o])
        receivers = [o for o in options if weights[o] < caps[o] - 1e-9]
        if not receivers:
            receivers = [o for o in healthy]
        room = sum(caps[o] - weights[o] for o in receivers) if receivers else 0.0
        if room <= 1e-9 or not receivers:
            weights[healthy[0] if healthy else options[0]] += excess
            break
        for o in receivers:
            weights[o] += excess * (caps[o] - weights[o]) / room
    weights = _norm(weights)
    if not weights:
        known = [(o, v) for o, v in rates.items() if v[0] >= min_dials and o not in degraded]
        if not known:
            return {"weights": None, "degraded": degraded, "rates": rates,
                    "global_rate": global_rate, "reason": "no_healthy_caller"}
        best = max(known, key=lambda kv: kv[1][1] / max(kv[1][0], 1))[0]
        weights = {o: (1.0 if o == best else 0.0) for o in options}
    return {"weights": weights, "degraded": degraded, "rates": rates,
            "global_rate": global_rate, "reason": "degraded"}


def hour_weights_for_segment(an, wf, wid, where: dict, current_day,
                             window=None) -> dict[str, float]:
    lo, hi = wf["dialling_window"]
    kind, value = "all", "all"
    if where.get("age_band") and len(where["age_band"]) == 1:
        kind, value = "age", where["age_band"][0]
    elif where.get("city_tier") and len(where["city_tier"]) == 1:
        kind, value = "city", where["city_tier"][0]
    td = tc = 0
    counts = {}
    for h in range(lo, hi):
        d, c = an.h_counts(wid, kind, value, h, current_day, window)
        counts[h] = (d, c)
        td += d
        tc += c
    if kind != "all" and td < 200:
        return hour_weights_for_segment(an, wf, wid, {}, current_day, window)
    global_rate = tc / td if td else 0.5
    weights = {}
    for h, (d, c) in counts.items():
        r = shrink_rate(c, d, global_rate, k=30)
        ratio = (r / global_rate) if global_rate > 1e-9 else 1.0
        w = min(4.0, max(0.25, ratio ** 1.5))
        weights[str(h)] = round(w, 3)
    return weights


def find_schedule(an, wf, wid, cfg, current_day) -> dict | None:
    """Only return a schedule if it predicts a material pickup improvement."""
    lo, hi = wf["dialling_window"]
    new_w = hour_weights_for_segment(an, wf, wid, {}, current_day)
    old_w = (cfg.get("schedule", {}).get("hour_weights", {}) or {}).get("default") or \
            {str(h): 1.0 for h in range(lo, hi)}

    def expected(weights):
        num = den = 0.0
        for h in range(lo, hi):
            w = weights.get(str(h), 0.0)
            d, c = an.h_counts(wid, "all", "all", h, current_day, None)
            r = shrink_rate(c, d, 0.5, k=30)
            num += w * r
            den += w
        return num / den if den else 0.0

    if expected(new_w) < expected(old_w) * 1.005:
        return None
    return new_w


def _best_per_segment(findings: list[dict]) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    for f in findings:
        seg = f["seg"]
        out.setdefault(seg, {})[f["dim"]] = f
    return out


def plan_config(an, wf, wid, cfg, current_day, findings, caller_plan,
                schedule_plan, prompt_override=None) -> dict:
    target = copy.deepcopy(cfg)
    dims = ("welcome_message", "voice")
    # ---- default allocation -------------------------------------------------
    for dim in dims:
        options = list(wf["options"][dim])
        alloc = dict(target["allocations"]["default"].get(dim) or {})
        incumbent = _top_option(alloc) if alloc else options[0]
        global_f = next((f for f in findings if f["seg"] == 0 and f["dim"] == dim), None)
        if global_f:
            target["allocations"]["default"][dim] = {
                global_f["option"]: WINNER_SHARE,
                global_f["incumbent"]: CONTROL_SHARE,
            }
        elif len(options) > 1:
            non_inc = [o for o in options if o != incumbent]
            explore = sum(w for o, w in alloc.items() if o != incumbent)
            if explore < 0.08:
                share = EXPLORE_SHARE / max(len(non_inc), 1)
                target["allocations"]["default"][dim] = {incumbent: 1 - EXPLORE_SHARE, **{o: share for o in non_inc}}
            else:
                target["allocations"]["default"][dim] = alloc
        else:
            target["allocations"]["default"][dim] = {options[0]: 1.0}

    if caller_plan and caller_plan.get("weights"):
        target["allocations"]["default"]["caller_id"] = caller_plan["weights"]

    # ---- cohort rules -------------------------------------------------------
    by_seg = _best_per_segment([f for f in findings if f["seg"] != 0])
    rules = []
    for seg, by_dim in by_seg.items():
        if seg == 0:
            continue
        where = _seg_where(an, wid, seg)
        lift = max(f["rel"] for f in by_dim.values())
        rules.append({"seg": seg, "where": where, "findings": by_dim, "lift": lift,
                      "spec": specificity(where)})
    rules.sort(key=lambda r: (-r["spec"], -r["lift"], _seg_id(an, wid, r["seg"])))
    rules = rules[:20]
    target["cohorts"] = []
    for i, r in enumerate(rules):
        cid = f"auto_{i + 1:02d}"
        target["cohorts"].append({"id": cid, "where": r["where"]})
        alloc = {}
        for dim, f in r["findings"].items():
            alloc[dim] = {f["option"]: WINNER_SHARE, f["incumbent"]: CONTROL_SHARE}
        target["allocations"].setdefault(cid, {}).update(alloc)
    # drop allocations for cohorts that no longer exist
    valid = {"default"} | {c["id"] for c in target["cohorts"]}
    target["allocations"] = {k: v for k, v in target["allocations"].items() if k in valid}

    # ---- schedule -----------------------------------------------------------
    sched = {"hour_weights": {}, "lead_preferred_hour": {}}
    if schedule_plan:
        sched["hour_weights"]["default"] = schedule_plan.get("default") or \
            hour_weights_for_segment(an, wf, wid, {}, current_day)
        for c in target["cohorts"]:
            sched["hour_weights"][c["id"]] = hour_weights_for_segment(
                an, wf, wid, c["where"], current_day)
    else:
        sched = copy.deepcopy(target.get("schedule") or sched)
    target["schedule"] = sched
    if prompt_override:
        target["prompt_version"] = prompt_override
    return normalise_config(wf, target)


def apply_rollbacks(an, wf, wid, cfg, rollbacks) -> dict:
    """Move a decaying incumbent back to its control arm without rebuilding the
    rest of the approved config.

    A local signal often shows up before the pooled full-population test has
    enough samples.  When the decaying arm is also the *default* incumbent, a
    segment-level finding still reduces the default exposure, otherwise the
    decaying arm would keep 85% of all traffic while we wait for the pooled
    p-value.  The segment rule remains in place to protect that segment.
    """
    target = copy.deepcopy(cfg)
    if not rollbacks:
        return target
    global_dims = {f["dim"] for f in rollbacks if not f["where"]}
    for dim in ("welcome_message", "voice"):
        default_alloc = target["allocations"]["default"].get(dim) or {}
        if not default_alloc:
            continue
        top = _top_option(default_alloc)
        if dim in global_dims:
            continue
        same = [f for f in rollbacks if f["dim"] == dim and f["incumbent"] == top]
        if not same or default_alloc.get(top, 0.0) < 0.5:
            continue
        best = max(same, key=lambda f: (f.get("rel", 0.0), -f.get("p", 1.0)))
        target["allocations"]["default"][dim] = {
            best["option"]: WINNER_SHARE, top: CONTROL_SHARE,
        }
        global_dims.add(dim)
    where_to_cohort = {_where_id(c["where"]): c["id"] for c in target.get("cohorts") or []}
    for f in rollbacks:
        dim = f["dim"]
        wid_key = where_to_cohort.get(_where_id(f["where"]))
        alloc = {f["option"]: WINNER_SHARE, f["incumbent"]: CONTROL_SHARE}
        if wid_key:
            target["allocations"].setdefault(wid_key, {})[dim] = alloc
        elif not f["where"]:
            target["allocations"]["default"][dim] = alloc
        elif len(target.get("cohorts") or []) < 20:
            cid = f"rb_{len(target['cohorts']) + 1:02d}"
            target["cohorts"].append({"id": cid, "where": copy.deepcopy(f["where"])})
            target["allocations"].setdefault(cid, {})[dim] = alloc
            where_to_cohort[_where_id(f["where"])] = cid
    return normalise_config(wf, target)


def _segment_alloc(cfg: dict, where: dict, dim: str) -> dict:
    # For an exact segment rule use its cohort; otherwise default.
    return _current_alloc(cfg, where, dim)


def plan_populations(an, wid: str, cfg: dict, current_day: int,
                     cadence_len: int) -> list[dict]:
    """Partition the active population by the *first* matching config cohort.

    Candidate segments overlap (all / age / lang / age x lang).  Weighting by
    each segment's raw population would double count.  This partition assigns
    every active lead to exactly one cell, the same way the simulator's
    cohort_of works, and then maps each cell to the most specific candidate
    segment we have statistics for (or "all" for an unmatched/default cell).
    """
    seg_index = an.label_to_idx.get(wid, {})
    cells = []
    for c in cfg.get("cohorts") or []:
        cells.append({"cohort_id": c["id"], "where": c.get("where") or {},
                      "seg": seg_index.get(_where_id(c.get("where") or {}), 0)})
    cells.append({"cohort_id": "default", "where": {}, "seg": 0})

    # For each public feature combination: index of the first matching cell.
    combo_cell = [len(cells) - 1] * N_COMBO
    for combo in range(N_COMBO):
        feats = combo_features(combo)
        for ci, cell in enumerate(cells[:-1]):
            if matches(cell["where"], feats):
                combo_cell[combo] = ci
                break

    counts = [0] * len(cells)
    camps = an.campaigns.get(wid) or {}
    min_day = current_day - max(cadence_len, 1) + 1
    active_camps = {i for i, c in camps.items() if c["day"] >= min_day}
    arr_c = an.lead_combo.get(wid)
    arr_p = an.lead_camp.get(wid)
    if arr_c and active_camps:
        for i in range(len(arr_c)):
            if arr_p[i] in active_camps:
                counts[combo_cell[arr_c[i]]] += 1
    for ci, cell in enumerate(cells):
        cell["population"] = counts[ci]
    return cells


def _where_id(where: dict) -> str:
    """Recreate the candidate-segment id for an exact where clause."""
    if not where:
        return "all"
    if set(where) == {"age_band"} and len(where["age_band"]) == 1:
        return f"age={where['age_band'][0]}"
    if set(where) == {"lang"} and len(where["lang"]) == 1:
        return f"lang={where['lang'][0]}"
    if set(where) == {"city_tier"} and len(where["city_tier"]) == 1:
        return f"tier={where['city_tier'][0]}"
    if set(where) == {"value_band"} and len(where["value_band"]) == 1:
        return f"value={where['value_band'][0]}"
    if set(where) == {"age_band", "lang"} and len(where["age_band"]) == 1 and len(where["lang"]) == 1:
        return f"age={where['age_band'][0]}|lang={where['lang'][0]}"
    return ""


def predict_effect(an, wf, wid, current_cfg, target_cfg, current_day,
                   cadence_len: int, window=VARIANT_WINDOW_DAYS) -> dict:
    """Model-based dry run against the current active population.

    It is a replay of learned segment rates, not a counterfactual claim: the
    output says so explicitly.  Populations come from a disjoint first-match
    partition, never from the overlapping candidate segments.
    """
    cells = plan_populations(an, wid, target_cfg, current_day, cadence_len)
    cells = [c for c in cells if c["population"] > 0] or [cells[-1]]
    pop_total = sum(c["population"] for c in cells) or 1

    # base rates from all calls in the window
    def base_rates(seg):
        wc = we = 0
        for o in wf["options"]["welcome_message"]:
            c, e = an.v_counts(wid, "welcome_message", o, seg, current_day, window)
            wc += c
            we += e
        pd = pc = 0
        for o in wf["options"]["caller_id"]:
            d, c = an.p_counts(wid, o, seg, current_day, window)
            pd += d
            pc += c
        eng = we / wc if wc else 0.5
        pick = pc / pd if pd else 0.3
        return eng, pick, wc, pd

    def option_rate(dim, option, seg, prior):
        c, e = an.v_counts(wid, dim, option, seg, current_day, window)
        return shrink_rate(e, c, prior, k=40)

    rows = []
    old_eng = new_eng = old_pick = new_pick = 0.0
    for cell in cells:
        seg = cell["seg"]
        where = cell["where"]
        base_eng, base_pick, n_conn, n_dials = base_rates(seg)
        cur_w = _segment_alloc(current_cfg, where, "welcome_message")
        cur_v = _segment_alloc(current_cfg, where, "voice")
        new_w = _segment_alloc(target_cfg, where, "welcome_message")
        new_v = _segment_alloc(target_cfg, where, "voice")
        cur_c = _segment_alloc(current_cfg, where, "caller_id")
        new_c = _segment_alloc(target_cfg, where, "caller_id")

        def rel(dim, a, b, prior):
            ra = sum(w * option_rate(dim, o, seg, prior) for o, w in a.items())
            rb = sum(w * option_rate(dim, o, seg, prior) for o, w in b.items())
            return rb / ra if ra > 0 else 1.0

        rw = rel("welcome_message", cur_w, new_w, base_eng)
        rv = rel("voice", cur_v, new_v, base_eng)
        rc = rel("caller_id", cur_c, new_c, base_pick)
        pop_s = cell["population"] / pop_total
        old_eng += pop_s * base_eng
        new_eng += pop_s * base_eng * rw * rv
        old_pick += pop_s * base_pick
        new_pick += pop_s * base_pick * rc
        rows.append({"cohort_id": cell["cohort_id"], "segment": _seg_id(an, wid, seg),
                     "population": cell["population"],
                     "connected_sample": n_conn, "dials_sample": n_dials,
                     "old_engagement": round(base_eng, 4), "new_engagement": round(base_eng * rw * rv, 4),
                     "old_pickup": round(base_pick, 4), "new_pickup": round(base_pick * rc, 4)})
    primary = wf["objective"]["primary"]
    guard = wf["objective"].get("guardrail") or {}
    if primary == "pickup_rate":
        pred_lift = new_pick / old_pick - 1 if old_pick else 0.0
        guard_delta = new_eng - old_eng
        guard_name = "engagement_rate"
    else:
        pred_lift = new_eng / old_eng - 1 if old_eng else 0.0
        guard_delta = new_pick - old_pick
        guard_name = "pickup_rate"
    allowed_drop = 0.02
    for k, v in guard.items():
        if k.endswith("_max_drop") and isinstance(v, (int, float)):
            allowed_drop = float(v)
    guard_ok = guard_delta >= -allowed_drop
    confidence = 0.5 + 0.5 * min(1.0, sum(r["connected_sample"] + r["dials_sample"] for r in rows) / 2000.0)
    return {
        "method": "learned_segment_replay",
        "note": "model-based replay of learned segment rates; not a randomised counterfactual",
        "population": pop_total,
        "primary": primary,
        "old_primary": round(old_pick if primary == "pickup_rate" else old_eng, 4),
        "new_primary": round(new_pick if primary == "pickup_rate" else new_eng, 4),
        "predicted_lift": round(pred_lift, 4),
        "guardrail": guard_name,
        "old_guardrail": round(old_eng if guard_name == "engagement_rate" else old_pick, 4),
        "new_guardrail": round(new_eng if guard_name == "engagement_rate" else new_pick, 4),
        "guardrail_delta": round(guard_delta, 4),
        "guardrail_ok": bool(guard_ok),
        "confidence": round(confidence, 3),
        "segments": rows,
    }


def impact_score(pred: dict) -> float:
    lift = max(0.0, pred.get("predicted_lift") or 0.0)
    return round(min(1.0, lift / 0.15) * pred.get("confidence", 0.5), 4)


def summarise_change(old_cfg: dict, new_cfg: dict) -> dict:
    dims = changed_dims(old_cfg, new_cfg)
    diff = {"changed_dims": dims}
    if "allocations" in dims:
        diff["allocations"] = {
            "default": new_cfg["allocations"]["default"],
        }
        for c in new_cfg.get("cohorts") or []:
            if c["id"] in new_cfg["allocations"]:
                diff["allocations"][c["id"]] = new_cfg["allocations"][c["id"]]
    if "cohorts" in dims:
        diff["cohorts"] = new_cfg.get("cohorts") or []
    if "schedule" in dims:
        diff["schedule"] = new_cfg.get("schedule") or {}
    if "prompt_version" in dims:
        diff["prompt"] = {"from": old_cfg.get("prompt_version"),
                          "to": new_cfg.get("prompt_version")}
    return diff
