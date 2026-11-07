# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.361 | +8.4% | 0.242 | +1.6% | 0.519 | +5.7% | 14% | 51 (47) |
| nw_kyc | 0.370 | +1.9% | 0.242 | -0.1% | 0.605 | +2.5% | 7% | 17 (21) |
| nw_renewal | 0.431 | +2.7% | 0.265 | +0.5% | 0.557 | +2.0% | 3% | 47 (42) |
| ALL | 0.386 | +4.8% | 0.250 | +0.9% | 0.549 | +3.6% | 8% | 115 (110) |

## Changes applied by the controller

6 applied — neutral: 2, beneficial: 4

- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['voice'] engageΔ=+0.070 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=+0.049 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 7,
  "decisions": {
    "approved": 6,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 6,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.053,
    "4": 0.057,
    "5": 0.054,
    "6": 0.058,
    "7": 0.055
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0