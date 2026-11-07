"""A deliberately naive controller. It exists to show the API round-trip, not how to solve the problem.

What it does wrong (on purpose): ignores cohorts, ignores sample size and significance, uses
one KPI, pushes 100% of traffic to yesterday's best voice, ignores the decay of winners, never
reads history, and treats every rejection as final.

    python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/naive
    python examples/naive_controller.py --url http://127.0.0.1:8700
"""
import argparse
import copy
import json
import uuid
from collections import defaultdict

import httpx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url, timeout=120)
    wfs = {w["workflow_id"]: w for w in c.get("/workflows").json()}
    last_chunk = max([ch["chunk"] for ch in c.get("/events/chunks", params={"kind": "history"}).json()], default=-1)
    pending: dict[str, str] = {}  # workflow -> approval_id
    stats = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # wf -> voice -> [connected, engaged]

    while True:
        st = c.post("/sim/advance", json={"minutes": 60}).json()
        for ch in st["new_chunks"]:
            last_chunk = ch["chunk"]
            for line in c.get(f"/events/chunks/{ch['chunk']}").text.splitlines():
                ev = json.loads(line)
                if ev["type"] == "call" and ev["connected"]:
                    s = stats[ev["workflow_id"]][ev["variant"]["voice"]]
                    s[0] += 1
                    s[1] += ev["engaged"]
        # apply anything approved
        for wid, aid in list(pending.items()):
            ap_ = c.get(f"/approvals/{aid}").json()
            if ap_["status"] == "approved":
                r = c.post(f"/workflows/{wid}/apply", json={"approval_id": aid})
                print(st["ts"], wid, "apply", r.status_code, r.json())
                pending.pop(wid)
            elif ap_["status"] in ("rejected", "expired", "lapsed"):
                print(st["ts"], wid, ap_["status"], ap_.get("reasons"))
                pending.pop(wid)
        # once a day at 20:00, explore then exploit
        if st["ts"][11:16] == "20:00":
            for wid, w in wfs.items():
                if wid in pending:
                    continue
                cur = c.get(f"/workflows/{wid}/config").json()
                cfg = copy.deepcopy(cur["config"])
                voices = w["options"]["voice"]
                tried = {v: s for v, s in stats[wid].items() if s[0]}
                if len(tried) < len(voices):
                    cfg["allocations"]["default"]["voice"] = {v: 1 for v in voices}  # explore uniformly
                else:
                    best = max(tried, key=lambda v: tried[v][1] / tried[v][0])
                    cfg["allocations"]["default"]["voice"] = {best: 1}  # exploit, naively
                if cfg == cur["config"]:
                    continue
                body = {"proposal_id": str(uuid.uuid4()), "workflow_id": wid, "base_version": cur["version"],
                        "proposed_config": cfg, "change_summary": "change voice allocation",
                        "diff": {"allocations.default.voice": cfg["allocations"]["default"]["voice"]},
                        "affected_leads": 0, "dry_run": {"note": "none"}, "impact_score": 0.5,
                        "rationale": "best observed engagement"}
                r = c.post("/approvals", json=body)
                print(st["ts"], wid, "request", r.status_code, r.json().get("approval_id", r.json()))
                if r.status_code == 200:
                    pending[wid] = r.json()["approval_id"]
        if st["finished"]:
            break
    print("done:", c.get("/sim/state").json())


if __name__ == "__main__":
    main()
