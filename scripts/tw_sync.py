"""Triple Whale ad performance → ClickUp, one subtask per ad under its batch task.

Daily, after the US ad-account day ends. Every live batch (an ad in the batch's
own ad set spent in the last `recent_days`) gets one subtask per ad on its Media
task, carrying the "Aris" column set from Triple Whale (lifetime, Triple
Attribution; NC ROAS also under Last Click). The batch task carries the totals.

Each run overwrites the same fields and writes only values that changed. It
never comments, never appends and never changes a status: a subtask is created
in `subtask_status` (closed by default, so the two Media boards that show
subtasks as cards, and the status counts, leave them out) and stays there. The
Meta delivery state lives in the "Meta status" field instead.

A subtask is named exactly like its Meta ad. Ads that spent under `min_ad_spend` get no subtask but count in the batch
total. Only ads in the batch's OWN ad set (ad-set name starts with the batch code) are
synced. Copies in scaling CBOs carry generic ad-set names and are left out.

Run: python3 scripts/tw_sync.py [--dry-run] [--only S457 ...] [--limit N]
"""
from __future__ import annotations

import argparse, collections, datetime as dt, json, sys, time, urllib.error, urllib.request
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import kill_sync as ks

CFG = ks.CONFIG["tw_sync"]
TW_SQL = "https://api.triplewhale.com/api/v2/orcabase/api/sql"

USD = {"precision": 2, "currency_type": "USD", "default": None}
# key, ClickUp field name, type, type_config. Found by name on the Media list,
# created when missing. "Meta Ad ID" is the existing space-level field.
FIELDS = [
    ("ad_id", "Meta Ad ID", "short_text", {}),
    ("meta_status", "Meta status", "short_text", {}),
    ("spend", "TW · Spend", "currency", USD),
    ("roas", "TW · ROAS", "number", {}),
    ("ncroas_ta", "TW · NC ROAS (TA)", "number", {}),
    ("ncroas_lc", "TW · NC ROAS (LC)", "number", {}),
    ("nccpa", "TW · NC CPA (TA)", "currency", USD),
    ("ncp", "TW · NCP (TA)", "number", {}),
    ("ncv", "TW · NCV (TA)", "currency", USD),
    ("cv", "TW · CV", "currency", USD),
    ("cpm", "TW · CPM", "currency", USD),
    ("ctr", "TW · CTR %", "number", {}),
    ("obctr", "TW · OB CTR %", "number", {}),
    ("cpc", "TW · CPC", "currency", USD),
    ("asof", "TW as of", "date", {}),
]
KIND = {k: t for k, _, t, _ in FIELDS}
SUMS = ("spend", "impr", "clicks", "ob", "cv", "ncp", "ncv", "ncv_lc", "recent")


