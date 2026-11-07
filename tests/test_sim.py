"""Smoke tests for the simulator contract. Run: python -m pytest -q tests"""
import copy

import pytest

from sim.scenario import load_scenario
from sim.world import ApiError, World
from sim.grader import grade


@pytest.fixture()
def world(tmp_path):
    sc = load_scenario("scenarios/public_01_dev.yaml")
    sc["days"], sc["history_days"] = 2, 2
    return World(sc, tmp_path / "run")


def _body(w, wid, cfg, **kw):
    b = {"proposal_id": kw.pop("pid", "p1"), "workflow_id": wid, "base_version": w.get_config(wid)["version"],
         "proposed_config": cfg, "change_summary": "s", "diff": {"x": 1}, "affected_leads": 10,
         "dry_run": {"ok": True}, "impact_score": 0.3, "rationale": "r"}
    b.update(kw)
    return b


def test_advance_and_events(world):
    r = world.advance(60 * 12)
    assert r["new_chunks"] and world.event_seq > 0


def test_approval_flow_and_stale(world):
    wid = "nw_renewal"
    cfg = copy.deepcopy(world.get_config(wid)["config"])
    cfg["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    world.advance(600)  # D0 10:00
    a = world.request_approval(_body(world, wid, cfg))
    assert a["status"] == "pending"
    with pytest.raises(ApiError) as e:
        world.request_approval(_body(world, wid, cfg))
    assert e.value.code == "duplicate_proposal_id"
    while world.get_approval(a["approval_id"])["status"] == "pending":
        world.advance(15)
    st = world.get_approval(a["approval_id"])["status"]
    if st == "approved":
        v = world.apply_approval(wid, a["approval_id"])
        assert v["changed_dims"] == ["voice"]


def test_auto_apply_policy(world):
    cfg = copy.deepcopy(world.get_config("nw_renewal")["config"])
    cfg["allocations"]["default"]["voice"] = {"V2": 1}
    with pytest.raises(ApiError) as e:
        world.auto_apply("nw_renewal", {"proposal_id": "a1", "base_version": 1, "proposed_config": cfg,
                                        "rationale": "x"})
    assert e.value.status == 403
    cfg = copy.deepcopy(world.get_config("nw_renewal")["config"])
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    r = world.auto_apply("nw_renewal", {"proposal_id": "a2", "base_version": 1, "proposed_config": cfg,
                                        "rationale": "spread load"})
    assert r["changed_dims"] == ["caller_id"]


def test_hard_rules(world):
    cfg = copy.deepcopy(world.get_config("nw_renewal")["config"])
    cfg["schedule"]["hour_weights"] = {"default": {"7": 1}}
    with pytest.raises(ApiError) as e:
        world.request_approval(_body(world, "nw_renewal", cfg))
    assert e.value.status == 422


def test_prompt_change_needs_evidence_and_credits(world):
    world.advance(600)
    v = world.run_validation({"workflow_id": "nw_renewal", "prompt_version": "P3", "n": 400})
    assert 0 <= v["pass_rate"] <= 1 and world.credits == 196
    cfg = copy.deepcopy(world.get_config("nw_renewal")["config"])
    cfg["prompt_version"] = "P3"
    a = world.request_approval(_body(world, "nw_renewal", cfg, pid="p2"))
    assert world.approvals[a["approval_id"]]["_decision"] in ("approved", "rejected")


def test_grader_runs(world, tmp_path):
    while not world.state()["finished"]:
        world.advance(1440)
    g = grade(str(world.run_dir), str(world.run_dir))
    assert g["kpis"]["ALL"]["dials"] > 0
