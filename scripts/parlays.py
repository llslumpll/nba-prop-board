"""
Parlays: the site builds its own parlays every day, locks them before the
first leg tips, and grades them, so the record shows whether its
combinations actually hit.

Families (each in 2-, 3- and 4-leg sizes unless noted):
  safe      "Safest props"   player props, any source, highest chance
  value     "Good price"     only legs whose chance beats their price (GOOD PRICE); 2 and 3 legs
  ml        "Moneyline"      game moneyline picks, highest chance

Rules
  * legs come from picks the site has already recorded (data/predictions,
    data/team_predictions): pending, not SKIP, game not started.
  * one leg per game (legs from the same game move together, which makes a
    parlay riskier than its chance says) and one per player.
  * until its FIRST leg tips, a parlay is rebuilt every build from the
    current best legs ("UPDATES TILL FIRST TIP"); from then on it's locked:
    legs, lines and sides are frozen exactly as they were.
  * grading uses the locked legs: a player leg against his final stat, a
    game leg against the final score. A leg that didn't play (or pushes) is
    dropped and the parlay pays as the smaller one, like most books; a parlay
    is LOST the moment any leg misses.
  * chance = the legs' chances multiplied; "pays about" = each leg's own
    price multiplied (how books price parlays; PrizePicks' fixed payout table
    can differ), 1 unit per parlay for the track record.

Stored in data/parlays/DATE.json, keyed "<family>-<size>".
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import predictions

DIR = Path(__file__).parent.parent / "data" / "parlays"
FAMILIES = [
    ("safe", "Safest props", (2, 3, 4), "The highest-chance player props on the board, one per game."),
    ("value", "Good price", (2, 3), "Only legs marked GOOD PRICE (their chance beats their price), highest chance first."),
    ("ml", "Moneyline", (2, 3, 4), "The site's moneyline picks with the highest chance to win, one per game."),
]
FAMILY = {f[0]: f for f in FAMILIES}
MIN_CHANCE = 0.5


def _now():
    return datetime.now(timezone.utc)


def _path(date):
    return DIR / f"{date}.json"


def load_date(date: str) -> dict:
    try:
        return json.loads(_path(date).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(date: str, d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _path(date).write_text(json.dumps(d, indent=1, default=str))


def all_dates() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.json")) if DIR.exists() else []


def _tip(p):
    try:
        return datetime.fromisoformat(p["tip_utc"])
    except Exception:  # noqa: BLE001
        return None


def _be(p):
    be = p.get("break_even")
    return be if be and 0.05 < be < 0.98 else 0.5238


def _kalshi_bet(p):
    """The same bet on Kalshi: OVER X.5 = YES on the (X+1)+ market, UNDER X.5 = NO on it."""
    if p.get("line_source") != "kalshi" and p.get("anchor") != "kalshi":
        return None
    n = int(round(p["line"] + 0.5))
    return f"{'YES' if p['call'] == 'OVER' else 'NO'} {p['player']} {n}+ {p['stat'].upper()}"


def _player_leg(k, p):
    return {"kalshi_bet": _kalshi_bet(p), "kind": "player", "key": k, "player": p["player"], "player_id": p.get("player_id"), "team": p["team"], "opp": p["opp"], "stat": p["stat"],
            "call": p["call"], "line": p["line"], "chance": p["chance"], "be": _be(p), "src": p.get("line_source"),
            "anchor": p.get("anchor"), "game_id": p["game_id"], "tip_utc": p["tip_utc"],
            "label": f"{p['player']} {p['call']} {p['line']:g} {p['stat'].upper()}"}


def _game_leg(k, t):
    return {"kind": "game", "key": k, "market": t["market"], "pick": t["pick"], "home": t["home"], "away": t["away"],
            "line": t.get("line"), "chance": t["chance"], "be": _be(t), "src": t.get("source"), "price_str": t.get("price_str"),
            "game_id": t["game_id"], "tip_utc": t["tip_utc"], "label": t.get("pick_label") or t["pick"]}


def _kalshi_ok(leg):
    return bool(leg.get("kalshi_bet")) or (leg["kind"] == "game" and leg["src"] == "kalshi")


def candidates(date: str, now: datetime) -> dict:
    """Open legs per family, best first."""
    ps = predictions.load_date(date)
    ts = predictions.load_date(date, team=True)

    def open_(p):
        t = _tip(p)
        return p.get("status") == "pending" and not p.get("skip") and t is not None and now < t and (p.get("chance") or 0) >= MIN_CHANCE

    pl = [_player_leg(k, p) for k, p in ps.items() if open_(p) and p.get("call") and p.get("line") is not None]
    gl = [_game_leg(k, t) for k, t in ts.items() if open_(t)]
    ml = [g for g in gl if g["market"] == "moneyline"]
    good = [x for x in pl if (ps[x["key"]].get("units") or 0) > 0] + [g for g in gl if (ts[g["key"]].get("units") or 0) > 0]
    by = lambda xs: sorted(xs, key=lambda x: -x["chance"])  # noqa: E731
    return {"safe": by(pl), "value": by(good), "ml": by(ml)}


def _pick_legs(cands: list[dict], n: int) -> list[dict] | None:
    legs, games, players = [], set(), set()
    for c in cands:
        if c["game_id"] in games or (c["kind"] == "player" and c["player"] in players):
            continue
        legs.append(c)
        games.add(c["game_id"])
        if c["kind"] == "player":
            players.add(c["player"])
        if len(legs) == n:
            return legs
    return None


def _summary(legs):
    ch = math.prod(l["chance"] for l in legs)
    pay = math.prod(1 / l["be"] for l in legs)
    return {"chance": round(ch, 4), "pays": round(pay, 2), "ev": round(ch * pay - 1, 4),
            "all_kalshi": all(_kalshi_ok(l) for l in legs), "first_tip": min(l["tip_utc"] for l in legs)}


def sync(date: str, now: datetime | None = None) -> dict:
    """Create / refresh tonight's parlays; locked ones never change."""
    now = now or _now()
    store = load_date(date)
    cands = candidates(date, now)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    made = updated = locked = 0
    for fam, _label, sizes, _desc in FAMILIES:
        for n in sizes:
            pid = f"{fam}-{n}"
            cur = store.get(pid)
            if cur and (cur.get("locked_at") or cur.get("status") != "pending"):
                locked += 1
                continue
            if cur and now >= datetime.fromisoformat(cur["first_tip"]):
                cur["locked_at"] = stamp
                locked += 1
                continue
            legs = _pick_legs(cands[fam], n)
            if not legs:
                continue
            rec = {"id": pid, "date": date, "family": fam, "size": n, "legs": legs, **_summary(legs),
                   "status": "pending", "hit": None}
            if not cur:
                rec.update(created_at=stamp, updated_at=stamp, changes=0)
                made += 1
            else:
                same = [l["key"] + l.get("call", l.get("pick", "")) + str(l.get("line")) for l in cur["legs"]] == \
                       [l["key"] + l.get("call", l.get("pick", "")) + str(l.get("line")) for l in legs]
                rec.update(created_at=cur["created_at"], updated_at=stamp if not same else cur["updated_at"],
                           changes=cur.get("changes", 0) + (0 if same else 1))
                if not same or abs(cur["chance"] - rec["chance"]) > 1e-4:
                    updated += 1
            store[pid] = rec
    _save(date, store)
    return {"made": made, "updated": updated, "locked": locked, "total": len(store)}


