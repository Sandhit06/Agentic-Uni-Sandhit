"""Durable state: SQLite is the controller's only stateful dependency.

Design points
-------------
* Every chunk is committed in one transaction together with the high-water
  mark.  A crash before commit means the chunk is replayed; a crash after
  commit means it is skipped.  Aggregates are never double counted.
* Leads and counters are small integer/short-string rows, so the store stays
  cheap at the load scenario's ~850k events/day.
* Proposals, approvals, config versions and audit records are permanent.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
  workflow TEXT NOT NULL,
  lead_idx INTEGER NOT NULL,
  combo INTEGER NOT NULL,
  campaign_idx INTEGER NOT NULL,
  person_idx INTEGER NOT NULL,
  PRIMARY KEY (workflow, lead_idx)
);
CREATE TABLE IF NOT EXISTS variant_stats (
  workflow TEXT NOT NULL, day INTEGER NOT NULL, dim TEXT NOT NULL,
  option TEXT NOT NULL, segment TEXT NOT NULL,
  connected INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workflow, day, dim, option, segment)
);
CREATE TABLE IF NOT EXISTS pickup_stats (
  workflow TEXT NOT NULL, day INTEGER NOT NULL, caller_id TEXT NOT NULL,
  segment TEXT NOT NULL,
  dials INTEGER NOT NULL DEFAULT 0, connected INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workflow, day, caller_id, segment)
);
CREATE TABLE IF NOT EXISTS hour_stats (
  workflow TEXT NOT NULL, day INTEGER NOT NULL, kind TEXT NOT NULL,
  value TEXT NOT NULL, hour INTEGER NOT NULL,
  dials INTEGER NOT NULL DEFAULT 0, connected INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workflow, day, kind, value, hour)
);
CREATE TABLE IF NOT EXISTS prompt_stats (
  workflow TEXT NOT NULL, day INTEGER NOT NULL, prompt TEXT NOT NULL,
  connected INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0,
  halluc INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workflow, day, prompt)
);
CREATE TABLE IF NOT EXISTS campaigns (
  workflow TEXT NOT NULL, campaign_idx INTEGER NOT NULL, campaign_id TEXT NOT NULL,
  day INTEGER NOT NULL, size INTEGER NOT NULL, created_minute INTEGER NOT NULL,
  config_version INTEGER NOT NULL,
  PRIMARY KEY (workflow, campaign_idx)
);
CREATE TABLE IF NOT EXISTS config_versions (
  workflow TEXT NOT NULL, version INTEGER NOT NULL, config_json TEXT NOT NULL,
  actor TEXT NOT NULL, applied_at_minute INTEGER,
  changed_dims TEXT, proposal_id TEXT, approval_id TEXT,
  PRIMARY KEY (workflow, version)
);
CREATE TABLE IF NOT EXISTS config_state (
  workflow TEXT PRIMARY KEY, current_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, base_version INTEGER NOT NULL,
  proposed_config TEXT NOT NULL, change_summary TEXT NOT NULL, diff_json TEXT NOT NULL,
  affected_leads INTEGER NOT NULL, dry_run_json TEXT NOT NULL, impact_score REAL NOT NULL,
  rationale TEXT NOT NULL, evidence_json TEXT, created_minute INTEGER NOT NULL,
  status TEXT NOT NULL, approval_id TEXT, kind TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_wf ON proposals(workflow, created_minute);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, proposal_id TEXT, workflow TEXT NOT NULL,
  status TEXT NOT NULL, created_minute INTEGER, decided_minute INTEGER,
  apply_by_minute INTEGER, expires_minute INTEGER,
  reasons_json TEXT, last_synced_minute INTEGER
);
CREATE INDEX IF NOT EXISTS ix_approvals_wf ON approvals(workflow, status);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sim_minute INTEGER NOT NULL, ts TEXT, workflow TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL, correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_wf ON audit(workflow, sim_minute);
CREATE INDEX IF NOT EXISTS ix_audit_corr ON audit(correlation_id);
CREATE TABLE IF NOT EXISTS exposure (
  workflow TEXT NOT NULL, dim TEXT NOT NULL, option TEXT NOT NULL,
  first_minute INTEGER NOT NULL,
  PRIMARY KEY (workflow, dim, option)
);
CREATE TABLE IF NOT EXISTS validation_runs (
  validation_run_id TEXT PRIMARY KEY,
  workflow TEXT NOT NULL, prompt_version TEXT NOT NULL, n INTEGER NOT NULL,
  pass_rate REAL NOT NULL, failures INTEGER, created_minute INTEGER,
  source TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow TEXT NOT NULL, kind TEXT NOT NULL, detected_minute INTEGER NOT NULL,
  status TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sim_minute INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL,
  labels_json TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- lifecycle -------------------------------------------------------
    def close(self):
        with self.lock:
            self.conn.commit()
            self.conn.close()

    def reset(self):
        """Wipe all state for a new world (same file, clean slate)."""
        with self.lock:
            names = [r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            self.conn.execute("PRAGMA foreign_keys=OFF")
            for n in names:
                self.conn.execute(f'DROP TABLE IF EXISTS "{n}"')
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except Exception:
            return r["value"]

    def set_meta(self, key: str, value: Any):
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)))
            self.conn.commit()

    # -- audit -----------------------------------------------------------
    def audit(self, sim_minute: int, ts: str | None, workflow: str | None, kind: str,
              detail: Any, correlation_id: str | None = None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit(sim_minute,ts,workflow,kind,detail_json,correlation_id) "
                "VALUES(?,?,?,?,?,?)",
                (sim_minute, ts, workflow, kind, json.dumps(detail, default=str), correlation_id))
            self.conn.commit()

    # -- chunk commit ----------------------------------------------------
    def commit_chunk(self, deltas: dict, meta_updates: dict):
        """Apply all aggregates for one chunk and advance the high-water mark."""
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                if deltas.get("leads"):
                    c.executemany(
                        "INSERT OR IGNORE INTO leads(workflow,lead_idx,combo,campaign_idx,person_idx) "
                        "VALUES(?,?,?,?,?)", deltas["leads"])
                if deltas.get("campaigns"):
                    c.executemany(
                        "INSERT OR IGNORE INTO campaigns(workflow,campaign_idx,campaign_id,day,size,"
                        "created_minute,config_version) VALUES(?,?,?,?,?,?,?)", deltas["campaigns"])
                if deltas.get("variant"):
                    c.executemany(
                        "INSERT INTO variant_stats(workflow,day,dim,option,segment,connected,engaged) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(workflow,day,dim,option,segment) DO UPDATE SET "
                        "connected=connected+excluded.connected, engaged=engaged+excluded.engaged",
                        deltas["variant"])
                if deltas.get("pickup"):
                    c.executemany(
                        "INSERT INTO pickup_stats(workflow,day,caller_id,segment,dials,connected) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(workflow,day,caller_id,segment) DO UPDATE SET "
                        "dials=dials+excluded.dials, connected=connected+excluded.connected",
                        deltas["pickup"])
                if deltas.get("hour"):
                    c.executemany(
                        "INSERT INTO hour_stats(workflow,day,kind,value,hour,dials,connected) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(workflow,day,kind,value,hour) DO UPDATE SET "
                        "dials=dials+excluded.dials, connected=connected+excluded.connected",
                        deltas["hour"])
                if deltas.get("prompt"):
                    c.executemany(
                        "INSERT INTO prompt_stats(workflow,day,prompt,connected,engaged,halluc) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(workflow,day,prompt) DO UPDATE SET "
                        "connected=connected+excluded.connected, engaged=engaged+excluded.engaged, "
                        "halluc=halluc+excluded.halluc", deltas["prompt"])
                if deltas.get("configs"):
                    c.executemany(
                        "INSERT OR REPLACE INTO config_versions(workflow,version,config_json,actor,"
                        "applied_at_minute,changed_dims,proposal_id,approval_id) "
                        "VALUES(?,?,?,?,?,?,?,?)", deltas["configs"])
                if deltas.get("exposure"):
                    c.executemany(
                        "INSERT OR IGNORE INTO exposure(workflow,dim,option,first_minute) VALUES(?,?,?,?)",
                        deltas["exposure"])
                if deltas.get("audit"):
                    c.executemany(
                        "INSERT INTO audit(sim_minute,ts,workflow,kind,detail_json,correlation_id) "
                        "VALUES(?,?,?,?,?,?)", deltas["audit"])
                for k, v in meta_updates.items():
                    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                              "SET value=excluded.value", (k, json.dumps(v, default=str)))
                c.commit()
            except Exception:
                c.rollback()
                raise

    # -- config ----------------------------------------------------------
    def save_config_version(self, workflow: str, version: int, config: dict, actor: str,
                            applied_at_minute: int | None, changed_dims: list,
                            proposal_id: str | None = None, approval_id: str | None = None):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO config_versions(workflow,version,config_json,actor,"
                "applied_at_minute,changed_dims,proposal_id,approval_id) VALUES(?,?,?,?,?,?,?,?)",
                (workflow, version, json.dumps(config), actor, applied_at_minute,
                 json.dumps(changed_dims), proposal_id, approval_id))
            self.conn.execute(
                "INSERT INTO config_state(workflow,current_version) VALUES(?,?) "
                "ON CONFLICT(workflow) DO UPDATE SET current_version=excluded.current_version",
                (workflow, version))
            self.conn.commit()

    def current_configs(self) -> dict[str, dict]:
        out = {}
        with self.lock:
            rows = self.conn.execute(
                "SELECT s.workflow, v.version, v.config_json, v.actor, v.applied_at_minute, "
                "v.changed_dims FROM config_state s JOIN config_versions v "
                "ON s.workflow=v.workflow AND s.current_version=v.version").fetchall()
        for r in rows:
            out[r["workflow"]] = {
                "version": r["version"], "config": json.loads(r["config_json"]),
                "actor": r["actor"], "applied_at_minute": r["applied_at_minute"],
                "changed_dims": json.loads(r["changed_dims"] or "[]"),
            }
        return out

    # -- proposals / approvals ------------------------------------------
    def save_proposal(self, p: dict):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO proposals(proposal_id,workflow,base_version,proposed_config,"
                "change_summary,diff_json,affected_leads,dry_run_json,impact_score,rationale,"
                "evidence_json,created_minute,status,approval_id,kind,notes) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["proposal_id"], p["workflow"], p["base_version"],
                 json.dumps(p["proposed_config"]), p["change_summary"], json.dumps(p["diff"]),
                 p["affected_leads"], json.dumps(p["dry_run"]), p["impact_score"],
                 p["rationale"], json.dumps(p.get("evidence")), p["created_minute"],
                 p["status"], p.get("approval_id"), p.get("kind"), p.get("notes")))
            self.conn.commit()

    def update_proposal(self, proposal_id: str, **kw):
        if not kw:
            return
        cols, vals = [], []
        for k, v in kw.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in ("proposed_config", "diff", "dry_run", "evidence") else v)
        vals.append(proposal_id)
        with self.lock:
            self.conn.execute(f"UPDATE proposals SET {', '.join(cols)} WHERE proposal_id=?", vals)
            self.conn.commit()

    def proposal(self, proposal_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return dict(r) if r else None

    def proposals_for(self, workflow: str, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE workflow=? ORDER BY created_minute DESC LIMIT ?",
                (workflow, limit)).fetchall()
        return [dict(r) for r in rows]

    def pending_proposals(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE status IN ('submitting','pending','approved') "
                "ORDER BY created_minute").fetchall()
        return [dict(r) for r in rows]

    def save_approval(self, a: dict):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO approvals(approval_id,proposal_id,workflow,status,"
                "created_minute,decided_minute,apply_by_minute,expires_minute,reasons_json,"
                "last_synced_minute) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (a.get("approval_id"), a.get("proposal_id"), a.get("workflow_id") or a.get("workflow"),
                 a.get("status"), a.get("created_minute"), a.get("decided_minute"),
                 a.get("apply_by_minute"), a.get("expires_minute"),
                 json.dumps(a.get("reasons") or []), a.get("last_synced_minute")))
            self.conn.commit()

    def approval(self, approval_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(r) if r else None

    def approvals_by_status(self, statuses: Iterable[str]) -> list[dict]:
        statuses = list(statuses)
        if not statuses:
            return []
        q = ",".join("?" for _ in statuses)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM approvals WHERE status IN ({q}) ORDER BY created_minute",
                statuses).fetchall()
        return [dict(r) for r in rows]

    # -- validation runs -------------------------------------------------
    def save_validation_run(self, run: dict, source: str = "controller"):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO validation_runs(validation_run_id,workflow,prompt_version,n,"
                "pass_rate,failures,created_minute,source) VALUES(?,?,?,?,?,?,?,?)",
                (run["validation_run_id"], run["workflow_id"], run["prompt_version"], run["n"],
                 run["pass_rate"], run.get("failures"), run.get("created_minute"), source))
            self.conn.commit()

    def validation_for(self, workflow: str, prompt_version: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM validation_runs WHERE workflow=? AND prompt_version=? "
                "ORDER BY created_minute DESC LIMIT 1", (workflow, prompt_version)).fetchone()
        return dict(r) if r else None

    # -- incidents / metrics --------------------------------------------
    def add_incident(self, workflow: str, kind: str, sim_minute: int, status: str, detail: dict):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO incidents(workflow,kind,detected_minute,status,detail_json) "
                "VALUES(?,?,?,?,?)", (workflow, kind, sim_minute, status, json.dumps(detail, default=str)))
            self.conn.commit()
            return cur.lastrowid

    def update_incident(self, incident_id: int, status: str, detail: dict | None = None):
        with self.lock:
            if detail is None:
                self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
            else:
                self.conn.execute("UPDATE incidents SET status=?, detail_json=? WHERE id=?",
                                  (status, json.dumps(detail, default=str), incident_id))
            self.conn.commit()

    def active_incidents(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM incidents WHERE status NOT IN ('fixed','resolved','dismissed') "
                "ORDER BY detected_minute").fetchall()
        return [dict(r) for r in rows]

    def incident(self, incident_id: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return dict(r) if r else None

    def requests_24h(self, workflow: str, now_minute: int) -> int:
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE workflow=? AND created_minute > ?",
                (workflow, now_minute - 1440)).fetchone()
        return int(r["n"] if r else 0)

    def metric(self, sim_minute: int, name: str, value: float, labels: dict | None = None):
        with self.lock:
            self.conn.execute("INSERT INTO metrics(sim_minute,name,value,labels_json) VALUES(?,?,?,?)",
                              (sim_minute, name, float(value), json.dumps(labels or {})))
            self.conn.commit()

    # -- read helpers used at startup / by the view ---------------------
    def iter_leads(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,lead_idx,combo,campaign_idx,person_idx "
                                    "FROM leads ORDER BY workflow,lead_idx")
            for r in cur:
                yield tuple(r)

    def iter_variant_stats(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,day,dim,option,segment,connected,engaged "
                                    "FROM variant_stats")
            for r in cur:
                yield tuple(r)

    def iter_pickup_stats(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,day,caller_id,segment,dials,connected "
                                    "FROM pickup_stats")
            for r in cur:
                yield tuple(r)

    def iter_hour_stats(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,day,kind,value,hour,dials,connected "
                                    "FROM hour_stats")
            for r in cur:
                yield tuple(r)

    def iter_prompt_stats(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,day,prompt,connected,engaged,halluc "
                                    "FROM prompt_stats")
            for r in cur:
                yield tuple(r)

    def iter_campaigns(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,campaign_idx,campaign_id,day,size,created_minute,"
                                    "config_version FROM campaigns")
            for r in cur:
                yield tuple(r)

    def iter_exposure(self):
        with self.lock:
            cur = self.conn.execute("SELECT workflow,dim,option,first_minute FROM exposure")
            for r in cur:
                yield tuple(r)

    def iter_approvals(self):
        with self.lock:
            cur = self.conn.execute("SELECT * FROM approvals")
            for r in cur:
                yield dict(r)

    def iter_proposals(self):
        with self.lock:
            cur = self.conn.execute("SELECT * FROM proposals")
            for r in cur:
                yield dict(r)

    def query_audit(self, workflow: str | None = None, start: int | None = None,
                    end: int | None = None, correlation_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM audit WHERE 1=1"
        args: list[Any] = []
        if workflow:
            q += " AND workflow=?"
            args.append(workflow)
        if start is not None:
            q += " AND sim_minute>=?"
            args.append(start)
        if end is not None:
            q += " AND sim_minute<=?"
            args.append(end)
        if correlation_id:
            q += " AND correlation_id=?"
            args.append(correlation_id)
        q += " ORDER BY sim_minute, id"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]
