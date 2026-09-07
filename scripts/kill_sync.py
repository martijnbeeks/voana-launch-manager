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
    spend, purch = num(m.get("amount_spent")), count(m.get("purchases"))
    cpa = m.get("cost_per_purchase")
    if num(cpa) is None and spend and purch:
        cpa = spend / purch
    return (f"{money(m.get('amount_spent'))} spent · {int(purch or 0)} purchase(s) · CPA {money(cpa)} · "
            f"CTR {pct(m.get('ctr'))} · CPC {money(m.get('cpc'))} · {count(m.get('impressions')):,} impr")


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
                else:
                    lines = [f"💀 Batch killed by {actor} on {when:%Y-%m-%d %H:%M} ({acc['name']})"]
                    tot_spend = sum(num(a.get("amount_spent")) or 0 for a in ads)
                    tot_p = sum(count(a.get("purchases")) for a in ads)
                    for a in sorted(ads, key=lambda a: a.get("name", "")):
                        lines.append(f"• {creative_no(a.get('name',''))} ({lander_code(a.get('name','')) or '-'}): {ad_summary(a)}")
                    live = days_live(min((a.get("created_time") for a in ads if a.get("created_time")), default=None), when)
                    result = (f"KILLED {when:%Y-%m-%d} after {live if live is not None else '?'} days · "
                              f"${tot_spend:,.2f} · {int(tot_p)} purchase(s) · "
                              f"CPA {money(tot_spend / tot_p) if tot_p else 'n/a'} · by {actor}")
                    lines.append(result)
                    lines.append("→ fill in 📖 Learnings")
                    cu_comment(task["id"], "\n".join(lines), dry)
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
    tasks = ClickUpTasks()
    f = CONFIG["clickup"]["fields"]
    missing = [t for t in tasks.tasks
               if t["status"]["status"] == CONFIG["clickup"]["batch_kill_task_status"]
               and not (tasks.field_value(t, f["learnings"]) or "").strip()]
    missing.sort(key=lambda t: int(t.get("date_closed") or t.get("date_updated") or 0))
    if not missing:
        print("digest: nothing owed"); return 0
    lines = [f"• [{batch_prefix(t['name']) or t['name'][:10]}]({task_url(t)}) — "
             f"{(tasks.field_value(t, f['result']) or 'no result yet')[:90]}" for t in missing[:20]]
    embed = {"title": f"📖 {len(missing)} killed batch(es) still without learnings",
             "description": "\n".join(lines)[:4000], "color": 0x3498DB}
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
