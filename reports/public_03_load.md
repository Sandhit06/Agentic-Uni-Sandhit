# public_03_load — controller throughput run

Run: `python solution/scripts/run_load.py` on a Windows laptop with a 12th Gen Intel Core i5-12450H (8 cores / 12 logical processors) and 16 GB physical RAM. Simulator and controller were separate processes. The controller ingested all history before advancing the live world.

| Measure | Result |
|---|---:|
| Scenario completion | 5 / 5 simulated days; finished at minute 7,200 |
| Attempted dials (D0–D4) | 3,240,000 (648,000 per day) |
| Historical events ingested | 5,537,379 |
| Live events ingested | 4,200,217 |
| Total events | 9,737,596 |
| Controller wall time | 531.42 s |
| Mean wall time per simulated day, including history bootstrap | 106.28 s |
| End-to-end event throughput, including history bootstrap | 18,324 events/s |
| Controller peak working set | 127.28 MB |
| Final event checkpoint | 9,737,596, matching simulator `last_event_id` |

Dial count is the sum of `pickup_stats.dials` for the `all` population segment and simulated days 0–4. Segment rows overlap by design, so summing all segment rows would multiply-count calls. Historical and live event counts come from the simulator's initial history high-water mark and final event ID. RSS is for the controller process and excludes simulator memory.

This run finished well below the 10-minute-per-day target on this machine. It is not a benchmark on the required 4-vCPU / 8-GB Linux target. The five-day mean includes the one-time history bootstrap. At 10×, SQLite write volume and the single-process sequential decision loop are expected to constrain throughput first; measure on the target host and concurrent workflow load before asserting a production ceiling.
