"""Event ingestion and the shared statistics used by detection, experiments
and the supervisor view.

The controller keeps one feature-combination code per lead and a compact
bitmask of candidate segments it matches.  That makes a call event O(number of
matching segments) with no string handling in the hot loop.
"""
from __future__ import annotations

import datetime as dt
import json
from array import array
from collections import defaultdict, deque
from typing import Iterable

from .features import (AGE, CITY, FEATURES, N_AGE, N_CITY, N_COMBO, N_LANG,
                       N_VALUE, candidate_segments, combo_features, combo_masks,
                       encode, iter_bits)

START_DATE = dt.date(2027, 1, 4)
_TZ_SUFFIX = "+05:30"


def suffix_int(s: str, marker: str) -> int | None:
    i = s.rfind(marker)
    if i < 0:
        return None
    try:
        return int(s[i + len(marker):])
    except ValueError:
        return None


def day_of_ts(ts: str) -> int:
    try:
        d = dt.date.fromisoformat(ts[:10])
        return (d - START_DATE).days
    except Exception:
        return 0


def minute_of_ts(ts: str) -> int:
    d = day_of_ts(ts)
    try:
        hh = int(ts[11:13])
        mm = int(ts[14:16])
    except Exception:
        hh = mm = 0
    return d * 1440 + hh * 60 + mm


def hour_of_ts(ts: str) -> int:
    try:
        return int(ts[11:13])
    except Exception:
        return 0


