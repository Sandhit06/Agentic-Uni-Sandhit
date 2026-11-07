"""Scores a run against a baseline (no-op controller) run of the same scenario.

Reads only the run directories: `_truth/aggregates.json`, `_truth/changes.jsonl`
and `sim_log.jsonl`. Produces numbers, not a verdict — the rubric decides how
they translate into a score, and a human reads the design doc.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

MIN_PER_DAY = 1440


def _load(run: Path):
    agg = json.loads((run / "_truth" / "aggregates.json").read_text())
    log = [json.loads(l) for l in (run / "sim_log.jsonl").read_text().splitlines() if l.strip()]
    ch_path = run / "_truth" / "changes.jsonl"
    changes = [json.loads(l) for l in ch_path.read_text().splitlines() if l.strip()] if ch_path.exists() else []
    return agg, log, changes


def _kpis(agg: dict) -> dict:
    by = defaultdict(lambda: Counter())
    for d in agg["days"]:
        c = by[d["workflow_id"]]
        for k in ("dials", "connected", "engaged", "halluc", "sum_p_eng", "sum_p_eng_default", "sum_p_eng_oracle"):
            c[k] += d[k]
    out = {}
    tot = Counter()
    for wid, c in sorted(by.items()):
        c["leads_uploaded"] = agg["leads_uploaded"][wid]
        c["leads_concluded"] = agg["leads_concluded"][wid]
        tot.update(c)
        out[wid] = _ratios(c)
    out["ALL"] = _ratios(tot)
    return out


def _ratios(c: Counter) -> dict:
    gap = c["sum_p_eng_oracle"] - c["sum_p_eng_default"]
    return {
        "dials": c["dials"], "connected": c["connected"], "engaged": c["engaged"],
        "leads_uploaded": c["leads_uploaded"], "leads_concluded": c["leads_concluded"],
        "conclusion_rate": c["leads_concluded"] / max(c["leads_uploaded"], 1),
        "pickup_rate": c["connected"] / max(c["dials"], 1),
        "engagement_rate": c["engaged"] / max(c["connected"], 1),
        "hallucinated_calls": c["halluc"],
        "engagement_gap_captured": (c["sum_p_eng"] - c["sum_p_eng_default"]) / gap if gap > 1e-9 else None,
    }


def grade(run_dir: str, baseline_dir: str) -> dict:
    run, base = Path(run_dir), Path(baseline_dir)
    agg, log, changes = _load(run)
    bagg, _, _ = _load(base)
    if agg["scenario"] != bagg["scenario"]:
        raise SystemExit("run and baseline are different scenarios")
    k, bk = _kpis(agg), _kpis(bagg)
    delta = {}
    for wid in k:
        delta[wid] = {m: (k[wid][m] / bk[wid][m] - 1) if bk[wid].get(m) else None
                      for m in ("conclusion_rate", "pickup_rate", "engagement_rate")}

    # --- changes made by the controller, judged against hidden truth at apply time
    ctrl = [c for c in changes if c["actor"].startswith("controller")]
    judged = []
    for c in ctrl:
        de = c["exp_engage_new"] / c["exp_engage_old"] - 1 if c["exp_engage_old"] else 0
        dd = c["exp_did_health_new"] / c["exp_did_health_old"] - 1 if c["exp_did_health_old"] else 0
        verdict = "harmful" if (de < -0.01 or dd < -0.02) else "beneficial" if (de > 0.01 or dd > 0.02) else "neutral"
        judged.append({**c, "engage_delta": round(de, 4), "did_health_delta": round(dd, 4), "verdict": verdict})

    # --- governance behaviour
    kinds = Counter(r["kind"] for r in log)
    decided = Counter(r["status"] for r in log if r["kind"] == "approval_decided")
    decided["expired"] = kinds["approval_expired"]
    reasons = Counter(rsn for r in log if r["kind"] == "approval_decided" and r["status"] == "rejected"
                      for rsn in r["reasons"])
    req_times = defaultdict(list)
    for r in log:
        if r["kind"] == "approval_requested":
            req_times[r["workflow_id"]].append(r["sim_minute"])
    peak = {}
    for wid, ts in req_times.items():
        ts.sort()
        peak[wid] = max(sum(1 for u in ts if t <= u < t + MIN_PER_DAY) for t in ts)

    # --- incidents
    halluc = agg["prompt_halluc"]
    inc_out = []
    for inc in agg["incidents"]:
        rec = {"type": inc["type"], "workflow": inc["workflow"], "at": inc["at"]}
        if inc["type"] == "supervisor_edit" and "prompt_version" in inc.get("patch", {}) \
                and halluc[inc["patch"]["prompt_version"]] > 0.01:
            tl = agg["prompt_timeline"][inc["workflow"]]
            fixed = next((t for t, p, a in tl if t is not None and t > inc["fired_minute"] and halluc[p] <= 0.01), None)
            rec["mttr_minutes"] = None if fixed is None else fixed - inc["fired_minute"]
            rec["fixed_by"] = None if fixed is None else next(a for t, p, a in tl if t == fixed)
        if inc["type"] == "did_flagged":
            after = [d for d in agg["days"] if d["workflow_id"] == inc["workflow"]
                     and d["day"] * MIN_PER_DAY >= inc["fired_minute"] - MIN_PER_DAY]
            rec["flagged_did_share_by_day"] = {d["day"]: round(d["did_dials"].get(inc["did"], 0) / max(d["dials"], 1), 3)
                                               for d in sorted(after, key=lambda x: x["day"])}
        inc_out.append(rec)

    # --- decaying variants
    decay = defaultdict(dict)
    for d in agg["days"]:
        for j, v in d.get("decay_exposure", {}).items():
            e = agg["effects"][int(j)]
            key = f"{d['workflow_id']}:{e['dim']}={e['value']}"
            decay[key][d["day"]] = round(v["with_variant"] / max(v["in_pop"], 1), 3)

    return {
        "scenario": agg["scenario"],
        "finished": agg["sim_minute"],
        "kpis": k, "baseline_kpis": bk, "delta_vs_baseline": delta,
        "controller_changes": {"count": len(judged), "verdicts": Counter(j["verdict"] for j in judged),
                               "detail": judged},
        "governance": {"approval_requests": kinds["approval_requested"], "decisions": decided,
                       "rejection_reasons": reasons, "applied_via_approval": kinds["approval_applied"],
                       "auto_applied": kinds["auto_applied"], "auto_apply_rejected": kinds["auto_apply_rejected"],
                       "apply_conflicts": kinds["apply_conflict"], "lapsed": kinds["approval_lapsed"],
                       "peak_requests_per_24h": peak},
        "incidents": inc_out,
        "decaying_variant_share_after_onset": decay,
        "credits_used": agg["credits_total"] - agg["credits_remaining"],
        "validation_runs": kinds["validation_run"], "llm_calls": kinds["llm_suggest_fixes"],
    }


def to_markdown(g: dict) -> str:
    pct = lambda x: "—" if x is None else f"{x * 100:+.1f}%"
    L = [f"# Grade report — {g['scenario']}", "", "## Outcomes vs baseline (no-op controller)", "",
         "| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |",
         "|---|---|---|---|---|---|---|---|---|"]
    for wid, m in g["kpis"].items():
        b, d = g["baseline_kpis"][wid], g["delta_vs_baseline"][wid]
        cap = m["engagement_gap_captured"]
        L.append(f"| {wid} | {m['conclusion_rate']:.3f} | {pct(d['conclusion_rate'])} | {m['pickup_rate']:.3f} | "
                 f"{pct(d['pickup_rate'])} | {m['engagement_rate']:.3f} | {pct(d['engagement_rate'])} | "
                 f"{'—' if cap is None else f'{cap * 100:.0f}%'} | {m['hallucinated_calls']} ({b['hallucinated_calls']}) |")
    c = g["controller_changes"]
    L += ["", "## Changes applied by the controller", "",
          f"{c['count']} applied — " + ", ".join(f"{k}: {v}" for k, v in c["verdicts"].items()), ""]
    for j in c["detail"]:
        L.append(f"- {j['workflow_id']} v{j['version']} ({j['actor']}) dims={j['dims']} "
                 f"engageΔ={j['engage_delta']:+.3f} didΔ={j['did_health_delta']:+.3f} → **{j['verdict']}**")
    gv = g["governance"]
    L += ["", "## Governance", "", "```", json.dumps(gv, indent=2, default=dict), "```",
          "", "## Incidents", "", "```", json.dumps(g["incidents"], indent=2), "```",
          "", "## Decaying variants — share of in-population calls still on the variant, by day after onset", "",
          "```", json.dumps(g["decaying_variant_share_after_onset"], indent=2), "```",
          "", f"Credits used: {g['credits_used']:.0f} · validation runs: {g['validation_runs']} · "
              f"LLM fix calls: {g['llm_calls']}"]
    return "\n".join(L)
