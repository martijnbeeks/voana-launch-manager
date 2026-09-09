#!/usr/bin/env python3
"""Kill-sync: Meta ad/ad-set pauses -> ClickUp Launch Manager + Discord.

The Meta half runs inside a headless Claude session (scripts/kill_sync_prompt.md)
because the Meta MCP is the only Meta credential on this Mac. That session drops
JSON files into state/inbox/; this script does everything deterministic:

  process   read state/inbox/*.json, detect kills, update ClickUp, post Discord,
            append state/kills.jsonl, advance state/last_run.json
  digest    post the "batches complete without learnings" reminder
  --dry-run print what would happen, write nothing to ClickUp/Discord

Inbox contract (written by the Claude runbook, one file per account):
  state/inbox/kills_<account_id>.json   list of activity-log events, each:
      {"object_type": "ad"|"adset", "object_id": "...", "object_name": "...",
       "actor_name": "...", "application_name": "...", "datetime": "<as returned>",
       "old_value": 1, "new_value": 7}
  state/inbox/adset_metrics_<account_id>.json  optional, level=adset rows for the
                                        killed ad sets (Meta dedupes people
                                        across ads; we cannot). Missing file =
                                        fall back to summing the ad rows.
  state/inbox/metrics_<account_id>.json list of ad entities (level=ad) for the
      killed ads AND for every ad in a killed ad set, each with at least:
      id, name, adset_id, adset_name, effective_status, created_time,
      amount_spent, impressions, clicks, ctr, cpc, purchases, cost_per_purchase
Values may be the MCP's localized display strings ("$20.00 USD", "2,56%") —
they are parsed leniently.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
INBOX = STATE / "inbox"
CONFIG = json.loads((ROOT / "scripts" / "kill_sync_config.json").read_text())

CLICKUP = "https://api.clickup.com/api/v2"
UA = "voana-kill-sync/1.0"


# ── env / secrets ────────────────────────────────────────────────────────────
def load_env() -> dict:
    env = dict(os.environ)
    for p in (ROOT / ".env", ROOT.parent / "voana-tools" / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


ENV = load_env()
CLICKUP_KEY = ENV.get("CLICKUP_API_KEY", "")
WEBHOOK = ENV.get("KILL_SYNC_DISCORD_WEBHOOK_URL") or ""
WEBHOOK_IS_FALLBACK = False
if not WEBHOOK and ENV.get("DISCORD_WEBHOOK_URL"):
    WEBHOOK, WEBHOOK_IS_FALLBACK = ENV["DISCORD_WEBHOOK_URL"], True


# ── parsing helpers ──────────────────────────────────────────────────────────
def num(v) -> float | None:
    """'$78.40 USD' -> 78.4, '2,56%' -> 2.56, '1.234,5' -> 1234.5, None -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v).replace(" ", ""))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def count(v) -> int:
    """'3.410' / '3,410' / 3410 -> 3410 (thousands separators only, never decimals)."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else 0


def money(v) -> str:
    x = num(v)
    return "n/a" if x is None else f"${x:,.2f}"


def pct(v) -> str:
    x = num(v)
    return "n/a" if x is None else f"{x:.2f}%"


def _first(m: dict, *keys):
    """First key that Meta actually populated. None means "Meta said nothing"."""
    for k in keys:
        v = m.get(k)
        if v is not None and v != "":
            return v
    return None


# Every accessor below returns None when Meta reported nothing, and a real 0
# when Meta reported 0. Never collapse the two: a missing number that prints as
# 0 is a *wrong* result, it reads as real, and nobody spots it. That is exactly
# how "0 purch · CPA n/a" shipped on every kill card until 2026-09-09.
# Field names verified against ads_get_field_context on 2026-09-09; the second
# name in each pair is the older alias.

def purchases_of(m: dict) -> int | None:
    v = _first(m, "omni_purchase", "purchases")
    return count(v) if v is not None else None


def atc_of(m: dict) -> int | None:
    """Adds to cart — the offer/messaging signal: clicks but no cart means the
    ad worked and the page did not; carts but no purchase points at checkout."""
    v = _first(m, "omni_add_to_cart", "adds_to_cart")
    return count(v) if v is not None else None


def outbound_ctr_of(m: dict) -> float | None:
    """Outbound CTR — clicks that actually left Meta. Plain `ctr` counts every
    click including likes and profile taps, so it flatters a weak ad."""
    return num(_first(m, "outbound_clicks_ctr"))


def outbound_clicks_of(m: dict) -> int | None:
    v = _first(m, "outbound_clicks")
    return count(v) if v is not None else None


def cpm_of(m: dict) -> float | None:
    return num(_first(m, "cpm"))


def roas_of(m: dict) -> float | None:
    return num(_first(m, "purchase_roas", "website_purchase_roas"))


def revenue_of(m: dict) -> float | None:
    return num(_first(m, "omni_purchase_values", "purchases_conversion_value"))


def aov_of(m: dict) -> float | None:
    """Average conversion value. Known to be understated while the upsell
    tracking problem is open — read it relatively, not as absolute revenue."""
    rev, p = revenue_of(m), purchases_of(m)
    return rev / p if rev is not None and p else None


def cpa_of(m: dict) -> float | None:
    """Cost per purchase: Meta's own value when present, else spend / purchases."""
    x = num(_first(m, "cost_per_omni_purchase", "cost_per_purchase"))
    if x is not None:
        return x
    spend, purch = num(m.get("amount_spent")), purchases_of(m)
    return spend / purch if spend and purch else None


