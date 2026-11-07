"""Scenario loading and static constants for the simulator.

A scenario YAML describes the world: tenants, workflows, dialling capacity,
lead batches, governance policy, and the hidden ground truth (effects,
prompt quality, caller-ID health, incidents). Candidates see the public
scenarios; graders run hidden ones with the same schema.
"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import yaml

# Lead features exposed to candidates (on `lead` events).
FEATURES: dict[str, list[str]] = {
    "lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"],
    "city_tier": ["1", "2", "3"],
    "age_band": ["18-30", "31-45", "46-60", "60+"],
    "value_band": ["low", "mid", "high"],
}

# Agent-facing dimensions that are randomised per call according to allocations.
AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
# Dimensions used in diffs / governance policy.
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]

# Hidden time-of-day pickup archetypes. Index = hour of day (0..23).
_ARCH = {
    "morning": [.2] * 8 + [1.0, 1.6, 1.6, 1.5, 1.0, 1.0, 0.9, 0.7, 0.7, 0.7, 0.6, 0.6, 0.6, 0.5, .2, .2],
    "lunch":   [.2] * 8 + [0.6, 0.8, 0.8, 1.0, 1.6, 1.6, 1.4, 0.9, 0.9, 0.8, 0.8, 0.8, 0.7, 0.6, .2, .2],
    "evening": [.2] * 8 + [0.5, 0.6, 0.6, 0.7, 0.8, 0.8, 0.8, 0.9, 1.0, 1.5, 1.6, 1.6, 1.5, 1.2, .2, .2],
    "flat":    [.2] * 8 + [1.0] * 14 + [.2, .2],
}
ARCHETYPES = list(_ARCH)
HOUR_PROFILE = np.array([_ARCH[a] for a in ARCHETYPES], dtype=np.float64)  # (4, 24)

MINUTES_PER_DAY = 1440


def parse_when(s: str | int) -> int:
    """'D3 14:30' -> sim minute. Integers are passed through."""
    if isinstance(s, int):
        return s
    day, hm = s.strip().split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * MINUTES_PER_DAY + int(h) * 60 + int(m)


def load_scenario(path: str | Path) -> dict:
    with open(path) as f:
        sc = yaml.safe_load(f)
    return normalise_scenario(sc)


def normalise_scenario(sc: dict) -> dict:
    sc = copy.deepcopy(sc)
    sc.setdefault("tick_minutes", 15)
    sc.setdefault("history_days", 14)
    sc.setdefault("credits", 300)
    sc.setdefault("effects", [])
    sc.setdefault("incidents", [])
    sc.setdefault("did_l0_per_dph", 30.0)
    sc.setdefault("did_tau_days", 2.0)
    assert MINUTES_PER_DAY % sc["tick_minutes"] == 0 and 60 % sc["tick_minutes"] == 0
    wf_ids = set()
    for t in sc["tenants"]:
        for wf in t["workflows"]:
            wf["tenant_id"] = t["id"]
            assert wf["id"] not in wf_ids, f"duplicate workflow {wf['id']}"
            wf_ids.add(wf["id"])
            wf.setdefault("min_gap_minutes", 120)
            wf.setdefault("mid_flight", "allowed")
            wf.setdefault("did_base_health", {})
            gov = wf.setdefault("governance", {})
            gov.setdefault("auto_apply", {"enabled": False, "allowed_dims": [], "max_shift": 0.0})
            gov.setdefault("approver_hours", [10, 19])
            gov.setdefault("approver_latency_minutes", [20, 240])
            gov.setdefault("proposal_ttl_hours", 24)
            gov.setdefault("apply_window_hours", 2)
            gov.setdefault("max_requests_per_24h", 4)
            gov.setdefault("base_reject_rate", 0.1)
            gov.setdefault("min_validation_pass_rate", 0.97)
            for p in wf["options"]["prompt_version"]:
                assert p in sc["prompts"], f"prompt {p} missing from catalog"
    for inc in sc["incidents"]:
        inc["at_minute"] = parse_when(inc["at"])
        assert inc["workflow"] in wf_ids
    for e in sc["effects"]:
        assert e["workflow"] in wf_ids or e["workflow"] == "*", e
        assert e["dim"] in ("welcome_message", "voice"), "effects are on welcome_message/voice"
    return sc


def all_workflows(sc: dict) -> list[dict]:
    return [wf for t in sc["tenants"] for wf in t["workflows"]]
