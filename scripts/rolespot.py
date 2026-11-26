"""Role-change assists (update 105, Nov 26 2026): NextLevelBets' way of finding players, as a rule the site can run.

His assists picks are the part of his record that wins two seasons running (2025-26 written plays: assists 43-28, +5.6U;
2026-27 ladders: assists base rung 6 of 9). His method: a player whose job just grew (starter out, more minutes, running
the offense) while the line is still set off his old job. As a rule on Kalshi assists rungs:
    last-5 assists average at least 1.25x his season average, AND last-5 minutes at least 1 above his season average,
    Kalshi ask 50-75%, the safest qualifying rung per player, up to 5 players a night (biggest assist jump first).
Replay Oct 28-Nov 22 (82 legs): 53-29, +10.5U (+12.8%/bet) BUT Oct 28-Nov 10 went 12-16 (-7.0U) and Nov 11-22 went
41-13 (+17.5U). Not proven, so it is tracked as TESTING and only counts in Proven picks once it earns it.
Legs come from triple.candidates (game not started, player not out/doubtful/questionable, no MINUTES DOWN, 5+ games).
"""
from __future__ import annotations

import json
import statistics as S
from collections import defaultdict
from datetime import date as _date, datetime, timezone
from pathlib import Path

import bestk
import tipsters as T
import triple

PATH = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "rolespot.json"
SIZE = 5
ASK_LO, ASK_HI = 0.50, 0.75
JUMP, MIN_UP, MIN_GAMES = 1.25, 1.0, 7


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _form(box, D: _date) -> dict:
    """player -> (season ast avg, last-5 ast avg, season min avg, last-5 min avg, games) before D, this season only."""
    start = _date(D.year if D.month >= 8 else D.year - 1, 10, 1)
    g = defaultdict(list)
    for x in box.select(["player", "date", "min", "ast"]).iter_rows(named=True):
        if x["player"] and start <= x["date"] < D and (x["min"] or 0) > 0:
            g[T._norm(x["player"])].append(x)
    out = {}
    for n, lg in g.items():
        if len(lg) < MIN_GAMES:
            continue
        lg.sort(key=lambda x: x["date"])
        l5 = lg[-5:]
        out[n] = (S.mean(x["ast"] for x in lg), S.mean(x["ast"] for x in l5),
                  S.mean(x["min"] for x in lg), S.mean(x["min"] for x in l5), len(lg))
    return out


def qualifies(f) -> bool:
    sea, l5, smin, m5, _n = f
    return sea > 0 and l5 >= JUMP * sea and m5 >= smin + MIN_UP


def sync(box, sl: dict, now: datetime | None = None, inj=None) -> None:
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load()
    cur = st.get(date) or {"legs": []}
    locked = [l for l in cur["legs"] if (bestk._tip(l) or now) <= now]
    players = {l["player"] for l in locked}
    form = _form(box, _date.fromisoformat(date))
    best = {}
    for c in triple.candidates(date, box, now, inj):
        if c["stat"] != "ast" or not ASK_LO <= c["ask"] <= ASK_HI or c["player"] in players:
            continue
        f = form.get(T._norm(c["player"]))
        if not f or not qualifies(f):
            continue
        c.update(sea=round(f[0], 1), l5=round(f[1], 1), smin=round(f[2], 1), m5=round(f[3], 1), jump=round(f[1] / f[0], 2))
        b = best.get(c["player"])
        if b is None or c["ask"] > b["ask"]:      # safest qualifying rung per player
            best[c["player"]] = c
    add = sorted(best.values(), key=lambda c: -c["jump"])[:max(SIZE - len(locked), 0)]
    legs = sorted(locked + add, key=lambda l: (l.get("tip") or "", -l.get("jump", 0)))
    if legs:
        st[date] = {"legs": legs, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    else:
        st.pop(date, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def summary(box, today: str | None) -> dict:
    return bestk.summary(box, today, load=_load)
