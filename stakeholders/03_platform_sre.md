# From: Kabir Menon, Platform & SRE lead
**Subject: Running your loop in production — my non-negotiables and my wishes**

Non-negotiables:

- **It must survive being killed at any moment.** We deploy at 21:30, nodes get recycled, and things OOM. I
  want to see a kill/restart test, not a promise.
- **Idempotency everywhere.** If a retry or restart makes the controller apply a change twice, or lose an
  approval it was waiting on, that's a client incident.
- **Every alert must be actionable.** If it can wait until morning, it doesn't page. If it pages, the
  message tells me what to look at.

Wishes, with costs attached:

- We already run Postgres and a Redis. **I'd prefer no new stateful infrastructure.** If you add one, bring
  a paragraph on who operates it at 2 a.m.
- In production the clock doesn't wait for you, whatever the simulator does. Show me you can ingest and
  decide on **one simulated day of `public_03_load` in under 10 wall-clock minutes** on 4 vCPU / 8 GB, and
  tell me where it breaks at 10×.
- I want to see the loop's own health (ingest lag, decisions per hour, credit burn) without reading logs.
