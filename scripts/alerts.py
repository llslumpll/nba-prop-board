"""
Phone alerts (update 50): the site's best edges are about timing (late injury news, lines that haven't moved),
so each build posts the new ones to a Discord channel the moment they appear.

Setup: Discord channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL, then
GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret DISCORD_WEBHOOK. No secret
= no alerts (and nothing breaks). The URL lives only in that secret.

What gets sent (each thing once; data/alerts_state.json remembers what was sent):
  * injury news: a player newly ruled out, with the teammate plays whose lines haven't moved yet
  * a new injury-news single / parlay
  * Kalshi lagging the books: only once that tracker has earned a stake
  * a morning recap (first build after 9 AM CT): last night's results in units and dollars (1U = $100)
  * a one-time "connected" message the first time the secret works
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent / "data"
STATE = ROOT / "alerts_state.json"
SITE = "https://llslumpll.github.io/nba-prop-board/"
CT = ZoneInfo("America/Chicago")
UNIT_USD = 100
MAX_LEN = 1900   # Discord's limit is 2000 characters per message
STAT = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA", "pr": "P+R", "pa": "P+A", "ra": "R+A"}
SRC = {"prizepicks": "PrizePicks", "kalshi": "Kalshi", "fanduel": "FanDuel"}


def _url() -> str | None:
    u = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
    return u if u.startswith("https://") else None


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"sent": {}}


def _save(st: dict, now: datetime) -> None:
    cut = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    st["sent"] = {k: v for k, v in st.get("sent", {}).items() if v[:10] >= cut}
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def _post(url: str, text: str) -> bool:
    body = json.dumps({"content": text[:MAX_LEN], "username": "Prop Edge NBA", "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "prop-edge-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"Alert not sent: {str(e)[:120]}")
        return False


def _ct(ts) -> str:
    d = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace(" UTC", "+00:00").replace("Z", "+00:00"))
    return d.astimezone(CT).strftime("%-I:%M %p CT")


def _money(u: float) -> str:
    return f"{u:+.2f}U ({'+' if u >= 0 else '-'}${abs(u) * UNIT_USD:,.0f})"


def news_messages(date: str, now: datetime) -> list[tuple[str, str]]:
    import news
    evs = news.load_date(date)
    news.mark_counted(evs)
    out = []
    for ev in sorted(evs.values(), key=lambda e: e["seen_at"]):
        if now >= datetime.fromisoformat(ev["tip_utc"]):
            continue
        plays = [b for b in ev["plays"] if b.get("counted", True) and not b.get("moved")]
        if not plays:
            continue
        lines = [f"🚨 **{ev['player']} ({ev['team']}) ruled {(ev.get('status') or 'OUT').upper()}** vs {ev['opp']} · tip {_ct(ev['tip_utc'])}",
                 "Teammates whose lines haven't moved yet:"]
        for b in plays[:6]:
            lines.append(f"• **{b['player']} {b['call']} {b['line']:g} {STAT.get(b['stat'], b['stat'])}** ({SRC.get(b.get('src'), b.get('src') or '')})"
                         f" — {b['bump']:+.1f} without him over {b['n_wo']} games")
        s = news.summary(None)
        lines.append(f"News plays so far: {s.get('hits', 0)}/{s.get('n', 0)}"
                     + (" · stake earned: 0.5U" if s.get("earned") else " · tracking, no stake yet"))
        lines.append(SITE)
        out.append((f"news|{ev['id']}", "\n".join(lines)))
    return out


def parlay_messages(date: str) -> list[tuple[str, str]]:
    import parlays
    out = []
    for pid, r in sorted(parlays.load_date(date).items()):
        if r["family"] != "news" or r.get("status") != "pending" or r.get("locked_at"):
            continue
        legs = "\n".join(f"• {l['label']}" + (f" ({l['news_out']} out)" if l.get("news_out") else "") for l in r["legs"])
        what = "single" if r["size"] == 1 else f"{r['size']}-leg parlay"
        stake = f"{r['stake']:g}U" if r.get("stake") else "tracking only, no stake"
        key = f"parlay|{date}|{pid}|" + "|".join(l["key"] + l.get("call", "") for l in r["legs"])
        out.append((key, f"🎟️ **New injury-news {what}** · locks {_ct(r['first_tip'])} · {stake}\n{legs}\n{SITE}parlays.html"))
    return out


def lag_messages(date: str) -> list[tuple[str, str]]:
    import market
    s = market.lag_summary(date)
    if not s.get("earned"):
        return []
    out = []
    for r in s["today"][:8]:
        if not r.get("cost"):
            continue
        books = ", ".join(f"{SRC.get(b, b)} {v:g}" for b, v in r["books"].items())
        out.append((f"lag|{r['game_id']}|{r['player']}|{r['stat']}",
                    f"📉 **Kalshi lagging: {r['label']}** — {r['bet']} at {r['cost'] * 100:.0f}% · books: {books} · tip {_ct(r['tip_utc'])} · 0.5U\n{SITE}games.html"))
    return out


def recap_message(now: datetime) -> tuple[str, str] | None:
    """Once a day, the first build after 9 AM CT: yesterday's slate."""
    local = now.astimezone(CT)
    if local.hour < 9:
        return None
    y = (local - timedelta(days=1)).strftime("%Y-%m-%d")
    import news
    import parlays
    import predictions
    ps = [p for p in predictions.load_date(y).values() if p.get("status") == "graded" and p.get("hit") is not None]
    pz = [r for r in parlays.load_date(y).values() if r.get("status") == "graded"]
    if not ps and not pz:
        return None
    lines = [f"🌅 **Recap {y}**"]
    if ps:
        h = sum(1 for p in ps if p["hit"])
        st = [p for p in ps if (p.get("units") or 0) > 0]
        u = sum(p["units"] * (((1 / p["break_even"]) - 1) if p["hit"] else -1) for p in st if p.get("break_even"))
        lines.append(f"Player picks: {h}/{len(ps)} ({100 * h / len(ps):.1f}%) · staked picks {_money(u)}")
    main = [r for r in pz if r["family"] != "news"]
    if main:
        u = sum(r.get("staked_units", r.get("units")) or 0 for r in main)
        lines.append(f"Parlays: {sum(1 for r in main if r['hit'])}/{len(main)} cashed · {_money(u)}")
    nz = [r for r in pz if r["family"] == "news"]
    if nz:
        lines.append(f"News parlays (tracking): {sum(1 for r in nz if r['hit'])}/{len(nz)}")
    nd = [b for ev in news.load_date(y).values() for b in ev["plays"] if b.get("result") in ("hit", "miss")]
    if nd:
        lines.append(f"News plays: {sum(1 for b in nd if b['result'] == 'hit')}/{len(nd)}")
    lines.append(SITE)
    return (f"recap|{y}", "\n".join(lines))


def run(sl: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    url = _url()
    if not url:
        return {"enabled": False}
    st = _load()
    sent = st.setdefault("sent", {})
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    msgs = []
    if not st.get("connected"):
        msgs.append(("connected", "✅ Prop Edge NBA alerts are connected. You'll get injury-news plays, news parlays, "
                                  f"Kalshi lags (once they earn a stake) and a morning recap here.\n{SITE}"))
    date = sl.get("date")
    for fn in ((lambda: news_messages(date, now)), (lambda: parlay_messages(date)), (lambda: lag_messages(date))):
        try:
            if date:
                msgs += fn()
        except Exception as e:  # noqa: BLE001
            print(f"Alert check failed: {e}")
    try:
        rc = recap_message(now)
        if rc:
            msgs.append(rc)
    except Exception as e:  # noqa: BLE001
        print(f"Recap failed: {e}")
    n = 0
    for key, text in msgs:
        if key in sent:
            continue
        if _post(url, text):
            sent[key] = stamp
            n += 1
            if key == "connected":
                st["connected"] = stamp
    _save(st, now)
    return {"enabled": True, "sent": n}
