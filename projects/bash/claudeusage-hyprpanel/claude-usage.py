#!/usr/bin/env python3
"""Claude usage fetcher for a HyprPanel custom module.

Reads the Claude Code OAuth token from ~/.claude/.credentials.json, queries
the Anthropic usage endpoint, and prints one JSON object that HyprPanel's
custom-module templating consumes ({session}, {weekly}, {fable}, {tooltip},
plus `alt`/`percentage` for icon selection).

On network/auth failure it falls back to the last successful result (marked
stale) so the bar doesn't flicker to an error state every time the laptop
sleeps or the token is mid-refresh.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")
CACHE_FILE = os.path.expanduser("~/.cache/hyprpanel-claude-usage.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def fmt_reset(iso_ts):
    """'2026-08-16T12:59:59.9+00:00' -> 'Sun 15:00' (or '15:00' if today)."""
    if not iso_ts:
        return "?"
    ts = datetime.fromisoformat(iso_ts).astimezone()
    now = datetime.now(ts.tzinfo)
    hhmm = ts.strftime("%H:%M")
    return hhmm if ts.date() == now.date() else f"{ts.strftime('%a')} {hhmm}"


def fetch_usage():
    with open(CREDENTIALS_FILE) as f:
        token = json.load(f)["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "hyprpanel-claude-usage/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def build_output(data):
    session = weekly = scoped = None
    scoped_name = "Fable"
    severity_rank = {"normal": 0}
    worst = "normal"

    for limit in data.get("limits", []):
        kind = limit.get("kind")
        entry = {
            "percent": limit.get("percent", 0),
            "resets": fmt_reset(limit.get("resets_at")),
        }
        if kind == "session":
            session = entry
        elif kind == "weekly_all":
            weekly = entry
        elif kind == "weekly_scoped":
            scoped = entry
            name = (limit.get("scope") or {}).get("model") or {}
            scoped_name = name.get("display_name") or scoped_name
        sev = limit.get("severity", "normal")
        if severity_rank.get(sev, 1) > severity_rank.get(worst, 1) or sev != "normal":
            worst = sev if sev != "normal" else worst

    empty = {"percent": "-", "resets": "?"}
    session, weekly, scoped = session or empty, weekly or empty, scoped or empty

    tooltip = (
        f"Session: {session['percent']}% (resets {session['resets']})\n"
        f"Weekly all models: {weekly['percent']}% (resets {weekly['resets']})\n"
        f"Weekly {scoped_name}: {scoped['percent']}% (resets {scoped['resets']})"
    )

    percents = [
        v["percent"] for v in (session, weekly, scoped) if isinstance(v["percent"], (int, float))
    ]
    return {
        "session": session["percent"],
        "weekly": weekly["percent"],
        "fable": scoped["percent"],
        "percentage": max(percents) if percents else 0,
        "alt": worst,
        "tooltip": tooltip,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    try:
        output = build_output(fetch_usage())
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(output, f)
    except Exception:
        try:
            with open(CACHE_FILE) as f:
                output = json.load(f)
            output["alt"] = "stale"
            output["tooltip"] += "\n(stale — last fetch failed)"
        except Exception:
            output = {
                "session": "-", "weekly": "-", "fable": "-",
                "percentage": 0, "alt": "stale",
                "tooltip": "Claude usage unavailable",
            }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
