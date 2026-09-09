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


checks = 0


def check(label, got, want):
    global checks
    checks += 1
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

# ── metrics added 2026-09-09 (CPA, outbound CTR, adds to cart, CPM, CPC,
#    ROAS, average conversion value) ──────────────────────────────────────────
row = {"name": "C3 - Video", "amount_spent": "$482.10 USD", "impressions": 40000,
       "clicks": 900, "ctr": "2.25%", "cpc": "$0.54 USD", "cpm": "$12.05 USD",
       "omni_purchase": 14, "cost_per_omni_purchase": "$34.44 USD",
       "outbound_clicks": 448, "outbound_clicks_ctr": "1.12%",
       "omni_add_to_cart": 38, "purchase_roas": "1.84",
       "omni_purchase_values": "$887.00 USD"}
check("outbound ctr", ks.outbound_ctr_of(row), 1.12)
check("adds to cart", ks.atc_of(row), 38)
check("cpm", ks.cpm_of(row), 12.05)
check("roas", ks.roas_of(row), 1.84)
check("revenue", ks.revenue_of(row), 887.0)
check("aov = revenue / purchases", round(ks.aov_of(row), 2), 63.36)
check("cpa from meta", ks.cpa_of(row), 34.44)

# old alias names still resolve
alias = {"adds_to_cart": 5, "purchases_conversion_value": "$100.00 USD",
         "omni_purchase": 2, "website_purchase_roas": "3.10"}
check("atc alias", ks.atc_of(alias), 5)
check("revenue alias", ks.revenue_of(alias), 100.0)
check("roas alias", ks.roas_of(alias), 3.10)

# missing stays n/a, never 0
bare = {"amount_spent": "$10.00 USD"}
for label, fn in (("octr", ks.outbound_ctr_of), ("atc", ks.atc_of), ("cpm", ks.cpm_of),
                  ("roas", ks.roas_of), ("revenue", ks.revenue_of), ("aov", ks.aov_of)):
    check(f"missing {label} is None", fn(bare), None)

# ── batch totals ────────────────────────────────────────────────────────────
a1 = {"amount_spent": "$100.00 USD", "omni_purchase": 1, "impressions": 10000,
      "outbound_clicks": 100, "clicks": 200, "omni_add_to_cart": 10,
      "omni_purchase_values": "$50.00 USD"}
a2 = {"amount_spent": "$300.00 USD", "omni_purchase": 9, "impressions": 30000,
      "outbound_clicks": 500, "clicks": 700, "omni_add_to_cart": 40,
      "omni_purchase_values": "$750.00 USD"}
t = ks.totals_of([a1, a2])
check("total spend", t["spend"], 400.0)
check("total purchases", t["purch"], 10)
check("total atc", t["atc"], 50)
# ratios must come from the sums, not from averaging the two ads
check("batch CPA = 400/10", t["cpa"], 40.0)
check("batch oCTR = 600/40000", round(t["octr"], 3), 1.5)
check("batch CPM = 400/40000*1000", t["cpm"], 10.0)
check("batch ROAS = 800/400", t["roas"], 2.0)
check("batch AOV = 800/10", t["aov"], 80.0)
check("source is ads", t["source"], "ads")

# Meta's own ad-set row wins over the summed ad rows
t2 = ks.totals_of([a1, a2], {"amount_spent": "$400.00 USD", "omni_purchase": 8,
                             "purchase_roas": "2.50", "omni_add_to_cart": 44,
                             "outbound_clicks_ctr": "1.40%", "impressions": 38000})
check("adset row wins on purchases", t2["purch"], 8)
check("adset row wins on roas", t2["roas"], 2.5)
check("adset row wins on octr", t2["octr"], 1.40)
check("source is adset", t2["source"], "adset")

# a batch with no metrics at all must not crash or invent zeros
t3 = ks.totals_of([{}, {}])
check("empty batch spend", t3["spend"], None)
check("empty batch cpa", t3["cpa"], None)
ks.ad_summary(row); ks.ad_line(row)

if fails:
    print("FAILED:")
    for x in fails:
        print("  -", x)
    sys.exit(1)
print(f"ok — {checks} checks passed")
