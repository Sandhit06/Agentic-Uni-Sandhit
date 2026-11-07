# Solution package

This folder contains the standalone implementation for the Formi Outcome-First Agent Loop assignment. It is based on the repository's existing `submission/controller` and includes the final guardrail gate and proposal-linked incident closure fixes. It uses only the simulator HTTP API for controller decisions; hidden scenario truth is not read.

## Run the controller

From the repository root, install the dependency and launch a simulator:

```powershell
python -m pip install -r solution/requirements.txt
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/solution01 --port 8700
```

In another terminal:

```powershell
./solution/run.ps1 http://127.0.0.1:8700
```

Linux entry point: `./solution/run.sh http://127.0.0.1:8700`. The controller runs until the scenario finishes. State defaults to `solution/state/`; set `CONTROLLER_STATE_DIR` to choose another directory. The supervisor view listens on port 8081 (`/`, `/health`, `/metrics`); `CONTROLLER_VIEW_PORT=0` disables it.

## Reproduce evidence

From the repository root:

```powershell
python solution/scripts/run_public.py
python solution/scripts/run_load.py
python solution/scripts/crash_recovery.py
```

Public grade tables are in `reports/public_01.md` and `reports/public_02.md`. The load and crash reports include measured limitations; the required 4-vCPU/8-GB Linux benchmark has not been run. To run the focused unit tests: `python -m pytest -q solution/tests`.

## Files

- `controller/`: HTTP client, event analytics, statistical decisions, SQLite store, governance/incident engine, audit CLI, supervisor/health view.
- `tests/`: unit tests for statistics, config validation, feature encoding, aggregation, persistence and decision logic.
- `scripts/`: reproducible public scenario, load, and crash-recovery runners.
- `reports/`: public_01 and public_02 grader outputs plus public_03 load measurements.
- `evidence/`: run logs and machine-readable load/crash evidence.
- `SUBMISSION.md`: detailed requirement analysis, scope decisions, stakeholder responses, measured outcomes and known weaknesses.
- `.gitignore`: excludes local controller SQLite state.