def ratio(v) -> str:
    x = num(v)
    return "n/a" if x is None else f"{x:.2f}"


def whole(v) -> str:
    return "n/a" if v is None else f"{int(v):,}"


def totals_of(ads: list[dict], adset_row: dict | None = None) -> dict:
    """Batch-level numbers.

    Prefers Meta's own ad-set row when the bridge supplied one, because Meta
    dedupes people across the ads in a set and we cannot.

    Falling back to the ad rows, ratios are RECOMPUTED from the summed totals —
    never averaged. The mean of per-ad CPAs is not the batch CPA, and averaging
    them would quietly misreport every batch whose ads spent unequally.
    """
    if adset_row:
        return {"spend": num(adset_row.get("amount_spent")), "purch": purchases_of(adset_row),
                "atc": atc_of(adset_row), "cpa": cpa_of(adset_row),
                "octr": outbound_ctr_of(adset_row), "cpm": cpm_of(adset_row),
                "cpc": num(adset_row.get("cpc")), "roas": roas_of(adset_row),
                "aov": aov_of(adset_row), "revenue": revenue_of(adset_row),
                "impr": count(adset_row.get("impressions")), "source": "adset"}

    def total(fn):
        vals = [v for v in (fn(a) for a in ads) if v is not None]
        return sum(vals) if vals else None

    spend = total(lambda a: num(a.get("amount_spent")))
    purch = total(purchases_of)
    rev = total(revenue_of)
    impr = total(lambda a: count(a.get("impressions")) if a.get("impressions") not in (None, "") else None)
    oclicks = total(outbound_clicks_of)
    clicks = total(lambda a: count(a.get("clicks")) if a.get("clicks") not in (None, "") else None)
    return {"spend": spend, "purch": purch, "atc": total(atc_of), "revenue": rev,
            "cpa": spend / purch if spend and purch else None,
            "octr": (oclicks / impr * 100) if oclicks is not None and impr else None,
            "cpm": (spend / impr * 1000) if spend and impr else None,
            "cpc": (spend / clicks) if spend and clicks else None,
            "roas": (rev / spend) if rev is not None and spend else None,
            "aov": (rev / purch) if rev is not None and purch else None,
            "impr": impr, "source": "ads"}


