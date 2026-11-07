"""CLI.

  python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/mine [--port 8700]
  python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/baseline
  python -m sim grade    --run runs/mine --baseline runs/baseline [--out report.md]
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--scenario", required=True)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--force", action="store_true", help="wipe an existing run dir")
    b = sub.add_parser("baseline")
    b.add_argument("--scenario", required=True)
    b.add_argument("--run-dir", required=True)
    g = sub.add_parser("grade")
    g.add_argument("--run", required=True)
    g.add_argument("--baseline", required=True)
    g.add_argument("--out")
    g.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd in ("serve", "baseline"):
        rd = Path(a.run_dir)
        if rd.exists() and any(rd.iterdir()):
            if a.cmd == "baseline" or a.force:
                shutil.rmtree(rd)
            else:
                sys.exit(f"{rd} is not empty — a run dir holds exactly one run. Use --force to wipe it.")
    if a.cmd == "serve":
        import uvicorn
        from . import server
        t0 = time.time()
        print(f"generating world + {a.scenario} history…", flush=True)
        server.init_world(a.scenario, a.run_dir)
        print(f"ready in {time.time() - t0:.1f}s — http://{a.host}:{a.port}/docs", flush=True)
        uvicorn.run(server.app, host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "baseline":
        from .scenario import load_scenario
        from .world import World
        t0 = time.time()
        w = World(load_scenario(a.scenario), a.run_dir)
        while not w.state()["finished"]:
            w.advance(1440)
        print(f"baseline done in {time.time() - t0:.1f}s, {w.event_seq} events")
    elif a.cmd == "grade":
        from .grader import grade, to_markdown
        g = grade(a.run, a.baseline)
        out = json.dumps(g, indent=2, default=dict) if a.json else to_markdown(g)
        if a.out:
            Path(a.out).write_text(out)
        print(out)


if __name__ == "__main__":
    main()