def _grade_leg(l, ps, ts):
    """'hit' | 'miss' | 'void' | None (not final yet)."""
    if l["kind"] == "player":
        p = ps.get(l["key"])
        if not p:
            return "void"
        if p.get("status") == "void":
            return "void"
        a = p.get("actual")
        if a is None:
            return None
        if a == l["line"]:
            return "void"
        return "hit" if (a > l["line"]) == (l["call"] == "OVER") else "miss"
    t = ts.get(l["key"])
    if not t:
        return "void"
    sc = t.get("scores")
    if not sc:
        return None
    hs, as_ = sc
    if l["market"] == "moneyline":
        return "hit" if (hs > as_) == (l["pick"] == l["home"]) else "miss"
    if l["market"] == "spread":
        adj = hs - as_ + l["line"]
        return "void" if adj == 0 else ("hit" if (adj > 0) == (l["pick"] == l["home"]) else "miss")
    tot = hs + as_
    return "void" if tot == l["line"] else ("hit" if (tot > l["line"]) == (l["pick"] == "OVER") else "miss")


def grade(now: datetime | None = None) -> dict:
    now = now or _now()
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    n_new = 0
    for date in all_dates():
        store = load_date(date)
        if not any(r["status"] == "pending" for r in store.values()):
            continue
        ps, ts = predictions.load_date(date), predictions.load_date(date, team=True)
        changed = False
        for r in store.values():
            if r["status"] != "pending":
                continue
            if not r.get("locked_at") and now >= datetime.fromisoformat(r["first_tip"]):
                r["locked_at"] = stamp
                changed = True
            if not r.get("locked_at"):
                continue
            res = [_grade_leg(l, ps, ts) for l in r["legs"]]
            for l, x in zip(r["legs"], res):
                if l.get("result") != x:
                    l["result"] = x
                    changed = True
            live = [l for l, x in zip(r["legs"], res) if x != "void"]
            if "miss" in res:
                r.update(status="graded", hit=False, graded_at=stamp)
            elif None in res:
                continue
            elif not live:
                r.update(status="void", hit=None, graded_at=stamp)
            else:
                r.update(status="graded", hit=True, graded_at=stamp)
            if r["status"] != "pending":
                r["legs_played"] = len(live)
                r["pays_final"] = round(math.prod(1 / l["be"] for l in live), 2) if live else None
                r["units"] = round(r["pays_final"] - 1, 2) if r["hit"] else (-1.0 if r["hit"] is False else 0.0)
                n_new += 1
                changed = True
        if changed:
            _save(date, store)
    return {"newly_graded": n_new}