# ── Triple Whale ─────────────────────────────────────────────────────────────
def tw_sql(key: str, query: str, start: str, end: str) -> list[dict]:
    body = json.dumps({"shopId": CFG["shop"], "query": query,
                       "period": {"startDate": start, "endDate": end}}).encode()
    for attempt in range(3):
        req = urllib.request.Request(TW_SQL, data=body, method="POST", headers={
            "x-api-key": key, "content-type": "application/json", "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            return d if isinstance(d, list) else d.get("data") or []
        except urllib.error.HTTPError as e:
            if (e.code < 500 and e.code != 429) or attempt == 2:
                raise RuntimeError(f"Triple Whale SQL → HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 2:
                raise RuntimeError(f"Triple Whale SQL → {e!r}") from e
        time.sleep(5 * 2 ** attempt)
    raise AssertionError("unreachable")


def fetch_ads(key: str, until: dt.date) -> list[dict]:
    """Lifetime numbers per Meta ad. An alias must not repeat a column name: the
    API answers a shadowed column with a bare HTTP 400."""
    recent_from = (until - dt.timedelta(days=CFG["recent_days"] - 1)).isoformat()
    base = ("FROM pixel_joined_tvf WHERE event_date BETWEEN @startDate AND @endDate "
            "AND channel='facebook-ads' ")
    ta = tw_sql(key, (
        "SELECT ad_id, MAX(ad_name) AS name_, MAX(adset_name) AS adset_name_, "
        "argMax(ad_effective_status, event_date) AS status_, SUM(spend) AS spend_, "
        "SUM(impressions) AS impr_, SUM(clicks) AS clicks_, SUM(outbound_clicks) AS ob_, "
        "SUM(channel_reported_conversion_value) AS cv_, SUM(new_customer_orders) AS ncp_, "
        "SUM(new_customer_order_revenue) AS ncv_, "
        f"sumIf(spend, event_date >= '{recent_from}') AS recent_ "
        + base + "AND model='Triple Attribution' AND attribution_window='lifetime' "
        "GROUP BY ad_id HAVING spend_ > 0"), CFG["since"], until.isoformat())
    lc = tw_sql(key, (
        "SELECT ad_id, SUM(new_customer_order_revenue) AS ncv_ "
        + base + "AND model='Last Click' AND attribution_window='28_days' GROUP BY ad_id"),
        CFG["since"], until.isoformat())
    ncv_lc = {str(r["ad_id"]): r["ncv_"] or 0 for r in lc}
    return [{"ad_id": str(r["ad_id"]), "ad_name": (r["name_"] or "").strip(),
             "adset_name": (r["adset_name_"] or "").strip(), "status": r["status_"] or "",
             "spend": r["spend_"] or 0, "impr": r["impr_"] or 0, "clicks": r["clicks_"] or 0,
             "ob": r["ob_"] or 0, "cv": r["cv_"] or 0, "ncp": r["ncp_"] or 0, "ncv": r["ncv_"] or 0,
             "ncv_lc": ncv_lc.get(str(r["ad_id"]), 0), "recent": r["recent_"] or 0} for r in ta]


# ── values ───────────────────────────────────────────────────────────────────
def div(a, b, k=1.0):
    return round(a / b * k, 2) if b else None


def metrics(s: dict, asof_ms: int) -> dict:
    sp, im, cl = s["spend"], s["impr"], s["clicks"]
    return {"spend": round(sp, 2), "roas": div(s["cv"], sp), "ncroas_ta": div(s["ncv"], sp),
            "ncroas_lc": div(s["ncv_lc"], sp), "nccpa": div(sp, s["ncp"]), "ncp": s["ncp"],
            "ncv": round(s["ncv"], 2), "cv": round(s["cv"], 2), "cpm": div(sp, im, 1000),
            "ctr": div(cl, im, 100), "obctr": div(s["ob"], im, 100), "cpc": div(sp, cl),
            "asof": asof_ms}


def same(kind: str, old, new) -> bool:
    if old in (None, ""):
        return False
    if kind == "date":   # ClickUp moves the hour of a date field
        return abs(int(old) - int(new)) < 24 * 3600 * 1000
    if isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 0.005
    return str(old) == str(new)


# ── plan ─────────────────────────────────────────────────────────────────────
@dataclass
class Plan:
    creates: list[dict] = field(default_factory=list)   # {parent, name, status, fields: {field_id: value}}
    writes: list[tuple] = field(default_factory=list)   # (task_id, label, field_id, value)
    renames: list[tuple] = field(default_factory=list)  # (task_id, ad name): a renamed ad renames its subtask
    batches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def live_batches(ads: list[dict]) -> list[tuple[str, list[dict]]]:
    """Batch code → its own-ad-set ads, for batches that spent recently, most
    recent spend first."""
    groups = collections.defaultdict(list)
    for a in ads:
        code = ks.batch_prefix(a["adset_name"])
        if code:
            groups[code].append(a)
    live = [(c, g) for c, g in groups.items() if sum(a["recent"] for a in g) > 0]
    return sorted(live, key=lambda cg: -sum(a["recent"] for a in cg[1]))


def reconcile(batches: list[tuple[str, list[dict]]], tasks: list[dict], F: dict,
              asof_ms: int, status: str, min_spend: float = 1.0) -> Plan:
    """Desired state from Triple Whale vs the Media list as it is. Pure."""
    plan = Plan()
    parents = collections.defaultdict(list)
    by_ad = collections.defaultdict(list)
    for t in tasks:
        if t.get("parent"):
            v = next((f.get("value") for f in t.get("custom_fields", []) if f["id"] == F["ad_id"]), None)
            if v:
                by_ad[str(v)].append(t)
        elif ks.batch_prefix(t["name"]):
            parents[ks.batch_prefix(t["name"])].append(t)
    for ad_id, ts in by_ad.items():
        if len(ts) > 1:
            plan.notes.append(f"{len(ts)} subtasks carry Meta Ad ID {ad_id}; updating {ts[0]['id']} only")

    def diff(task, values, label):
        cur = {f["id"]: f.get("value") for f in task.get("custom_fields", [])}
        for k, v in values.items():
            if v is not None and not same(KIND[k], cur.get(F[k]), v):
                plan.writes.append((task["id"], label, F[k], v))

    for code, group in batches:
        hits = parents.get(code, [])
        if len(hits) != 1:
            plan.notes.append(f"{code}: {len(hits)} Media tasks named {code}, skipped")
            continue
        parent = hits[0]
        plan.batches.append(code)
        for a in sorted(group, key=lambda a: a["ad_name"]):
            if a["spend"] < min_spend:
                continue
            values = {"ad_id": a["ad_id"], "meta_status": a["status"] or None, **metrics(a, asof_ms)}
            del values["asof"]   # one date per batch, on the batch task: a daily write per ad buys nothing
            existing = by_ad.get(a["ad_id"])
            if existing:
                diff(existing[0], values, f"{code} {existing[0]['name']}")
                if existing[0]["name"] != a["ad_name"]:
                    plan.renames.append((existing[0]["id"], a["ad_name"]))
            else:
                plan.creates.append({"parent": parent["id"], "name": a["ad_name"], "status": status,
                                     "fields": {F[k]: v for k, v in values.items() if v is not None}})
        total = {k: sum(a[k] for a in group) for k in SUMS}
        diff(parent, metrics(total, asof_ms), code)
    return plan


# ── ClickUp ──────────────────────────────────────────────────────────────────
_calls: collections.deque = collections.deque()


def cu(method: str, path: str, body: dict | None = None) -> dict:
    """kill_sync.cu, paced under the token's 100 requests a minute: a first run
    makes over a thousand calls, and kill_sync.cu's three retries give up
    long before a 60-second window resets."""
    now = time.monotonic()
    while _calls and now - _calls[0] > 60:
        _calls.popleft()
    if len(_calls) >= CFG.get("max_calls_per_minute", 90):
        ks._SLEEP(60 - (now - _calls[0]) + 0.5)
    _calls.append(time.monotonic())
    return ks.cu(method, path, body)


def create_once(path: str, body: dict) -> dict:
    """A create is not retried: a timeout after ClickUp already stored the task
    made a retry add a second subtask (twice on the first run). The next run
    finds the ad by its Meta Ad ID and creates it only if it is missing."""
    saved, ks.CU_RETRIES = ks.CU_RETRIES, 0
    try:
        return cu("POST", path, body)
    finally:
        ks.CU_RETRIES = saved


def media_tasks(list_id: str) -> list[dict]:
    tasks, page = [], 0
    while True:
        d = cu("GET", f"/list/{list_id}/task?include_closed=true&subtasks=true&page={page}")
        tasks += d.get("tasks", [])
        if d.get("last_page", True) or not d.get("tasks"):
            return tasks
        page += 1


def ensure_fields(list_id: str, dry: bool) -> dict:
    have = {f["name"]: f["id"] for f in cu("GET", f"/list/{list_id}/field")["fields"]}
    ids = {}
    for key, name, typ, cfg in FIELDS:
        if name not in have:
            if dry:
                have[name] = f"new:{name}"
            else:
                r = cu("POST", f"/list/{list_id}/field", {"name": name, "type": typ, "type_config": cfg})
                have[name] = (r.get("field") or r)["id"]
        ids[key] = have[name]
    return ids


def apply(plan: Plan, list_id: str) -> list[str]:
    errors = []
    for c in plan.creates:
        try:
            create_once(f"/list/{list_id}/task", {"name": c["name"], "parent": c["parent"], "status": c["status"],
               "custom_fields": [{"id": k, "value": v} for k, v in c["fields"].items()]})
        except ks.ClickUpError as e:
            errors.append(f"create {c['name']}: {e}")
    for task_id, name in plan.renames:
        try:
            cu("PUT", f"/task/{task_id}", {"name": name})
        except ks.ClickUpError as e:
            errors.append(f"rename {name[:60]}: {e}")
    for task_id, label, fid, v in plan.writes:
        try:
            cu("POST", f"/task/{task_id}/field/{fid}", {"value": v})
        except ks.ClickUpError as e:
            errors.append(f"{label}: {e}")
    return errors


def alert(lines: list[str], dry: bool):
    print("\n".join(lines))
    if ks.WEBHOOK:
        ks._post_lines(lines, dry, mention=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", action="append", default=[], metavar="BATCH")
    ap.add_argument("--limit", type=int, help="at most N batches, most recent spend first")
    a = ap.parse_args(argv)
    key = ks.ENV.get("TRIPLE_WHALE_API_KEY", "")
    if not key:
        alert(["🟠 **tw-sync did not run**: TRIPLE_WHALE_API_KEY is not set, so the ClickUp ad numbers are a day older."], a.dry_run)
        return 1
    tz = ZoneInfo(CFG["timezone"])
    until = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    asof_ms = int(dt.datetime.combine(until, dt.time(12), tz).timestamp() * 1000)
    try:
        ads = fetch_ads(key, until)
    except RuntimeError as e:
        alert([f"🟠 **tw-sync failed**: {e}"], a.dry_run)
        return 1
    batches = [b for b in live_batches(ads) if not a.only or b[0] in a.only][:a.limit]
    list_id = CFG["media_list"]
    F = ensure_fields(list_id, a.dry_run)
    plan = reconcile(batches, media_tasks(list_id), F, asof_ms, CFG["subtask_status"], CFG["min_ad_spend"])
    print(f"tw-sync {until} · {len(ads)} ads in TW · {len(plan.batches)} live batches · "
          f"{len(plan.creates)} subtasks to create · {len(plan.renames)} to rename · "
          f"{len(plan.writes)} values to overwrite")
    for n in plan.notes:
        print("  note:", n)
    if a.dry_run:
        for c in plan.creates[:10]:
            print("  create", c["parent"], c["name"])
        for w in plan.writes[:10]:
            print("  write", w[1], w[2], w[3])
        return 0
    errors = apply(plan, list_id)
    print(f"done · {len(errors)} error(s)")
    if errors:
        alert([f"🟠 **tw-sync** finished with {len(errors)} ClickUp error(s); the next run retries them."]
              + [f"• {e[:180]}" for e in errors[:10]], False)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
