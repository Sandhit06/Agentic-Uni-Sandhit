from __future__ import annotations
import json, os, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, 'PYTHONUTF8': '1'}
SCENARIO = 'scenarios/public_02_incidents.yaml'
PORT = '8714'
RUN = ROOT / 'runs' / 'solution_crash_public02'
STATE = ROOT / 'solution' / 'state' / 'crash_public02'
EVIDENCE = ROOT / 'solution' / 'evidence'
server_log = (EVIDENCE / 'crash-simulator.log').open('w')
server = subprocess.Popen([sys.executable, '-m', 'sim', 'serve', '--scenario', SCENARIO,
    '--run-dir', str(RUN), '--port', PORT, '--force'], cwd=ROOT, stdout=server_log,
    stderr=subprocess.STDOUT, env=ENV)
proc = None
try:
    for _ in range(120):
        if server.poll() is not None: raise RuntimeError('simulator exited early')
        try:
            state = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=2).read())
            if state.get('scenario') == 'public_02_incidents': break
        except Exception: time.sleep(.25)
    else: raise RuntimeError('simulator not ready')
    controller_log = (EVIDENCE / 'crash-controller.log').open('w')
    proc = subprocess.Popen([sys.executable, '-m', 'solution.controller', f'http://127.0.0.1:{PORT}',
        '--state-dir', str(STATE), '--view-port', '0'], cwd=ROOT, stdout=controller_log,
        stderr=subprocess.STDOUT, env=ENV)
    killed_at = None
    while proc.poll() is None:
        try: state = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=2).read())
        except Exception: time.sleep(.02); continue
        if state.get('sim_minute', 0) >= 720 and not state.get('finished'):
            try:
                db = sqlite3.connect(STATE / 'controller.sqlite', timeout=.1)
                pending_rows = db.execute("select proposal_id,approval_id,status from proposals where status in ('pending','approved')").fetchall()
                db.close()
            except sqlite3.Error:
                pending_rows = []
            if pending_rows:
                killed_at = {**state, 'pending_proposals': pending_rows}
                proc.kill()
                proc.wait(timeout=10)
                break
        time.sleep(.02)
    if killed_at is None:
        raise RuntimeError('controller completed before a pending-approval kill point was observed')
    controller_log.close()
    time.sleep(.5)
    con = sqlite3.connect(STATE / 'controller.sqlite')
    meta = dict(con.execute('select key,value from meta').fetchall())
    pending_rows = con.execute("select proposal_id,approval_id,status from proposals where status in ('submitting','pending','approved')").fetchall()
    pending = len(pending_rows)
    checkpoint = int(meta.get('last_event_id', '0'))
    con.close()
    with (EVIDENCE / 'crash-restart-controller.log').open('w') as log:
        resumed = subprocess.run([sys.executable, '-m', 'solution.controller', f'http://127.0.0.1:{PORT}',
            '--state-dir', str(STATE), '--view-port', '0'], cwd=ROOT,
            stdout=log, stderr=subprocess.STDOUT, env=ENV)
    if resumed.returncode: raise RuntimeError(f'restarted controller exited {resumed.returncode}')
    final = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=10).read())
    con = sqlite3.connect(STATE / 'controller.sqlite')
    final_meta = dict(con.execute('select key,value from meta').fetchall())
    starts = con.execute("select count(*) from audit where kind='controller_started'").fetchone()[0]
    stops = con.execute("select count(*) from audit where kind='controller_stopped'").fetchone()[0]
    proposals = con.execute('select status,count(*) from proposals group by status').fetchall()
    con.close()
    result = {'scenario': final.get('scenario'), 'killed_midrun_sim_minute': killed_at.get('sim_minute'),
        'sim_minute_before_restart': int(meta.get('last_sim_minute', '0')),
        'last_event_id_checkpoint_before_restart': checkpoint, 'pending_proposals_at_restart': pending,
        'pending_approval_ids_at_restart': [r[1] for r in pending_rows if r[1]],
        'restart_exit_code': resumed.returncode, 'finished_after_restart': final.get('finished'),
        'final_sim_minute': final.get('sim_minute'), 'simulator_last_event_id': final.get('last_event_id'),
        'controller_last_event_id': int(final_meta.get('last_event_id', '0')),
        'controller_starts_recorded': starts, 'clean_stop_records': stops,
        'proposal_status_counts': dict(proposals)}
    (EVIDENCE / 'crash_recovery.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
finally:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
    if 'controller_log' in locals() and not controller_log.closed: controller_log.close()
    if server.poll() is None:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
    server_log.close()
