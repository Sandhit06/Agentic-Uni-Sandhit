"""The always-on outcome loop.

The controller is deliberately a *reconciler*: every durable fact is committed
with the event high-water mark in one transaction, every write has an
idempotency key, and after a restart the first thing it does is read the
simulator's clock and replay only what it has not durably consumed.

Control flow at each simulated tick:
  1. reconcile approvals (apply anything approved, notice rejections/expiry)
  2. detect new incidents from short rolling windows
  3. run the daily, batched experiment/config plan
  4. advance the simulator and ingest the new event chunk
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from .analytics import Analytics, minute_of_ts
from .api import ApiError, SimClient
from .configs import (ConfigError, changed_dims, default_config, dist,
                      limit_shift, max_shift, normalise_config)
from .decisions import (apply_rollbacks, find_caller_health, find_rollbacks,
                        find_schedule, find_variant_findings, impact_score,
                        plan_config, plan_populations, predict_effect,
                        summarise_change)
from .stats import required_n, wilson, z_two_proportion
from .store import Store

ROUTINE_HOUR = 12
DAY_MINUTES = 1440
HALLUC_WINDOW_MIN = 360
HALLUC_MIN_CONN = 180
CALLER_WINDOW_MIN = 240
CALLER_MIN_DIALS = 70
PROMPT_RETRY_MIN = 90
SUPERVISOR_QUIET_MIN = 120


def _ts_minute(ts: str | None, fallback: int = 0) -> int:
    return minute_of_ts(ts) if ts else fallback


class Controller:
    def __init__(self, sim_url: str, state_dir: str | Path | None = None):
        self.client = SimClient(sim_url)
        self.state_dir = Path(state_dir or Path(__file__).resolve().parent.parent / "state")
        self.store = Store(self.state_dir / "controller.sqlite")
        self.st = self.client.state()
        self.scenario = self.st["scenario"]
        self.now = int(self.st.get("sim_minute", 0))
        self.credits = float(self.st.get("credits_remaining", 0))
        self.tick = int(self.st.get("tick_minutes", 15))
        self.stop = False
        self.dirty_configs: set[str] = set()
        self.dirty_approvals: set[str] = set()
        self.supervisor_edit_minute: dict[str, int] = {}
        self.caller_last_action: dict[str, int] = {}
        self.prompt_last_attempt: dict[str, int] = {}
        self.last_metric_minute = -10 ** 9
        self.retired: dict[str, int] = dict(self.store.get_meta("retired_variants") or {})

        self._bootstrap_world()
        self.workflows = {w["workflow_id"]: w for w in self.client.workflows()}
        self.an = Analytics(self.workflows)
        self.an.load_from_store(self.store)
        self.an.current_day = max(0, self.now // DAY_MINUTES)

        self.current: dict[str, dict] = {}
        self._load_current_configs()
        self.active: dict[tuple[str, str], dict] = {}
        for inc in self.store.active_incidents():
            self.active[(inc["workflow"], inc["kind"])] = inc
        self.store.set_meta("wall_beat", time.time())
        self._log("controller_started", {
            "scenario": self.scenario, "sim_minute": self.now,
            "workflows": sorted(self.workflows), "credits": self.credits,
        })

    # ------------------------------------------------------------------ state
    def _bootstrap_world(self):
        meta_scenario = self.store.get_meta("scenario")
        last_minute = self.store.get_meta("last_sim_minute")
        last_eid = self.store.get_meta("last_event_id", -1)
        reset = False
        if meta_scenario and meta_scenario != self.scenario:
            reset = True
        if last_minute is not None and self.now < int(last_minute) - 60:
            reset = True
        if last_eid is not None and int(self.st.get("last_event_id", 0)) < int(last_eid):
            reset = True
        if reset:
            self.store.reset()
        self.store.set_meta("scenario", self.scenario)

    def _load_current_configs(self):
        stored = self.store.current_configs()
        for wid in self.workflows:
            try:
                remote = self.client.get_config(wid)
            except ApiError as e:
                self._log("config_fetch_failed", {"workflow": wid, "error": str(e)})
                continue
            ver = int(remote["version"])
            cfg = remote["config"]
            old = stored.get(wid)
            if old is None or old["version"] != ver:
                dims = changed_dims(old["config"], cfg) if old else []
                self.store.save_config_version(
                    wid, ver, cfg, remote.get("actor") or "unknown",
                    _ts_minute(remote.get("applied_at"), self.now), dims)
            self.current[wid] = {
                "version": ver, "config": cfg,
                "actor": remote.get("actor") or "unknown",
                "applied_at_minute": _ts_minute(remote.get("applied_at"), self.now),
            }
        self._log("configs_loaded", {wid: v["version"] for wid, v in sorted(self.current.items())})

    def _log(self, kind: str, detail: Any, workflow: str | None = None):
        try:
            self.store.audit(self.now, None, workflow, kind, detail)
        except Exception:
            pass

    def _retire(self, wid: str, dim: str, option: str, minutes: int = 3 * DAY_MINUTES):
        key = f"{wid}|{dim}|{option}"
        self.retired[key] = self.now + max(minutes, DAY_MINUTES)
        self.store.set_meta("retired_variants", self.retired)
        self._log("variant_retired", {"dim": dim, "option": option,
                                      "until_minute": self.retired[key],
                                      "reason": "decay rollback"}, wid)

    def _is_retired(self, wid: str, dim: str, option: str) -> bool:
        key = f"{wid}|{dim}|{option}"
        until = self.retired.get(key)
        if until is None:
            return False
        if self.now >= int(until):
            self.retired.pop(key, None)
            try:
                self.store.set_meta("retired_variants", self.retired)
            except Exception:
                pass
            return False
        return True

    def _refresh_config(self, wid: str):
        try:
            remote = self.client.get_config(wid)
        except ApiError as e:
            self._log("config_refresh_failed", {"error": str(e)}, wid)
            return
        ver = int(remote["version"])
        cfg = remote["config"]
        old = self.current.get(wid)
        if old is None or int(old["version"]) != ver:
            dims = changed_dims(old["config"], cfg) if old else []
            self.store.save_config_version(
                wid, ver, cfg, remote.get("actor") or "unknown",
                _ts_minute(remote.get("applied_at"), self.now), dims)
            self.current[wid] = {
                "version": ver, "config": cfg,
                "actor": remote.get("actor") or "unknown",
                "applied_at_minute": _ts_minute(remote.get("applied_at"), self.now),
            }
            self._log("config_synced", {
                "version": ver, "actor": remote.get("actor"), "dims": dims,
            }, wid)

    def _refresh_dirty(self):
        for wid in list(self.dirty_configs):
            self.dirty_configs.discard(wid)
            self._refresh_config(wid)

    def _cfg(self, wid: str) -> dict:
        return self.current[wid]["config"]

    def _version(self, wid: str) -> int:
        return int(self.current[wid]["version"])

    # --------------------------------------------------------------- ingestion
    def ingest_pending(self):
        hist_after = int(self.store.get_meta("last_history_chunk", -1))
        ev_after = int(self.store.get_meta("last_event_chunk", -1))
        try:
            hist = self.client.list_chunks("history", after=hist_after)
            evs = self.client.list_chunks("events", after=ev_after)
        except ApiError as e:
            self._log("chunk_list_failed", {"error": str(e)})
            return
        self._ingest_chunks(hist + evs)

    def _ingest_chunks(self, metas: list[dict]):
        for meta in metas:
            if self.stop:
                return
            self._process_chunk(meta)

    def _process_chunk(self, meta: dict):
        try:
            self.an.begin_chunk()
            for line in self.client.iter_chunk_lines(int(meta["chunk"])):
                ev = json.loads(line)
                self._dispatch(ev)
            kind = meta.get("kind") or "events"
            updates = {
                "last_event_id": int(meta["last_event_id"]),
                "last_sim_minute": int(meta.get("until_minute", self.now)),
            }
            if kind == "history":
                updates["last_history_chunk"] = int(meta["chunk"])
                updates["last_history_event_id"] = int(meta["last_event_id"])
            else:
                updates["last_event_chunk"] = int(meta["chunk"])
                updates["last_event_event_id"] = int(meta["last_event_id"])
            self.an.commit_chunk(self.store, updates)
            self.now = max(self.now, int(meta.get("until_minute", self.now)))
            self.an.current_day = max(self.an.current_day, self.now // DAY_MINUTES)
        except Exception as exc:
            self._log("chunk_failed", {"chunk": meta.get("chunk"), "error": repr(exc)})
            raise

    def _dispatch(self, ev: dict):
        t = ev.get("type")
        if t in ("lead", "call", "campaign_created"):
            self.an.observe(ev)
            return
        if t == "config_changed":
            wid = ev["workflow_id"]
            actor = ev.get("actor")
            self.dirty_configs.add(wid)
            self._log("config_event", {
                "version": ev.get("version"), "actor": actor,
                "dims": ev.get("changed_dims"), "proposal_id": ev.get("proposal_id"),
                "approval_id": ev.get("approval_id"),
            }, wid)
            if actor == "supervisor":
                self.supervisor_edit_minute[wid] = minute_of_ts(ev.get("ts"))
            return
        if t == "approval_decided":
            self.dirty_approvals.add(ev.get("approval_id"))
            self._log("approval_event", {
                "approval_id": ev.get("approval_id"), "status": ev.get("status"),
                "reasons": ev.get("reasons") or [],
            }, ev.get("workflow_id"))
            return

    # --------------------------------------------------------------- approvals
    def reconcile_approvals(self):
        for p in self.store.pending_proposals():
            pid = p["proposal_id"]
            wid = p["workflow"]
            aid = p.get("approval_id")
            if not aid:
                if p.get("status") == "submitting" and (p.get("kind") or "") == "auto":
                    self._reconcile_auto(p)
                    continue
                # The request may have reached the simulator even if the
                # response was lost; look for it by proposal id.
                try:
                    apps = self.client.list_approvals(wid)
                except ApiError:
                    continue
                match = next((a for a in apps if a.get("proposal_id") == pid), None)
                if not match:
                    if p.get("status") == "submitting" and self.now - int(p["created_minute"]) > 120:
                        self.store.update_proposal(pid, status="abandoned",
                                                   notes="no approval found; safe to replan")
                        self._log("proposal_abandoned", {"proposal_id": pid}, wid)
                    continue
                aid = match["approval_id"]
                self.store.update_proposal(pid, approval_id=aid, status="pending")
                self._save_approval(match)
            try:
                a = self.client.get_approval(aid)
            except ApiError as e:
                if e.status == 404:
                    self.store.update_proposal(pid, status="rejected",
                                               notes="approval not found")
                    self.store.save_approval({"approval_id": aid, "proposal_id": pid,
                                              "workflow_id": wid, "status": "not_found"})
                    self._log("approval_not_found", {"approval_id": aid}, wid)
                else:
                    self._log("approval_get_failed", {"approval_id": aid, "error": str(e)}, wid)
                continue
            self._save_approval(a)
            status = a.get("status")
            if status == "approved":
                self._apply_approved(p, a)
            elif status in ("rejected", "expired", "applied", "lapsed"):
                self.store.update_proposal(pid, status=status,
                                           notes=json.dumps(a.get("reasons") or []))
                if status in ("rejected", "expired", "lapsed"):
                    self._note_approval_failure(wid, p, status, a)
                self._log("approval_terminal", {
                    "approval_id": aid, "status": status,
                    "reasons": a.get("reasons") or [],
                }, wid)

    def _save_approval(self, a: dict):
        self.store.save_approval({
            "approval_id": a.get("approval_id"),
            "proposal_id": a.get("proposal_id"),
            "workflow_id": a.get("workflow_id"),
            "status": a.get("status"),
            "created_minute": _ts_minute(a.get("created_at"), self.now),
            "decided_minute": _ts_minute(a.get("decided_at")) if a.get("decided_at") else None,
            "apply_by_minute": _ts_minute(a.get("apply_by")) if a.get("apply_by") else None,
            "expires_minute": _ts_minute(a.get("expires_at")) if a.get("expires_at") else None,
            "reasons": a.get("reasons") or [],
            "last_synced_minute": self.now,
        })

    def _apply_approved(self, p: dict, a: dict):
        wid, pid, aid = p["workflow"], p["proposal_id"], a["approval_id"]
        try:
            res = self.client.apply_approval(wid, aid)
        except ApiError as e:
            if e.status == 409 and e.code == "stale_base_version":
                self.store.update_proposal(pid, status="stale",
                                           notes="supervisor changed config before apply")
                self._save_approval({**a, "status": "stale"})
                self._note_approval_failure(wid, p, "stale", a)
                self._log("apply_stale", {"approval_id": aid, "detail": e.detail}, wid)
            elif e.status == 410:
                self.store.update_proposal(pid, status="lapsed", notes="apply window passed")
                self._save_approval({**a, "status": "lapsed"})
                self._note_approval_failure(wid, p, "lapsed", a)
                self._log("apply_lapsed", {"approval_id": aid}, wid)
            elif e.status == 409 and e.code == "not_approved":
                try:
                    a2 = self.client.get_approval(aid)
                    self._save_approval(a2)
                    self.store.update_proposal(pid, status=a2.get("status") or "unknown")
                    if a2.get("status") in ("rejected", "expired", "lapsed"):
                        self._note_approval_failure(wid, p, a2.get("status"), a2)
                except ApiError:
                    pass
                self._log("apply_not_approved", {"approval_id": aid, "detail": e.detail}, wid)
            else:
                self._log("apply_failed", {"approval_id": aid, "error": str(e)}, wid)
            return
        target = json.loads(p["proposed_config"])
        self._save_applied(wid, res, target, "controller_approved", pid, aid)
        self.store.update_proposal(pid, status="applied", approval_id=aid,
                                   notes=json.dumps({"version": res.get("version")}))
        self._save_approval({**a, "status": "applied"})
        self._log("approval_applied", {
            "approval_id": aid, "version": res.get("version"),
            "dims": res.get("changed_dims"),
        }, wid)
        self._on_change_applied(wid, p)

    def _reconcile_auto(self, p: dict):
        wid, pid = p["workflow"], p["proposal_id"]
        try:
            hist = self.client.config_history(wid)
        except ApiError:
            return
        match = next((v for v in hist if v.get("proposal_id") == pid), None)
        if match:
            cfg = self._get_config_or_none(wid)
            if cfg:
                self._save_applied(wid, {"version": match["version"],
                                         "changed_dims": match.get("changed_dims") or []},
                                   cfg, "controller_auto", pid, None)
            self.store.update_proposal(pid, status="applied",
                                       notes=json.dumps({"reconciled": True}))
            self._log("auto_reconciled", {"proposal_id": pid, "version": match["version"]}, wid)
        elif self.now - int(p["created_minute"]) > 60:
            self.store.update_proposal(pid, status="abandoned",
                                       notes="auto-apply did not land; safe to replan")
            self._log("auto_abandoned", {"proposal_id": pid}, wid)

    def _get_config_or_none(self, wid: str) -> dict | None:
        try:
            return self.client.get_config(wid)["config"]
        except ApiError:
            return None

    def _save_applied(self, wid: str, res: dict, target: dict, actor: str,
                      pid: str | None, aid: str | None):
        old = self._cfg(wid)
        dims = res.get("changed_dims") or changed_dims(old, target)
        ver = int(res["version"])
        self.store.save_config_version(wid, ver, target, actor, self.now, dims, pid, aid)
        self.current[wid] = {"version": ver, "config": target, "actor": actor,
                             "applied_at_minute": self.now}

    def _on_change_applied(self, wid: str, p: dict):
        if p.get("kind") == "incident_prompt":
            inc = self.active.get((wid, "prompt_safety"))
            if inc:
                detail = json.loads(inc["detail_json"])
                detail["fixed_minute"] = self.now
                detail["mttr_minutes"] = self.now - int(inc["detected_minute"])
                self.store.update_incident(int(inc["id"]), "fixed", detail)
                self.active.pop((wid, "prompt_safety"), None)
                self._log("incident_fixed", {"incident_id": inc["id"], "mttr": detail["mttr_minutes"]}, wid)

    def _note_approval_failure(self, wid: str, p: dict, status: str, a: dict):
        if p.get("kind") == "incident_prompt":
            inc = self.active.get((wid, "prompt_safety"))
            if inc:
                detail = json.loads(inc["detail_json"])
                detail["last_failure"] = {"status": status, "reasons": a.get("reasons") or [],
                                          "at": self.now}
                self.store.update_incident(int(inc["id"]), "fix_rejected", detail)
                self.active[(wid, "prompt_safety")] = self.store.incident(int(inc["id"])) or inc
                self.prompt_last_attempt[wid] = self.now
        elif p.get("kind") == "incident_caller":
            inc = self.active.get((wid, "caller_id_health"))
            if inc:
                detail = json.loads(inc["detail_json"])
                detail["last_failure"] = {"status": status, "reasons": a.get("reasons") or [],
                                          "at": self.now}
                self.store.update_incident(int(inc["id"]), "fix_rejected", detail)
                self.active[(wid, "caller_id_health")] = self.store.incident(int(inc["id"])) or inc

    # ------------------------------------------------------------- governance
    def _pending_proposal(self, wid: str, kinds: tuple[str, ...] | None = None) -> dict | None:
        for p in self.store.pending_proposals():
            if p["workflow"] != wid:
                continue
            if kinds is not None and p.get("kind") not in kinds:
                continue
            return p
        return None

    def _within_approver_hours(self, wid: str, now: int | None = None,
                               routine: bool = False) -> bool:
        gov = self.workflows[wid].get("governance") or {}
        lo, hi = gov.get("approver_hours") or [9, 19]
        hour = ((now if now is not None else self.now) % DAY_MINUTES) // 60
        earliest = ROUTINE_HOUR if routine else max(11, lo)
        return max(lo, earliest) <= hour < hi

    def _requests_24h(self, wid: str) -> int:
        local = self.store.requests_24h(wid, self.now)
        try:
            apps = self.client.list_approvals(wid)
        except ApiError:
            return local
        remote = sum(1 for a in apps
                     if _ts_minute(a.get("created_at"), -10 ** 9) > self.now - DAY_MINUTES)
        return max(local, remote)

    def _has_budget(self, wid: str, reserve: int = 0) -> bool:
        gov = self.workflows[wid].get("governance") or {}
        cap = int(gov.get("max_requests_per_24h", 0))
        return self._requests_24h(wid) + 1 + reserve <= cap

    def _new_proposal_id(self) -> str:
        return f"c-{self.scenario[:24]}-{self.now}-{uuid.uuid4().hex[:8]}"

    def _proposal_body(self, wid: str, target: dict, summary: str, diff: dict,
                       affected: int, dry: dict, impact: float, rationale: str,
                       evidence: dict | None = None) -> dict:
        return {
            "proposal_id": self._new_proposal_id(),
            "workflow_id": wid,
            "base_version": self._version(wid),
            "proposed_config": target,
            "change_summary": summary,
            "diff": diff or {"changed_dims": []},
            "affected_leads": int(max(0, affected)),
            "dry_run": dry,
            "impact_score": float(max(0.0, min(1.0, impact))),
            "rationale": rationale,
            "evidence": evidence or {},
        }

    def _save_proposal_record(self, wid: str, body: dict, kind: str, status: str):
        self.store.save_proposal({
            "proposal_id": body["proposal_id"],
            "workflow": wid,
            "base_version": body["base_version"],
            "proposed_config": body["proposed_config"],
            "change_summary": body["change_summary"],
            "diff": body["diff"],
            "affected_leads": body["affected_leads"],
            "dry_run": body["dry_run"],
            "impact_score": body["impact_score"],
            "rationale": body["rationale"],
            "evidence": body.get("evidence") or {},
            "created_minute": self.now,
            "status": status,
            "kind": kind,
        })

    def _submit_approval(self, wid: str, body: dict, kind: str) -> dict | None:
        pid = body["proposal_id"]
        routine = kind == "routine"
        if not self._within_approver_hours(wid, routine=routine):
            self._log("approval_deferred_hours", {"proposal_id": pid, "kind": kind}, wid)
            return None
        if not self._has_budget(wid, reserve=0):
            self._log("approval_deferred_budget", {"proposal_id": pid, "kind": kind,
                                                   "requests_24h": self._requests_24h(wid)}, wid)
            return None
        self._save_proposal_record(wid, body, kind, "submitting")
        try:
            a = self.client.request_approval(body)
        except ApiError as e:
            if e.status == 409 and e.code == "duplicate_proposal_id":
                try:
                    apps = self.client.list_approvals(wid)
                except ApiError:
                    apps = []
                match = next((x for x in apps if x.get("proposal_id") == pid), None)
                if match:
                    self.store.update_proposal(pid, status="pending",
                                               approval_id=match["approval_id"])
                    self._save_approval(match)
                    self._log("approval_duplicate_reconciled", {"proposal_id": pid}, wid)
                    return self.store.proposal(pid)
                self.store.update_proposal(pid, status="abandoned")
                return None
            if e.status == 409 and e.code == "stale_base_version":
                self.store.update_proposal(pid, status="stale", notes=json.dumps(e.detail))
                self.dirty_configs.add(wid)
                self._log("proposal_stale", {"proposal_id": pid, "detail": e.detail}, wid)
                return None
            if e.status == 422:
                self.store.update_proposal(pid, status="rejected",
                                           notes=f"{e.code}: {e.detail}")
                self._log("proposal_invalid", {"proposal_id": pid, "code": e.code,
                                               "detail": e.detail}, wid)
                return None
            self._log("approval_submit_failed", {"proposal_id": pid, "error": str(e)}, wid)
            return None
        self._save_approval(a)
        self.store.update_proposal(pid, status="pending", approval_id=a["approval_id"])
        self._log("approval_requested", {
            "proposal_id": pid, "approval_id": a["approval_id"], "kind": kind,
            "summary": body["change_summary"], "impact": body["impact_score"],
        }, wid)
        return self.store.proposal(pid)

    def _try_auto(self, wid: str, target: dict, summary: str, diff: dict,
                  affected: int, dry: dict, impact: float, rationale: str,
                  kind: str, evidence: dict | None = None) -> dict | None:
        body = self._proposal_body(wid, target, summary, diff, affected, dry,
                                   impact, rationale, evidence)
        pid = body["proposal_id"]
        self._save_proposal_record(wid, body, "auto", "submitting")
        auto_body = {
            "proposal_id": pid, "base_version": body["base_version"],
            "proposed_config": body["proposed_config"], "rationale": rationale,
            "evidence": evidence or {},
        }
        try:
            res = self.client.auto_apply(wid, auto_body)
        except ApiError as e:
            if e.status == 403:
                self.store.update_proposal(pid, status="rejected",
                                           notes=json.dumps(e.detail))
                self._log("auto_rejected", {"proposal_id": pid, "detail": e.detail}, wid)
            elif e.status == 409 and e.code == "stale_base_version":
                self.store.update_proposal(pid, status="stale", notes=json.dumps(e.detail))
                self.dirty_configs.add(wid)
                self._log("auto_stale", {"proposal_id": pid, "detail": e.detail}, wid)
            elif e.status == 409 and e.code == "duplicate_proposal_id":
                self._reconcile_auto(self.store.proposal(pid))
            else:
                self._log("auto_failed", {"proposal_id": pid, "error": str(e)}, wid)
            return None
        self._save_applied(wid, res, target, "controller_auto", pid, None)
        self.store.update_proposal(pid, status="applied",
                                   notes=json.dumps({"version": res.get("version")}))
        self._log("auto_applied", {
            "proposal_id": pid, "version": res.get("version"),
            "dims": res.get("changed_dims"), "summary": summary,
        }, wid)
        return self.store.proposal(pid)

    # --------------------------------------------------------------- incidents
    def check_incidents(self):
        self._check_prompt_safety()
        self._check_caller_health()

    def _check_prompt_safety(self):
        for wid, wf in self.workflows.items():
            cfg = self._cfg(wid)
            prompt = cfg.get("prompt_version")
            if not prompt:
                continue
            n, h = self.an.recent_halluc_counts(wid, prompt, self.now, HALLUC_WINDOW_MIN)
            gov = wf.get("governance") or {}
            min_pass = float(gov.get("min_validation_pass_rate", 0.97))
            safe_rate = self._safe_prompt_rate(wid)
            threshold = max(0.04, 1.0 - min_pass + 0.005, 3.0 * safe_rate)
            trigger = (n >= HALLUC_MIN_CONN and h >= 8 and
                       h / n >= threshold)
            inc = self.active.get((wid, "prompt_safety"))
            if (inc is not None and self._is_safe_prompt(wid, prompt)):
                detail = json.loads(inc["detail_json"])
                detail["resolved_minute"] = self.now
                detail["mttr_minutes"] = self.now - int(inc["detected_minute"])
                self.store.update_incident(int(inc["id"]), "fixed", detail)
                self.active.pop((wid, "prompt_safety"), None)
                self._log("incident_fixed", {"incident_id": inc["id"],
                                             "mttr": detail["mttr_minutes"]}, wid)
                continue
            if trigger and inc is None:
                detail = {"prompt": prompt, "window_minutes": HALLUC_WINDOW_MIN,
                          "connected": n, "hallucinated": h,
                          "rate": round(h / n, 4), "threshold": threshold,
                          "safe_baseline": round(safe_rate, 4)}
                iid = self.store.add_incident(wid, "prompt_safety", self.now, "detected", detail)
                inc = self.store.incident(iid)
                self.active[(wid, "prompt_safety")] = inc
                self._log("incident_detected", {"incident_id": iid, **detail}, wid)
                self._respond_prompt_incident(wid, inc)
            elif inc is not None:
                status = inc.get("status")
                if status == "detected":
                    if self.now - int(inc["detected_minute"]) >= 30:
                        self._respond_prompt_incident(wid, inc)
                elif status == "fix_rejected":
                    if self.now - self.prompt_last_attempt.get(wid, -10 ** 9) >= PROMPT_RETRY_MIN:
                        self._respond_prompt_incident(wid, inc)

    def _safe_prompt_rate(self, wid: str) -> float:
        rates = []
        for p in self.workflows[wid]["options"]["prompt_version"]:
            conn, _, hall = self.an.prompt_counts(wid, p, self.an.current_day, None)
            if conn >= 200:
                rates.append(hall / conn)
        return min(rates) if rates else 0.01

    def _is_safe_prompt(self, wid: str, prompt: str) -> bool:
        gov = self.workflows[wid].get("governance") or {}
        min_pass = float(gov.get("min_validation_pass_rate", 0.97))
        val = self.store.validation_for(wid, prompt)
        if val and float(val["pass_rate"]) >= min_pass:
            return True
        conn, _, hall = self.an.prompt_counts(wid, prompt, self.an.current_day, None)
        safe_rate = self._safe_prompt_rate(wid)
        return conn >= 100 and hall / max(conn, 1) <= max(0.02, 3.0 * safe_rate)

    def _summary_score(self, text: str) -> int:
        t = (text or "").lower()
        score = 0
        for w, s in (("grounded", 2), ("policy", 2), ("compliance", 2),
                     ("tighter", 2), ("objection handling", 1), ("safe", 1)):
            if w in t:
                score += s
        for w, s in (("urgency", -2), ("aggressive", -2), ("persuasive", -2),
                     ("benefit claims", -2), ("freer", -2), ("richer", -2),
                     ("verbatim", -2), ("strict", -1), ("pressure", -2)):
            if w in t:
                score += s
        return score

    def _validate(self, wid: str, prompt: str, n: int) -> dict | None:
        cached = self.store.validation_for(wid, prompt)
        gov = self.workflows[wid].get("governance") or {}
        min_pass = float(gov.get("min_validation_pass_rate", 0.97))
        if cached and int(cached["n"]) >= n and float(cached["pass_rate"]) >= min_pass:
            return cached
        need = (n + 99) // 100
        try:
            st = self.client.state()
            if float(st.get("credits_remaining", 0)) < need:
                self._log("validation_skipped_credits", {"prompt": prompt, "need": need}, wid)
                return cached
            run = self.client.run_validation(wid, prompt, n)
        except ApiError as e:
            self._log("validation_failed", {"prompt": prompt, "error": str(e)}, wid)
            return cached
        run["created_minute"] = _ts_minute(run.get("ts"), self.now)
        self.store.save_validation_run(run)
        self._log("validation_run", {"prompt": prompt, "n": n,
                                     "pass_rate": run.get("pass_rate"),
                                     "validation_run_id": run.get("validation_run_id")}, wid)
        return run

    def _respond_prompt_incident(self, wid: str, inc: dict):
        if self._pending_proposal(wid):
            return
        if not self._within_approver_hours(wid):
            self._log("prompt_fix_deferred_hours", {"incident_id": inc["id"]}, wid)
            return
        gov = self.workflows[wid].get("governance") or {}
        cap = int(gov.get("max_requests_per_24h", 0))
        if self._requests_24h(wid) + 1 > cap:
            self._log("prompt_fix_deferred_budget", {"incident_id": inc["id"],
                                                     "requests_24h": self._requests_24h(wid)}, wid)
            return
        cfg = self._cfg(wid)
        current_prompt = cfg.get("prompt_version")
        problem = (f"Hallucination spike on prompt {current_prompt}: "
                   f"{inc['detail_json']}")
        suggestions: list[dict] = []
        try:
            st = self.client.state()
            if float(st.get("credits_remaining", 0)) >= 3:
                resp = self.client.suggest_fixes(wid, problem)
                suggestions = resp.get("suggestions") or []
                self._log("llm_suggest_fixes", {"incident_id": inc["id"],
                                                "suggestions": suggestions}, wid)
        except ApiError as e:
            self._log("llm_suggest_failed", {"incident_id": inc["id"], "error": str(e)}, wid)

        catalog = self.workflows[wid].get("prompt_catalog") or {}
        candidates: dict[str, dict] = {}
        for s in suggestions:
            p = s.get("prompt_version")
            if p and p != current_prompt and p in self.workflows[wid]["options"]["prompt_version"]:
                candidates[p] = {"summary": s.get("summary") or catalog.get(p, ""),
                                 "source": "llm"}
        for p in self.workflows[wid]["options"]["prompt_version"]:
            if p == current_prompt:
                continue
            conn, eng, hall = self.an.prompt_counts(wid, p, self.an.current_day, None)
            if conn >= 100 and hall / max(conn, 1) <= 0.01:
                candidates.setdefault(p, {"summary": catalog.get(p, ""),
                                          "source": "historical_safe"})
                candidates[p]["hist_rate"] = eng / conn
        # The production prompt is always an allowed fallback.
        for p in self.workflows[wid]["options"]["prompt_version"]:
            if p == current_prompt:
                continue
            if p not in candidates and (self.store.validation_for(wid, p) or
                                        self.an.prompt_counts(wid, p, self.an.current_day, None)[0] > 0):
                candidates[p] = {"summary": catalog.get(p, ""), "source": "fallback"}

        ranked = []
        for p, meta in candidates.items():
            score = self._summary_score(meta.get("summary", ""))
            ranked.append((score, meta.get("hist_rate", 0.0), p, meta))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        if not ranked:
            self._log("prompt_fix_no_candidates", {"incident_id": inc["id"]}, wid)
            return

        validated = []
        # Validate the two best heuristic candidates and the best historical
        # safe prompt (usually the current production prompt).
        wanted: list[str] = [r[2] for r in ranked[:2]]
        hist = [r[2] for r in ranked if r[3].get("source") == "historical_safe"]
        if hist and hist[0] not in wanted:
            wanted.append(hist[0])
        for p in wanted[:3]:
            run = self._validate(wid, p, 200)
            if not run:
                continue
            validated.append((run, p, candidates[p]))

        min_pass = float(gov.get("min_validation_pass_rate", 0.97))
        ok = [(r, p, m) for r, p, m in validated if float(r["pass_rate"]) >= min_pass]
        chosen = None
        if ok:
            def rank(item):
                run, p, meta = item
                score = self._summary_score(meta.get("summary", ""))
                hist_bonus = 3 if meta.get("source") == "historical_safe" else 0
                top = 10 if score >= 4 else 0
                return (top + score + hist_bonus, float(run["pass_rate"]),
                        meta.get("hist_rate", 0.0))
            chosen = max(ok, key=rank)
        if chosen is None and hist:
            run = self._validate(wid, hist[0], 400)
            if run and float(run["pass_rate"]) >= min_pass:
                chosen = (run, hist[0], candidates[hist[0]])
        if chosen is None:
            detail = json.loads(inc["detail_json"])
            detail["last_failure"] = {"status": "no_validated_safe_prompt",
                                      "at": self.now,
                                      "evidence": [(r["validation_run_id"], r["pass_rate"])
                                                   for r, _, _ in validated]}
            self.store.update_incident(int(inc["id"]), "fix_rejected", detail)
            self.active[(wid, "prompt_safety")] = self.store.incident(int(inc["id"])) or inc
            self.prompt_last_attempt[wid] = self.now
            self._log("prompt_fix_no_validated_candidate",
                      {"incident_id": inc["id"],
                       "validated": detail["last_failure"]["evidence"]}, wid)
            return

        run, chosen_prompt, meta = chosen
        target = json.loads(json.dumps(cfg))
        target["prompt_version"] = chosen_prompt
        target = normalise_config(self.workflows[wid], target)
        dims = changed_dims(cfg, target)
        if not dims:
            return
        cells = plan_populations(self.an, wid, cfg, self.an.current_day,
                                 len(self.workflows[wid].get("cadence") or [1]))
        affected = sum(c["population"] for c in cells)
        detail = json.loads(inc["detail_json"])
        rate = float(detail.get("rate") or 0)
        threshold = float(detail.get("threshold") or 0.02)
        dry = {
            "method": "validated_prompt_revert",
            "incident": {"detected_minute": int(inc["detected_minute"]),
                         "observed_halluc_rate": rate, "threshold": threshold},
            "validation": {"validation_run_id": run["validation_run_id"],
                           "n": run["n"], "pass_rate": run["pass_rate"]},
            "note": "validation evidence is observed; engagement impact is not claimed as a counterfactual",
        }
        impact = min(1.0, 0.35 + max(0.0, rate - threshold) / 0.05 * 0.5
                     + max(0.0, float(run["pass_rate"]) - min_pass) * 5)
        summary = (f"Revert prompt {current_prompt} -> {chosen_prompt} after hallucination spike "
                   f"({rate:.1%} of recent connected calls, threshold {threshold:.1%})")
        rationale = (
            f"Detected {detail.get('hallucinated')} flagged calls in {detail.get('connected')} "
            f"recent connections ({rate:.2%}) for {wid}; safe baseline is {detail.get('safe_baseline', 0):.2%}. "
            f"Validated {chosen_prompt} at n={run['n']} with pass rate {run['pass_rate']:.3f} "
            f"(min {min_pass:.2f}). Chosen from {meta.get('source')} candidate; summary: "
            f"{meta.get('summary', '')}. This is a supervised revert, not an autonomous prompt rewrite."
        )
        diff = summarise_change(cfg, target)
        body = self._proposal_body(wid, target, summary, diff, affected, dry, impact,
                                   rationale, {"validation_run_id": run["validation_run_id"],
                                               "incident_id": inc["id"],
                                               "llm_suggestions": suggestions})
        prop = self._submit_approval(wid, body, "incident_prompt")
        self.prompt_last_attempt[wid] = self.now
        if prop:
            detail["fix_proposal_id"] = prop["proposal_id"]
            detail["fix_prompt"] = chosen_prompt
            detail["validation_run_id"] = run["validation_run_id"]
            self.store.update_incident(int(inc["id"]), "fix_pending", detail)
            self.active[(wid, "prompt_safety")] = self.store.incident(int(inc["id"])) or inc

    def _check_caller_health(self):
        for wid, wf in self.workflows.items():
            cfg = self._cfg(wid)
            options = list(wf["options"]["caller_id"])
            counts = {}
            total_d = total_c = 0
            for o in options:
                d, c = self.an.recent_pickup_counts(wid, o, self.now, CALLER_WINDOW_MIN)
                counts[o] = (d, c)
                total_d += d
                total_c += c
            if total_d < 150:
                continue
            global_rate = total_c / total_d if total_d else 0.5
            degraded = []
            for o, (d, c) in counts.items():
                if d < CALLER_MIN_DIALS:
                    continue
                r = c / d
                lo, hi = wilson(c, d)
                if r < 0.60 * global_rate and hi < 0.90 * global_rate:
                    degraded.append(o)
            inc = self.active.get((wid, "caller_id_health"))
            if not degraded:
                if inc and inc.get("status") in ("mitigating", "fix_pending"):
                    detail = json.loads(inc["detail_json"])
                    detail["resolved_minute"] = self.now
                    detail["mttr_minutes"] = self.now - int(inc["detected_minute"])
                    self.store.update_incident(int(inc["id"]), "fixed", detail)
                    self.active.pop((wid, "caller_id_health"), None)
                    self._log("incident_fixed", {"incident_id": inc["id"],
                                                 "mttr": detail["mttr_minutes"]}, wid)
                continue
            detail_rates = {o: {"dials": d, "connected": c,
                                "rate": round(c / max(d, 1), 4)}
                            for o, (d, c) in counts.items()}
            if inc is None:
                detail = {"degraded": degraded, "window_minutes": CALLER_WINDOW_MIN,
                          "global_rate": round(global_rate, 4),
                          "rates": detail_rates, "actions": []}
                iid = self.store.add_incident(wid, "caller_id_health", self.now, "detected", detail)
                inc = self.store.incident(iid)
                self.active[(wid, "caller_id_health")] = inc
                self._log("incident_detected", {"incident_id": iid, **detail}, wid)
            if self._pending_proposal(wid):
                continue
            if self.now - self.caller_last_action.get(wid, -10 ** 9) < 60:
                continue
            self.caller_last_action[wid] = self.now
            self._respond_caller_incident(wid, inc, degraded, detail_rates)

    def _respond_caller_incident(self, wid: str, inc: dict, degraded: list[str],
                                 rates: dict):
        wf = self.workflows[wid]
        cfg = self._cfg(wid)
        plan = find_caller_health(self.an, wf, wid, cfg, self.an.current_day,
                                  window=2, degraded_override=degraded)
        if not plan.get("weights"):
            self._log("caller_fix_no_weights", {"incident_id": inc["id"]}, wid)
            return
        target = json.loads(json.dumps(cfg))
        target["allocations"]["default"]["caller_id"] = plan["weights"]
        target = normalise_config(wf, target)
        dims = changed_dims(cfg, target)
        if not dims:
            return
        gov = wf.get("governance") or {}
        auto = gov.get("auto_apply") or {}
        allowed = set(auto.get("allowed_dims") or [])
        can_auto = bool(auto.get("enabled")) and set(dims) <= allowed and set(dims) <= {"schedule", "caller_id"}
        detail = json.loads(inc["detail_json"])
        detail["actions"] = detail.get("actions") or []
        cells = plan_populations(self.an, wid, cfg, self.an.current_day,
                                 len(wf.get("cadence") or [1]))
        affected = sum(c["population"] for c in cells)
        if can_auto:
            shifted = limit_shift(cfg, target, float(auto.get("max_shift") or 0.0))
            sdims = changed_dims(cfg, shifted)
            if not sdims:
                return
            diff = summarise_change(cfg, shifted)
            dry = {"method": "caller_health_shift",
                   "degraded": degraded,
                   "recent_rates": {o: rates.get(o, {}) for o in degraded},
                   "expected_pickup_direction": "up",
                   "note": "short-window pickup evidence; no randomised counterfactual"}
            impact = min(1.0, 0.4 + 0.6 * (1 - plan["weights"].get(degraded[0], 0)))
            summary = (f"Down-weight caller ID {', '.join(degraded)} after pickup collapse "
                       f"(auto-apply within policy max_shift)")
            rationale = (
                f"Recent {CALLER_WINDOW_MIN}-minute pickup for {', '.join(degraded)} is below "
                f"60% of the workflow rate. New target caps any DID's share and spreads the freed "
                f"traffic; unobserved DIDs are used only as spare capacity. Shifting within the "
                f"workflow's auto-apply max_shift; will re-check next hour and repeat if needed."
            )
            prop = self._try_auto(wid, shifted, summary, diff, affected, dry, impact,
                                  rationale, "incident_caller")
            if prop:
                detail["actions"].append({"minute": self.now, "kind": "auto",
                                          "proposal_id": prop["proposal_id"],
                                          "degraded": degraded})
                self.store.update_incident(int(inc["id"]), "mitigating", detail)
                self.active[(wid, "caller_id_health")] = self.store.incident(int(inc["id"])) or inc
                return
        # Approval path.  Do not submit more than one; approval latency is the
        # constraint and a second request would make the first stale.
        diff = summarise_change(cfg, target)
        dry = {"method": "caller_health_shift",
               "degraded": degraded,
               "recent_rates": {o: rates.get(o, {}) for o in degraded},
               "expected_pickup_direction": "up",
               "note": "short-window pickup evidence; no randomised counterfactual"}
        impact = min(1.0, 0.4 + 0.6 * (1 - plan["weights"].get(degraded[0], 0)))
        summary = f"Shift traffic away from caller ID {', '.join(degraded)} after pickup collapse"
        rationale = (
            f"Short-window pickup on {', '.join(degraded)} is below 60% of the workflow rate. "
            f"The proposal caps any DID's share and spreads freed traffic; unobserved DIDs are "
            f"not promoted unless they are needed as spare capacity. Requesting because this "
            f"workflow does not permit auto-apply for the required change."
        )
        body = self._proposal_body(wid, target, summary, diff, affected, dry, impact,
                                   rationale, {"incident_id": inc["id"],
                                               "degraded": degraded})
        prop = self._submit_approval(wid, body, "incident_caller")
        if prop:
            detail["actions"].append({"minute": self.now, "kind": "approval",
                                      "proposal_id": prop["proposal_id"],
                                      "degraded": degraded})
            self.store.update_incident(int(inc["id"]), "fix_pending", detail)
            self.active[(wid, "caller_id_health")] = self.store.incident(int(inc["id"])) or inc

    # ---------------------------------------------------------------- planning
    def maybe_daily_plan(self):
        day = self.now // DAY_MINUTES
        hour = (self.now % DAY_MINUTES) // 60
        if day < 0 or hour < ROUTINE_HOUR:
            return
        for wid, wf in self.workflows.items():
            key = f"plan_day:{wid}"
            if int(self.store.get_meta(key, -1)) >= day:
                continue
            if self._pending_proposal(wid):
                continue
            if self.now - self.supervisor_edit_minute.get(wid, -10 ** 9) < SUPERVISOR_QUIET_MIN:
                continue
            gov = wf.get("governance") or {}
            cap = int(gov.get("max_requests_per_24h", 0))
            # Reserve one slot for an incident (unless the cap is tiny).
            reserve = 1 if cap >= 2 else 0
            if self._requests_24h(wid) + 1 + reserve > cap:
                self.store.set_meta(key, day)
                continue
            try:
                target, ctx = self._plan_target(wid, wf)
            except Exception as exc:
                self._log("plan_failed", {"error": repr(exc)}, wid)
                continue
            if not target:
                self.store.set_meta(key, day)
                continue
            current = self._cfg(wid)
            dims = changed_dims(current, target)
            if not dims:
                self.store.set_meta(key, day)
                self._log("plan_no_change", {"dims": []}, wid)
                continue
            auto = gov.get("auto_apply") or {}
            allowed = set(auto.get("allowed_dims") or [])
            ops_only = bool(auto.get("enabled")) and set(dims) <= allowed and set(dims) <= {"schedule", "caller_id"}
            if ops_only:
                shifted = limit_shift(current, target, float(auto.get("max_shift") or 0.0))
                sdims = changed_dims(current, shifted)
                if sdims:
                    diff = summarise_change(current, shifted)
                    dry, affected = self._dry_run(wid, shifted)
                    impact = impact_score(dry)
                    summary = "Apply " + ", ".join(sdims) + " change learned from recent call outcomes"
                    rationale = self._rationale(wid, ctx, dry, "operational auto-apply")
                    prop = self._try_auto(wid, shifted, summary, diff, affected, dry, impact,
                                          rationale, "routine")
                    if prop:
                        self.store.set_meta(key, day)
                        continue
                # fall through to approval if auto was rejected
            if not self._within_approver_hours(wid):
                continue
            diff = summarise_change(current, target)
            dry, affected = self._dry_run(wid, target)
            impact = impact_score(dry)
            summary = self._summary(wid, dims, ctx)
            rationale = self._rationale(wid, ctx, dry, "supervised config change")
            body = self._proposal_body(wid, target, summary, diff, affected, dry, impact,
                                       rationale, self._evidence(ctx))
            prop = self._submit_approval(wid, body, "routine")
            if prop:
                self.store.set_meta(key, day)

    def _plan_target(self, wid: str, wf: dict) -> tuple[dict | None, dict]:
        cfg = self._cfg(wid)
        current_day = self.an.current_day
        findings = [f for f in find_variant_findings(self.an, wf, wid, cfg, current_day)
                    if not self._is_retired(wid, f["dim"], f["option"])]
        rollbacks = [f for f in find_rollbacks(self.an, wf, wid, cfg, current_day)
                     if not self._is_retired(wid, f["dim"], f["option"])]
        all_findings = findings + rollbacks
        caller_plan = find_caller_health(self.an, wf, wid, cfg, current_day)
        schedule = find_schedule(self.an, wf, wid, cfg, current_day)
        schedule_plan = {"default": schedule} if schedule else None
        target = plan_config(self.an, wf, wid, cfg, current_day, all_findings,
                             caller_plan, schedule_plan)
        ctx = {"findings": all_findings, "rollbacks": rollbacks,
               "caller": caller_plan, "schedule": bool(schedule)}
        return target, ctx

    def _dry_run(self, wid: str, target: dict) -> tuple[dict, int]:
        wf = self.workflows[wid]
        cadence_len = len(wf.get("cadence") or [1])
        pred = predict_effect(self.an, wf, wid, self._cfg(wid), target,
                              self.an.current_day, cadence_len)
        cells = plan_populations(self.an, wid, target, self.an.current_day, cadence_len)
        affected = sum(c["population"] for c in cells)
        return pred, int(affected)

    def _summary(self, wid: str, dims: list[str], ctx: dict) -> str:
        parts = []
        if ctx.get("rollbacks"):
            rb = ctx["rollbacks"][0]
            parts.append(f"roll back decaying {rb['dim']}={rb['incumbent']} to {rb['option']}")
        if any(f.get("kind") == "promote" for f in ctx.get("findings") or []):
            f = next(f for f in ctx["findings"] if f.get("kind") == "promote")
            parts.append(f"promote {f['dim']}={f['option']} for {f['where'] or 'all'} "
                         f"(+{f['rel']:.1%} relative, n={f['n_opt']})")
        if "caller_id" in dims:
            parts.append("rebalance caller IDs from recent pickup")
        if "schedule" in dims:
            parts.append("reweight dialling hours from learned pickup profile")
        if "cohorts" in dims:
            parts.append("add learned cohort rules")
        if not parts:
            parts.append("apply learned " + ", ".join(dims) + " change")
        return "; ".join(parts)

    def _rationale(self, wid: str, ctx: dict, dry: dict, boundary: str) -> str:
        parts = []
        if ctx.get("rollbacks"):
            r = ctx["rollbacks"][0]
            parts.append(f"rollback detected: {r['dim']}={r['incumbent']} now trails "
                         f"{r['option']} by {r['rel']:.1%} (n={r['n_opt']}).")
        for f in (ctx.get("findings") or []):
            if f.get("kind") != "promote":
                continue
            parts.append(f"{f['dim']}={f['option']} beats {f['incumbent']} by {f['rel']:.1%} "
                         f"in {f['where'] or 'the full population'} (n={f['n_opt']}, p={f['p']:.4f}); "
                         f"BH-FDR controlled across the workflow.")
            break
        if ctx.get("caller"):
            c = ctx["caller"]
            if c.get("degraded"):
                parts.append(f"caller IDs degraded: {', '.join(c['degraded'])}; "
                             f"traffic spread with per-DID caps.")
        if ctx.get("schedule"):
            parts.append("hour profile shows a material pickup improvement, so hour weights are updated.")
        parts.append(f"dry run: predicted {dry.get('primary')} lift {dry.get('predicted_lift', 0):+.2%} "
                     f"with guardrail delta {dry.get('guardrail_delta', 0):+.2%} "
                     f"(population {dry.get('population')}); method is a learned-segment replay, "
                     f"not a randomised counterfactual.")
        parts.append(f"This request is {boundary}; no customer-facing change is applied silently.")
        return " ".join(parts)

    def _evidence(self, ctx: dict) -> dict:
        ev = {"rollbacks": ctx.get("rollbacks") or [],
              "findings": ctx.get("findings") or []}
        if ctx.get("caller"):
            ev["caller"] = {k: v for k, v in ctx["caller"].items() if k != "rates"}
        return ev

    def maybe_decay_check(self):
        day = self.now // DAY_MINUTES
        hour = (self.now % DAY_MINUTES) // 60
        if day < 0 or hour not in (11, 17):
            return
        block = day * 2 + (0 if hour == 11 else 1)
        for wid, wf in self.workflows.items():
            key = f"decay_block:{wid}"
            if int(self.store.get_meta(key, -1)) >= block:
                continue
            if self._pending_proposal(wid):
                continue
            cfg = self._cfg(wid)
            rollbacks = find_rollbacks(self.an, wf, wid, cfg, self.an.current_day)
            if not rollbacks:
                self.store.set_meta(key, block)
                continue
            target = apply_rollbacks(self.an, wf, wid, cfg, rollbacks)
            dims = changed_dims(cfg, target)
            if not dims:
                self.store.set_meta(key, block)
                continue
            diff = summarise_change(cfg, target)
            dry, affected = self._dry_run(wid, target)
            impact = max(impact_score(dry), 0.5)
            r = rollbacks[0]
            summary = (f"Roll back decaying {r['dim']}={r['incumbent']} to {r['option']} "
                       f"({r['where'] or 'all'})")
            rationale = (
                f"Over the last {len(rollbacks)} findings, the promoted arm now trails an "
                f"alternative by at least 5% with p<0.10 and adequate exposure (n={r['n_opt']}). "
                f"This is a safety rollback to the configured control arm, submitted for approval."
            )
            body = self._proposal_body(wid, target, summary, diff, affected, dry, impact,
                                       rationale, {"rollbacks": rollbacks})
            prop = self._submit_approval(wid, body, "incident_decay")
            if prop:
                self.store.set_meta(key, block)
                for f in rollbacks:
                    self._retire(wid, f["dim"], f["incumbent"])
                self._log("decay_rollback_requested", {
                    "proposal_id": prop["proposal_id"], "rollbacks": rollbacks}, wid)

    # ------------------------------------------------------------------- loop
    def _record_metrics(self):
        if self.now - self.last_metric_minute < 60:
            return
        self.last_metric_minute = self.now
        try:
            self.store.set_meta("wall_beat", time.time())
        except Exception:
            pass
        try:
            st = self.client.state()
            self.credits = float(st.get("credits_remaining", self.credits))
        except ApiError:
            st = {"sim_minute": self.now}
        self.store.metric(self.now, "credits_remaining", self.credits)
        self.store.metric(self.now, "pending_proposals", len(self.store.pending_proposals()))
        self.store.metric(self.now, "active_incidents", len(self.active))
        self.store.metric(self.now, "sim_minute", self.now)
        self.store.metric(self.now, "call_events_per_workflow",
                          self.an.call_events / max(len(self.workflows), 1))

    def on_tick(self):
        self.reconcile_approvals()
        self.check_incidents()
        self.maybe_daily_plan()
        self.maybe_decay_check()
        self._record_metrics()

    def run(self):
        self.ingest_pending()
        self._refresh_dirty()
        self.reconcile_approvals()
        while not self.stop:
            st = self.client.state()
            self.st = st
            self.now = int(st.get("sim_minute", self.now))
            self.credits = float(st.get("credits_remaining", self.credits))
            if st.get("finished"):
                break
            self.ingest_pending()
            self._refresh_dirty()
            st = self.client.state()
            self.st = st
            self.now = int(st.get("sim_minute", self.now))
            if st.get("finished"):
                break
            self.on_tick()
            if self.stop:
                break
            try:
                res = self.client.advance(int(st.get("tick_minutes", self.tick)))
            except ApiError as e:
                if e.code == "finished":
                    break
                self._log("advance_failed", {"error": str(e)})
                time.sleep(0.5)
                continue
            self.now = int(res.get("sim_minute", self.now))
            self.st = res
            self._ingest_chunks(res.get("new_chunks") or [])
            if not res.get("new_chunks"):
                self.ingest_pending()
            self._refresh_dirty()
            if res.get("finished"):
                break
        self.finish()

    def finish(self):
        try:
            self.reconcile_approvals()
        except Exception:
            pass
        self._record_metrics()
        self._log("controller_stopped", {"sim_minute": self.now,
                                         "credits": self.credits,
                                         "active_incidents": len(self.active)})
        try:
            self.store.set_meta("last_sim_minute", self.now)
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass

    def request_stop(self, *_):
        self.stop = True
