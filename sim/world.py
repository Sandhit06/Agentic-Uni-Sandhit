"""The simulated world: leads, dialler, calls, hidden effects, governance.

The world advances in fixed ticks (default 15 sim-minutes). Each tick, for each
workflow, it may upload a lead batch, then dials up to its hourly capacity from
the eligible leads, choosing *who* to dial according to the active schedule and
*which variant* according to the active allocations. Outcomes are drawn from a
hidden model the candidate never sees directly.

Nothing in here is visible to a candidate's controller except through the HTTP
API (`server.py`) and the event files written to the run directory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .scenario import (AGENT_DIMS, ARCHETYPES, CHANGE_DIMS, FEATURES, HOUR_PROFILE,
                       MINUTES_PER_DAY, all_workflows)

START_ISO_DATE = (2027, 1, 4)  # Monday. D0 == 2027-01-04 (IST)


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


def fmt_ts(minute: int) -> str:
    import datetime as dt
    base = dt.datetime(*START_ISO_DATE, 0, 0)
    t = base + dt.timedelta(minutes=minute)
    return t.strftime("%Y-%m-%dT%H:%M:00+05:30")


def _stable_rng(*parts) -> np.random.Generator:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


# --------------------------------------------------------------------------- tables
class Table:
    """Column store that grows by appending numpy arrays."""

    def __init__(self, dtypes: dict[str, str]):
        self.n = 0
        self._cap = 1024
        self.c = {k: np.zeros(self._cap, dtype=d) for k, d in dtypes.items()}

    def append(self, cols: dict[str, np.ndarray]) -> np.ndarray:
        k = len(next(iter(cols.values())))
        while self.n + k > self._cap:
            self._cap *= 2
            for name, arr in self.c.items():
                new = np.zeros(self._cap, dtype=arr.dtype)
                new[: self.n] = arr[: self.n]
                self.c[name] = new
        for name, arr in cols.items():
            self.c[name][self.n: self.n + k] = arr
        idx = np.arange(self.n, self.n + k)
        self.n += k
        return idx

    def __getitem__(self, name):
        return self.c[name][: self.n]


# --------------------------------------------------------------------------- config
class ConfigError(ApiError):
    def __init__(self, problems: list[str]):
        super().__init__(422, "invalid_config", problems)


def default_config(wf: dict) -> dict:
    cfg = copy.deepcopy(wf["default_config"])
    cfg.setdefault("cohorts", [])
    cfg.setdefault("schedule", {"hour_weights": {}, "lead_preferred_hour": {}})
    return cfg


def normalise_config(wf: dict, cfg: dict) -> dict:
    """Validate a proposed config against the workflow's hard rules. Returns a clean copy."""
    p: list[str] = []
    if not isinstance(cfg, dict):
        raise ConfigError(["config must be an object"])
    extra = set(cfg) - {"cohorts", "allocations", "prompt_version", "schedule"}
    if extra:
        p.append(f"unknown keys: {sorted(extra)}")
    out = {"cohorts": [], "allocations": {}, "prompt_version": None,
           "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    cohort_ids = []
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
            if any((not isinstance(w, (int, float))) or w < 0 or math.isnan(w) for w in weights.values()):
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
    out["schedule"]["lead_preferred_hour"] = {str(k): int(v) for k, v in lph.items() if isinstance(v, int)}
    if p:
        raise ConfigError(p)
    return out


def _dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = []
    if old["cohorts"] != new["cohorts"]:
        ch.append("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for dim in AGENT_DIMS:
        if any(_dist(old, k, dim) != _dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old["prompt_version"] != new["prompt_version"]:
        ch.append("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    """Largest total-variation distance of any agent-dim allocation across cohorts."""
    keys = {"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = _dist(old, k, dim), _dist(new, k, dim)
            tv = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))
            worst = max(worst, tv)
    return worst


# --------------------------------------------------------------------------- workflow state
class WorkflowState:
    def __init__(self, spec: dict, sc: dict, rng: np.random.Generator):
        self.spec = spec
        self.id = spec["id"]
        self.opts = {d: list(spec["options"][d]) for d in AGENT_DIMS + ["prompt_version"]}
        self.persons = Table({"lang": "i1", "city_tier": "i1", "age_band": "i1", "value_band": "i1",
                              "arch": "i1", "p0": "f8", "e0": "f8", "interest": "f8", "active": "?"})
        self.enroll = Table({"person": "i8", "campaign": "i4", "status": "i1", "attempts_today": "i2",
                             "attempts": "i2", "last_attempt": "i8"})  # status 0 active 1 concluded 2 unreachable
        self.campaigns: list[dict] = []
        cfg0 = normalise_config(spec, default_config(spec))
        self.versions = [{"version": 1, "config": cfg0, "actor": "business_default", "applied_at": None,
                          "proposal_id": None, "approval_id": None, "changed_dims": []}]
        self.baseline_prompt = cfg0["prompt_version"]
        self.did_load = np.zeros(len(self.opts["caller_id"]))
        self.did_base = np.array([float(spec["did_base_health"].get(d, 1.0)) for d in self.opts["caller_id"]])
        self.did_l0 = sc["did_l0_per_dph"] * spec["dials_per_hour"]
        self.exposure: dict[tuple[str, str], int] = {}
        self.effects = [e for e in sc["effects"] if e["workflow"] in (self.id, "*")]
        self._pref_cache: tuple[int, int, np.ndarray] | None = None
        self.person_ids_lookup: dict[str, int] = {}

    # ---- config helpers
    @property
    def current(self) -> dict:
        return self.versions[-1]

    def config_at_version(self, v: int) -> dict:
        return self.versions[v - 1]["config"]

    def did_health(self) -> np.ndarray:
        return self.did_base / (1.0 + (self.did_load / self.did_l0) ** 2)

    # ---- hidden-model helpers
    def where_mask(self, where: dict, pidx: np.ndarray) -> np.ndarray:
        m = np.ones(len(pidx), dtype=bool)
        for feat, vals in where.items():
            codes = [FEATURES[feat].index(v) for v in vals]
            m &= np.isin(self.persons[feat][pidx], codes)
        return m

    def cohort_of(self, cfg: dict, pidx: np.ndarray) -> np.ndarray:
        """Returns cohort index per person: 0 = default, i+1 = cfg['cohorts'][i]."""
        out = np.zeros(len(pidx), dtype=np.int16)
        unassigned = np.ones(len(pidx), dtype=bool)
        for i, c in enumerate(cfg["cohorts"]):
            m = unassigned & self.where_mask(c["where"], pidx)
            out[m] = i + 1
            unassigned &= ~m
        return out

    def effect_mult_now(self, e: dict, t: int) -> float:
        d = e.get("decay")
        if not d:
            return float(e["mult"])
        start = self.exposure.get((e["dim"], e["value"]))
        if start is None:
            return float(e["mult"])
        days = (t - start) / MINUTES_PER_DAY
        a, over = d["after_exposure_days"], max(d.get("over_days", 1), 1e-9)
        if days <= a:
            return float(e["mult"])
        f = min((days - a) / over, 1.0)
        return float(e["mult"] + (d["to_mult"] - e["mult"]) * f)

    def dim_mults(self, dim: str, pidx: np.ndarray, t: int) -> np.ndarray:
        M = np.ones((len(self.opts[dim]), len(pidx)))
        for e in self.effects:
            if e["dim"] != dim or e["value"] not in self.opts[dim]:
                continue
            m = self.where_mask(e.get("where") or {}, pidx)
            M[self.opts[dim].index(e["value"]), m] *= self.effect_mult_now(e, t)
        return M

    def expected_engagement(self, cfg: dict, pidx: np.ndarray, t: int, prompts: dict) -> np.ndarray:
        """E[engaged | connected] for each person under a config (randomisation averaged out)."""
        coh = self.cohort_of(cfg, pidx)
        names = ["default"] + [c["id"] for c in cfg["cohorts"]]
        out = self.persons["e0"][pidx].copy()
        for dim in ("welcome_message", "voice"):
            M = self.dim_mults(dim, pidx, t)
            W = np.zeros_like(M)
            for ci, name in enumerate(names):
                sel = coh == ci
                if not sel.any():
                    continue
                for o, w in _dist(cfg, name, dim).items():
                    W[self.opts[dim].index(o), sel] = w
            out *= (M * W).sum(axis=0)
        pr = prompts[cfg["prompt_version"]]
        out *= pr["engage_mult"] * (1 - 0.5 * pr["halluc"])
        return np.minimum(out, 0.95)


# --------------------------------------------------------------------------- world
class World:
    def __init__(self, sc: dict, run_dir: str | Path):
        self.sc = sc
        self.run_dir = Path(run_dir)
        for sub in ("events", "history", "_truth"):
            (self.run_dir / sub).mkdir(parents=True, exist_ok=True)
        self.rng = np.random.default_rng(sc["seed"])
        self.tick = sc["tick_minutes"]
        self.prompts = sc["prompts"]
        self.wfs = {wf["id"]: WorkflowState(wf, sc, self.rng) for wf in all_workflows(sc)}
        self.now = -sc["history_days"] * MINUTES_PER_DAY
        self.end = sc["days"] * MINUTES_PER_DAY
        self.credits = float(sc["credits"])
        self.approvals: dict[str, dict] = {}
        self.validation_runs: dict[str, dict] = {}
        self.proposal_ids: set[str] = set()
        self.event_seq = 0
        self.chunks: list[dict] = []
        self._buf: list[str] = []
        self._writing_history = True
        self.truth: dict[str, dict] = {}
        self.incident_log: list[dict] = []
        self._log = open(self.run_dir / "sim_log.jsonl", "a")
        self._truth_changes = open(self.run_dir / "_truth" / "changes.jsonl", "a")
        self._pending_incidents = sorted(sc["incidents"], key=lambda x: x["at_minute"])
        self._run(0)  # history, written to history/
        self._writing_history = False
        self.log("sim_started", scenario=sc["name"], days=sc["days"])

    # ------------------------------------------------------------------ logging
    def log(self, kind: str, **kw):
        rec = {"kind": kind, "sim_minute": self.now, "ts": fmt_ts(self.now), **kw}
        self._log.write(json.dumps(rec, default=_json_default) + "\n")
        self._log.flush()

    def emit(self, ev: dict):
        self.event_seq += 1
        ev["event_id"] = self.event_seq
        self._buf.append(json.dumps(ev, separators=(",", ":")))
        if len(self._buf) >= 250_000:
            self._flush_chunk()

    def _flush_chunk(self):
        if not self._buf:
            return
        sub = "history" if self._writing_history else "events"
        n = len(self.chunks)
        name = f"{sub}/{n:06d}.jsonl"
        with open(self.run_dir / name, "w") as f:
            f.write("\n".join(self._buf) + "\n")
        meta = {"chunk": n, "path": name, "kind": sub, "events": len(self._buf),
                "first_event_id": self.event_seq - len(self._buf) + 1, "last_event_id": self.event_seq,
                "until_minute": self.now, "until_ts": fmt_ts(self.now)}
        self.chunks.append(meta)
        self._buf = []
        return meta

    # ------------------------------------------------------------------ public ops
    def advance(self, minutes: int) -> dict:
        if minutes <= 0 or minutes % self.tick:
            raise ApiError(400, "bad_minutes", f"minutes must be a positive multiple of {self.tick}")
        if self.now >= self.end:
            raise ApiError(409, "finished", "scenario has ended")
        before = len(self.chunks)
        target = min(self.now + minutes, self.end)
        self._run(target)
        self._write_truth()
        if self.now >= self.end:
            self.log("sim_finished")
        return {"sim_minute": self.now, "ts": fmt_ts(self.now), "finished": self.now >= self.end,
                "new_chunks": self.chunks[before:]}

    def state(self) -> dict:
        return {"scenario": self.sc["name"], "sim_minute": self.now, "ts": fmt_ts(self.now),
                "end_minute": self.end, "finished": self.now >= self.end, "tick_minutes": self.tick,
                "credits_remaining": round(self.credits, 2), "chunks": len(self.chunks),
                "last_event_id": self.event_seq}

    def workflow_public(self, wid: str) -> dict:
        w = self._wf(wid)
        s = w.spec
        return {"workflow_id": w.id, "tenant_id": s["tenant_id"], "use_case": s.get("use_case"),
                "languages": list(s["language_mix"]), "dialling_window": s["dialling_window"],
                "dials_per_hour": s["dials_per_hour"], "cadence": s["cadence"],
                "min_gap_minutes": s["min_gap_minutes"], "daily_batch": s["daily_batch"],
                "mid_flight": s["mid_flight"], "objective": s["objective"], "options": w.opts,
                "prompt_catalog": {p: self.prompts[p].get("summary", "") for p in w.opts["prompt_version"]},
                "governance": s["governance"], "current_version": w.current["version"]}

    def get_config(self, wid: str) -> dict:
        w = self._wf(wid)
        v = w.current
        return {"workflow_id": wid, "version": v["version"], "actor": v["actor"],
                "applied_at": v["applied_at"] and fmt_ts(v["applied_at"]), "config": v["config"]}

    def config_history(self, wid: str) -> list[dict]:
        w = self._wf(wid)
        return [{"version": v["version"], "actor": v["actor"], "changed_dims": v["changed_dims"],
                 "applied_at": v["applied_at"] and fmt_ts(v["applied_at"]),
                 "proposal_id": v["proposal_id"], "approval_id": v["approval_id"]} for v in w.versions]

    def auto_apply(self, wid: str, body: dict) -> dict:
        w = self._wf(wid)
        pol = w.spec["governance"]["auto_apply"]
        pid = body.get("proposal_id")
        self._check_proposal_id(pid)
        new = normalise_config(w.spec, body.get("proposed_config"))
        if body.get("base_version") != w.current["version"]:
            raise ApiError(409, "stale_base_version", {"current_version": w.current["version"]})
        dims = changed_dims(w.current["config"], new)
        problems = []
        if not pol.get("enabled"):
            problems.append("auto-apply is not enabled for this workflow")
        bad = [d for d in dims if d not in pol.get("allowed_dims", [])]
        if bad:
            problems.append(f"dims not permitted for auto-apply: {bad}")
        sh = max_shift(w.current["config"], new)
        if sh > pol.get("max_shift", 0) + 1e-9:
            problems.append(f"allocation shift {sh:.3f} exceeds max_shift {pol.get('max_shift', 0)}")
        if not dims:
            problems.append("no change")
        if not body.get("rationale"):
            problems.append("rationale is required")
        if problems:
            self.log("auto_apply_rejected", workflow_id=wid, proposal_id=pid, problems=problems, dims=dims)
            raise ApiError(403, "auto_apply_not_permitted", problems)
        self.proposal_ids.add(pid)
        v = self._apply(w, new, "controller_auto", pid, None)
        self.log("auto_applied", workflow_id=wid, proposal_id=pid, version=v, dims=dims,
                 rationale=body.get("rationale"), evidence=body.get("evidence"))
        return {"workflow_id": wid, "version": v, "changed_dims": dims}

    REQUIRED = ("proposal_id", "workflow_id", "base_version", "proposed_config", "change_summary",
                "diff", "affected_leads", "dry_run", "impact_score", "rationale")

    def request_approval(self, body: dict) -> dict:
        missing = [k for k in self.REQUIRED if k not in body]
        if missing:
            raise ApiError(422, "missing_fields", missing)
        wid = body["workflow_id"]
        w = self._wf(wid)
        pid = body["proposal_id"]
        self._check_proposal_id(pid)
        new = normalise_config(w.spec, body["proposed_config"])
        if body["base_version"] != w.current["version"]:
            raise ApiError(409, "stale_base_version", {"current_version": w.current["version"]})
        dims = changed_dims(w.current["config"], new)
        if not dims:
            raise ApiError(422, "no_change", "proposed_config equals current config")
        self.proposal_ids.add(pid)
        gov = w.spec["governance"]
        rng = _stable_rng(self.sc["seed"], "approval", pid)
        reasons = []
        if not str(body.get("change_summary") or "").strip() or not body.get("diff") or not body.get("dry_run") \
                or not str(body.get("rationale") or "").strip():
            reasons.append("change_summary, diff, dry_run and rationale must all be non-empty")
        if not isinstance(body.get("affected_leads"), int) or body["affected_leads"] < 0:
            reasons.append("affected_leads must be a non-negative integer")
        isc = body.get("impact_score")
        if not isinstance(isc, (int, float)) or not 0 <= isc <= 1:
            reasons.append("impact_score must be in [0,1]")
        if "prompt_version" in dims:
            run = self.validation_runs.get(((body.get("evidence") or {}).get("validation_run_id")) or "")
            if not run or run["workflow_id"] != wid or run["prompt_version"] != new["prompt_version"]:
                reasons.append("prompt changes need evidence.validation_run_id for this workflow and prompt")
            elif run["pass_rate"] < gov["min_validation_pass_rate"]:
                reasons.append(f"validation pass rate {run['pass_rate']:.3f} below "
                               f"{gov['min_validation_pass_rate']}")
        recent = [a for a in self.approvals.values()
                  if a["workflow_id"] == wid and a["created_minute"] > self.now - MINUTES_PER_DAY]
        if len(recent) + 1 > gov["max_requests_per_24h"]:
            reasons.append(f"approver fatigue: more than {gov['max_requests_per_24h']} requests in 24h for "
                           f"this workflow — batch your changes")
        if not reasons and rng.random() < gov["base_reject_rate"]:
            reasons.append("approver declined (judgement call)")
        ttl = min(float(body.get("expires_in_hours") or gov["proposal_ttl_hours"]), gov["proposal_ttl_hours"])
        lo, hi = gov["approver_latency_minutes"]
        decide_at = self._next_working(self.now + int(rng.integers(lo, hi + 1)), gov["approver_hours"])
        expires_at = self.now + int(ttl * 60)
        aid = f"apr_{len(self.approvals) + 1:05d}"
        a = {"approval_id": aid, "proposal_id": pid, "workflow_id": wid, "status": "pending",
             "created_minute": self.now, "decide_minute": decide_at, "expires_minute": expires_at,
             "base_version": body["base_version"], "proposed_config": new, "changed_dims": dims,
             "_decision": "rejected" if reasons else "approved", "_reasons": reasons,
             "request": {k: body.get(k) for k in body if k != "proposed_config"}}
        self.approvals[aid] = a
        self.log("approval_requested", approval_id=aid, workflow_id=wid, proposal_id=pid, dims=dims,
                 request=a["request"])
        return self._approval_public(a)

    def get_approval(self, aid: str) -> dict:
        if aid not in self.approvals:
            raise ApiError(404, "not_found", aid)
        return self._approval_public(self.approvals[aid])

    def list_approvals(self, workflow_id: str | None, status: str | None) -> list[dict]:
        return [self._approval_public(a) for a in self.approvals.values()
                if (not workflow_id or a["workflow_id"] == workflow_id) and (not status or a["status"] == status)]

    def apply_approval(self, wid: str, aid: str) -> dict:
        w = self._wf(wid)
        a = self.approvals.get(aid)
        if not a or a["workflow_id"] != wid:
            raise ApiError(404, "not_found", aid)
        if a["status"] != "approved":
            raise ApiError(409, "not_approved", {"status": a["status"]})
        window = w.spec["governance"]["apply_window_hours"] * 60
        if self.now > a["decided_minute"] + window:
            a["status"] = "lapsed"
            self.log("approval_lapsed", approval_id=aid, workflow_id=wid)
            raise ApiError(410, "approval_lapsed", "approved change was not applied within the apply window")
        if a["base_version"] != w.current["version"]:
            self.log("apply_conflict", approval_id=aid, workflow_id=wid, current=w.current["version"])
            raise ApiError(409, "stale_base_version", {"current_version": w.current["version"]})
        v = self._apply(w, a["proposed_config"], "controller_approved", a["proposal_id"], aid)
        a["status"] = "applied"
        a["applied_version"] = v
        self.log("approval_applied", approval_id=aid, workflow_id=wid, version=v, dims=a["changed_dims"])
        return {"workflow_id": wid, "version": v, "changed_dims": a["changed_dims"]}

    def suggest_fixes(self, body: dict) -> dict:
        wid = body.get("workflow_id")
        w = self._wf(wid)
        if not str(body.get("problem") or "").strip():
            raise ApiError(422, "missing_fields", ["problem"])
        self._spend(3, "llm_suggest_fixes")
        cur = w.current["config"]["prompt_version"]
        cands = [p for p in w.opts["prompt_version"] if p != cur]
        rng = _stable_rng(self.sc["seed"], "llm", wid, self.now)
        rng.shuffle(cands)
        out = [{"prompt_version": p, "summary": self.prompts[p].get("summary", "")} for p in cands[:3]]
        self.log("llm_suggest_fixes", workflow_id=wid, problem=body.get("problem"), suggestions=out)
        return {"suggestions": out, "credits_remaining": round(self.credits, 2)}

    def run_validation(self, body: dict) -> dict:
        wid, pv, n = body.get("workflow_id"), body.get("prompt_version"), body.get("n", 200)
        w = self._wf(wid)
        if pv not in w.opts["prompt_version"]:
            raise ApiError(422, "bad_prompt_version", pv)
        if not isinstance(n, int) or not 50 <= n <= 2000:
            raise ApiError(422, "bad_n", "n must be an int in [50, 2000]")
        self._spend(math.ceil(n / 100), "validation_run")
        rid = f"val_{len(self.validation_runs) + 1:05d}"
        rng = _stable_rng(self.sc["seed"], "val", rid)
        h = self.prompts[pv]["halluc"]
        fails = int(rng.binomial(n, min(h * 0.9 + 0.005, 1.0)))
        run = {"validation_run_id": rid, "workflow_id": wid, "prompt_version": pv, "n": n,
               "failures": fails, "pass_rate": round(1 - fails / n, 4), "ts": fmt_ts(self.now)}
        self.validation_runs[rid] = run
        self.log("validation_run", **run)
        return {**run, "credits_remaining": round(self.credits, 2)}

    # ------------------------------------------------------------------ internals
    def _wf(self, wid) -> WorkflowState:
        if wid not in self.wfs:
            raise ApiError(404, "unknown_workflow", wid)
        return self.wfs[wid]

    def _check_proposal_id(self, pid):
        if not isinstance(pid, str) or not pid:
            raise ApiError(422, "missing_fields", ["proposal_id"])
        if pid in self.proposal_ids:
            raise ApiError(409, "duplicate_proposal_id", pid)

    def _spend(self, c: float, what: str):
        if self.credits < c:
            raise ApiError(402, "out_of_credits", {"needed": c, "remaining": self.credits})
        self.credits -= c

    @staticmethod
    def _next_working(t: int, hours: list[int]) -> int:
        lo, hi = hours
        day, mod = divmod(t, MINUTES_PER_DAY)
        if mod < lo * 60:
            return day * MINUTES_PER_DAY + lo * 60
        if mod >= hi * 60:
            return (day + 1) * MINUTES_PER_DAY + lo * 60
        return t

    def _approval_public(self, a: dict) -> dict:
        out = {k: a[k] for k in ("approval_id", "proposal_id", "workflow_id", "status", "base_version",
                                 "changed_dims")}
        out["created_at"] = fmt_ts(a["created_minute"])
        out["expires_at"] = fmt_ts(a["expires_minute"])
        if a["status"] in ("approved", "rejected", "applied", "lapsed"):
            out["decided_at"] = fmt_ts(a["decided_minute"])
            out["reasons"] = a["_reasons"]
        if a["status"] in ("approved",):
            w = self.wfs[a["workflow_id"]]
            out["apply_by"] = fmt_ts(a["decided_minute"] + w.spec["governance"]["apply_window_hours"] * 60)
        return out

    def _process_approvals(self):
        for a in self.approvals.values():
            if a["status"] != "pending":
                continue
            if a["decide_minute"] > a["expires_minute"] and self.now >= a["expires_minute"]:
                a["status"] = "expired"
                a["decided_minute"] = a["expires_minute"]
                self.log("approval_expired", approval_id=a["approval_id"], workflow_id=a["workflow_id"])
                self.emit({"type": "approval_decided", "ts": fmt_ts(self.now), "approval_id": a["approval_id"],
                           "workflow_id": a["workflow_id"], "status": "expired"})
            elif self.now >= a["decide_minute"] and a["decide_minute"] <= a["expires_minute"]:
                a["status"] = a["_decision"]
                a["decided_minute"] = a["decide_minute"]
                self.log("approval_decided", approval_id=a["approval_id"], workflow_id=a["workflow_id"],
                         status=a["status"], reasons=a["_reasons"])
                self.emit({"type": "approval_decided", "ts": fmt_ts(a["decide_minute"]),
                           "approval_id": a["approval_id"], "workflow_id": a["workflow_id"],
                           "status": a["status"], "reasons": a["_reasons"]})

    def _apply(self, w: WorkflowState, cfg: dict, actor: str, pid, aid) -> int:
        old = w.current["config"]
        dims = changed_dims(old, cfg)
        v = len(w.versions) + 1
        w.versions.append({"version": v, "config": copy.deepcopy(cfg), "actor": actor, "applied_at": self.now,
                           "proposal_id": pid, "approval_id": aid, "changed_dims": dims})
        if actor == "supervisor" and cfg["prompt_version"] != old["prompt_version"]:
            w.baseline_prompt = cfg["prompt_version"]
        self.emit({"type": "config_changed", "ts": fmt_ts(self.now), "workflow_id": w.id, "version": v,
                   "actor": actor, "changed_dims": dims, "proposal_id": pid, "approval_id": aid})
        if not self._writing_history:
            self._truth_eval_change(w, old, cfg, v, actor, dims)
        return v

    def _truth_eval_change(self, w, old, new, v, actor, dims):
        active = w.enroll["person"][w.enroll["status"] == 0]
        if len(active) == 0:
            return
        sample = active if len(active) <= 20000 else self.rng.choice(active, 20000, replace=False)
        eo = w.expected_engagement(old, sample, self.now, self.prompts).mean()
        en = w.expected_engagement(new, sample, self.now, self.prompts).mean()
        h = w.did_health()

        def did_exp(cfg):
            coh = w.cohort_of(cfg, sample)
            names = ["default"] + [c["id"] for c in cfg["cohorts"]]
            tot = 0.0
            for ci, name in enumerate(names):
                k = (coh == ci).sum()
                tot += k * sum(p * h[w.opts["caller_id"].index(o)] for o, p in _dist(cfg, name, "caller_id").items())
            return tot / len(sample)
        rec = {"workflow_id": w.id, "version": v, "actor": actor, "dims": dims, "minute": self.now,
               "exp_engage_old": round(float(eo), 5), "exp_engage_new": round(float(en), 5),
               "exp_did_health_old": round(did_exp(old), 5), "exp_did_health_new": round(did_exp(new), 5)}
        self._truth_changes.write(json.dumps(rec) + "\n")
        self._truth_changes.flush()

    # ------------------------------------------------------------------ simulation loop
    def _run(self, target: int):
        while self.now < target:
            self._fire_incidents()
            for w in self.wfs.values():
                self._tick_workflow(w)
            self._process_approvals()
            self.now += self.tick
            # DID load decay
            decay = math.exp(-self.tick / (self.sc["did_tau_days"] * MINUTES_PER_DAY))
            for w in self.wfs.values():
                w.did_load *= decay
        self._flush_chunk()

    def _fire_incidents(self):
        while self._pending_incidents and self._pending_incidents[0]["at_minute"] <= self.now:
            inc = self._pending_incidents.pop(0)
            w = self.wfs[inc["workflow"]]
            if inc["type"] == "supervisor_edit":
                cfg = copy.deepcopy(w.current["config"])
                _deep_merge(cfg, inc["patch"])
                cfg = normalise_config(w.spec, cfg)
                v = self._apply(w, cfg, "supervisor", None, None)
                self.log("supervisor_edit", workflow_id=w.id, version=v, patch=inc["patch"],
                         note=inc.get("note"))
            elif inc["type"] == "did_flagged":
                i = w.opts["caller_id"].index(inc["did"])
                w.did_base[i] = inc["base_health"]
                self.log("hidden_incident", workflow_id=w.id, incident=inc["type"], did=inc["did"])
            self.incident_log.append({**inc, "fired_minute": self.now})

    def _new_campaign(self, w: WorkflowState, day: int):
        spec = w.spec
        b = spec["daily_batch"]
        size = int(b["size"])
        n_ret = 0
        if w.persons.n:
            idle = np.flatnonzero(~w.persons["active"])
            n_ret = min(int(round(size * b.get("returning_share", 0.0))), len(idle))
        ret = self.rng.choice(idle, n_ret, replace=False) if n_ret else np.array([], dtype=np.int64)
        new = self._make_persons(w, size - n_ret)
        pidx = np.concatenate([ret.astype(np.int64), new])
        w.persons.c["active"][pidx] = True
        cid = f"{w.id}:c{len(w.campaigns) + 1:04d}"
        camp = {"campaign_id": cid, "index": len(w.campaigns), "day": day, "created_minute": self.now,
                "config_version": w.current["version"], "size": len(pidx)}
        w.campaigns.append(camp)
        eidx = w.enroll.append({"person": pidx, "campaign": np.full(len(pidx), camp["index"]),
                                "status": np.zeros(len(pidx)), "attempts_today": np.zeros(len(pidx)),
                                "attempts": np.zeros(len(pidx)), "last_attempt": np.full(len(pidx), -10**9)})
        ts = fmt_ts(self.now)
        self.emit({"type": "campaign_created", "ts": ts, "tenant_id": spec["tenant_id"], "workflow_id": w.id,
                   "campaign_id": cid, "leads": len(pidx), "config_version_at_creation": camp["config_version"]})
        P = w.persons
        for e, p in zip(eidx.tolist(), pidx.tolist()):
            self.emit({"type": "lead", "ts": ts, "workflow_id": w.id, "campaign_id": cid,
                       "lead_id": f"{w.id}:l{e:08d}", "person_id": f"{w.id}:p{p:07d}",
                       "features": {f: FEATURES[f][int(P.c[f][p])] for f in FEATURES}})

    def _make_persons(self, w: WorkflowState, k: int) -> np.ndarray:
        spec, rng = w.spec, self.rng
        mix = spec["language_mix"]
        langs = rng.choice([FEATURES["lang"].index(l) for l in mix], k, p=np.array(list(mix.values())) / sum(mix.values()))
        tier = rng.choice(3, k, p=spec.get("tier_mix", [0.4, 0.35, 0.25]))
        age = rng.choice(4, k, p=spec.get("age_mix", [0.25, 0.35, 0.25, 0.15]))
        val = rng.choice(3, k, p=spec.get("value_mix", [0.5, 0.35, 0.15]))
        # archetype logits (hidden): morning, lunch, evening, flat
        L = np.tile(np.array([0.0, 0.0, 0.0, 0.3]), (k, 1))
        L[age == 3, 0] += 1.6
        L[age == 2, 0] += 0.6
        L[age == 0, 2] += 1.3
        L[age == 1, 1] += 0.5
        L[age == 1, 2] += 0.5
        L[tier == 0, 1] += 0.5
        L[tier == 2, 0] += 0.3
        L += rng.gumbel(size=L.shape) * spec.get("archetype_noise", 1.0)
        arch = L.argmax(axis=1)
        p0 = spec["base_pickup"] * np.exp(rng.normal(0, 0.35, k)) * np.array([0.9, 1.0, 1.1])[tier]
        e0 = spec["base_engage"] * np.exp(rng.normal(0, 0.2, k)) * np.array([0.85, 1.0, 1.05, 1.1])[age]
        interest = np.array([0.25, 0.35, 0.45])[val] * np.exp(rng.normal(0, 0.2, k))
        return w.persons.append({"lang": langs, "city_tier": tier, "age_band": age, "value_band": val,
                                 "arch": arch, "p0": np.minimum(p0, 0.9), "e0": np.minimum(e0, 0.95),
                                 "interest": np.minimum(interest, 0.9), "active": np.zeros(k, bool)})

    def _pref_hours(self, w: WorkflowState, cfg_version: int, cfg: dict) -> np.ndarray:
        """person index -> preferred hour (-1 none), cached per (config version, persons.n)."""
        c = w._pref_cache
        if c and c[0] == cfg_version and c[1] == w.persons.n:
            return c[2]
        arr = np.full(w.persons.n, -1, dtype=np.int16)
        lph = cfg["schedule"]["lead_preferred_hour"]
        if lph:
            prefix = f"{w.id}:p"
            for pid, h in lph.items():
                if pid.startswith(prefix):
                    try:
                        i = int(pid[len(prefix):])
                    except ValueError:
                        continue
                    if 0 <= i < w.persons.n:
                        arr[i] = h
        w._pref_cache = (cfg_version, w.persons.n, arr)
        return arr

    def _tick_workflow(self, w: WorkflowState):
        spec = w.spec
        day, mod = divmod(self.now, MINUTES_PER_DAY)
        hour = mod // 60
        E = w.enroll
        if mod == 0 and E.n:
            E.c["attempts_today"][: E.n] = 0
            camp_day = np.array([c["day"] for c in w.campaigns])[E["campaign"]]
            expired = (E["status"] == 0) & (day - camp_day >= len(spec["cadence"]))
            if expired.any():
                E.c["status"][np.flatnonzero(expired)] = 2
                w.persons.c["active"][E["person"][expired]] = False
        if mod == spec["daily_batch"]["at_hour"] * 60:
            self._new_campaign(w, day)
        lo, hi = spec["dialling_window"]
        if not (lo <= hour < hi) or E.n == 0:
            return
        cur = w.current["config"]
        cap_f = spec["dials_per_hour"] * self.tick / 60
        cap = int(cap_f) + (1 if self.rng.random() < cap_f - int(cap_f) else 0)
        camp_day = np.array([c["day"] for c in w.campaigns])
        cadence = np.array(spec["cadence"] + [0])
        di = np.minimum(day - camp_day[E["campaign"]], len(spec["cadence"]))
        elig = np.flatnonzero((E["status"] == 0) & (E["attempts_today"] < cadence[di]) &
                              (self.now - E["last_attempt"] >= spec["min_gap_minutes"]))
        if len(elig) == 0 or cap == 0:
            return
        pidx = E["person"][elig]
        # scheduling score: cohort hour weight x per-person preference
        coh = w.cohort_of(cur, pidx)
        names = ["default"] + [c["id"] for c in cur["cohorts"]]
        hw = cur["schedule"]["hour_weights"]
        score = np.ones(len(elig))
        for ci, name in enumerate(names):
            table = hw.get(name) if name in hw else hw.get("default")
            if table:
                score[coh == ci] = table.get(str(hour), 0.0)
        pref = self._pref_hours(w, w.current["version"], cur)[pidx]
        has = pref >= 0
        score[has] = np.where(pref[has] == hour, 6.0, 0.25) * np.maximum(score[has], 0.05)
        pos = score > 0
        if not pos.any():
            return
        keys = np.full(len(elig), -np.inf)
        keys[pos] = np.log(score[pos]) + self.rng.gumbel(size=pos.sum())
        k = min(cap, int(pos.sum()))
        pick = np.argpartition(-keys, k - 1)[:k]
        sel_e = elig[pick]
        sel_p = pidx[pick]
        self._dial(w, sel_e, sel_p, hour, day)

    def _dial(self, w: WorkflowState, eidx, pidx, hour, day):
        spec, rng, P, E = w.spec, self.rng, w.persons, w.enroll
        n = len(eidx)
        cur = w.current["config"]
        # effective agent config per lead (campaign lock if mid_flight == next_campaign_only)
        if spec["mid_flight"] == "next_campaign_only":
            ver = np.array([w.campaigns[c]["config_version"] for c in E["campaign"][eidx]])
        else:
            ver = np.full(n, w.current["version"])
        var = {d: np.zeros(n, dtype=np.int16) for d in AGENT_DIMS}
        coh_name = np.empty(n, dtype=object)
        for v in np.unique(ver):
            cfg = w.config_at_version(int(v))
            sv = np.flatnonzero(ver == v)
            coh = w.cohort_of(cfg, pidx[sv])
            names = ["default"] + [c["id"] for c in cfg["cohorts"]]
            for ci, name in enumerate(names):
                s = sv[coh == ci]
                if len(s) == 0:
                    continue
                coh_name[s] = name
                for d in AGENT_DIMS:
                    dist = _dist(cfg, name, d)
                    opts = list(dist)
                    choice = rng.choice(len(opts), len(s), p=np.array([dist[o] for o in opts]))
                    var[d][s] = np.array([w.opts[d].index(o) for o in opts])[choice]
        prompt = cur["prompt_version"]
        pr = self.prompts[prompt]
        for d in ("welcome_message", "voice"):
            for o in np.unique(var[d]):
                w.exposure.setdefault((d, w.opts[d][o]), self.now)
        # pickup
        health = w.did_health()
        p_conn = np.minimum(P["p0"][pidx] * HOUR_PROFILE[P["arch"][pidx], hour] * health[var["caller_id"]], 0.95)
        conn = rng.random(n) < p_conn
        np.add.at(w.did_load, var["caller_id"], 1.0)
        # engagement
        Mw = w.dim_mults("welcome_message", pidx, self.now)
        Mv = w.dim_mults("voice", pidx, self.now)
        ar = np.arange(n)
        pf = pr["engage_mult"] * (1 - 0.5 * pr["halluc"])
        e_base = P["e0"][pidx]
        p_eng = np.minimum(e_base * Mw[var["welcome_message"], ar] * Mv[var["voice"], ar] * pf, 0.95)
        halluc = conn & (rng.random(n) < pr["halluc"])
        p_eng_real = np.minimum(e_base * Mw[var["welcome_message"], ar] * Mv[var["voice"], ar] *
                                pr["engage_mult"] * np.where(halluc, 0.5, 1.0), 0.95)
        eng = conn & (rng.random(n) < p_eng_real)
        interested = eng & (rng.random(n) < P["interest"][pidx])
        flag = np.where(halluc, rng.random(n) < 0.8, rng.random(n) < 0.01) & conn
        adherence = np.clip(rng.normal(0.93, 0.04, n) - 0.15 * halluc, 0, 1)
        vm = ~conn & (rng.random(n) < 0.3)
        dur = np.where(eng, rng.integers(60, 241, n), np.where(conn, rng.integers(5, 26, n), 0))
        turns = np.where(eng, rng.integers(6, 19, n), np.where(conn, rng.integers(1, 4, n), 0))
        # state updates
        E.c["attempts_today"][eidx] += 1
        E.c["attempts"][eidx] += 1
        E.c["last_attempt"][eidx] = self.now
        E.c["status"][eidx[eng]] = 1
        P.c["active"][pidx[eng]] = False
        # truth aggregates
        if not self._writing_history:
            self._truth_accumulate(w, day, pidx, conn, eng, p_conn, p_eng, halluc, var)
        # events
        ts = fmt_ts(self.now + int(rng.integers(0, self.tick)))
        wm, vo, ci = w.opts["welcome_message"], w.opts["voice"], w.opts["caller_id"]
        camp = E["campaign"][eidx]
        att = E["attempts"][eidx]
        camp_day = np.array([c["day"] for c in w.campaigns])
        for i in range(n):
            if eng[i]:
                disp = "concluded_interested" if interested[i] else "concluded_not_interested"
            elif conn[i]:
                disp = "short_hangup"
            else:
                disp = "voicemail" if vm[i] else "no_answer"
            c = int(camp[i])
            self.emit({"type": "call", "ts": ts, "tenant_id": spec["tenant_id"], "workflow_id": w.id,
                       "campaign_id": w.campaigns[c]["campaign_id"], "lead_id": f"{w.id}:l{int(eidx[i]):08d}",
                       "person_id": f"{w.id}:p{int(pidx[i]):07d}", "attempt_no": int(att[i]),
                       "campaign_day_index": int(day - camp_day[c]), "config_version": int(ver[i]),
                       "cohort_id": coh_name[i],
                       "variant": {"welcome_message": wm[var["welcome_message"][i]], "voice": vo[var["voice"][i]],
                                   "caller_id": ci[var["caller_id"][i]], "prompt_version": prompt},
                       "connected": bool(conn[i]), "engaged": bool(eng[i]), "disposition": disp,
                       "duration_s": int(dur[i]), "turns": int(turns[i]),
                       "qa": {"hallucination_flag": bool(flag[i]), "flow_adherence": round(float(adherence[i]), 3)}
                       if conn[i] else None})

    def _truth_accumulate(self, w, day, pidx, conn, eng, p_conn, p_eng, halluc, var):
        key = f"{w.id}|{day}"
        t = self.truth.setdefault(key, {"workflow_id": w.id, "day": day, "dials": 0, "connected": 0,
                                        "engaged": 0, "sum_p_conn": 0.0, "sum_p_eng": 0.0,
                                        "sum_p_eng_default": 0.0, "sum_p_eng_oracle": 0.0, "halluc": 0,
                                        "decay_exposure": {}, "did_dials": {}})
        t["dials"] += len(pidx)
        t["connected"] += int(conn.sum())
        t["engaged"] += int(eng.sum())
        t["sum_p_conn"] += float(p_conn.sum())
        t["halluc"] += int(halluc.sum())
        for o, c in zip(*np.unique(var["caller_id"], return_counts=True)):
            name = w.opts["caller_id"][int(o)]
            t["did_dials"][name] = t["did_dials"].get(name, 0) + int(c)
        cp = pidx[conn]
        if len(cp):
            t["sum_p_eng"] += float(p_eng[conn].sum())
            base_cfg = copy.deepcopy(w.versions[0]["config"])
            base_cfg["prompt_version"] = w.baseline_prompt
            t["sum_p_eng_default"] += float(w.expected_engagement(base_cfg, cp, self.now, self.prompts).sum())
            Mw = w.dim_mults("welcome_message", cp, self.now).max(axis=0)
            Mv = w.dim_mults("voice", cp, self.now).max(axis=0)
            safe = [p for p in w.opts["prompt_version"] if self.prompts[p]["halluc"] <= 0.01] or w.opts["prompt_version"]
            best_pf = max(self.prompts[p]["engage_mult"] * (1 - 0.5 * self.prompts[p]["halluc"]) for p in safe)
            t["sum_p_eng_oracle"] += float(np.minimum(w.persons["e0"][cp] * Mw * Mv * best_pf, 0.95).sum())
        # exposure of decaying variants inside their target population, after decay onset
        for e in w.effects:
            if not e.get("decay"):
                continue
            j = self.sc["effects"].index(e)  # global index, so the grader can label it
            start = w.exposure.get((e["dim"], e["value"]))
            if start is None or self.now < start + e["decay"]["after_exposure_days"] * MINUTES_PER_DAY:
                continue
            m = w.where_mask(e.get("where") or {}, pidx)
            vi = w.opts[e["dim"]].index(e["value"])
            d = t["decay_exposure"].setdefault(str(j), {"in_pop": 0, "with_variant": 0, "onset_minute":
                                                       start + int(e["decay"]["after_exposure_days"] * MINUTES_PER_DAY)})
            d["in_pop"] += int(m.sum())
            d["with_variant"] += int((m & (var[e["dim"]] == vi)).sum())

    def _write_truth(self):
        out = {"scenario": self.sc["name"], "sim_minute": self.now, "days": list(self.truth.values()),
               "incidents": self.incident_log, "credits_remaining": self.credits,
               "credits_total": self.sc["credits"],
               "effects": self.sc["effects"],
               "prompt_halluc": {p: v["halluc"] for p, v in self.prompts.items()},
               "leads_uploaded": {wid: sum(c["size"] for c in w.campaigns if c["day"] >= 0)
                                  for wid, w in self.wfs.items()},
               "leads_concluded": {wid: int(((w.enroll["status"] == 1) &
                                               (np.array([c["day"] for c in w.campaigns])[w.enroll["campaign"]] >= 0)).sum())
                                     for wid, w in self.wfs.items()},
               "prompt_timeline": {wid: [(v["applied_at"], v["config"]["prompt_version"], v["actor"])
                                         for v in w.versions] for wid, w in self.wfs.items()}}
        with open(self.run_dir / "_truth" / "aggregates.json", "w") as f:
            json.dump(out, f, default=_json_default)


def _deep_merge(dst: dict, patch: dict):
    """Recursive merge; allocation distributions (welcome_message/voice/caller_id) are replaced, not merged."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict) and k not in AGENT_DIMS:
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
