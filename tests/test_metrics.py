"""Purchase/CPA field reading — offline, no network, no MCP.

Meta renamed the purchase fields to `omni_purchase` / `cost_per_omni_purchase`.
kill_sync.py read the old names, so every kill card reported "0 purch · CPA n/a"
while looking perfectly healthy. These tests pin both spellings and, just as
important, pin that "no data" stays n/a instead of collapsing into 0.

Run: python3 tests/test_metrics.py
"""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import kill_sync as ks

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")


# what Meta returns today
now = {"amount_spent": "$7.79 USD", "omni_purchase": 1,
       "cost_per_omni_purchase": "$7.79 USD", "name": "2 - 7R"}
check("current names -> purchases", ks.purchases_of(now), 1)
check("current names -> cpa", ks.cpa_of(now), 7.79)

# the older spelling still works
old = {"amount_spent": "$7.79 USD", "purchases": 1, "cost_per_purchase": "$7.79 USD"}
check("legacy names -> purchases", ks.purchases_of(old), 1)
check("legacy names -> cpa", ks.cpa_of(old), 7.79)

# both present (the bridge sometimes aliases) — must not double count
both = dict(now, purchases=1, cost_per_purchase="$7.79 USD")
check("both spellings -> purchases", ks.purchases_of(both), 1)

# THE REGRESSION: no purchase data at all must be None, never 0
missing = {"amount_spent": "$38.90 USD", "ctr": "1.34%", "cpc": "$0.97 USD"}
check("missing -> purchases is None not 0", ks.purchases_of(missing), None)
check("missing -> cpa is None", ks.cpa_of(missing), None)
check("missing -> printed as n/a", "n/a" in ks.ad_summary(missing), True)
check("missing -> not printed as 0", "0 purchase" in ks.ad_summary(missing), False)

# a real zero is data, and must survive as 0
zero = {"amount_spent": "$38.90 USD", "omni_purchase": 0}
check("real zero -> stays 0", ks.purchases_of(zero), 0)
check("real zero -> printed as 0", "0 purchase(s)" in ks.ad_summary(zero), True)

# CPA falls back to spend / purchases when Meta gives no cost field
nocpa = {"amount_spent": "$100.00 USD", "omni_purchase": 4}
check("cpa fallback", ks.cpa_of(nocpa), 25.0)

# archived rows carry no metrics at all and must not crash
check("empty row survives", ks.purchases_of({}), None)
ks.ad_summary({})
ks.ad_line({})

if fails:
    print("FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"ok — {12 - len(fails)} checks passed")
