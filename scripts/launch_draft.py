#!/usr/bin/env python3
"""launch_draft.py — hand a prepared batch to the dashboard for review, and
activate only what a person approved there.

The launch manager prepares a batch as before (ad set + ads created PAUSED in
Meta). Instead of activating it, it posts the result to the dashboard's
/launches page. A person checks every ad and approves; only then does the agent
activate — exactly the Meta ids the dashboard hands back. Full flow: CLAUDE.md →
"Dashboard review flow".

  launch_draft.py submit draft.json     post (or replace) a draft for review
  launch_draft.py approved              the work list: approved + sent-back batches
  launch_draft.py result KEY STATUS     report launching | launched | failed
        [--error TEXT] [--ads ads.json]

Env (.env in the repo root, or the process environment):
  VOANA_DASHBOARD_URL   e.g. https://<dashboard host>
  LAUNCH_AGENT_TOKEN    shared secret, same value as on the dashboard

This script never talks to Meta. It moves JSON between the agent and the
dashboard; the Meta writes stay with the Meta Ads MCP.

Exit codes: 0 ok · 2 usage/config · 3 the dashboard refused (message printed).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ("launching", "launched", "failed")


def load_env() -> dict:
    env = dict(os.environ)
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


class Refused(Exception):
    """The dashboard answered, and the answer was no."""


def call(env: dict, method: str, path: str, body=None):
    base = env.get("VOANA_DASHBOARD_URL", "").rstrip("/")
    token = env.get("LAUNCH_AGENT_TOKEN", "")
    if not base or not token:
        print("VOANA_DASHBOARD_URL and LAUNCH_AGENT_TOKEN must be set (.env or environment).", file=sys.stderr)
        sys.exit(2)
    req = urllib.request.Request(
        f"{base}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Explicit: some edges reject python-urllib's default agent.
            "User-Agent": "voana-launch-manager/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {"error": raw[:300]}
        lines = [f"HTTP {e.code}: {detail.get('error', 'refused')}"]
        lines += [f"  - {x}" for x in detail.get("errors", [])]
        raise Refused("\n".join(lines)) from None


def submit(env: dict, path: str) -> int:
    draft = json.loads(Path(path).read_text())
    out = call(env, "POST", "/api/launches/drafts", draft)
    print(f"{out['draft_key']} → revision {out['revision']} ({'new' if out['created'] else 'replaced'})")
    if out.get("url"):
        print(out["url"])
    for f in out.get("qa_flags", []):
        print(f"  [{f['level']}] {f['message']}")
    if any(f["level"] == "block" for f in out.get("qa_flags", [])):
        print("Blocking flags: a reviewer cannot approve this until they are fixed and resubmitted.")
    return 0


def approved(env: dict) -> int:
    print(json.dumps(call(env, "GET", "/api/launches/drafts"), indent=2))
    return 0


def result(env: dict, key: str, status: str, error, ads_path) -> int:
    body = {"draft_key": key, "status": status}
    if error:
        body["error"] = error
    if ads_path:
        body["ads"] = json.loads(Path(ads_path).read_text())
    out = call(env, "POST", "/api/launches/result", body)
    print(f"{key}: {out['status']}" + ("" if out.get("changed") else " (already reported)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("draft")
    sub.add_parser("approved")
    r = sub.add_parser("result")
    r.add_argument("draft_key")
    r.add_argument("status", choices=RESULTS)
    r.add_argument("--error")
    r.add_argument("--ads")
    a = ap.parse_args(argv)

    env = load_env()
    try:
        if a.cmd == "submit":
            return submit(env, a.draft)
        if a.cmd == "approved":
            return approved(env)
        return result(env, a.draft_key, a.status, a.error, a.ads)
    except Refused as e:
        print(e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