def parse_dt(s: str | None) -> dt.datetime | None:
    """Handles ISO ('2026-09-07T02:21:16-0400') and the MCP's localized
    '7-9-2026 om 03:14' (d-m-Y). Returns naive local-ish datetime."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.replace(tzinfo=None)
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})\D+(\d{1,2}):(\d{2})", s)
    if m:
        d_, mo, y, h, mi = map(int, m.groups())
        return dt.datetime(y, mo, d_, h, mi)
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        d_, mo, y = map(int, m.groups())
        return dt.datetime(y, mo, d_)
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", s)  # '7 sep 2026', '1 juli 2026'
    if m:
        d_, mon, y = m.groups()
        mo = MONTHS.get(mon[:3].lower())
        if mo:
            return dt.datetime(int(y), mo, int(d_))
    return None


def days_live(created: str | None, until: dt.datetime | None = None) -> int | None:
    c = parse_dt(created)
    if not c:
        return None
    u = until or dt.datetime.now()
    return max(0, (u - c).days)


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTHS.update({"mrt": 3, "mei": 5, "okt": 10})

BATCH_RE = re.compile(r"^(S\d{3}|#\d{3,4})\b")
CREATIVE_RE = re.compile(r"_(C\d+|V\d+)$")


def batch_prefix(name: str) -> str | None:
    m = BATCH_RE.match(name.strip())
    return m.group(1) if m else None


def creative_no(ad_name: str) -> str:
    m = CREATIVE_RE.search(ad_name.strip())
    return m.group(1) if m else ad_name.split(" - ")[-1]


def lander_code(ad_name: str) -> str:
    parts = [p.strip() for p in ad_name.split(" - ")]
    return parts[1] if len(parts) > 2 and parts[1] in {"OG", "7R", "COMP", "PG8", "P18", "P9A", "Comp"} else ""


# ── ClickUp ──────────────────────────────────────────────────────────────────
def cu(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        CLICKUP + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": CLICKUP_KEY, "Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


class ClickUpTasks:
    def __init__(self):
        # Launched batches are auto-moved by a ClickUp automation from the
        # Launch Manager list into the Media list (status "learning"), so kills
        # are matched against Media first and Launch Manager as a fallback.
        self.tasks: list[dict] = []
        for list_id in CONFIG["clickup"]["list_ids"].values():
            page = 0
            while True:
                d = cu("GET", f"/list/{list_id}/task?include_closed=true&subtasks=true&page={page}")
                self.tasks += d.get("tasks", [])
                if d.get("last_page", True) or not d.get("tasks"):
                    break
                page += 1

    def find(self, adset_name: str) -> tuple[dict | None, str]:
        """Exact name match first, then unique batch-prefix match."""
        exact = [t for t in self.tasks if t["name"].strip() == adset_name.strip()]
        if len(exact) == 1:
            return exact[0], "exact"
        pre = batch_prefix(adset_name)
        if pre:
            cands = [t for t in self.tasks if batch_prefix(t["name"]) == pre]
            if len(cands) == 1:
                return cands[0], "prefix"
            if len(cands) > 1:
                return None, f"ambiguous prefix {pre} ({len(cands)} tasks)"
        return None, "no match"

    @staticmethod
    def field_value(task: dict, field_id: str):
        for f in task.get("custom_fields", []):
            if f["id"] == field_id:
                v = f.get("value")
                if f["type"] == "drop_down" and v is not None:
                    for o in f.get("type_config", {}).get("options", []):
                        if o.get("orderindex") == v or o.get("id") == v:
                            return o.get("name")
                return v
        return None


SKIP_CLICKUP = os.environ.get("KILL_SYNC_SKIP_CLICKUP") == "1"


def cu_comment(task_id: str, text: str, dry: bool):
    if dry or SKIP_CLICKUP:
        print(f"    [dry] comment on {task_id}:\n" + "\n".join("      " + l for l in text.splitlines()))
        return
    cu("POST", f"/task/{task_id}/comment", {"comment_text": text, "notify_all": False})


def cu_set_field(task_id: str, field_id: str, value, dry: bool):
    if dry or SKIP_CLICKUP:
        print(f"    [dry] set field {field_id[:8]}… = {value!r}")
        return
    cu("POST", f"/task/{task_id}/field/{field_id}", {"value": value})


def cu_set_status(task_id: str, status: str, dry: bool):
    if dry or SKIP_CLICKUP:
        print(f"    [dry] task status -> {status}")
        return
    cu("PUT", f"/task/{task_id}", {"status": status})


# ── Discord ──────────────────────────────────────────────────────────────────
def discord(embeds: list[dict], content: str | None, dry: bool) -> bool:
    if not embeds and not content:
        return True
    if dry or not WEBHOOK:
        print("    [dry/no-webhook] discord:", json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False)[:1500])
        return bool(WEBHOOK)
    for i in range(0, max(1, len(embeds)), 10):
        payload = {"username": "Voana kill-sync", "embeds": embeds[i:i + 10]}
        if i == 0 and content:
            payload["content"] = content[:1900]
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as e:
            print(f"    discord HTTP {e.code}: {e.read()[:300]!r}")
            return False
    return True


def ads_manager_url(account: str, kind: str, obj_id: str) -> str:
    if kind == "adset":
        return f"https://www.facebook.com/adsmanager/manage/adsets/edit?act={account}&selected_adset_ids={obj_id}"
    return f"https://www.facebook.com/adsmanager/manage/ads/edit?act={account}&selected_ad_ids={obj_id}"


def task_url(task: dict | None) -> str:
    return f"https://app.clickup.com/t/{task['id']}" if task else ""


# ── ClickUp metric reporting (Result field + checklist) ──────────────────────
CHECKLIST_NAME = "Ad performance"
AD_LINE_RE = re.compile(r"^(C\d+|V\d+)\b")


def ad_line(m: dict, killed_on: dt.datetime | None = None) -> str:
    """One compact line per creative for the Result field / checklist item:
    'C3 ✖09-09 · $38.90 · 0 purch · CPA n/a · CTR 1.34% · CPC $0.97 · 2,977 impr · 2d'"""
    purch, cpa = purchases_of(m), cpa_of(m)
    mark = f"✖{killed_on:%m-%d}" if killed_on else ("▶" if m.get("effective_status") == "ACTIVE" else "⏸")
    live = days_live(m.get("created_time"), killed_on)
    return (f"{creative_no(m.get('name', ''))} {mark} · {money(m.get('amount_spent'))} · {purch if purch is not None else 'n/a'} purch · "
            f"CPA {money(cpa)} · oCTR {pct(outbound_ctr_of(m))} · ATC {whole(atc_of(m))} · "
            f"CPC {money(m.get('cpc'))} · {count(m.get('impressions')):,} impr · {live if live is not None else '?'}d")


def merge_result(existing: str | None, new_lines: list[str], header: str | None) -> str:
    """Keep one line per creative (newest wins), optional header on top."""
    lines = {}
    old_header = None
    for l in (existing or "").splitlines():
        l = l.strip()
        if not l:
            continue
        m = AD_LINE_RE.match(l)
        if m:
            lines[m.group(1)] = l
        elif old_header is None and l.startswith("KILLED"):
            old_header = l
    for l in new_lines:
        m = AD_LINE_RE.match(l)
        if m:
            lines[m.group(1)] = l
    def order(k):  # C1, C2, … C10 numerically
        return (k[0], int(k[1:]))
    body = [lines[k] for k in sorted(lines, key=order)]
    head = header or old_header
    return "\n".join(([head] if head else []) + body)


def upsert_checklist(task: dict, ads: list[dict], killed_ids: set[str], when: dt.datetime, dry: bool):
    """One checklist item per creative, name = metrics line, resolved when dead."""
    if not ads:
        return
    cl = next((c for c in task.get("checklists", []) if c.get("name") == CHECKLIST_NAME), None)
    if cl is None:
        if dry or SKIP_CLICKUP:
            print(f"    [dry] create checklist '{CHECKLIST_NAME}'")
            cl = {"id": "dry", "items": []}
        else:
            cl = cu("POST", f"/task/{task['id']}/checklist", {"name": CHECKLIST_NAME}).get("checklist", {})
            task.setdefault("checklists", []).append(cl)
    items = {creative_no(i.get("name", "")): i for i in cl.get("items", [])}
    for a in sorted(ads, key=lambda a: a.get("name", "")):
        code = creative_no(a.get("name", ""))
        dead = str(a.get("id")) in killed_ids or a.get("effective_status") not in ("ACTIVE", "IN_PROCESS", "PENDING_REVIEW")
        line = ad_line(a, when if str(a.get("id")) in killed_ids else None)
        body = {"name": line, "resolved": bool(dead)}
        item = items.get(code)
        if dry or SKIP_CLICKUP:
            print(f"    [dry] checklist {'update' if item else 'add'}: {line}{' [resolved]' if dead else ''}")
            continue
        if item:
            cu("PUT", f"/checklist/{cl['id']}/checklist_item/{item['id']}", body)
        else:
            cu("POST", f"/checklist/{cl['id']}/checklist_item", body)


# ── state ────────────────────────────────────────────────────────────────────
def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def ledger_keys() -> set[str]:
    p = STATE / "kills.jsonl"
    if not p.exists():
        return set()
    return {json.loads(l)["key"] for l in p.read_text().splitlines() if l.strip()}


def ledger_append(rows: list[dict]):
    with (STATE / "kills.jsonl").open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── core ─────────────────────────────────────────────────────────────────────
def ad_summary(m: dict) -> str:
    purch, cpa = purchases_of(m), cpa_of(m)
    return (f"{money(m.get('amount_spent'))} spent · "
            f"{purch if purch is not None else 'n/a'} purchase(s) · CPA {money(cpa)} · "
            f"oCTR {pct(outbound_ctr_of(m))} · ATC {whole(atc_of(m))}"
            f"  |  ROAS {ratio(roas_of(m))} · AOV {money(aov_of(m))} · CPM {money(cpm_of(m))} · "
            f"CPC {money(m.get('cpc'))} · CTR {pct(m.get('ctr'))} · {count(m.get('impressions')):,} impr")


def describe_task(task: dict | None, adset_name: str) -> str:
    """Format / angle / avatar / awareness straight from the name grammar."""
    parts = [p.strip() for p in adset_name.split(" - ")]
    if len(parts) >= 13 and parts[0].startswith("S"):
        return (f"Format {parts[2]} · Angle {parts[6]} · Avatar {parts[7]} · "
                f"Awareness {parts[10]} · Type {parts[12]}")
    return ""


def process(dry: bool) -> int:
    if not CLICKUP_KEY:
        print("CLICKUP_API_KEY missing in .env"); return 2
    cfg = CONFIG
    seen = ledger_keys()
    tasks = ClickUpTasks()
    print(f"ClickUp: {len(tasks.tasks)} tasks loaded{' · CLICKUP WRITES SKIPPED' if SKIP_CLICKUP else ''} · webhook: "
          f"{'dedicated' if WEBHOOK and not WEBHOOK_IS_FALLBACK else 'OPS FALLBACK' if WEBHOOK else 'NONE'}")
    new_rows, embeds, problems = [], [], []
    now = dt.datetime.now()

    for account, acc in cfg["accounts"].items():
        events = read_json(INBOX / f"kills_{account}.json", [])
        metrics = read_json(INBOX / f"metrics_{account}.json", [])
        active_adsets = read_json(INBOX / f"active_adsets_{account}.json", [])
        # Meta's own ad-set rows, when the bridge managed to fetch them. Optional
        # on purpose: an older inbox, or a Meta call that failed, must still
        # produce a kill report rather than crash — totals_of() then falls back
        # to summing the ad rows.
        adset_metrics = {str(r.get("id")): r
                         for r in read_json(INBOX / f"adset_metrics_{account}.json", [])
                         if r.get("id")}
        active_by_name = {a.get("name", "").strip(): str(a.get("id")) for a in active_adsets}
        by_id = {str(m.get("id")): m for m in metrics}
        by_adset: dict[str, list[dict]] = {}
        for m in metrics:
            by_adset.setdefault(str(m.get("adset_id")), []).append(m)
        print(f"\n== {acc['name']} ({account}): {len(events)} pause events, {len(metrics)} metric rows")

        kills = []
        for e in events:
            old, new = str(e.get("old_value")), str(e.get("new_value"))
            if new != "7":
                continue
            if e.get("actor_name") in cfg["ignore_actors"]:
                continue
            if e.get("application_name") in cfg["ignore_applications"] and old == "17":
                continue  # our own create-paused flow
            if old not in ("1", "9"):
                continue  # only live (or in-review) -> paused counts as a kill
            key = f"{account}:{e['object_type']}:{e['object_id']}:{e.get('datetime')}"
            if key in seen:
                continue
            kills.append((key, e))

        # collapse: if an ad set was killed, skip the individual ad events of that set
        killed_adsets = {e["object_id"] for _, e in kills if e["object_type"] == "adset"}
        for key, e in kills:
            kind, obj_id, name = e["object_type"], str(e["object_id"]), e.get("object_name", "")
            when = parse_dt(e.get("datetime")) or now
            actor = e.get("actor_name") or "unknown"
            if kind == "ad" and str(by_id.get(obj_id, {}).get("adset_id")) in killed_adsets:
                new_rows.append({"key": key, "skipped": "covered by adset kill", "at": now.isoformat()})
                continue
            if kind == "adset" and active_by_name.get(name.strip()) not in (None, obj_id):
                print(f"  ↻ adset {obj_id} · {name[:60]} · replaced by active {active_by_name[name.strip()]} — not a kill")
                new_rows.append({"key": key, "skipped": f"replaced by {active_by_name[name.strip()]}", "at": now.isoformat()})
                continue

            if kind == "adset":
                adset_name, ads = name, by_adset.get(obj_id, [])
            else:
                m = by_id.get(obj_id, {})
                adset_name, ads = m.get("adset_name") or name.rsplit("_", 1)[0], [m] if m else []
                # normalise the ad name back to the ad-set grammar for matching
                adset_name = re.sub(r" - (OG|7R|COMP|PG8|P18|P9A|Comp) - ", " - ", adset_name)
                adset_name = CREATIVE_RE.sub("", adset_name)

            task, how = tasks.find(adset_name)
            print(f"  💀 {kind} {obj_id} · {name[:70]} · by {actor} · task: {how}")
            if not task:
                problems.append(f"{kind} `{name[:80]}` killed by {actor} — ClickUp task {how}")

            still = []
            if kind == "ad" and ads:
                siblings = by_adset.get(str(ads[0].get("adset_id")), [])
                still = [creative_no(s.get("name", "")) for s in siblings
                         if str(s.get("id")) != obj_id and s.get("effective_status") == "ACTIVE"]

            # ── ClickUp
            batch_no = batch_prefix(adset_name) or adset_name[:12]
            if task:
                if kind == "ad":
                    m = ads[0] if ads else {}
                    text = (f"💀 {creative_no(name)} killed by {actor} on {when:%Y-%m-%d %H:%M} "
                            f"({acc['name']}, {lander_code(name) or 'lander n/a'}) after {days_live(m.get('created_time'), when)} days\n"
                            f"{ad_summary(m) if m else 'metrics n/a'}\n"
                            f"Still running: {', '.join(still) if still else 'none'}\n"
                            f"{ads_manager_url(account, 'ad', obj_id)}")
                    cu_comment(task["id"], text, dry)
                    siblings = by_adset.get(str(m.get("adset_id")), []) if m else []
                    if m:
                        merged = merge_result(tasks.field_value(task, cfg["clickup"]["fields"]["result"]),
                                              [ad_line(m, when)], None)
                        cu_set_field(task["id"], cfg["clickup"]["fields"]["result"], merged, dry)
                    upsert_checklist(task, siblings or ads, {obj_id}, when, dry)
                else:
                    lines = [f"💀 Batch killed by {actor} on {when:%Y-%m-%d %H:%M} ({acc['name']})"]
                    t = totals_of(ads, adset_metrics.get(str(obj_id)))
                    for a in sorted(ads, key=lambda a: a.get("name", "")):
                        lines.append(f"• {creative_no(a.get('name',''))} ({lander_code(a.get('name','')) or '-'}): {ad_summary(a)}")
                    live = days_live(min((a.get("created_time") for a in ads if a.get("created_time")), default=None), when)
                    # Batch totals. Primary metrics (CPA / outbound CTR / adds to
                    # cart) go in the one-line Result header — merge_result
                    # re-parses that line, so it must stay a single line. The
                    # secondary context lands in the comment just below.
                    result = (f"KILLED {when:%Y-%m-%d} after {live if live is not None else '?'} days · "
                              f"{money(t['spend'])} · {whole(t['purch'])} purchase(s) · "
                              f"CPA {money(t['cpa'])} · oCTR {pct(t['octr'])} · ATC {whole(t['atc'])} · "
                              f"ROAS {ratio(t['roas'])} · by {actor}")
                    lines.append(result)
                    lines.append(f"Batch context · CPM {money(t['cpm'])} · CPC {money(t['cpc'])} · "
                                 f"AOV {money(t['aov'])} · revenue {money(t['revenue'])} · "
                                 f"{whole(t['impr'])} impr"
                                 + ("" if t["source"] == "adset" else "  (summed from ad rows)"))
                    lines.append("→ fill in 📖 Learnings")
                    cu_comment(task["id"], "\n".join(lines), dry)
                    # every ad in the set is dead now; ads killed earlier keep their own ✖ date
                    prior = tasks.field_value(task, cfg["clickup"]["fields"]["result"]) or ""
                    earlier = {AD_LINE_RE.match(l.strip()).group(1) for l in prior.splitlines()
                               if AD_LINE_RE.match(l.strip()) and "✖" in l}
                    table = [ad_line(a, when) for a in ads if creative_no(a.get("name", "")) not in earlier]
                    result = merge_result(prior, table, result)
                    upsert_checklist(task, ads, {str(a.get("id")) for a in ads}, when, dry)
                    cur = tasks.field_value(task, cfg["clickup"]["fields"]["status"]) or ""
                    if cur in cfg["clickup"]["overridable_statuses"]:
                        cu_set_field(task["id"], cfg["clickup"]["fields"]["status"],
                                     cfg["clickup"]["status_options"]["Losing Ad"], dry)
                    else:
                        print(f"    Status left as '{cur}' (human-set)")
                    cu_set_field(task["id"], cfg["clickup"]["fields"]["result"], result, dry)
                    if task["status"]["status"] in cfg["clickup"]["protected_task_statuses"]:
                        print(f"    task status left as '{task['status']['status']}' (human-set)")
                    else:
                        cu_set_status(task["id"], cfg["clickup"]["batch_kill_task_status"], dry)

            # ── Discord embed
            if kind == "ad":
                m = ads[0] if ads else {}
                title = f"💀 {batch_no} — {creative_no(name)} killed by {actor}"
                desc = (f"{acc['name']} · live {days_live(m.get('created_time'), when)} days\n{ad_summary(m) if m else 'metrics n/a'}\n"
                        f"{describe_task(task, adset_name)}\nLander: {lander_code(name) or 'n/a'}\n"
                        f"Still running: {', '.join(still) if still else 'none'}")
            else:
                title = f"💀💀 {batch_no} — whole batch killed by {actor}"
                desc = (f"{acc['name']} · {len(ads)} ads\n" +
                        "\n".join(f"• {creative_no(a.get('name',''))}: {ad_summary(a)}" for a in sorted(ads, key=lambda a: a.get('name',''))) +
                        f"\n{describe_task(task, adset_name)}\n**→ fill in 📖 Learnings**")
            links = " · ".join(x for x in [f"[ClickUp]({task_url(task)})" if task else "ClickUp: no task",
                                           f"[Ads Manager]({ads_manager_url(account, kind, obj_id)})"])
            embeds.append({"title": title[:256], "description": (desc + "\n" + links)[:4000],
                           "color": 0xE74C3C if kind == "adset" else 0xE67E22,
                           "url": task_url(task) or ads_manager_url(account, kind, obj_id)})
            new_rows.append({"key": key, "account": account, "kind": kind, "object_id": obj_id, "name": name,
                             "actor": actor, "at": when.isoformat(), "task_id": task["id"] if task else None,
                             "match": how, "detected": now.isoformat()})

    content = None
    if problems:
        content = "⚠️ Kill-sync could not map:\n" + "\n".join("• " + p for p in problems)
    if WEBHOOK_IS_FALLBACK and embeds:
        content = (content or "") + "\n_(posted to the ops channel — set KILL_SYNC_DISCORD_WEBHOOK_URL for a dedicated channel)_"
    ok = discord(embeds, content, dry)

    if not dry:
        if new_rows:
            ledger_append(new_rows)
        (STATE / "last_run.json").write_text(json.dumps({"last_run": now.isoformat(timespec="seconds"),
                                                          "kills": len(embeds)}, indent=1))
        archive = INBOX / "processed" / f"{now:%Y-%m-%d_%H%M}"
        archive.mkdir(parents=True, exist_ok=True)
        for p in INBOX.glob("*.json"):
            p.replace(archive / p.name)
        if embeds and not WEBHOOK:
            (STATE / "reports").mkdir(exist_ok=True)
            (STATE / "reports" / f"{now:%Y-%m-%d_%H%M}.md").write_text(
                "\n\n".join(f"### {e['title']}\n{e['description']}" for e in embeds))
    print(f"\nDone: {len(embeds)} kill(s) reported, {len(problems)} unmapped, discord={'ok' if ok else 'FAILED/none'}")
    return 0 if ok or not embeds else 3


def digest(dry: bool) -> int:
    """List what the run that just finished pushed into ClickUp.

    Every row `process` ledgers carries the same `detected` stamp, so the newest
    stamp is exactly one run. A row only reaches a ClickUp task when it matched
    one, so `task_id` is the test for "updated in ClickUp" — replacement-guard
    rows (which carry `skipped`) and kills with no matching task never have it.
    Reads only the ledger, so a ClickUp outage cannot break this step.
    """
    p = STATE / "kills.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    runs = [r for r in rows if r.get("detected")]
    if not runs:
        print("digest: no synced kills in the ledger yet")
        return 0
    latest = max(r["detected"] for r in runs)
    synced = [r for r in runs if r["detected"] == latest and r.get("task_id")]
    if not synced:
        print(f"digest: run {latest[:19]} updated no ClickUp tasks")
        return 0
    synced.sort(key=lambda r: r.get("name", ""))
    lines = []
    for r in synced:
        name = r.get("name", "")
        label = batch_prefix(name) or name[:12] or "?"
        what = "whole batch" if r.get("kind") == "adset" else "single ad"
        lines.append(f"• [{label}](https://app.clickup.com/t/{r['task_id']}) — "
                     f"{what}, killed by {r.get('actor') or 'unknown'}")
    embed = {"title": f"\u2705 {len(synced)} killed batch(es) updated in ClickUp",
             "description": "\n".join(lines)[:4000], "color": 0x2ECC71}
    return 0 if discord([embed], None, dry) else 3


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["process", "digest"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-clickup", action="store_true", help="post Discord + record state, but do not write to ClickUp")
    a = ap.parse_args(argv)
    global SKIP_CLICKUP
    SKIP_CLICKUP = SKIP_CLICKUP or a.skip_clickup
    STATE.mkdir(exist_ok=True); INBOX.mkdir(exist_ok=True)
    return process(a.dry_run) if a.cmd == "process" else digest(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
