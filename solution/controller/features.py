"""Feature encoding, candidate segments and cohort/rule helpers.

Everything here is derived from the public schema (lead events and workflow
metadata).  No scenario truth is read.
"""
from __future__ import annotations

from itertools import product

# Public feature values documented in docs/SIMULATOR.md and emitted on lead
# events.  The order is used only for compact encoding inside this process.
LANG = ["hi", "en", "ta", "te", "kn", "mr", "bn"]
CITY = ["1", "2", "3"]
AGE = ["18-30", "31-45", "46-60", "60+"]
VALUE = ["low", "mid", "high"]

FEATURES = {
    "lang": LANG,
    "city_tier": CITY,
    "age_band": AGE,
    "value_band": VALUE,
}

N_LANG = len(LANG)
N_CITY = len(CITY)
N_AGE = len(AGE)
N_VALUE = len(VALUE)
N_COMBO = N_LANG * N_CITY * N_AGE * N_VALUE


def encode(lang: str, city: str, age: str, value: str) -> int:
    """Encode four feature values into an int in [0, N_COMBO)."""
    li = LANG.index(lang) if lang in LANG else 0
    ci = CITY.index(city) if city in CITY else 0
    ai = AGE.index(age) if age in AGE else 0
    vi = VALUE.index(value) if value in VALUE else 0
    return ((li * N_CITY + ci) * N_AGE + ai) * N_VALUE + vi


def decode(combo: int) -> tuple[str, str, str, str]:
    vi = combo % N_VALUE
    combo //= N_VALUE
    ai = combo % N_AGE
    combo //= N_AGE
    ci = combo % N_CITY
    combo //= N_CITY
    li = combo % N_LANG
    return LANG[li], CITY[ci], AGE[ai], VALUE[vi]


def combo_features(combo: int) -> dict[str, str]:
    lang, city, age, value = decode(combo)
    return {"lang": lang, "city_tier": city, "age_band": age, "value_band": value}


# ---------------------------------------------------------------------------
# Candidate segments
# ---------------------------------------------------------------------------
# A segment is a conjunction of feature -> allowed values.  The candidate pool
# is deliberately small: it is the set of tests we are willing to pay a
# multiple-comparison penalty for.  It covers main effects on every feature and
# the interaction between age and language, which the two agent-facing
# dimensions most often depend on in outbound voice.
def candidate_segments(languages: list[str]) -> list[dict]:
    """Return [{id, where}] for a workflow whose language mix is known."""
    langs = [l for l in languages if l in LANG]
    if not langs:
        langs = LANG
    segs: list[dict] = [{"id": "all", "where": {}}]
    for a in AGE:
        segs.append({"id": f"age={a}", "where": {"age_band": [a]}})
    for l in langs:
        segs.append({"id": f"lang={l}", "where": {"lang": [l]}})
    for c in CITY:
        segs.append({"id": f"tier={c}", "where": {"city_tier": [c]}})
    for v in VALUE:
        segs.append({"id": f"value={v}", "where": {"value_band": [v]}})
    for a, l in product(AGE, langs):
        segs.append({"id": f"age={a}|lang={l}",
                     "where": {"age_band": [a], "lang": [l]}})
    return segs


def segment_of_combo(combo: int) -> dict[str, str]:
    return combo_features(combo)


def matches(where: dict, feats: dict[str, str]) -> bool:
    for feat, vals in where.items():
        if feats.get(feat) not in vals:
            return False
    return True


def combo_masks(segments: list[dict]) -> list[int]:
    """For each of the 252 feature combinations, a bitmask of matching segments."""
    out: list[int] = []
    for combo in range(N_COMBO):
        f = combo_features(combo)
        mask = 0
        for i, seg in enumerate(segments):
            if matches(seg["where"], f):
                mask |= 1 << i
        out.append(mask)
    return out


def iter_bits(mask: int):
    while mask:
        b = mask & -mask
        yield b.bit_length() - 1
        mask ^= b


def conditions_key(where: dict) -> str:
    """Stable label for a segment/cohort where-clause."""
    parts = []
    for feat in ("lang", "city_tier", "age_band", "value_band"):
        if feat in where:
            parts.append(f"{feat}=" + "|".join(sorted(str(v) for v in where[feat])))
    return ",".join(parts) or "all"


def specificity(where: dict) -> int:
    return sum(len(v) for v in where.values())
