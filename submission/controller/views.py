"""Supervisor view and process health.

The consolidated view is built from committed SQLite rows, not from the
in-flight ingest buffers.  It deliberately shows the same candidate cohorts
that detection and experiment planning use.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _q(store, sql: str, args: tuple = ()) -> list[dict]:
    with store.lock:
        rows = store.conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _scalar(store, sql: str, args: tuple = (), default: Any = 0) -> Any:
    rows = _q(store, sql, args)
    if not rows:
        return default
    v = next(iter(rows[0].values()))
    return default if v is None else v


def _workflow_kpis(store, wid: str) -> dict:
    row = _q(store, "SELECT COALESCE(SUM(dials),0) AS d, COALESCE(SUM(connected),0) AS c "
                    "FROM pickup_stats WHERE workflow=? AND segment='all'", (wid,))
    d = row[0]["d"] if row else 0
    c = row[0]["c"] if row else 0
    row = _q(store, "SELECT COALESCE(SUM(connected),0) AS c, COALESCE(SUM(engaged),0) AS e "
                    "FROM variant_stats WHERE workflow=? AND dim='welcome_message' "
                    "AND segment='all'", (wid,))
    vc = row[0]["c"] if row else 0
    ve = row[0]["e"] if row else 0
    row = _q(store, "SELECT COALESCE(SUM(connected),0) AS c, COALESCE(SUM(engaged),0) AS e, "
                    "COALESCE(SUM(halluc),0) AS h FROM prompt_stats WHERE workflow=?", (wid,))
    pc = row[0]["c"] if row else 0
    pe = row[0]["e"] if row else 0
    ph = row[0]["h"] if row else 0
    uploaded = _scalar(store, "SELECT COALESCE(SUM(size),0) AS n FROM campaigns "
                              "WHERE workflow=? AND day>=0", (wid,))
    return {
        "leads_uploaded": int(uploaded),
        "dials": int(d),
        "connected": int(c),
        "pickup_rate": round(c / d, 4) if d else None,
        "engagement_rate": round(ve / vc, 4) if vc else None,
        "prompt_connected": int(pc),
        "prompt_engaged": int(pe),
        "prompt_halluc": int(ph),
    }


def _segment_stats(store, wid: str, segment: str) -> dict:
    row = _q(store, "SELECT COALESCE(SUM(dials),0) AS d, COALESCE(SUM(connected),0) AS c "
                    "FROM pickup_stats WHERE workflow=? AND segment=?", (wid, segment))
    d = row[0]["d"] if row else 0
    c = row[0]["c"] if row else 0
    row = _q(store, "SELECT COALESCE(SUM(connected),0) AS c, COALESCE(SUM(engaged),0) AS e "
                    "FROM variant_stats WHERE workflow=? AND dim='welcome_message' "
                    "AND segment=?", (wid, segment))
    vc = row[0]["c"] if row else 0
    ve = row[0]["e"] if row else 0
    return {
        "dials": int(d), "pickup_rate": round(c / d, 4) if d else None,
        "connected": int(vc),
        "engagement_rate": round(ve / vc, 4) if vc else None,
    }


def _alloc_for(cfg: dict, where: dict) -> dict:
    for c in cfg.get("cohorts") or []:
        if c.get("where") == where:
            return {k: v for k, v in (cfg["allocations"].get(c["id"]) or {}).items()}
    return {k: v for k, v in cfg["allocations"]["default"].items()}


def build_snapshot(controller) -> dict:
    store = controller.store
    now = controller.now
    last_beat = store.get_meta("wall_beat")
    snapshot = {
        "scenario": controller.scenario,
        "sim_minute": now,
        "sim_day": now // 1440,
        "credits_remaining": controller.credits,
        "finished": bool(controller.st.get("finished")),
        "last_event_id": int(store.get_meta("last_event_id", 0)),
        "last_history_chunk": int(store.get_meta("last_history_chunk", -1)),
        "last_event_chunk": int(store.get_meta("last_event_chunk", -1)),
        "wall_heartbeat_age_s": round(time.time() - last_beat, 1) if last_beat else None,
        "workflows": {},
        "governance": {},
        "audit_tail": _q(store, "SELECT sim_minute,ts,workflow,kind,detail_json,correlation_id "
                               "FROM audit ORDER BY id DESC LIMIT 20"),
    }
    for wid, wf in controller.workflows.items():
        cfg = controller._cfg(wid)
        gov = wf.get("governance") or {}
        cohorts = []
        for seg in controller.an.segments[wid]:
            cohorts.append({
                "segment_id": seg["id"],
                "where": seg["where"],
                "allocation": _alloc_for(cfg, seg["where"]),
                "observed": _segment_stats(store, wid, seg["id"]),
            })
        proposals = store.proposals_for(wid, limit=10)
        snapshot["workflows"][wid] = {
            "tenant_id": wf.get("tenant_id"),
            "objective": wf.get("objective"),
            "current_version": controller._version(wid),
            "current_actor": controller.current[wid].get("actor"),
            "prompt_version": cfg.get("prompt_version"),
            "config": cfg,
            "cohorts": cohorts,
            "kpis": _workflow_kpis(store, wid),
            "active_incidents": [i for i in controller.active.values() if i["workflow"] == wid],
            "recent_proposals": proposals,
            "governance": {
                "auto_apply": gov.get("auto_apply"),
                "approver_hours": gov.get("approver_hours"),
                "requests_24h": store.requests_24h(wid, now),
                "max_requests_per_24h": gov.get("max_requests_per_24h"),
            },
        }
        snapshot["governance"][wid] = snapshot["workflows"][wid]["governance"]
    return snapshot


def prometheus_text(controller) -> str:
    store = controller.store
    now = controller.now
    last_beat = store.get_meta("wall_beat")
    age = (time.time() - last_beat) if last_beat else None
    lines = [
        "# HELP outcome_loop_sim_minute Current simulated minute.",
        "# TYPE outcome_loop_sim_minute gauge",
        f"outcome_loop_sim_minute {now}",
        "# HELP outcome_loop_credits_remaining Simulator credits remaining.",
        "# TYPE outcome_loop_credits_remaining gauge",
        f"outcome_loop_credits_remaining {controller.credits}",
        "# HELP outcome_loop_pending_proposals Proposals not in a terminal state.",
        "# TYPE outcome_loop_pending_proposals gauge",
        f"outcome_loop_pending_proposals {len(store.pending_proposals())}",
        "# HELP outcome_loop_active_incidents Active incidents.",
        "# TYPE outcome_loop_active_incidents gauge",
        f"outcome_loop_active_incidents {len(controller.active)}",
        "# HELP outcome_loop_heartbeat_age_seconds Seconds since the last controller beat.",
        "# TYPE outcome_loop_heartbeat_age_seconds gauge",
        f"outcome_loop_heartbeat_age_seconds {age if age is not None else -1}",
    ]
    for wid in sorted(controller.workflows):
        k = _workflow_kpis(store, wid)
        labels = f'workflow="{wid}"'
        if k["pickup_rate"] is not None:
            lines.append(f"outcome_loop_pickup_rate{{{labels}}} {k['pickup_rate']}")
        if k["engagement_rate"] is not None:
            lines.append(f"outcome_loop_engagement_rate{{{labels}}} {k['engagement_rate']}")
        lines.append(f"outcome_loop_prompt_hallucinations{{{labels}}} {k['prompt_halluc']}")
    return "\n".join(lines) + "\n"


def start_server(controller, port: int = 8081) -> ThreadingHTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path in ("/health", "/healthz"):
                    beat = controller.store.get_meta("wall_beat")
                    body = json.dumps({
                        "status": "ok",
                        "scenario": controller.scenario,
                        "sim_minute": controller.now,
                        "finished": bool(controller.st.get("finished")),
                        "last_event_id": int(controller.store.get_meta("last_event_id", 0)),
                        "wall_heartbeat_age_s": round(time.time() - beat, 1) if beat else None,
                    }).encode()
                    self._send(200, body, "application/json")
                elif self.path == "/metrics":
                    self._send(200, prometheus_text(controller).encode(), "text/plain; version=0.0.4")
                elif self.path in ("/", "/view", "/supervisor"):
                    body = json.dumps(build_snapshot(controller), indent=2, default=str).encode()
                    self._send(200, body, "application/json")
                else:
                    self._send(404, b'{"error":"not_found"}', "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")

        def log_message(self, *_args):
            return

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return None
    t = threading.Thread(target=srv.serve_forever, name="supervisor-view", daemon=True)
    t.start()
    return srv
