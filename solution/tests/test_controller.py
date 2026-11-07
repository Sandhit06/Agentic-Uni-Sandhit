from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution.controller.analytics import Analytics, day_of_ts, minute_of_ts
from solution.controller.configs import (ConfigError, changed_dims,
                                           limit_shift, max_shift,
                                           normalise_config)
from solution.controller.decisions import (apply_rollbacks,
                                             find_caller_health,
                                             plan_populations)
from solution.controller.features import (N_COMBO, candidate_segments, decode,
                                            encode)
from solution.controller.stats import bh, holm, required_n, wilson, z_two_proportion
from solution.controller.store import Store


def workflow(wid: str = "wf") -> dict:
    return {
        "workflow_id": wid,
        "tenant_id": "tenant",
        "objective": "engagement_rate",
        "languages": ["en", "hi"],
        "dialling_window": [9, 19],
        "options": {
            "welcome_message": ["wm1", "wm2"],
            "voice": ["v1", "v2"],
            "caller_id": ["c1", "c2"],
            "prompt_version": ["p1", "p2"],
        },
        "default_config": {
            "cohorts": [],
            "allocations": {"default": {
                "welcome_message": {"wm1": 1.0},
                "voice": {"v1": 1.0},
                "caller_id": {"c1": 1.0},
            }},
            "prompt_version": "p1",
            "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
        },
        "governance": {"approver_hours": [10, 18], "max_requests_per_24h": 4,
                       "auto_apply": {"dims": ["caller_id"], "max_shift": 0.3}},
    }


class StatsTests(unittest.TestCase):
    def test_wilson_bounds(self):
        lo, hi = wilson(5, 10)
        self.assertLessEqual(lo, 0.5)
        self.assertGreaterEqual(hi, 0.5)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))

    def test_pickup_improvement_detected(self):
        z, p = z_two_proportion(60, 100, 40, 100)
        self.assertGreater(z, 0)
        self.assertLess(p, 0.01)

    def test_equal_arms_not_significant(self):
        z, p = z_two_proportion(50, 100, 50, 100)
        self.assertEqual(z, 0.0)
        self.assertAlmostEqual(p, 0.5)

    def test_bh_and_holm(self):
        self.assertEqual(bh([0.001, 0.2, 0.9]), [True, False, False])
        self.assertEqual(holm([0.001, 0.04, 0.9]), [True, False, False])

    def test_required_n(self):
        n = required_n(0.5, 0.55)
        self.assertGreater(n, 1000)
        self.assertEqual(required_n(0.5, 0.5), 10**9)


