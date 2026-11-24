"""
Teleios game picks, side by side with ours.

NBA Teleios Analyst (github.com/llslumpll/nba-teleios-analyst) predicts every
game with its own model: a team's strength is the sum of the players who will
actually play, plus home court, rest, travel and altitude. Its predictions are
frozen at tip-off and graded in its own repo.

This module reads those predictions (public raw GitHub files, no key), keeps a
copy in data/teleios/<date>.json, and attaches Teleios's pick to each game on
our slate so the Games page can show both picks and keep score of both on the
same games. It changes none of our own picks. If Teleios can't be reached, the
last copy is used and the Games page simply shows no Teleios row.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import requests

RAW = "https://raw.githubusercontent.com/llslumpll/nba-teleios-analyst/main/data/predictions/{d}.json"
SITE = "https://llslumpll.github.io/nba-teleios-analyst/"
DIR = Path(__file__).parent.parent / "data" / "teleios"
TEAM_DIR = Path(__file__).parent.parent / "data" / "team_predictions"
KEEP = ("game_id", "date", "home", "away", "pick", "chance", "p_home", "margin", "market_p_home",
        "locked", "frozen_at", "tip_utc", "result")


def _slim(rec: dict) -> dict:
    out = {k: rec.get(k) for k in KEEP}
    why = rec.get("why") or {}
    out["out"] = {t: [o.get("player") for o in lst if (o.get("minutes") or 0) >= 20]
                  for t, lst in (why.get("out") or {}).items()}
    return out


def fetch(day: str) -> dict:
    """Teleios's predictions for one date, keyed by game_id (str). Never raises."""
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{day}.json"
    try:
        r = requests.get(RAW.format(d=day), timeout=20)
        if r.status_code == 404:
            return _load(path)
        r.raise_for_status()
        data = {k: _slim(v) for k, v in r.json().items()}
        if data:
            path.write_text(json.dumps(data, indent=1, sort_keys=True))
        return data
    except Exception:  # noqa: BLE001
        return _load(path)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}


def attach(slate: dict) -> int:
    """Put Teleios's pick on each game as g["teleios"]. Also refreshes the
    last few days so finished games get Teleios's grades."""
    days = {g["date"] for g in slate.get("games", []) if g.get("date")}
    if slate.get("date"):
        d0 = date.fromisoformat(slate["date"])
        days |= {str(d0 - timedelta(days=i)) for i in range(1, 4)}
    cache = {d: fetch(d) for d in sorted(days)}
    n = 0
    for g in slate.get("games", []):
        t = cache.get(g.get("date"), {}).get(str(g["game_id"]))
        if t:
            t = dict(t)
            t["url"] = f"{SITE}#game"
            g["teleios"] = t
            n += 1
    return n


def head_to_head(since: str = "2026-11-23") -> dict:
    """Moneyline record of both models on the same graded games."""
    rows = []
    for f in sorted(DIR.glob("*.json")) if DIR.exists() else []:
        if f.stem < since:
            continue
        tel = _load(f)
        ours = _load(TEAM_DIR / f.name)
        ml = {str(v.get("game_id")): v for k, v in ours.items() if k.endswith("|moneyline")}
        for gid, t in tel.items():
            res = t.get("result")
            o = ml.get(gid)
            if not res or not o or o.get("hit") is None:
                continue
            rows.append({"date": f.stem, "game": f"{t['away']} @ {t['home']}", "teleios": t["pick"], "teleios_hit": bool(res.get("hit")),
                         "ours": o.get("pick"), "ours_hit": bool(o.get("hit")), "disagree": t["pick"] != o.get("pick")})
    if not rows:
        return {"games": 0, "since": since, "site": SITE}
    dis = [r for r in rows if r["disagree"]]
    return {"games": len(rows), "since": since, "site": SITE,
            "teleios": sum(r["teleios_hit"] for r in rows), "ours": sum(r["ours_hit"] for r in rows),
            "disagreements": len(dis), "teleios_right_when_split": sum(r["teleios_hit"] for r in dis),
            "ours_right_when_split": sum(r["ours_hit"] for r in dis), "recent": rows[-12:][::-1]}