def load_all() -> list[dict]:
    out = []
    for d in all_dates():
        out += list(load_date(d).values())
    return out


def summary() -> dict:
    allp = load_all()
    done = [r for r in allp if r["status"] == "graded"]
    rows = []
    for fam, label, sizes, desc in FAMILIES:
        for n in sizes:
            g = [r for r in done if r["family"] == fam and r["size"] == n]
            if not g and not any(r["family"] == fam and r["size"] == n for r in allp):
                continue
            hits = sum(1 for r in g if r["hit"])
            units = round(sum(r.get("units") or 0 for r in g), 2)
            legs = [l for r in g for l in r["legs"] if l.get("result") in ("hit", "miss")]
            rows.append({"family": fam, "label": label, "size": n, "n": len(g), "hits": hits,
                         "rate": round(100 * hits / len(g), 1) if g else None,
                         "expected": round(100 * sum(r["chance"] for r in g) / len(g), 1) if g else None,
                         "units": units, "roi": round(100 * units / len(g), 1) if g else None,
                         "leg_rate": round(100 * sum(1 for l in legs if l["result"] == "hit") / len(legs), 1) if legs else None,
                         "pending": sum(1 for r in allp if r["family"] == fam and r["size"] == n and r["status"] == "pending")})
    tot_n = len(done)
    tot_h = sum(1 for r in done if r["hit"])
    tot_u = round(sum(r.get("units") or 0 for r in done), 2)
    legs = [l for r in done for l in r["legs"] if l.get("result") in ("hit", "miss")]
    days = []
    for d in reversed(all_dates()):
        st = load_date(d)
        days.append({"date": d, "rows": sorted(st.values(), key=lambda r: ([f[0] for f in FAMILIES].index(r["family"]), r["size"]))})
    return {"rows": rows, "n": tot_n, "hits": tot_h, "units": tot_u, "roi": round(100 * tot_u / tot_n, 1) if tot_n else None,
            "rate": round(100 * tot_h / tot_n, 1) if tot_n else None,
            "expected": round(100 * sum(r["chance"] for r in done) / tot_n, 1) if tot_n else None,
            "leg_rate": round(100 * sum(1 for l in legs if l["result"] == "hit") / len(legs), 1) if legs else None,
            "pending": sum(1 for r in allp if r["status"] == "pending"), "days": days[:30],
            "families": [{"key": f[0], "label": f[1], "sizes": f[2], "desc": f[3]} for f in FAMILIES]}