class FeatureTests(unittest.TestCase):
    def test_roundtrip(self):
        for v in (0, 1, 71, N_COMBO - 1):
            self.assertEqual(encode(*decode(v)), v)

    def test_segments_unique_and_mask_shape(self):
        segs = candidate_segments(["en", "hi"])
        ids = [s["id"] for s in segs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "all")
        self.assertIn("age=18-30|lang=en", ids)
        self.assertLessEqual(len(segs), 40)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.wf = workflow()
        self.cfg = normalise_config(self.wf, self.wf["default_config"])

    def test_normalise_and_diff(self):
        target = json.loads(json.dumps(self.cfg))
        target["allocations"]["default"]["voice"] = {"v1": 0.6, "v2": 0.4}
        target2 = normalise_config(self.wf, target)
        self.assertEqual(changed_dims(self.cfg, target2), ["voice"])
        self.assertAlmostEqual(max_shift(self.cfg, target2), 0.4, places=6)

    def test_limit_shift_respects_policy(self):
        target = json.loads(json.dumps(self.cfg))
        target["allocations"]["default"]["voice"] = {"v2": 1.0}
        target2 = normalise_config(self.wf, target)
        limited = limit_shift(self.cfg, target2, 0.2)
        self.assertLessEqual(max_shift(self.cfg, limited), 0.2 + 1e-6)
        self.assertLess(max_shift(self.cfg, limited), 1.0)

    def test_rejects_unknown_feature(self):
        bad = json.loads(json.dumps(self.cfg))
        bad["cohorts"] = [{"id": "x", "where": {"nope": ["a"]}}]
        with self.assertRaises(ConfigError):
            normalise_config(self.wf, bad)

    def test_rejects_bad_weight(self):
        bad = json.loads(json.dumps(self.cfg))
        bad["allocations"]["default"]["voice"] = {"v1": -1, "v2": 1}
        with self.assertRaises(ConfigError):
            normalise_config(self.wf, bad)


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.wf = workflow()
        self.an = Analytics({self.wf["workflow_id"]: self.wf})

    def test_timestamp_helpers(self):
        self.assertEqual(day_of_ts("2027-01-04T00:00:00+05:30"), 0)
        self.assertEqual(minute_of_ts("2027-01-05T12:30:00+05:30"), 2190)

    def test_observe_call_and_counts(self):
        wf = self.wf
        wid = wf["workflow_id"]
        an = self.an
        an.observe({"type": "lead", "workflow_id": wid, "lead_id": f"{wid}:l0",
                    "campaign_id": f"{wid}:c0", "person_id": f"{wid}:p0",
                    "ts": "2027-01-04T10:00:00+05:30",
                    "features": {"lang": "en", "city_tier": "1",
                                 "age_band": "18-30", "value_band": "mid"}})
        for i in range(10):
            an.observe({"type": "call", "workflow_id": wid, "lead_id": f"{wid}:l0",
                        "ts": f"2027-01-04T10:{i:02d}:00+05:30",
                        "connected": i < 6, "engaged": i < 3,
                        "variant": {"welcome_message": "wm1", "voice": "v1",
                                    "caller_id": "c1", "prompt_version": "p1"},
                        "qa": {"hallucination_flag": i == 0}})
        an.current_day = 0
        seg = an.label_to_idx[wid]["all"]
        self.assertEqual(an.d_pickup[(wid, 0, "c1")][2 * seg:2 * seg + 2], [10, 6])
        self.assertEqual(an.d_variant[(wid, 0, "welcome_message", "wm1")][2 * seg:2 * seg + 2],
                         [6, 3])
        self.assertEqual(an.d_prompt[(wid, 0, "p1")], [6, 3, 1])
        d, con = an.recent_pickup_counts(wid, "c1", 10 * 60, 60)
        self.assertEqual((d, con), (10, 6))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_config_and_proposal_roundtrip(self):
        cfg = normalise_config(workflow(), workflow()["default_config"])
        self.store.save_config_version("wf", 1, cfg, "seed", 0, [])
        cur = self.store.current_configs()["wf"]
        self.assertEqual(cur["version"], 1)
        p = {"proposal_id": "p1", "workflow": "wf", "base_version": 1,
             "proposed_config": cfg, "change_summary": "s", "diff": {},
             "affected_leads": 10, "dry_run": {}, "impact_score": 0.2,
             "rationale": "r", "created_minute": 100, "status": "pending",
             "kind": "routine"}
        self.store.save_proposal(p)
        self.assertEqual(len(self.store.pending_proposals()), 1)
        self.store.update_proposal("p1", status="applied")
        self.assertEqual(self.store.pending_proposals(), [])
        self.assertEqual(self.store.proposal("p1")["status"], "applied")

    def test_approval_budget_and_validation(self):
        for i in range(3):
            self.store.save_approval({"approval_id": f"a{i}", "proposal_id": f"p{i}",
                                      "workflow_id": "wf", "status": "applied",
                                      "created_minute": 1000 + i})
        self.assertEqual(self.store.requests_24h("wf", 1200), 3)
        self.assertEqual(self.store.requests_24h("wf", 3000), 0)
        self.store.save_validation_run({"validation_run_id": "v1", "workflow_id": "wf",
                                        "prompt_version": "p2", "n": 200,
                                        "pass_rate": 0.99, "failures": 2,
                                        "created_minute": 10})
        self.assertEqual(self.store.validation_for("wf", "p2")["pass_rate"], 0.99)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.wf = workflow()
        self.wid = "wf"
        self.cfg = normalise_config(self.wf, self.wf["default_config"])
        self.an = Analytics({self.wid: self.wf})

    def test_caller_prior_preserved_for_unobserved_did(self):
        prior = {"c1": 0.8, "c2": 0.2}
        cfg = json.loads(json.dumps(self.cfg))
        cfg["allocations"]["default"]["caller_id"] = dict(prior)
        cfg = normalise_config(self.wf, cfg)
        self.an.pickup[(self.wid, 0, "c1")] = [100, 70] + [0] * (2 * self.an.nseg[self.wid] - 2)
        plan = find_caller_health(self.an, self.wf, self.wid, cfg, 0,
                                  window=1, min_dials=10)
        self.assertIsNotNone(plan["weights"])
        self.assertAlmostEqual(plan["weights"]["c2"], 0.2, delta=0.02)

    def test_degraded_did_downweighted(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["allocations"]["default"]["caller_id"] = {"c1": 0.5, "c2": 0.5}
        cfg = normalise_config(self.wf, cfg)
        self.an.pickup[(self.wid, 0, "c1")] = [100, 70] + [0] * (2 * self.an.nseg[self.wid] - 2)
        self.an.pickup[(self.wid, 0, "c2")] = [100, 70] + [0] * (2 * self.an.nseg[self.wid] - 2)
        plan = find_caller_health(self.an, self.wf, self.wid, cfg, 0,
                                  window=1, min_dials=10,
                                  degraded_override=["c1"])
        self.assertIn("c1", plan["degraded"])
        self.assertLess(plan["weights"]["c1"], 0.1)

    def test_rollback_adds_cohort(self):
        rb = [{"dim": "voice", "where": {"age_band": ["18-30"]},
               "incumbent": "v1", "option": "v2"}]
        out = apply_rollbacks(self.an, self.wf, self.wid, self.cfg, rb)
        self.assertEqual(len(out["cohorts"]), 1)
        self.assertIn("v2", out["allocations"][out["cohorts"][0]["id"]]["voice"])

    def test_populations_partition_leads(self):
        wid = self.wid
        self.an.campaigns[wid][0] = {"campaign_id": f"{wid}:c0", "day": 0,
                                     "size": 2, "created_minute": 0,
                                     "config_version": 1}
        self.an._put_lead(wid, 0, encode("en", "1", "18-30", "mid"), 0, 0)
        self.an._put_lead(wid, 1, encode("hi", "2", "60+", "high"), 0, 1)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["cohorts"] = [{"id": "x", "where": {"age_band": ["18-30"]}}]
        cfg["allocations"]["x"] = {"voice": {"v2": 1.0}}
        cfg = normalise_config(self.wf, cfg)
        cells = plan_populations(self.an, wid, cfg, 0, 1)
        self.assertEqual(sum(c["population"] for c in cells), 2)
        self.assertEqual(cells[0]["population"], 1)
        self.assertEqual(cells[-1]["population"], 1)


if __name__ == "__main__":
    unittest.main()

