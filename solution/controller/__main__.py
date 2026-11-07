"""Entry point: python -m submission.controller <SIM_URL> [options]."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from .engine import Controller
from .views import start_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Outcome-first simulator controller")
    parser.add_argument("sim_url", nargs="?", default=os.environ.get("SIM_URL"),
                        help="Base URL of the running simulator")
    parser.add_argument("--state-dir", default=os.environ.get("CONTROLLER_STATE_DIR"),
                        help="Durable controller state directory")
    parser.add_argument("--view-port", type=int,
                        default=int(os.environ.get("CONTROLLER_VIEW_PORT", "8081")),
                        help="Local supervisor/health HTTP port (0 disables)")
    args = parser.parse_args(argv)
    if not args.sim_url:
        print("usage: python -m submission.controller <SIM_URL>", file=sys.stderr)
        return 2

    state_dir = Path(args.state_dir) if args.state_dir else \
        Path(__file__).resolve().parent.parent / "state"
    controller = Controller(args.sim_url.rstrip("/"), state_dir)
    srv = None
    if args.view_port:
        srv = start_server(controller, args.view_port)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, controller.request_stop)
            except (AttributeError, ValueError, OSError):
                pass
        print(f"controller up scenario={controller.scenario} "
              f"sim_minute={controller.now} view_port={args.view_port} "
              f"state={state_dir}", flush=True)
        controller.run()
    finally:
        if srv is not None:
            srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
