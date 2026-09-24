#!/usr/bin/env python3
"""Kill-sync: Meta ad/ad-set pauses -> ClickUp Launch Manager + Discord.

The Meta half runs inside a headless Claude session (scripts/kill_sync_prompt.md)
because the Meta MCP is the only Meta credential on this Mac. That session drops
JSON files into state/inbox/; this script does everything deterministic:

  process   read state/inbox/*.json, detect kills, update ClickUp, post Discord,
            append data/kill-sync/kills.jsonl, advance data/kill-sync/last_run.json
  digest    post the "synced into ClickUp" digest for the run that just finished
  replay D  re-post the Discord report for an archived inbox D (no ClickUp, no state)
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
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"          # scratch: inbox, log, reports — gitignored
INBOX = STATE / "inbox"
# DURABLE state — the kills ledger and the poll window — lives in a TRACKED
# directory. Since 2026-09-17 the job runs as a Multica autopilot on the Mac
# Mini, where every run is a fresh checkout: anything under the gitignored
# state/ would be gone next run, the window would reset to "now minus 36h" and
# the ledger would forget every kill it had reported. `push-state` commits this
# directory to main after each run (same pattern as voana-tools' format radar).
DURABLE = Path(os.environ.get("KILL_SYNC_STATE_DIR") or ROOT / "data" / "kill-sync")
ARCHIVE_KEEP = 14   # a week of twice-daily runs; each archive is a few hundred KB at most


def archive_dir() -> Path:
    return DURABLE / "inbox-archive"
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
    """'$78.40 USD' -> 78.4, '2,56%' -> 2.56, '1.234,5' -> 1234.5, None -> None.

    Money fields can also arrive as {'value': '50.42', 'unit': 'USD'} (the MCP
    shape seen 2026-09-23). Unwrap it first: stringifying the dict leaves a
    trailing ',' that the locale guess below reads as a decimal comma, turning
    $50.42 into $5,042 on every kill card."""
    if isinstance(v, dict):
        v = v.get("value")
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

def _delivered(m: dict) -> bool:
    """Did this row actually run?

    Meta omits a count field entirely when the event never happened — it does not
    send a 0. So on a row that spent money and served impressions, a missing
    purchase or add-to-cart count means the event happened zero times, which is a
    real answer and the one the kill decision needs. On a row with no delivery
    data at all (an archived ad, which returns no metrics) the same absence means
    we simply do not know, and 0 would be a lie.
    """
    return num(m.get("amount_spent")) is not None or count(m.get("impressions")) > 0


def purchases_of(m: dict) -> int | None:
    v = _first(m, "omni_purchase", "purchases")
    if v is not None:
        return count(v)
    return 0 if _delivered(m) else None


def atc_of(m: dict) -> int | None:
    """Adds to cart — the offer/messaging signal: clicks but no cart means the
    ad worked and the page did not; carts but no purchase points at checkout."""
    v = _first(m, "omni_add_to_cart", "adds_to_cart")
    if v is not None:
        return count(v)
    return 0 if _delivered(m) else None


def outbound_ctr_of(m: dict) -> float | None:
    """Outbound CTR — clicks that actually left Meta. Plain `ctr` counts every
    click including likes and profile taps, so it flatters a weak ad."""
    return num(_first(m, "outbound_clicks_ctr"))


def outbound_clicks_of(m: dict) -> int | None:
    v = _first(m, "outbound_clicks")
    return count(v) if v is not None else None


def cpm_of(m: dict) -> float | None:
    return num(_first(m, "cpm"))


# Link-click metrics, as Ads Manager shows them: "CTR (link click-through rate)"
# and "CPC (cost per link click)". The Meta MCP names them `website_ctr` and
# `cost_per_link_click` — NOT the Graph API's inline_link_click_ctr /
# cost_per_inline_link_click, which the MCP reports as unknown fields (verified
# with ads_get_field_context 2026-09-17). Link clicks differ from outbound clicks
# (oCTR): a link click can land on a Meta surface, an outbound click cannot.

def link_clicks_of(m: dict) -> int | None:
    v = _first(m, "link_click", "link_clicks")
    return count(v) if v is not None else None


def link_ctr_of(m: dict) -> float | None:
    v = num(_first(m, "website_ctr", "link_ctr"))
    if v is not None:
        return v
    clicks, impr = link_clicks_of(m), count(m.get("impressions"))
    return clicks / impr * 100 if clicks is not None and impr else None


def link_cpc_of(m: dict) -> float | None:
    v = num(_first(m, "cost_per_link_click"))
    if v is not None:
        return v
    spend, clicks = num(m.get("amount_spent")), link_clicks_of(m)
    return spend / clicks if spend is not None and clicks else None


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
                "lctr": link_ctr_of(adset_row), "lcpc": link_cpc_of(adset_row),
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
    lclicks = total(link_clicks_of)
    return {"spend": spend, "purch": purch, "atc": total(atc_of), "revenue": rev,
            "cpa": spend / purch if spend and purch else None,
            "octr": (oclicks / impr * 100) if oclicks is not None and impr else None,
            "cpm": (spend / impr * 1000) if spend and impr else None,
            "cpc": (spend / clicks) if spend and clicks else None,
            # recomputed from summed link clicks, never an average of per-ad rates
            "lctr": (lclicks / impr * 100) if lclicks is not None and impr else None,
            "lcpc": (spend / lclicks) if spend and lclicks else None,
            "roas": (rev / spend) if rev is not None and spend else None,
            "aov": (rev / purch) if rev is not None and purch else None,
            "impr": impr, "source": "ads"}


def parse_dt(s: str | None) -> dt.datetime | None:
    """Handles ISO ('2026-09-07T02:21:16-0400'), the Dutch-locale MCP's
    '7-9-2026 om 03:14' (d-m-Y) and the English-locale '9/17/2026 at 3:46 AM'
    (m/d/Y, 12-hour). Returns naive local-ish datetime."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.replace(tzinfo=None)
        except ValueError:
            pass
    # English locale, as the claude.ai Meta connector on the Mac Mini returns it:
    # '9/17/2026 at 3:46\u202fAM' — M/D/Y, 12-hour, a NARROW no-break space
    # before AM/PM. Unparsed, every kill fell back to "now": wrong kill time on
    # the card and in ClickUp, and a comment head that changed on every re-run,
    # so the comment dedupe could not match (duplicate on S182, 2026-09-17).
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\D+?(\d{1,2}):(\d{2})(?:[\s\u202f\u00a0]*([AaPp])\.?[Mm]\.?)?", s)
    if m:
        mo, d_, y, h, mi = map(int, m.groups()[:5])
        ampm = (m.group(6) or "").lower()
        if ampm == "p" and h != 12:
            h += 12
        elif ampm == "a" and h == 12:
            h = 0
        return dt.datetime(y, mo, d_, h, mi)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mo, d_, y = map(int, m.groups())
        return dt.datetime(y, mo, d_)
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
class ClickUpError(Exception):
    """One ClickUp call that failed for good. `code` is the HTTP status (None for
    a network error) so callers can tell a 404 (the thing is gone) from a 5xx."""

    def __init__(self, method: str, path: str, code: int | None, detail: str = ""):
        self.method, self.path, self.code, self.detail = method, path, code, detail
        super().__init__(f"{method} {path} → {'HTTP ' + str(code) if code else 'network error'}"
                         + (f": {detail}" if detail else ""))


