"""Audit timeline CLI: python -m submission.controller.audit_cli [options]."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import Store


def _minute_to_ts(minute: int) -> str:
    import datetime as dt
    base = dt.datetime(2027, 1, 4, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    return (base + dt.timedelta(minutes=int(minute))).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the controller audit timeline")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent.parent /
                                           "state" / "controller.sqlite"))
    parser.add_argument("--workflow")
    parser.add_argument("--kind")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store = Store(args.db)
    sql = "SELECT * FROM audit WHERE 1=1"
    vals: list = []
    if args.workflow:
        sql += " AND workflow=?"
        vals.append(args.workflow)
    if args.kind:
        sql += " AND kind=?"
        vals.append(args.kind)
    sql += " ORDER BY id DESC LIMIT ?"
    vals.append(args.limit)
    with store.lock:
        rows = [dict(r) for r in store.conn.execute(sql, tuple(vals))]
    store.close()

    for r in reversed(rows):
        try:
            detail = json.loads(r["detail_json"])
        except Exception:
            detail = r["detail_json"]
        if args.json:
            print(json.dumps({"sim_minute": r["sim_minute"],
                              "ts": _minute_to_ts(r["sim_minute"]),
                              "workflow": r["workflow"], "kind": r["kind"],
                              "detail": detail}, default=str))
        else:
            wf = r["workflow"] or "-"
            print(f"{_minute_to_ts(r['sim_minute'])}  {wf:14s} {r['kind']:28s} "
                  f"{json.dumps(detail, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
