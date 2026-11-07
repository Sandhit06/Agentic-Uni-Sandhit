"""Config normalisation and diff helpers (public simulator contract).

Implemented locally rather than importing the simulator: the controller is a
separate service and must run against the grader's copy of the world.
"""
from __future__ import annotations

import copy
import math

from .features import FEATURES

AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def default_config(wf: dict) -> dict:
    cfg = copy.deepcopy(wf["default_config"])
    cfg.setdefault("cohorts", [])
    cfg.setdefault("schedule", {"hour_weights": {}, "lead_preferred_hour": {}})
    return cfg


def normalise_config(wf: dict, cfg: dict) -> dict:
    p: list[str] = []
    if not isinstance(cfg, dict):
        raise ConfigError(["config must be an object"])
    extra = set(cfg) - {"cohorts", "allocations", "prompt_version", "schedule"}
    if extra:
        p.append(f"unknown keys: {sorted(extra)}")
    out = {"cohorts": [], "allocations": {}, "prompt_version": None,
           "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    cohort_ids: list[str] = []
    for i, c in enumerate(cfg.get("cohorts") or []):
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or cid == "default" or cid in cohort_ids:
            p.append(f"cohorts[{i}].id must be a unique non-empty string other than 'default'")
            continue
        where = c.get("where") or {}
        for feat, vals in where.items():
            if feat not in FEATURES:
                p.append(f"cohort {cid}: unknown feature {feat}")
            elif not isinstance(vals, list) or not vals or any(v not in FEATURES[feat] for v in vals):
                p.append(f"cohort {cid}: bad values for {feat}: {vals}")
        cohort_ids.append(cid)
        out["cohorts"].append({"id": cid, "where": {k: list(v) for k, v in where.items()}})
    if len(cohort_ids) > 20:
        p.append("at most 20 cohorts")
    allocs = cfg.get("allocations") or {}
    if "default" not in allocs:
        p.append("allocations.default is required")
    for key, dims in allocs.items():
        if key != "default" and key not in cohort_ids:
            p.append(f"allocations.{key}: not a defined cohort")
            continue
        out["allocations"][key] = {}
        for dim, weights in (dims or {}).items():
            if dim not in AGENT_DIMS:
                p.append(f"allocations.{key}.{dim}: unknown dimension")
                continue
            opts = wf["options"][dim]
            if not isinstance(weights, dict) or not weights:
                p.append(f"allocations.{key}.{dim}: must be a non-empty {{option: weight}} map")
                continue
            bad = [o for o in weights if o not in opts]
            if bad:
                p.append(f"allocations.{key}.{dim}: options not allowed for this workflow: {bad}")
                continue
            if any((not isinstance(w, (int, float))) or w < 0 or math.isnan(w)
                   for w in weights.values()):
                p.append(f"allocations.{key}.{dim}: weights must be >= 0")
                continue
            s = float(sum(weights.values()))
            if s <= 0:
                p.append(f"allocations.{key}.{dim}: weights sum to 0")
                continue
            out["allocations"][key][dim] = {o: float(w) / s for o, w in weights.items() if w > 0}
        if key == "default":
            missing = [d for d in AGENT_DIMS if d not in out["allocations"]["default"]]
            if missing:
                p.append(f"allocations.default missing dims {missing}")
    pv = cfg.get("prompt_version")
    if pv not in wf["options"]["prompt_version"]:
        p.append(f"prompt_version {pv!r} not allowed")
    out["prompt_version"] = pv
    sched = cfg.get("schedule") or {}
    lo, hi = wf["dialling_window"]
    for key, hw in (sched.get("hour_weights") or {}).items():
        if key != "default" and key not in cohort_ids:
            p.append(f"schedule.hour_weights.{key}: not a defined cohort")
            continue
        clean = {}
        for h, w in hw.items():
            try:
                hh = int(h)
            except (TypeError, ValueError):
                p.append(f"schedule.hour_weights.{key}: bad hour {h!r}")
                continue
            if not lo <= hh < hi:
                p.append(f"schedule.hour_weights.{key}: hour {hh} is outside the dialling window [{lo},{hi})")
                continue
            if not isinstance(w, (int, float)) or w < 0:
                p.append(f"schedule.hour_weights.{key}.{h}: weight must be >= 0")
                continue
            clean[str(hh)] = float(w)
        out["schedule"]["hour_weights"][key] = clean
    lph = sched.get("lead_preferred_hour") or {}
    if len(lph) > 200_000:
        p.append("schedule.lead_preferred_hour: at most 200k entries")
    for pid, h in lph.items():
        if not isinstance(h, int) or not lo <= h < hi:
            p.append(f"schedule.lead_preferred_hour[{pid}]: hour {h!r} outside dialling window")
            break
    out["schedule"]["lead_preferred_hour"] = {str(k): int(v) for k, v in lph.items()
                                              if isinstance(v, int)}
    if p:
        raise ConfigError(p)
    return out


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = []
    if old["cohorts"] != new["cohorts"]:
        ch.append("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for dim in AGENT_DIMS:
        if any(dist(old, k, dim) != dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old["prompt_version"] != new["prompt_version"]:
        ch.append("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = dist(old, k, dim), dist(new, k, dim)
            tv = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))
            worst = max(worst, tv)
    return worst


def blend_allocation(current: dict, target: dict, t: float) -> dict:
    """Blend two distributions (already normalised)."""
    opts = set(current) | set(target)
    out = {o: (1 - t) * current.get(o, 0.0) + t * target.get(o, 0.0) for o in opts}
    s = sum(out.values())
    return {o: w / s for o, w in out.items() if w > 1e-12}


def limit_shift(old_cfg: dict, new_cfg: dict, max_tv: float) -> dict:
    """Return a config as close to new_cfg as possible while keeping the
    largest per-dimension total-variation shift <= max_tv.

    Schedule, prompt and cohort structure changes are kept; only agent-dimension
    distributions are blended.  This is used to stay inside auto-apply policy.
    """
    if max_shift(old_cfg, new_cfg) <= max_tv + 1e-9:
        return copy.deepcopy(new_cfg)
    out = copy.deepcopy(new_cfg)
    # Blend every cohort distribution that moved too far.  A single global
    # blend factor is enough because the policy uses the worst dimension.
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        cand = copy.deepcopy(new_cfg)
        keys = {"default"} | {c["id"] for c in new_cfg["cohorts"]}
        for k in keys:
            if k not in cand["allocations"]:
                continue
            for dim in AGENT_DIMS:
                if dim not in cand["allocations"][k]:
                    continue
                # current may not define this cohort; inherit default
                cur = (old_cfg["allocations"].get(k) or {}).get(dim) or old_cfg["allocations"]["default"][dim]
                cand["allocations"][k][dim] = blend_allocation(cur, cand["allocations"][k][dim], mid)
        if max_shift(old_cfg, cand) <= max_tv + 1e-9:
            lo = mid
        else:
            hi = mid
    cand = copy.deepcopy(new_cfg)
    keys = {"default"} | {c["id"] for c in new_cfg["cohorts"]}
    for k in keys:
        if k not in cand["allocations"]:
            continue
        for dim in AGENT_DIMS:
            if dim not in cand["allocations"][k]:
                continue
            cur = (old_cfg["allocations"].get(k) or {}).get(dim) or old_cfg["allocations"]["default"][dim]
            cand["allocations"][k][dim] = blend_allocation(cur, cand["allocations"][k][dim], lo)
    return cand