class Analytics:
    def __init__(self, workflows: dict[str, dict]):
        self.workflows = workflows
        self.wf_ids = list(workflows)
        self.segments: dict[str, list[dict]] = {}
        self.label_to_idx: dict[str, dict[str, int]] = {}
        self.masks: dict[str, list[int]] = {}
        for wid, wf in workflows.items():
            segs = candidate_segments(list(wf.get("languages") or []))
            self.segments[wid] = segs
            self.label_to_idx[wid] = {s["id"]: i for i, s in enumerate(segs)}
            self.masks[wid] = combo_masks(segs)
        self.nseg = {wid: len(self.segments[wid]) for wid in self.wf_ids}

        # leads, indexed by the workflow-local enrollment index
        self.lead_combo: dict[str, array] = {wid: array("H") for wid in self.wf_ids}
        self.lead_camp: dict[str, array] = {wid: array("I") for wid in self.wf_ids}
        self.lead_person: dict[str, array] = {wid: array("I") for wid in self.wf_ids}

        # counters (main / committed)
        self.variant: dict[tuple, list[int]] = {}
        self.pickup: dict[tuple, list[int]] = {}
        self.hour: dict[tuple, list[int]] = {}
        self.prompt: dict[tuple, list[int]] = {}
        self.campaigns: dict[str, dict[int, dict]] = {wid: {} for wid in self.wf_ids}
        self.exposure: dict[tuple, int] = {}
        self.recent_halluc: dict[tuple, deque] = {}
        self.recent_pickup: dict[tuple, deque] = {}

        # current-chunk deltas
        self.d_variant: dict[tuple, list[int]] = {}
        self.d_pickup: dict[tuple, list[int]] = {}
        self.d_hour: dict[tuple, list[int]] = {}
        self.d_prompt: dict[tuple, list[int]] = {}
        self.d_leads: list[tuple] = []
        self.d_campaigns: list[tuple] = []
        self.d_exposure: list[tuple] = []

        self.min_day = 0
        self.current_day = 0
        self.config_version_at_last_change: dict[str, int] = {}
        self.lead_events = 0
        self.call_events = 0
        self.history_events = 0

    # -- loading ---------------------------------------------------------
    def load_from_store(self, store):
        for wid, lead_idx, combo, camp_idx, person_idx in store.iter_leads():
            self._put_lead(wid, lead_idx, combo, camp_idx, person_idx)
        for wid, day, dim, option, seg, conn, eng in store.iter_variant_stats():
            i = self.label_to_idx.get(wid, {}).get(seg)
            if i is None:
                continue
            key = (wid, day, dim, option)
            arr = self.variant.get(key)
            if arr is None:
                arr = [0] * (2 * self.nseg[wid])
                self.variant[key] = arr
            arr[2 * i] += conn
            arr[2 * i + 1] += eng
        for wid, day, caller, seg, dials, conn in store.iter_pickup_stats():
            i = self.label_to_idx.get(wid, {}).get(seg)
            if i is None:
                continue
            key = (wid, day, caller)
            arr = self.pickup.get(key)
            if arr is None:
                arr = [0] * (2 * self.nseg[wid])
                self.pickup[key] = arr
            arr[2 * i] += dials
            arr[2 * i + 1] += conn
        for wid, day, kind, value, hour, dials, conn in store.iter_hour_stats():
            self.hour[(wid, day, kind, value, hour)] = [dials, conn]
        for wid, day, prompt, conn, eng, halluc in store.iter_prompt_stats():
            self.prompt[(wid, day, prompt)] = [conn, eng, halluc]
        for wid, idx, cid, day, size, created, ver in store.iter_campaigns():
            self.campaigns.setdefault(wid, {})[idx] = {
                "campaign_id": cid, "day": day, "size": size,
                "created_minute": created, "config_version": ver}
            self.min_day = min(self.min_day, day)
        for wid, dim, option, first in store.iter_exposure():
            self.exposure[(wid, dim, option)] = first

    def _put_lead(self, wid: str, idx: int, combo: int, camp: int, person: int):
        arr_c = self.lead_combo.setdefault(wid, array("H"))
        arr_p = self.lead_camp.setdefault(wid, array("I"))
        arr_e = self.lead_person.setdefault(wid, array("I"))
        if idx >= len(arr_c):
            grow = idx - len(arr_c) + 1
            arr_c.extend([0] * grow)
            arr_p.extend([0] * grow)
            arr_e.extend([0] * grow)
        arr_c[idx] = combo
        arr_p[idx] = camp
        arr_e[idx] = person

    # -- ingestion -------------------------------------------------------
    def begin_chunk(self):
        self.d_variant.clear()
        self.d_pickup.clear()
        self.d_hour.clear()
        self.d_prompt.clear()
        self.d_leads.clear()
        self.d_campaigns.clear()
        self.d_exposure.clear()

    def observe(self, ev: dict):
        t = ev.get("type")
        if t == "lead":
            self._observe_lead(ev)
        elif t == "call":
            self._observe_call(ev)
        elif t == "campaign_created":
            self._observe_campaign(ev)

    def _observe_lead(self, ev: dict):
        wid = ev["workflow_id"]
        if wid not in self.segments:
            return
        f = ev["features"]
        combo = encode(f.get("lang", "hi"), f.get("city_tier", "1"),
                       f.get("age_band", "18-30"), f.get("value_band", "low"))
        idx = suffix_int(ev["lead_id"], ":l")
        if idx is None:
            return
        cidx = suffix_int(ev.get("campaign_id", ""), ":c") or 0
        pidx = suffix_int(ev.get("person_id", ""), ":p") or 0
        self._put_lead(wid, idx, combo, cidx, pidx)
        self.d_leads.append((wid, idx, combo, cidx, pidx))
        self.lead_events += 1

    def _observe_campaign(self, ev: dict):
        wid = ev["workflow_id"]
        idx = suffix_int(ev.get("campaign_id", ""), ":c")
        if idx is None:
            return
        day = day_of_ts(ev["ts"])
        self.campaigns.setdefault(wid, {})[idx] = {
            "campaign_id": ev["campaign_id"], "day": day, "size": int(ev.get("leads", 0)),
            "created_minute": minute_of_ts(ev["ts"]),
            "config_version": int(ev.get("config_version_at_creation", 0))}
        self.d_campaigns.append((wid, idx, ev["campaign_id"], day, int(ev.get("leads", 0)),
                                 minute_of_ts(ev["ts"]),
                                 int(ev.get("config_version_at_creation", 0))))
        self.min_day = min(self.min_day, day)

    def _observe_call(self, ev: dict):
        wid = ev["workflow_id"]
        if wid not in self.segments:
            return
        idx = suffix_int(ev["lead_id"], ":l")
        if idx is None:
            return
        arr = self.lead_combo.get(wid)
        if arr is None or idx >= len(arr):
            return
        combo = arr[idx]
        mask = self.masks[wid][combo]
        if not mask:
            return
        ts = ev["ts"]
        day = day_of_ts(ts)
        hour = hour_of_ts(ts)
        connected = bool(ev.get("connected"))
        engaged = bool(ev.get("engaged"))
        var = ev.get("variant") or {}
        welcome = var.get("welcome_message")
        voice = var.get("voice")
        caller = var.get("caller_id")
        prompt = var.get("prompt_version")
        segs = list(iter_bits(mask))
        ns = self.nseg[wid]

        # pickup per caller x segment
        pkey = (wid, day, caller)
        pk = self.d_pickup.get(pkey)
        if pk is None:
            pk = [0] * (2 * ns)
            self.d_pickup[pkey] = pk
        for s in segs:
            pk[2 * s] += 1
            if connected:
                pk[2 * s + 1] += 1

        # engagement per variant x segment (denominator = connected)
        if connected:
            for pkey2 in ((wid, day, "welcome_message", welcome),
                          (wid, day, "voice", voice)):
                v = self.d_variant.get(pkey2)
                if v is None:
                    v = [0] * (2 * ns)
                    self.d_variant[pkey2] = v
                for s in segs:
                    v[2 * s] += 1
                    if engaged:
                        v[2 * s + 1] += 1

        # hour profile for scheduling: global, age and city
        feats = combo_features(combo)
        for kind, value in (("all", "all"), ("age", feats["age_band"]),
                            ("city", feats["city_tier"])):
            hkey = (wid, day, kind, value, hour)
            h = self.d_hour.get(hkey)
            if h is None:
                h = [0, 0]
                self.d_hour[hkey] = h
            h[0] += 1
            if connected:
                h[1] += 1

        # short rolling window for fast caller-ID health detection
        if caller:
            dq = self.recent_pickup.get((wid, caller))
            if dq is None:
                dq = deque(maxlen=5000)
                self.recent_pickup[(wid, caller)] = dq
            dq.append((minute_of_ts(ts), connected))

        # prompt safety and engagement
        if prompt:
            pr = self.d_prompt.get((wid, day, prompt))
            if pr is None:
                pr = [0, 0, 0]
                self.d_prompt[(wid, day, prompt)] = pr
            if connected:
                pr[0] += 1
                if engaged:
                    pr[1] += 1
                h = bool(ev.get("qa") and ev["qa"].get("hallucination_flag"))
                if h:
                    pr[2] += 1
                dq = self.recent_halluc.get((wid, prompt))
                if dq is None:
                    dq = deque(maxlen=400)
                    self.recent_halluc[(wid, prompt)] = dq
                dq.append((minute_of_ts(ts), h))

        # first exposure of agent-facing variants
        for dim, opt in (("welcome_message", welcome), ("voice", voice)):
            if not opt:
                continue
            key = (wid, dim, opt)
            if key not in self.exposure:
                m = minute_of_ts(ts)
                self.exposure[key] = m
                self.d_exposure.append((wid, dim, opt, m))

        self.call_events += 1
        self.current_day = max(self.current_day, day)

    # -- commit ----------------------------------------------------------
    def commit_chunk(self, store, meta_updates: dict):
        rows_v = []
        for (wid, day, dim, option), arr in self.d_variant.items():
            segs = self.segments[wid]
            for i, seg in enumerate(segs):
                c, e = arr[2 * i], arr[2 * i + 1]
                if c or e:
                    rows_v.append((wid, day, dim, option, seg["id"], c, e))
        rows_p = []
        for (wid, day, caller), arr in self.d_pickup.items():
            segs = self.segments[wid]
            for i, seg in enumerate(segs):
                d, c = arr[2 * i], arr[2 * i + 1]
                if d or c:
                    rows_p.append((wid, day, caller, seg["id"], d, c))
        rows_h = [(k[0], k[1], k[2], k[3], k[4], v[0], v[1])
                  for k, v in self.d_hour.items()]
        rows_pr = [(k[0], k[1], k[2], v[0], v[1], v[2])
                   for k, v in self.d_prompt.items()]
        store.commit_chunk({
            "leads": self.d_leads,
            "campaigns": self.d_campaigns,
            "variant": rows_v,
            "pickup": rows_p,
            "hour": rows_h,
            "prompt": rows_pr,
            "exposure": self.d_exposure,
        }, meta_updates)
        # merge into committed in-memory counters
        for key, arr in self.d_variant.items():
            base = self.variant.get(key)
            if base is None:
                self.variant[key] = list(arr)
            else:
                for i, v in enumerate(arr):
                    base[i] += v
        for key, arr in self.d_pickup.items():
            base = self.pickup.get(key)
            if base is None:
                self.pickup[key] = list(arr)
            else:
                for i, v in enumerate(arr):
                    base[i] += v
        for key, v in self.d_hour.items():
            base = self.hour.get(key)
            if base is None:
                self.hour[key] = list(v)
            else:
                base[0] += v[0]
                base[1] += v[1]
        for key, v in self.d_prompt.items():
            base = self.prompt.get(key)
            if base is None:
                self.prompt[key] = list(v)
            else:
                for i, x in enumerate(v):
                    base[i] += x
        # update min day from campaign deltas
        for row in self.d_campaigns:
            self.min_day = min(self.min_day, row[3])

    # -- queries ---------------------------------------------------------
    def days(self, window: int | None, current_day: int) -> range:
        if window is None:
            return range(self.min_day, current_day + 1)
        return range(max(self.min_day, current_day - window + 1), current_day + 1)

    def v_counts(self, wid: str, dim: str, option: str, seg: int,
                 current_day: int, window: int | None = None) -> tuple[int, int]:
        c = e = 0
        for day in self.days(window, current_day):
            arr = self.variant.get((wid, day, dim, option))
            if arr is not None:
                c += arr[2 * seg]
                e += arr[2 * seg + 1]
        return c, e

    def p_counts(self, wid: str, caller: str, seg: int,
                 current_day: int, window: int | None = None) -> tuple[int, int]:
        d = c = 0
        for day in self.days(window, current_day):
            arr = self.pickup.get((wid, day, caller))
            if arr is not None:
                d += arr[2 * seg]
                c += arr[2 * seg + 1]
        return d, c

    def h_counts(self, wid: str, kind: str, value: str, hour: int,
                 current_day: int, window: int | None = None) -> tuple[int, int]:
        d = c = 0
        for day in self.days(window, current_day):
            v = self.hour.get((wid, day, kind, value, hour))
            if v is not None:
                d += v[0]
                c += v[1]
        return d, c

    def prompt_counts(self, wid: str, prompt: str, current_day: int,
                      window: int | None = None) -> tuple[int, int, int]:
        conn = eng = hall = 0
        for day in self.days(window, current_day):
            v = self.prompt.get((wid, day, prompt))
            if v is not None:
                conn += v[0]
                eng += v[1]
                hall += v[2]
        return conn, eng, hall

    def recent_pickup_counts(self, wid: str, caller: str, now_minute: int,
                             window_minutes: int) -> tuple[int, int]:
        dq = self.recent_pickup.get((wid, caller))
        if not dq:
            return 0, 0
        keep = now_minute - window_minutes
        d = c = 0
        for m, conn in reversed(dq):
            if m < keep:
                break
            d += 1
            if conn:
                c += 1
        return d, c

    def recent_halluc_counts(self, wid: str, prompt: str, now_minute: int,
                             window_minutes: int) -> tuple[int, int]:
        dq = self.recent_halluc.get((wid, prompt))
        if not dq:
            return 0, 0
        keep = now_minute - window_minutes
        n = h = 0
        for m, hall in reversed(dq):
            if m < keep:
                break
            n += 1
            if hall:
                h += 1
        return n, h

    def prompt_conn(self, wid: str, current_day: int, window: int | None = None) -> int:
        total = 0
        for day in self.days(window, current_day):
            for (w, d, p), v in self.prompt.items():
                if w == wid and d == day:
                    total += v[0]
        return total

    def segment_population(self, wid: str, current_day: int,
                           cadence_len: int) -> list[int]:
        """Approximate active leads per candidate segment.

        Leads from campaigns created within the cadence length are still being
        worked; older ones are either concluded or unreachable.  This is used
        only for honest affected-lead counts and dry-run population sizes.
        """
        pop = [0] * self.nseg[wid]
        camps = self.campaigns.get(wid) or {}
        min_day = current_day - max(cadence_len, 1) + 1
        active_camps = {i for i, c in camps.items() if c["day"] >= min_day}
        arr_c = self.lead_combo.get(wid)
        arr_p = self.lead_camp.get(wid)
        if not arr_c or not active_camps:
            return pop
        masks = self.masks[wid]
        for i in range(len(arr_c)):
            if arr_p[i] in active_camps:
                mask = masks[arr_c[i]]
                while mask:
                    b = mask & -mask
                    pop[b.bit_length() - 1] += 1
                    mask ^= b
        return pop

    # -- convenience -----------------------------------------------------
    def latest_campaign_day(self, wid: str) -> int | None:
        camps = self.campaigns.get(wid) or {}
        return max((c["day"] for c in camps.values()), default=None)

    def exposure_minute(self, wid: str, dim: str, option: str) -> int | None:
        return self.exposure.get((wid, dim, option))
