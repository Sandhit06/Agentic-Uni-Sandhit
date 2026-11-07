from __future__ import annotations
import os, subprocess, sys, time, urllib.request, json
ENV = {**os.environ, 'PYTHONUTF8': '1'}
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ALL_CASES = [('public01', 'scenarios/public_01_dev.yaml'), ('public02', 'scenarios/public_02_incidents.yaml')]
CASES = [c for c in ALL_CASES if len(sys.argv) == 1 or c[0] in sys.argv[1:]]
for name, scenario in CASES:
    baseline = ROOT / 'runs' / f'solution_baseline_{name}'
    run = ROOT / 'runs' / f'solution_{name}'
    reports = ROOT / 'solution' / 'reports'
    evidence = ROOT / 'solution' / 'evidence'
    baseline.mkdir(parents=True, exist_ok=True)
    with (evidence / f'{name}-baseline.log').open('w') as log:
        subprocess.run([sys.executable, '-m', 'sim', 'baseline', '--scenario', scenario,
                        '--run-dir', str(baseline)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, env=ENV)
    port = {'public01': '8712', 'public02': '8713'}[name]
    server_log = (evidence / f'{name}-simulator.log').open('w')
    server = subprocess.Popen([sys.executable, '-m', 'sim', 'serve', '--scenario', scenario,
        '--run-dir', str(run), '--port', port, '--force'], cwd=ROOT,
        stdout=server_log, stderr=subprocess.STDOUT)
    controller_log = (evidence / f'{name}-controller.log').open('w')
    try:
        ready = False
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError(f'{name}: simulator exited early')
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2).read()
                state = json.loads(urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/sim/state', timeout=2).read())
                if state.get('scenario') != Path(scenario).stem:
                    time.sleep(.25)
                    continue
                ready = True
                break
            except Exception:
                time.sleep(.5)
        if not ready:
            raise RuntimeError(f'{name}: simulator did not become healthy')
        state_dir = ROOT / 'solution' / 'state' / name
        proc = subprocess.run([sys.executable, '-m', 'solution.controller', f'http://127.0.0.1:{port}',
            '--state-dir', str(state_dir), '--view-port', '0'], cwd=ROOT,
            stdout=controller_log, stderr=subprocess.STDOUT, env=ENV)
        if proc.returncode:
            raise RuntimeError(f'{name}: controller exited {proc.returncode}; see {controller_log.name}')
        with (evidence / f'{name}-grade.log').open('w') as log:
            subprocess.run([sys.executable, '-m', 'sim', 'grade', '--run', str(run),
                '--baseline', str(baseline), '--out', str(reports / f'{name.replace("public", "public_")}.md')],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, env=ENV)
        print(f'{name}: complete; exit={proc.returncode}')
    finally:
        controller_log.close()
        if server.poll() is None:
            server.terminate()
            try: server.wait(timeout=10)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
        server_log.close()