CU_RETRIES = 3
_SLEEP = time.sleep  # patched in tests


def cu(method: str, path: str, body: dict | None = None) -> dict:
    """ClickUp call with backoff on 429/5xx (honouring Retry-After) and on
    network drops. Anything else — 401, 404 — raises ClickUpError at once.
    A run makes ~25 calls per batch kill, so a 100 req/min token cap is
    reachable mid-run; without this a rate limit tore a kill in half."""
    for attempt in range(CU_RETRIES + 1):
        req = urllib.request.Request(
            CLICKUP + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": CLICKUP_KEY, "Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace") if e.fp else ""
            transient = e.code == 429 or e.code >= 500
            if not transient or attempt == CU_RETRIES:
                raise ClickUpError(method, path, e.code, detail) from e
            wait = float(e.headers.get("Retry-After") or 0) or (2.0 * 2 ** attempt)
            print(f"    clickup {method} {path} → HTTP {e.code}; retry {attempt + 1}/{CU_RETRIES} in {wait:.0f}s")
            _SLEEP(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt == CU_RETRIES:
                raise ClickUpError(method, path, None, str(e)) from e
            wait = 2.0 * 2 ** attempt
            print(f"    clickup {method} {path} → {e!r}; retry {attempt + 1}/{CU_RETRIES} in {wait:.0f}s")
            _SLEEP(wait)
    raise AssertionError("unreachable")


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
    """Post once. The first line of every kill comment is deterministic (actor +
    kill time), so a re-run after a crash finds it and does not repeat itself —
    the 2026-09-10 crash had already commented on four tasks it never ledgered."""
    if dry or SKIP_CLICKUP:
        print(f"    [dry] comment on {task_id}:\n" + "\n".join("      " + l for l in text.splitlines()))
        return
    head = text.strip().splitlines()[0].strip()
    # Match on the head WITHOUT its clock time. Until 2026-09-17 the kill time
    # could fall back to "now" (unparsed English dates), so the same kill got a
    # different HH:MM on every run and was commented again. Actor + kill DATE +
    # account still identifies a kill: nobody kills one batch twice in a day.
    def _key(h: str) -> str:
        return re.sub(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}", r"\1", h.strip())
    try:
        existing = cu("GET", f"/task/{task_id}/comment").get("comments", [])
    except ClickUpError as e:
        print(f"    could not read comments on {task_id} ({e}); posting anyway")
        existing = []
    if any(_key((c.get("comment_text") or "").strip().splitlines()[0] if (c.get("comment_text") or "").strip() else "")
           == _key(head) for c in existing):
        print(f"    comment already on {task_id}: {head[:70]} — not repeated")
        return
    cu("POST", f"/task/{task_id}/comment", {"comment_text": text, "notify_all": False})


def cu_set_field(task_id: str, field_id: str, value, dry: bool):
    if dry or SKIP_CLICKUP:
        print(f"    [dry] set field {field_id[:8]}… = {value!r}")
        return
    cu("POST", f"/task/{task_id}/field/{field_id}", {"value": value})


_LIST_CACHE: dict[str, dict] = {}


def list_info(list_id: str) -> dict:
    """{name, statuses} for a list, read once per run."""
    if list_id not in _LIST_CACHE:
        d = cu("GET", f"/list/{list_id}")
        _LIST_CACHE[list_id] = {"name": d.get("name") or list_id,
                                "statuses": [str(x.get("status", "")).lower() for x in d.get("statuses") or []]}
    return _LIST_CACHE[list_id]


def task_list_id(task: dict) -> str | None:
    lst = task.get("list")
    return str(lst.get("id")) if isinstance(lst, dict) else (str(lst) if lst else None)


def cu_set_status(task: dict, status: str, dry: bool) -> str | None:
    """Set the task status, or explain why not. Returns None when set (or dry),
    else a one-line reason.

    Statuses belong to a LIST. A launched batch is normally moved by a ClickUp
    automation from Launch Manager (ready for launch / launched / complete) into
    Media (… / killed / …). When that automation does not fire, the task is
    still in Launch Manager and "killed" does not exist there: ClickUp answers
    400 "Status does not exist" (S182, 2026-09-17). That is a fact about the
    task's placement, not a failed sync, so it is reported and skipped — and it
    must never stop the writes that follow it.
    """
    lid = task_list_id(task)
    if lid:
        info = list_info(lid)
        if info["statuses"] and status.lower() not in info["statuses"]:
            cur = (task.get("status") or {}).get("status", "?")
            return (f"task is in list '{info['name']}' (status '{cur}'), which has no '{status}' "
                    f"status — left as is; move it to Media if it launched")
    if dry or SKIP_CLICKUP:
        print(f"    [dry] task status -> {status}")
        return None
    cu("PUT", f"/task/{task['id']}", {"status": status})
    return None


# ── Discord ──────────────────────────────────────────────────────────────────
DISCORD_MAX_EMBEDS = 10
DISCORD_MAX_CHARS = 6000   # title + description across ALL embeds in one message


def _embed_len(e: dict) -> int:
    return len(e.get("title") or "") + len(e.get("description") or "")


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split into messages Discord accepts: at most 10 embeds AND at most 6000
    characters of embed text per message. Seven whole-batch kills in one payload
    were 400'd on 2026-09-10 and the kill post was silently lost — the old
    loop only counted embeds."""
    chunks, cur, size = [], [], 0
    for e in embeds:
        n = _embed_len(e)
        if cur and (len(cur) >= DISCORD_MAX_EMBEDS or size + n > DISCORD_MAX_CHARS):
            chunks.append(cur); cur, size = [], 0
        cur.append(e); size += n
    if cur:
        chunks.append(cur)
    return chunks


def discord(embeds: list[dict], content: str | None, dry: bool) -> bool:
    if not embeds and not content:
        return True
    if dry or not WEBHOOK:
        print("    [dry/no-webhook] discord:", json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False)[:1500])
        return bool(WEBHOOK)
    for i, batch in enumerate(chunk_embeds(embeds) or [[]]):
        payload = {"username": "Voana kill-sync", "embeds": batch}
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
        except (urllib.error.URLError, OSError) as e:
            print(f"    discord network error: {e!r}")
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
# 'C4/7R ✖09-09 · …' or legacy 'C4 ✖09-09 · …'. The key is what merge_result
# and the checklist upsert match on, so it must be unique per AD, and one
# creative runs on two landers (OG and 7R) — a bare 'C4' silently collapsed
# them into one line, dropping half the batch from the Result field.
AD_LINE_RE = re.compile(r"^((?:C|V)\d+(?:/[A-Za-z0-9]+)?)\b")
SCRIPT_LINE_RE = re.compile(r"^(?:C|V)\d+(?:/[A-Za-z0-9]+)? (?:✖\d\d-\d\d|▶|⏸) · ")


def ad_key(ad_name: str) -> str:
    """'S014 - 7R - C1-C6 - … _C4' → 'C4/7R'; an ad with no lander segment → 'C4'."""
    code, lander = creative_no(ad_name), lander_code(ad_name)
    return f"{code}/{lander}" if lander else code


def _key_order(k: str):
    """C1, C2, … C10 numerically, landers alphabetically inside a creative."""
    m = re.match(r"([CV])(\d+)(?:/(.*))?$", k)
    return (m.group(1), int(m.group(2)), m.group(3) or "") if m else ("Z", 0, k)


def ad_line(m: dict, killed_on: dt.datetime | None = None) -> str:
    """One compact line per creative for the Result field / checklist item:
    'C3 ✖09-09 · $38.90 · 0 purch · CPA n/a · CTR 1.34% · CPC $0.97 · 2,977 impr · 2d'"""
    purch, cpa = purchases_of(m), cpa_of(m)
    mark = f"✖{killed_on:%m-%d}" if killed_on else ("▶" if m.get("effective_status") == "ACTIVE" else "⏸")
    live = days_live(m.get("created_time"), killed_on)
    return (f"{ad_key(m.get('name', ''))} {mark} · {money(m.get('amount_spent'))} · {purch if purch is not None else 'n/a'} purch · "
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
    # A legacy bare-code line ('C4 …') is the collapsed version of the per-lander
    # lines that now replace it — drop it once 'C4/<lander>' exists.
    for k in [k for k in lines if "/" not in k]:
        if any(o.startswith(k + "/") for o in lines):
            del lines[k]
    body = [lines[k] for k in sorted(lines, key=_key_order)]
    head = header or old_header
    return "\n".join(([head] if head else []) + body)


def _checklist_of(task: dict) -> dict:
    cl = next((c for c in task.get("checklists", []) if c.get("name") == CHECKLIST_NAME), None)
    if cl is None:
        cl = cu("POST", f"/task/{task['id']}/checklist", {"name": CHECKLIST_NAME}).get("checklist", {})
        task.setdefault("checklists", []).append(cl)
    return cl


def _refetch_checklists(task: dict):
    """Re-read the task's checklists from ClickUp. The list endpoint's copy can be
    stale (checklist or item deleted in the UI since the run started)."""
    task["checklists"] = cu("GET", f"/task/{task['id']}").get("checklists", [])


def _item_map(cl: dict) -> tuple[dict, list]:
    """Script-authored items by ad key (first wins), plus legacy bare-code items.
    Items a human typed do not match SCRIPT_LINE_RE and are never touched."""
    items, legacy = {}, []
    for i in cl.get("items", []):
        name = (i.get("name") or "").strip()
        m = AD_LINE_RE.match(name)
        if not m or not SCRIPT_LINE_RE.match(name):
            continue
        if m.group(1) in items:
            legacy.append(i)          # a duplicate of a line we already hold
        else:
            items[m.group(1)] = i
    return items, legacy


def upsert_checklist(task: dict, ads: list[dict], killed_ids: set[str], when: dt.datetime, dry: bool):
    """One checklist item per AD (creative + lander), name = metrics line,
    resolved when dead. Tolerates ClickUp's flakiness: a 404 on an item write
    re-reads the task and retries the whole upsert once (items already written
    are then matched, not duplicated); an item deleted in the UI is recreated.
    Legacy 'C4 …' items are removed once 'C4/<lander>' lines exist — they were
    the collapsed form and would otherwise sit beside the real ones forever."""
    if not ads:
        return
    plan = []
    for a in sorted(ads, key=lambda a: a.get("name", "")):
        dead = str(a.get("id")) in killed_ids or a.get("effective_status") not in ("ACTIVE", "IN_PROCESS", "PENDING_REVIEW")
        line = ad_line(a, when if str(a.get("id")) in killed_ids else None)
        plan.append((ad_key(a.get("name", "")), {"name": line, "resolved": bool(dead)}))
    if dry or SKIP_CLICKUP:
        for _, body in plan:
            print(f"    [dry] checklist upsert: {body['name']}{' [resolved]' if body['resolved'] else ''}")
        return
    for attempt in (1, 2):
        try:
            cl = _checklist_of(task)
            items, legacy = _item_map(cl)
            new_keys = {k for k, _ in plan}
            for k, body in plan:
                item = items.get(k)
                if item:
                    try:
                        cu("PUT", f"/checklist/{cl['id']}/checklist_item/{item['id']}", body)
                    except ClickUpError as e:
                        if e.code != 404:
                            raise
                        print(f"    checklist item {item['id']} gone — recreating")
                        cu("POST", f"/checklist/{cl['id']}/checklist_item", body)
                else:
                    cu("POST", f"/checklist/{cl['id']}/checklist_item", body)
            stale = legacy + [i for k, i in items.items()
                              if "/" not in k and any(n.startswith(k + "/") for n in new_keys)]
            for i in stale:
                print(f"    retiring legacy checklist item: {(i.get('name') or '')[:50]}")
                try:
                    cu("DELETE", f"/checklist/{cl['id']}/checklist_item/{i['id']}")
                except ClickUpError as e:
                    if e.code != 404:
                        raise
            return
        except ClickUpError as e:
            if e.code != 404 or attempt == 2:
                raise
            print(f"    checklist write on {task['id']} → 404 ({e.path}); re-reading the task and retrying once")
            _refetch_checklists(task)


# ── state ────────────────────────────────────────────────────────────────────
def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def prune_archive(keep: int = ARCHIVE_KEEP) -> list[str]:
    """Drop all but the newest `keep` archived inboxes (names sort by time)."""
    import shutil
    root = archive_dir()
    if not root.is_dir():
        return []
    dirs = sorted(d for d in root.iterdir() if d.is_dir())
    gone = dirs[:-keep] if keep > 0 else dirs
    for d in gone:
        shutil.rmtree(d, ignore_errors=True)
    return [d.name for d in gone]


def ledger_keys() -> set[str]:
    p = DURABLE / "kills.jsonl"
    if not p.exists():
        return set()
    return {json.loads(l)["key"] for l in p.read_text().splitlines() if l.strip()}


def ledger_append(rows: list[dict]):
    with (DURABLE / "kills.jsonl").open("a") as f:
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


def brief_metrics(x: dict) -> str:
    """The numbers a kill decision is read by. Takes an ad row (Meta field
    names) or a totals_of() dict. Two lines: outcome, then delivery cost
    (CPM, CTR (link), CPC (link) — added 2026-09-17 at Martijn's request)."""
    if not x:
        return "metrics n/a"
    if "spend" in x and "purch" in x:      # totals_of()
        spend, purch, cpa, octr, roas = x["spend"], x["purch"], x["cpa"], x["octr"], x["roas"]
        cpm, lctr, lcpc = x.get("cpm"), x.get("lctr"), x.get("lcpc")
    else:
        spend, purch, cpa, octr, roas = num(x.get("amount_spent")), purchases_of(x), cpa_of(x), outbound_ctr_of(x), roas_of(x)
        cpm, lctr, lcpc = cpm_of(x), link_ctr_of(x), link_cpc_of(x)
    return (f"{money(spend)} · {purch if purch is not None else 'n/a'} purch · CPA {money(cpa)} · "
            f"oCTR {pct(octr)} · ROAS {ratio(roas)}\n"
            f"CPM {money(cpm)} · CTR (link) {pct(lctr)} · CPC (link) {money(lcpc)}")


def describe_task(task: dict | None, adset_name: str) -> str:
    """Format / angle / avatar / awareness straight from the name grammar."""
    parts = [p.strip() for p in adset_name.split(" - ")]
    if len(parts) >= 13 and parts[0].startswith("S"):
        return (f"Format {parts[2]} · Angle {parts[6]} · Avatar {parts[7]} · "
                f"Awareness {parts[10]} · Type {parts[12]}")
    return ""


def process(dry: bool, inbox: Path | None = None, replay: bool = False,
            rewrite: bool = False, only: set[str] | None = None) -> int:
    """`replay=True` re-posts the Discord report for an already-processed inbox
    (data/kill-sync/inbox-archive/<stamp>/): ClickUp writes are skipped, the ledger is
    ignored for detection and NOT appended, last_run is left alone. It exists for
    the case where ClickUp was synced but the Discord post failed."""
    global SKIP_CLICKUP
    if not CLICKUP_KEY:
        print("CLICKUP_API_KEY missing in .env"); return 2
    cfg = CONFIG
    inbox = inbox or INBOX
    if replay:
        SKIP_CLICKUP = True
        print(f"REPLAY of {inbox} — Discord only, no ClickUp writes, no state change")
    if rewrite:
        # The mirror of replay: ClickUp only. For a kill whose ClickUp writes
        # failed after Discord already posted — re-running `process` cannot
        # help, the ledger has it. Every ClickUp write is idempotent (comments
        # match on their first line, fields and checklist items upsert).
        print(f"REWRITE of {inbox} — ClickUp only{' for ' + ', '.join(sorted(only)) if only else ''}; "
              f"no Discord, no ledger, no state change")
    seen = set() if (replay or rewrite) else ledger_keys()
    tasks = ClickUpTasks()
    print(f"ClickUp: {len(tasks.tasks)} tasks loaded{' · CLICKUP WRITES SKIPPED' if SKIP_CLICKUP else ''} · webhook: "
          f"{'dedicated' if WEBHOOK and not WEBHOOK_IS_FALLBACK else 'OPS FALLBACK' if WEBHOOK else 'NONE'}")
    new_rows, embeds, problems, notes = [], [], [], []
    failed_clickup = 0
    now = dt.datetime.now()

    for account, acc in cfg["accounts"].items():
        events = read_json(inbox / f"kills_{account}.json", [])
        metrics = read_json(inbox / f"metrics_{account}.json", [])
        active_adsets = read_json(inbox / f"active_adsets_{account}.json", [])
        # Meta's own ad-set rows, when the bridge managed to fetch them. Optional
        # on purpose: an older inbox, or a Meta call that failed, must still
        # produce a kill report rather than crash — totals_of() then falls back
        # to summing the ad rows.
        adset_metrics = {str(r.get("id")): r
                         for r in read_json(inbox / f"adset_metrics_{account}.json", [])
                         if r.get("id")}
        active_by_name = {a.get("name", "").strip(): str(a.get("id")) for a in active_adsets}
        # one row per ad id: a bridge that lists an ad twice (killed-ads query and
        # ads-of-killed-set query overlap) must not double the batch totals
        by_id = {str(m.get("id")): m for m in metrics}
        by_adset: dict[str, list[dict]] = {}
        for m in by_id.values():
            by_adset.setdefault(str(m.get("adset_id")), []).append(m)
        print(f"\n== {acc['name']} ({account}): {len(events)} pause events, {len(by_id)} metric rows")

        kills, in_run = [], set()
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
            if key in seen or key in in_run:
                continue
            in_run.add(key)
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
                problems.append(f"{batch_prefix(adset_name) or name[:30]} · no ClickUp task ({how})")

            still = []
            if kind == "ad" and ads:
                siblings = by_adset.get(str(ads[0].get("adset_id")), [])
                still = [ad_key(s.get("name", "")) for s in siblings
                         if str(s.get("id")) != obj_id and s.get("effective_status") == "ACTIVE"]

            batch_no = batch_prefix(adset_name) or adset_name[:12]
            if only and batch_no not in only:
                continue

            # ── ClickUp. Everything for one kill is isolated: a failure here is
            # logged, ledgered and reported, and the run carries on to the next
            # kill. Before 2026-09-10 one ClickUp 404 raised straight out of the
            # loop, so four batches that were already written were never
            # ledgered, two were never reached, and the inbox/last_run stayed
            # put — the next run would have re-commented on all of them.
            def write_ad_kill():
                m = ads[0] if ads else {}
                text = (f"💀 {ad_key(name)} killed by {actor} on {when:%Y-%m-%d %H:%M} "
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
                # the checklist is the chattiest and flakiest write — last, so a
                # failure there costs nothing above
                upsert_checklist(task, siblings or ads, {obj_id}, when, dry)

            def write_batch_kill():
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
                table = [ad_line(a, when) for a in ads if ad_key(a.get("name", "")) not in earlier]
                cu_set_field(task["id"], cfg["clickup"]["fields"]["result"], merge_result(prior, table, result), dry)
                cur = tasks.field_value(task, cfg["clickup"]["fields"]["status"]) or ""
                if cur in cfg["clickup"]["overridable_statuses"]:
                    cu_set_field(task["id"], cfg["clickup"]["fields"]["status"],
                                 cfg["clickup"]["status_options"]["Losing Ad"], dry)
                else:
                    print(f"    Status left as '{cur}' (human-set)")
                # Batch totals also go into real ClickUp number fields, so the
                # list can be sorted and filtered on CPA / ROAS instead of the
                # numbers being locked inside the Result text. Only on a whole
                # -batch kill: these fields describe the ad set, and a single
                # dead creative must not overwrite them with its own numbers.
                # A metric Meta did not report is SKIPPED, never written as 0.
                # Written BEFORE the task status: on 2026-09-17 a rejected
                # status aborted S182 here and its metric fields never landed.
                # Custom fields are scoped to a list/folder. The metric fields live
                # on Media only, so a task the move-to-Media automation missed has
                # none of them and ClickUp answers 400 FIELD_115 (S182, 2026-09-17).
                # The task payload lists exactly the fields its location has.
                present = {f.get("id") for f in task.get("custom_fields") or []}
                missing = []
                for mkey, fid in (cfg["clickup"].get("metric_fields") or {}).items():
                    v = t.get(mkey)
                    if v is None:
                        continue
                    if present and fid not in present:
                        missing.append(mkey)
                        continue
                    cu_set_field(task["id"], fid, round(float(v), 2), dry)
                if missing:
                    why = (f"metric fields {', '.join(missing)} do not exist on this task's list "
                           f"— not written; move it to Media if it launched")
                    print(f"    {why}")
                    notes.append(f"{batch_no} · {why} · {task_url(task)}")
                if task["status"]["status"] in cfg["clickup"]["protected_task_statuses"]:
                    print(f"    task status left as '{task['status']['status']}' (human-set)")
                else:
                    why = cu_set_status(task, cfg["clickup"]["batch_kill_task_status"], dry)
                    if why:
                        print(f"    task status not set: {why}")
                        notes.append(f"{batch_no} · {why} · {task_url(task)}")
                upsert_checklist(task, ads, {str(a.get("id")) for a in ads}, when, dry)

            clickup_error = None
            if task:
                try:
                    write_ad_kill() if kind == "ad" else write_batch_kill()
                except ClickUpError as ex:
                    clickup_error = str(ex)
                except Exception as ex:  # a bug in one kill must not abort the batch either
                    clickup_error = f"{type(ex).__name__}: {ex}"
                    traceback.print_exc()
                if clickup_error:
                    failed_clickup += 1
                    print(f"    ✗ ClickUp update failed: {clickup_error}")
                    problems.append(f"{batch_no} · ClickUp update failed · {task_url(task)} · {clickup_error[:120]}")

            # ── Discord embed: ONE line of numbers + links. Keep it short — the
            # per-ad table is in the ClickUp comment, and the channel is read on
            # a phone. Seven batch kills used to be 7 × 15 lines.
            if kind == "ad":
                m = ads[0] if ads else {}
                title = f"💀 {batch_no} · {ad_key(name)} · {actor}"
                desc = (f"{acc['name']} · {days_live(m.get('created_time'), when) if m else '?'}d · {brief_metrics(m)}"
                        f" · still: {', '.join(still) if still else 'none'}")
            else:
                t = totals_of(ads, adset_metrics.get(str(obj_id)))
                live = days_live(min((a.get("created_time") for a in ads if a.get("created_time")), default=None), when)
                title = f"💀💀 {batch_no} · batch · {actor}"
                desc = (f"{acc['name']} · {len(ads)} ads · {live if live is not None else '?'}d · {brief_metrics(t)}"
                        f" · 📖 learnings?")
            if clickup_error:
                desc += "\n⚠️ ClickUp update failed"
            links = " · ".join(x for x in [f"[ClickUp]({task_url(task)})" if task else "no ClickUp task",
                                           f"[Ads Manager]({ads_manager_url(account, kind, obj_id)})"])
            embeds.append({"title": title[:256], "description": (desc + "\n" + links)[:4000],
                           "color": 0xE74C3C if kind == "adset" else 0xE67E22,
                           "url": task_url(task) or ads_manager_url(account, kind, obj_id)})
            row = {"key": key, "account": account, "kind": kind, "object_id": obj_id, "name": name,
                   "actor": actor, "at": when.isoformat(), "task_id": task["id"] if task else None,
                   "match": how, "detected": now.isoformat()}
            if clickup_error:
                row["error"] = clickup_error[:300]
            new_rows.append(row)

    content = None
    if problems or notes:
        content = "\n".join(["⚠️ " + p for p in problems] + ["ℹ️ " + n for n in notes])
    if WEBHOOK_IS_FALLBACK and embeds:
        content = (content or "") + "\n_(ops channel — set KILL_SYNC_DISCORD_WEBHOOK_URL)_"
    if rewrite:
        print(f"\nRewrite done: {len(embeds)} kill(s) rewritten, {failed_clickup} ClickUp failure(s)"
              + ("".join(f"\n  ℹ️ {n}" for n in notes)))
        return 4 if failed_clickup else 0
    ok = discord(embeds, content, dry)

    if not dry and not replay:
        if new_rows:
            ledger_append(new_rows)
        (DURABLE / "last_run.json").write_text(json.dumps({"last_run": now.isoformat(timespec="seconds"),
                                                          "kills": len(embeds)}, indent=1))
        # The archive lives in the TRACKED durable dir: on the Mac Mini every run
        # is a fresh checkout and Multica deletes the workdir afterwards, so an
        # archive under state/ was gone before `replay` or `rewrite` could ever
        # read it (found repairing S182, 2026-09-17). push-state commits it.
        archive = archive_dir() / f"{now:%Y-%m-%d_%H%M}"
        archive.mkdir(parents=True, exist_ok=True)
        for p in INBOX.glob("*.json"):
            p.replace(archive / p.name)
        prune_archive()
        if embeds and (not WEBHOOK or not ok):
            # keep the report so a failed post can be read (or replayed from the
            # archived inbox) instead of vanishing with the process
            (STATE / "reports").mkdir(exist_ok=True)
            (STATE / "reports" / f"{now:%Y-%m-%d_%H%M}.md").write_text(
                "\n\n".join(f"### {e['title']}\n{e['description']}" for e in embeds))
            if WEBHOOK:
                print(f"    Discord post failed — report saved to state/reports/{now:%Y-%m-%d_%H%M}.md; "
                      f"re-post with: kill_sync.py replay {archive}")
    print(f"\nDone: {len(embeds)} kill(s) reported, {len(problems)} unmapped/failed, "
          f"{failed_clickup} ClickUp failure(s), discord={'ok' if ok else 'FAILED/none'}")
    if failed_clickup:
        return 4
    return 0 if ok or not embeds else 3

def digest(dry: bool) -> int:
    """List what the run that just finished pushed into ClickUp.

    Every row `process` ledgers carries the same `detected` stamp, so the newest
    stamp is exactly one run — but only when the run ledgered rows at all. A
    zero-kill run appends nothing and leaves the newest stamp on its
    predecessor, so the digest re-announced kills that were already reported:
    three identical "15 kill(s)" cards for the 2026-09-20 20:45 run, one per
    zero-kill run that followed. `last_run.json` carries the stamp of the run
    that just finished, so the digest now posts only when the newest ledger
    stamp is that run's own. A row only reaches a ClickUp task when it matched
    one, so `task_id` is the test for "updated in ClickUp" — replacement-guard
    rows (which carry `skipped`) and kills with no matching task never have it.
    Reads only the ledger, so a ClickUp outage cannot break this step.
    """
    p = DURABLE / "kills.jsonl"
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
    # `process --dry-run` writes no last_run.json, so a dry digest still previews.
    this_run = str(read_json(DURABLE / "last_run.json", {}).get("last_run", ""))
    if not dry and latest[:19] != this_run[:19]:
        print(f"digest: this run ({this_run[:19] or 'no last_run.json'}) ledgered no kills — "
              f"newest ledger stamp is {latest[:19]}, already reported")
        return 0
    synced = [r for r in runs if r["detected"] == latest and r.get("task_id")]
    if not synced:
        print(f"digest: run {latest[:19]} updated no ClickUp tasks")
        return 0
    synced.sort(key=lambda r: r.get("name", ""))
    links = []
    for r in synced:
        name = r.get("name", "")
        label = batch_prefix(name) or name[:12] or "?"
        if r.get("kind") == "ad":
            label += f" {ad_key(name)}"
        links.append(f"[{label}](https://app.clickup.com/t/{r['task_id']})" + (" ⚠️" if r.get("error") else ""))
    embed = {"title": f"\u2705 {len(synced)} kill(s) → ClickUp",
             "description": " · ".join(links)[:4000], "color": 0x2ECC71}
    return 0 if discord([embed], None, dry) else 3


def push_state(branch: str = "main") -> int:
    """Commit DURABLE and push it to `branch`. 0 = pushed or nothing to push.

    The Multica checkout sits on a throwaway `agent/...` branch that does not
    exist on GitHub, so this pushes `HEAD:main` explicitly — "push the current
    branch" fails there every time. State files never conflict with human
    commits, so the one commit is rebased onto the target first.
    """
    import subprocess

    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} -> {r.stderr.strip() or r.returncode}")
        return r.stdout.strip()

    try:
        rel = DURABLE.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        print(f"push-state: {DURABLE} is outside the repo — nothing to push")
        return 0
    try:
        git("add", "--", rel)
        if not git("status", "--porcelain", "--", rel):
            print("push-state: state unchanged — nothing to push")
            return 0
        last = read_json(DURABLE / "last_run.json", {})
        git("-c", "user.name=kill-sync", "-c", "user.email=kill-sync@voana.local", "commit", "-q",
            "-m", f"kill-sync: {str(last.get('last_run', '?'))[:16]} — {last.get('kills', 0)} kill(s)")
        git("pull", "--rebase", "-q", "origin", branch)
        git("push", "-q", "origin", f"HEAD:{branch}")
    except RuntimeError as exc:
        print(f"push-state FAILED: {exc}")
        return 4
    print(f"push-state: pushed {rel} to origin/{branch}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["process", "digest", "replay", "rewrite", "push-state"])
    ap.add_argument("inbox", nargs="?", help="replay/rewrite: an archived inbox dir (data/kill-sync/inbox-archive/<stamp>)")
    ap.add_argument("--only", action="append", default=[], metavar="BATCH",
                    help="rewrite: only these batch prefixes, e.g. --only S182 (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-clickup", action="store_true", help="post Discord + record state, but do not write to ClickUp")
    a = ap.parse_args(argv)
    global SKIP_CLICKUP
    SKIP_CLICKUP = SKIP_CLICKUP or a.skip_clickup
    STATE.mkdir(exist_ok=True); INBOX.mkdir(exist_ok=True); DURABLE.mkdir(parents=True, exist_ok=True)
    if a.cmd == "push-state":
        return push_state()
    if a.cmd == "replay":
        if not a.inbox or not Path(a.inbox).is_dir():
            ap.error("replay needs an archived inbox directory")
        return process(a.dry_run, inbox=Path(a.inbox), replay=True)
    if a.cmd == "rewrite":
        if not a.inbox or not Path(a.inbox).is_dir():
            ap.error("rewrite needs an archived inbox directory")
        return process(a.dry_run, inbox=Path(a.inbox), rewrite=True, only=set(a.only) or None)
    return process(a.dry_run) if a.cmd == "process" else digest(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
