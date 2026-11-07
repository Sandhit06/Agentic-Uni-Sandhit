from __future__ import annotations
import ctypes, json, os, sqlite3, subprocess, sys, threading, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, 'PYTHONUTF8': '1'}
SCENARIO = 'scenarios/public_03_load.yaml'
PORT = '8710'
RUN = ROOT / 'runs' / 'solution_public03'
LOGS = ROOT / 'solution' / 'evidence'
STATE = ROOT / 'solution' / 'state' / 'public03'
server_log = (LOGS / 'public03-simulator.log').open('w')
server = subprocess.Popen([sys.executable, '-m', 'sim', 'serve', '--scenario', SCENARIO,
        '--run-dir', str(RUN), '--port', PORT, '--force'], cwd=ROOT,
    stdout=server_log, stderr=subprocess.STDOUT, env=ENV)
try:
    for _ in range(120):
        if server.poll() is not None: raise RuntimeError('load simulator exited early')
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=2).read()
            state = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=2).read())
            if state.get('scenario') == 'public_03_load':
                initial_event_id = int(state.get('last_event_id', 0))
                break
        except Exception: time.sleep(.5)
    else: raise RuntimeError('load simulator did not become healthy')
    log = (LOGS / 'public03-controller.log').open('w')
    proc = subprocess.Popen([sys.executable, '-m', 'solution.controller', f'http://127.0.0.1:{PORT}',
        '--state-dir', str(STATE), '--view-port', '0'], cwd=ROOT,
        stdout=log, stderr=subprocess.STDOUT, env=ENV)
    start = time.monotonic(); peak = 0; latest = {}; done = False
    if os.name == 'nt':
        from ctypes import wintypes
        class PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True); psapi = ctypes.WinDLL('psapi', use_last_error=True)
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, proc.pid)
        def rss():
            c = PMC(); c.cb = ctypes.sizeof(c)
            return int(c.WorkingSetSize) if psapi.GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb) else 0
    else:
        def rss():
            try: return int(Path(f'/proc/{proc.pid}/status').read_text().split('VmRSS:')[1].split()[0]) * 1024
            except Exception: return 0
    last_print = 0
    while proc.poll() is None:
        try:
            latest = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=2).read())
        except Exception: pass
        peak = max(peak, rss())
        elapsed = time.monotonic() - start
        if elapsed - last_print >= 10:
            print(f"elapsed={elapsed:.0f}s sim_minute={latest.get('sim_minute')} last_event_id={latest.get('last_event_id')} controller_rss_mb={peak/1048576:.1f}", flush=True)
            last_print = elapsed
        time.sleep(.5)
    log.close()
    duration = time.monotonic() - start
    if proc.returncode: raise RuntimeError(f'controller exited {proc.returncode}; inspect public03-controller.log')
    try: latest = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/sim/state', timeout=10).read())
    except Exception: pass
    final_event_id = int(latest.get('last_event_id', 0))
    db = sqlite3.connect(STATE / 'controller.sqlite')
    dials = int(db.execute("select coalesce(sum(dials),0) from pickup_stats where day between 0 and 4 and segment='all'").fetchone()[0])
    db.close()
    total_events = final_event_id
    result = {'scenario': latest.get('scenario'), 'sim_minute': latest.get('sim_minute'),
        'finished': latest.get('finished'), 'last_event_id': latest.get('last_event_id'),
        'elapsed_seconds': round(duration, 2), 'simulated_days': (latest.get('sim_minute') or 0)/1440,
        'average_wall_seconds_per_sim_day': round(duration/((latest.get('sim_minute') or 1)/1440), 2),
        'controller_peak_rss_mb': round(peak/1048576, 2), 'controller_pid': proc.pid,
        'initial_history_event_id': initial_event_id,
        'live_events': final_event_id - initial_event_id,
        'total_events': total_events,
        'events_per_second': round(total_events/duration, 2) if duration else None,
        'dials_d0_d4_all_segment': dials,
        'dials_per_simulated_day': round(dials/((latest.get('sim_minute') or 1)/1440))}
    (LOGS / 'public03-metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
finally:
    if 'proc' in locals() and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=15)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
    if 'log' in locals() and not log.closed: log.close()
    if server.poll() is None:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
    server_log.close()

