"""Tipster tracker (update 78).

The accounts the user follows on X were checked against final box scores from opening night (Nov 18 2026 review).
Only the ones with a real winning record on every pick they posted are tracked: Dr. Profit, WizBetz, byrnetts, PeakyProps,
tipsterdiogo and IzzoBets
(player props; the last two added Nov 19 after a free-account search), plus
Goblin's spread legs. Their picks are logged by hand (Claude reads their posts when asked) into
data/tipsters/picks.json; this module prices, grades and summarizes them on every build.

  picks.json  {"picks": [{id, date, tipster, kind: prop|spread, legs:[{player, stat, line, side}] | team+line,
                          odds, book, posted (UTC ISO or null for the season backfill), odds_est}]}
  state.json  written by the build: {"price": {id: {...}}, "combos": {date: {...}}}

Tail test: the first time a build sees a pick before its game tips, it records the best price YOU could get right then
(Kalshi rung at the same number, or FanDuel at the same line) and grades the pick at that price as well.
Cushion combo: each day, 2-4 of their prop overs moved one Kalshi rung safer (legs costing 65-82%), locked at the
first tip and graded. Everything is tracking only: nothing here carries a stake.
"""
from __future__ import annotations

import json
import math
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "data" / "tipsters"
PICKS = DIR / "picks.json"
STATE = DIR / "state.json"
PRED_DIR = Path(__file__).resolve().parent.parent / "data" / "predictions"

TIPSTERS = {
    "doc": {"name": "Dr. Profit", "handle": "@DoctorProfit", "what": "straights", "honest": True,
            "note": "Role-player props after a minutes or role change. Posts the price he got; tailers often get worse."},
    "wiz": {"name": "WizBetz", "handle": "@WizBetz", "what": "straights", "honest": True,
            "note": "Points/rebounds/assists combos on role players, repeat \"wagon\" bets. Some season odds estimated at -115."},
    "byrn": {"name": "byrnetts", "handle": "@byrnetts", "what": "straights", "honest": True,
             "note": "Big nightly lists of 1U props (4-18 a night), running November record posted daily and it matches the box scores. Rarely posts odds: graded at -110 (est.). Added Nov 19."},
    "peaky": {"name": "PeakyProps", "handle": "@PeakyProps", "what": "straights", "honest": True,
              "note": "Daily 3-5 prop card at posted odds with a full recap. Best on assists, rebounds and reb+ast; points about break-even. Added Nov 19."},
    "diogo": {"name": "tipsterdiogo", "handle": "@tipsterdiogo", "what": "straights", "honest": True,
              "note": "'Sneaky Picks' / 'Fade Zone' props at posted odds, often collabs with WizBetz. Posts misses too. Added Nov 19."},
    "izzo": {"name": "IzzoBets", "handle": "@IzzoBets", "what": "straights", "honest": False,
             "note": "Daily 'NBA Prop #1-6' cards with odds. Every posted pick graded here, but he never posts a miss himself. Added Nov 19."},
    "gobpm": {"name": "Goblin Polymarket", "handle": "@PotdPrizepicks", "what": "Polymarket daily combo", "honest": False,
              "note": "His 'Daily $1,000' Polymarket combos: 6-7 legs priced about 70% each (overs and 'No' unders), paying about 6-7x. Graded 1U per combo; the legs record shows which props he gets right. Added Nov 19."},
    "wizv": {"name": "WizBetz video leans", "handle": "@WizBetz on YouTube", "what": "video leans", "honest": True,
             "note": "Leans from his daily YouTube breakdown (his official play is logged under WizBetz). Graded at -110 (est.). Added Nov 19."},
    "gob": {"name": "Goblin", "handle": "@PotdPrizepicks", "what": "spread legs", "honest": False,
            "note": "Only the spread legs out of his parlays, graded as -110 straights. His full parlays went 1 of 10."},
    "samb": {"name": "Sam boring straights", "handle": "@TheModelManSam", "what": "straights", "honest": False,
             "note": "His free daily 'boring straight' (one NBA prop, spread or moneyline). NBA ones only; his college and NFL straights aren't graded here. Usually posts the wins, rarely the misses. Added Nov 20 (backfilled from Oct 27)."},
    "sam": {"name": "Sam", "handle": "@TheModelManSam", "what": "safest lotto slip", "honest": None,
            "note": "The lowest tier of his daily 3-tier lotto ladder (3 role players at low lines). Priced +210 or shorter it went 7 of 11 through Nov 17."},
}
SHORT_MAX = 210  # Sam's safest slip: the ones priced +210 or shorter are the ones that cashed
DROPPED = ("FieldsFocus (61-60, -6.8U), vanishpicks (9-13, -5.4U), QuentinLocks (12-13, -3.6U), billhpicks (19-17, -0.5U), AngeloProps (7-6, -0.5U), swan_wbg (36-57, -25.4U), ZiggyBetsFR (ladders, 14-14 flat), NextLevelBets (31-31, -4.0U, posts only winning days), "
           "Bearprops (13-14 legs, never posts a loss)")
LAB = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA", "pr": "P+R", "pa": "P+A", "ra": "R+A", "dd": "DOUBLE-DOUBLE"}
PARTS = {"pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",), "pra": ("pts", "reb", "ast"),
         "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}
KFEE = 0.07
CUSH_LO, CUSH_HI = 0.75, 0.92   # update 87: cushion legs 75-92% (the user wants the safest, most probable legs)
ML_MIN_FAIR, ML_MAX_COST = 0.75, 0.95   # favorites' moneylines added as safe legs (Kalshi price)
EARN_N = 50


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    for ch in ".'-":
        s = s.replace(ch, "" if ch != "-" else " ")
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


def am_to_p(o: int | float | None) -> float | None:
    if o is None:
        return None
    return (-o) / (-o + 100) if o < 0 else 100 / (o + 100)


def p_to_am(p: float | None) -> str:
    if not p or p <= 0 or p >= 1:
        return "-"
    return f"-{round(100 * p / (1 - p))}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def profit(o, win: bool) -> float:
    if not win:
        return -1.0
    return (100 / -o) if o < 0 else o / 100


def _kcost(ask: float) -> float:
    return ask + KFEE * ask * (1 - ask)


# ---------------------------------------------------------------- grading
def _box_index(box):
    """{(date_iso, normname): row} and {(date_iso, team): (team_score, opp_score, opp)} for the current season."""
    import polars as pl
    if box is None or not box.height:
        return {}, {}
    cur = box.filter(pl.col("date") >= datetime(2026, 10, 1).date())
    ply, team = {}, {}
    for r in cur.iter_rows(named=True):
        d = r["date"].isoformat()
        ply[(d, _norm(r["player"]))] = r
        if r["team_score"] is not None:
            team[(d, r["team"])] = (r["team_score"], r["opp_score"], r["opp"])
    return ply, team


def _find(ply: dict, d: str, name: str):
    k = _norm(name)
    r = ply.get((d, k))
    if r is not None:
        return r
    last = k.split()[-1] if k else ""
    hits = [v for (dd, n), v in ply.items() if dd == d and n.split()[-1:] == [last] and n[:1] == k[:1]]
    return hits[0] if len(hits) == 1 else None


def _val(r, stat):
    if stat == "dd":
        return sum(int((r.get(c) or 0) >= 10) for c in ("pts", "reb", "ast", "stl", "blk"))
    return sum(int(r.get(c) or 0) for c in PARTS[stat])


def grade_leg(leg: dict, d: str, ply: dict, team: dict | None = None):
    """-> (result 'hit'|'miss'|'push'|'void'|None, actual, player row)"""
    if leg.get("res"):                      # graded by hand (e.g. an NFL leg inside a Polymarket combo)
        return leg["res"], leg.get("actual"), None
    if leg.get("kind") == "total":
        t = (team or {}).get((d, leg["team"]))
        if not t:
            return None, None, None
        tot = t[0] + t[1]
        if tot == float(leg["line"]):
            return "push", tot, None
        return ("hit" if (tot > float(leg["line"])) == (leg.get("side", "over") == "over") else "miss"), tot, None
    if leg.get("kind") == "spread":
        t = (team or {}).get((d, leg["team"]))
        if not t:
            return None, None, None
        m = t[0] - t[1] + float(leg["line"])
        return ("push" if m == 0 else ("hit" if m > 0 else "miss")), t[0] - t[1], None
    r = _find(ply, d, leg["player"])
    if r is None:
        return None, None, None
    if not r["played"]:
        return "void", None, r
    x = _val(r, leg["stat"])
    ln = float(leg["line"])
    if x == ln:
        return "push", x, r
    over = leg.get("side", "over") == "over"
    return ("hit" if (x > ln) == over else "miss"), x, r


def grade_pick(p: dict, ply: dict, team: dict) -> dict:
    d = p["date"]
    if p["kind"] == "spread":
        t = team.get((d, p["team"]))
        if not t:
            return {"status": "open"}
        margin = t[0] - t[1]
        m = margin + float(p["line"])
        res = "push" if m == 0 else ("hit" if m > 0 else "miss")
        return {"status": "graded", "res": res, "detail": f"{'won' if margin > 0 else 'lost'} by {abs(margin)} vs {t[2]}"}
    legs, res_all = [], []
    for leg in p["legs"]:
        res, x, r = grade_leg(leg, d, ply, team)
        legs.append({**leg, "res": res, "actual": x, "team": (r["team"] if r is not None else leg.get("team"))})
        res_all.append(res)
    if "miss" not in res_all and any(r is None for r in res_all):
        return {"status": "open", "legs": legs}
    if "miss" in res_all:
        res = "miss"
    elif "void" in res_all:
        res = "void"
    elif all(r == "push" for r in res_all):
        res = "push"
    else:
        res = "hit"
    return {"status": "graded", "res": res, "legs": legs}


# ---------------------------------------------------------------- prices
def _pred_rows(date: str) -> dict:
    d = _load(PRED_DIR / f"{date}.json", {})
    out = {}
    for k, r in (d.items() if isinstance(d, dict) else []):
        out.setdefault((_norm(r.get("player", "")), r.get("stat")), r)
    return out


def your_price(leg: dict, prow: dict | None) -> dict | None:
    """Best price you could get for this exact leg right now: Kalshi rung at the same number, or FanDuel at the same line."""
    if not prow:
        return None
    ln, over = float(leg["line"]), leg.get("side", "over") == "over"
    best = None
    lad = prow.get("kladder") or []
    if ln != int(ln):
        n = math.ceil(ln)
        for rung, bid, ask in lad:
            if rung == n:
                c = _kcost(ask) if over and ask else (_kcost(1 - bid) if (not over and bid) else None)
                if c:
                    best = {"src": "Kalshi", "p": round(c, 4)}
    fd = (prow.get("mkt") or {}).get("fanduel")
    if fd and fd[0] == ln:
        c = fd[1] if over else fd[2]
        if c and (best is None or c < best["p"]):
            best = {"src": "FanDuel", "p": round(c, 4)}
    try:  # Polymarket US, same number (update 79)
        import polymarket_client
        c = polymarket_client.prop_cost(leg["player"], leg["stat"], ln, over, prow.get("game_id"), prow.get("date"))
        if c and (best is None or c < best["p"]):
            best = {"src": "Polymarket", "p": round(c, 4)}
    except Exception:  # noqa: BLE001
        pass
    return best


CUSH_SOFT = {"pts": 5, "reb": 3, "ast": 2.5, "fg3m": 1, "pra": 6, "pr": 5, "pa": 5, "ra": 3}   # update 87: per-stat cushion
CUSH_SRC = ("doc", "wiz", "byrn", "peaky", "diogo", "izzo", "samb", "gobpm", "sam", "tacos", "fav")  # leg priority when a player repeats
CUSH_TIERS = (4.0, 5.0, 6.5, 8.0)   # +300 (cushion legs 75-85% only) and +400 / +550 / +700 (all safe legs)
CUSH_300_HI = 0.85
CUSH_MAX_LEGS = 16   # safest-first legs are 86-92%, so +400 takes about 13 of them


def minutes_flag(prow: dict | None) -> dict | None:
    """MINUTES DOWN (update 89): the model projects 1.5+ fewer minutes than his recent games, read from the
    projection's own note ('27.1 min (recent 31.6, ...)'). Sam's legs with minutes dropping hit 13/22 vs 32/39 holding."""
    if not prow:
        return None
    import re as _re
    m = _re.search(r"([\d.]+) min \(recent ([\d.]+)", prow.get("why") or "")
    if not m:
        return None
    proj, recent = float(m.group(1)), float(m.group(2))
    return {"proj": proj, "recent": recent} if proj < recent - 1.5 else None


def split_leg(leg: dict, rows: dict) -> dict | None:
    """For a two/three-stat pick (P+A, P+R, R+A, PRA) Kalshi doesn't list (update 88): check the rebounds and
    assists parts the player has a Kalshi ladder for (points parts lost in the replay) and keep the most upside: the rung
    costing 75-92% where the model's chance beats the price by the most (ties: the likelier rung)."""
    import model as _model
    over = leg.get("side", "over") == "over"
    if not over or leg.get("stat") not in ("pa", "pr", "ra", "pra"):
        return None
    best = None
    for comp in PARTS[leg["stat"]]:
        if comp == "pts":   # replay Oct 31-Nov 19: points parts hit 37/48 (77%) vs about 85% price; rebounds 94/108, assists 55/62
            continue
        r = rows.get((_norm(leg["player"]), comp))
        if not r or not r.get("kladder"):
            continue
        mu, var = r.get("blend_mean") or r.get("projected"), r.get("var")
        for rung, _bid, ask in r["kladder"]:
            if not ask:
                continue
            cost = _kcost(ask)
            if not CUSH_LO <= cost <= CUSH_HI:
                continue
            p = _model.prob_over(comp, mu, var, rung - 0.5) if mu is not None and var else None
            if p is None:
                continue
            key = (round(p - cost, 3), p)
            if best is None or key > best[0]:
                best = (key, {"player": leg["player"], "stat": comp, "rung": rung, "line": rung - 0.5, "side": "over",
                              "p": round(cost, 4), "ask": round(ask, 2), "model_p": round(p, 3), "game_id": r.get("game_id"),
                              "their_line": float(leg["line"]), "split_from": leg["stat"], "min_down": minutes_flag(r)})
    return best[1] if best else None


def cushion_leg(leg: dict, prow: dict | None) -> dict | None:
    """The user's cushion (update 87): soften their line by the stat's cushion (points 5, rebounds 3, assists 2.5,
    threes 1, PRA 6, two-stat combos 5 / R+A 3), then take the Kalshi rung nearest to that which costs 75-85% with
    the fee. Overs use the rung's YES; unders ("No N+") use NO on a higher rung. Also returns the one-rung-safer
    "confident" rung (e.g. 6.5 REB -> 6+) with its price, for players you trust more."""
    if not prow or leg.get("stat") not in CUSH_SOFT:
        return None
    lad = prow.get("kladder") or []
    if not lad:
        return None
    L, over, soft = float(leg["line"]), leg.get("side", "over") == "over", CUSH_SOFT[leg["stat"]]
    cands, conf = [], None
    for rung, bid, ask in lad:
        if over and rung < math.ceil(L) and ask:
            cost, tgt, raw = _kcost(ask), math.ceil(L - soft), ask
            if rung == math.ceil(L) - 1:
                conf = {"rung": rung, "p": round(cost, 4), "ask": round(raw, 2)}
        elif not over and rung > math.floor(L) + 1 and bid:
            cost, tgt, raw = _kcost(1 - bid), math.floor(L + soft) + 1, 1 - bid
            if rung == math.floor(L) + 2:
                conf = {"rung": rung, "p": round(cost, 4), "ask": round(raw, 2)}
        else:
            continue
        if CUSH_LO <= cost <= CUSH_HI:
            cands.append((abs(rung - tgt), -cost, rung, cost, raw))
    if not cands:
        return None
    _, _, rung, cost, raw = min(cands)
    out = {"player": leg["player"], "stat": leg["stat"], "rung": rung, "line": rung - 0.5, "side": "over" if over else "under",
           "p": round(cost, 4), "ask": round(raw, 2), "game_id": prow.get("game_id"), "their_line": L,
           "min_down": minutes_flag(prow)}
    if conf and conf["rung"] != rung:
        out["confident"] = conf
    return out


def cushion_tiers(legs: list[dict]) -> list[dict]:
    """+300: only cushion props priced 75-85% (the version that did best in the Nov 20 replay).
    +400 / +550 / +700: every safe leg (cushion props up to 92% and 75%+ favorites), safest first, as few as it takes."""
    tiers = []
    t300 = _stack([l for l in legs if l.get("stat") != "ml" and l["p"] <= CUSH_300_HI], (4.0,))
    for t in t300:
        t["only"] = "cushion"
    return t300 + _stack(legs, CUSH_TIERS[1:])


def _stack(legs: list[dict], targets) -> list[dict]:
    legs = sorted(legs, key=lambda l: -l["p"])[:CUSH_MAX_LEGS]
    tiers = []
    for tgt in targets:
        pay, used = 1.0, []
        for l in legs:
            used.append(l)
            pay /= l["p"]
            if pay >= tgt:
                break
        if pay >= tgt:
            payk = 1.0
            for l in used:
                payk /= (l.get("ask") or l["p"])
            tiers.append({"target": tgt, "legs": used, "pays": round(pay, 2), "pays_k": round(payk, 2)})
    return tiers


# ---------------------------------------------------------------- daily sync (prices + combo), then summary
def _tips(sl) -> tuple[dict, dict]:
    by_team, by_gid = {}, {}
    for g in (sl or {}).get("games") or []:
        tip = g["tip_utc"]
        if isinstance(tip, str):
            tip = datetime.fromisoformat(tip.replace("Z", "+00:00"))
        by_gid[g["game_id"]] = tip
        for t in (g["away"], g["home"]):
            by_team[t] = tip
    return by_team, by_gid


def sync(sl: dict, now: datetime | None = None) -> None:
    """Record tail-test prices for today's picks before tip; build/lock today's cushion combo."""
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    picks = [p for p in _load(PICKS, {}).get("picks", []) if p["date"] == date]
    st = _load(STATE, {"price": {}, "combos": {}})
    rows = _pred_rows(date)
    by_team, by_gid = _tips(sl)
    for p in picks:
        if p["id"] in st["price"]:
            continue
        if p["kind"] == "spread":
            tip = by_team.get(p["team"])
            if tip and now < tip:
                yp = {"src": "book", "p": round(am_to_p(-110), 4)}
                try:  # Polymarket US at the same spread, if cheaper (update 79)
                    import polymarket_client
                    c = polymarket_client.spread_cost(p["team"], float(p["line"]), date)
                    if c and c < yp["p"]:
                        yp = {"src": "Polymarket", "p": c}
                except Exception:  # noqa: BLE001
                    pass
                st["price"][p["id"]] = {**yp, "at": now.strftime("%Y-%m-%d %H:%M UTC")}
            continue
        if len(p["legs"]) != 1:
            continue
        prow = rows.get((_norm(p["legs"][0]["player"]), p["legs"][0]["stat"]))
        tip = by_gid.get(prow.get("game_id")) if prow else None
        if prow and tip and now < tip:
            yp = your_price(p["legs"][0], prow)
            if yp:
                st["price"][p["id"]] = {**yp, "at": now.strftime("%Y-%m-%d %H:%M UTC")}
    # cushion combo: rebuilt every build until its first leg tips, then locked
    cur = st["combos"].get(date)
    locked = bool(cur and cur.get("locked"))
    if cur and not locked and cur.get("legs"):
        first = min((by_gid.get(l["game_id"]) for t in (cur.get("tiers") or [{"legs": cur["legs"]}]) for l in t["legs"]
                     if by_gid.get(l["game_id"])), default=None)
        if first and now >= first:
            cur["locked"] = True
            locked = True
    if not locked:
        cand = []
        for p in picks:
            if p["kind"] != "prop" or p["tipster"] not in CUSH_SRC:
                continue
            for l in p["legs"]:
                if l.get("kind") in ("total", "spread") or str(l.get("stat", "")).startswith("nfl"):
                    continue
                cand.append((l, p["tipster"], p["id"]))
        for i, s_ in enumerate(_load(DIR / "safelines.json", {}).get("days", {}).get(date, [])):
            cand.append(({"player": s_["player"], "stat": s_["stat"], "line": s_["line"], "side": "over"}, "tacos", f"{date}|{i}"))
        cand.sort(key=lambda c: CUSH_SRC.index(c[1]))
        pool, seen = [], set()
        for l, src, pid in cand:
            prow = rows.get((_norm(l["player"]), l["stat"]))
            anyrow = prow or next((rows.get((_norm(l["player"]), c_)) for c_ in PARTS.get(l["stat"], ())
                                   if rows.get((_norm(l["player"]), c_))), None)
            tip = by_gid.get(anyrow.get("game_id")) if anyrow else None
            if not tip or now >= tip or _norm(l["player"]) in seen:
                continue
            c = cushion_leg(l, prow) if prow else None
            if not c:   # a combo stat Kalshi doesn't list (or no rung in range): use its best single-stat part
                c = split_leg(l, rows)
            if c:
                seen.add(_norm(c["player"]))
                pool.append({**c, "from": pid, "tipster": src})
        for g in (sl or {}).get("games") or []:   # favorites' moneylines (75%+ fair) as safe legs, Kalshi price
            if g.get("started") or g.get("completed"):
                continue
            tip = by_gid.get(g["game_id"])
            if tip and now >= tip:
                continue
            fd = (g.get("fanduel") or {}).get("moneyline") or {}
            km = g.get("kalshi_ml") or {}
            hp = fd.get("home_prob") if fd.get("home_prob") is not None else km.get("home_prob")
            if hp is None:
                continue
            hp /= 100
            side = "home" if hp >= 0.5 else "away"
            fair = hp if side == "home" else 1 - hp
            ka = km.get(f"{side}_ask")
            if fair < ML_MIN_FAIR or not ka:
                continue
            ka = ka / 100 if ka > 1 else ka
            cost = _kcost(ka)
            if cost <= ML_MAX_COST:
                team_ = g[side]
                pool.append({"kind": "spread", "team": team_, "line": 0.0, "player": f"{team_} to win", "stat": "ml",
                             "rung": 0, "side": "over", "p": round(cost, 4), "ask": round(ka, 2), "fair": round(fair, 3), "game_id": g["game_id"],
                             "from": f"{date}|ml|{team_}", "tipster": "fav"})
        tiers = cushion_tiers(pool)
        if pool:   # the leg list is kept even when no tier reaches +300 yet
            pay = 1.0
            for l in pool:
                pay /= l["p"]
            st["combos"][date] = {"v": 2, "tiers": tiers, "pool": sorted(pool, key=lambda l: -l["p"]),
                                  "legs": tiers[-1]["legs"] if tiers else sorted(pool, key=lambda l: -l["p"]),
                                  "pays": tiers[-1]["pays"] if tiers else round(pay, 2),
                                  "built": now.strftime("%Y-%m-%d %H:%M UTC"), "locked": False}
        else:
            st["combos"].pop(date, None)
    DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


# ---------------------------------------------------------------- best spots (Nov 19)
KIND_OF = {"pts": "points", "reb": "rebounds", "ast": "assists", "fg3m": "threes", "pr": "combos", "pa": "combos",
           "ra": "combos", "pra": "combos"}
KIND_LABEL = {"points": "points", "rebounds": "rebounds", "assists": "assists", "threes": "threes", "combos": "combos (P+R, P+A, R+A, PRA)"}
SPOT_MIN = 5          # picks of that kind before a tag is given
JUICE = -126          # odds at or below this count as heavy juice
JUICE_MIN = 4


def _kind(r):
    if r.get("kind") != "prop" or len(r.get("legs") or []) != 1:
        return None
    return KIND_OF.get(r["legs"][0]["stat"])


def _spot_stats(rs):
    """{kind: (n, w, units)} and juice (n, w, units) from graded single-leg props."""
    by, ju = {}, [0, 0, 0.0]
    for r in rs:
        k = _kind(r)
        if not k:
            continue
        b = by.setdefault(k, [0, 0, 0.0])
        b[0] += 1
        b[1] += r["res"] == "hit"
        b[2] += r["units"]
        if r["odds"] <= JUICE:
            ju[0] += 1
            ju[1] += r["res"] == "hit"
            ju[2] += r["units"]
    return by, ju


def spot_tag(r, by, ju):
    """'in' | 'out' | None, with a short reason, from the tipster's record BEFORE this pick's date."""
    k = _kind(r)
    if not k:
        return None, None
    if r["odds"] <= JUICE and ju[0] >= JUICE_MIN and ju[2] < 0:
        return "out", f"he's {ju[1]}-{ju[0] - ju[1]} at {JUICE} or more juice"
    b = by.get(k)
    if not b or b[0] < SPOT_MIN:
        return None, None
    roi = b[2] / b[0]
    if roi >= 0.10:
        return "in", f"his {k} {b[1]}-{b[0] - b[1]}, {b[2]:+.1f}U"
    if roi < 0:
        return "out", f"his {k} {b[1]}-{b[0] - b[1]}, {b[2]:+.1f}U"
    return None, None


AGREE_MIN, AGREE_JUICE = 32.0, -125
AGREE_SRC = ("doc", "wiz", "byrn", "peaky", "diogo", "izzo", "samb")


def model_check(leg: dict, odds, rows: dict) -> dict | None:
    """Update 95 (Nov 20 2026): does our projection agree with the tipster's side, and is it the strong spot?
    Season review of the six profitable tipsters (408 props): model agrees 131-70 (+23%/bet) vs disagrees 113-91 (+4%);
    agrees + our projected minutes 32+ + price better than -125: 47-16 (+41%/bet). Tracked point in time from here."""
    pr = rows.get((_norm(leg.get("player", "")), leg.get("stat")))
    if not pr or pr.get("projected") is None:
        return None
    proj, ln, over = float(pr["projected"]), float(leg["line"]), leg.get("side", "over") == "over"
    if proj == ln:
        return None
    agree = (proj > ln) == over
    pm = pr.get("proj_min")
    strong = agree and (pm or 0) >= AGREE_MIN and (odds is None or odds > AGREE_JUICE)
    return {"tag": "strong" if strong else ("agree" if agree else "disagree"), "proj": round(proj, 1),
            "pmin": round(pm) if pm else None}


def summary(box, sl: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    allp = _load(PICKS, {}).get("picks", [])
    st = _load(STATE, {"price": {}, "combos": {}})
    ply, team = _box_index(box)
    by_team, by_gid = _tips(sl)
    today = (sl or {}).get("date")
    out_rows = []
    pr_cache: dict = {}
    for p in allp:
        g = grade_pick(p, ply, team)
        r = {**p, **g, "who": TIPSTERS[p["tipster"]]["name"]}
        if p["kind"] == "prop" and len(p.get("legs") or []) == 1:
            if p["date"] not in pr_cache:
                pr_cache[p["date"]] = _pred_rows(p["date"])
            r["model"] = model_check(p["legs"][0], p.get("odds"), pr_cache[p["date"]])
        if p["tipster"] == "sam" and p.get("legs"):  # update 96: Sam + model cushion check per leg
            import siteladder
            if p["date"] not in pr_cache:
                pr_cache[p["date"]] = _pred_rows(p["date"])
            r["sam_checks"] = [siteladder.sam_check(l, pr_cache[p["date"]].get((_norm(l["player"]), l["stat"])))
                               for l in p["legs"]]
        if p["tipster"] == "sam":
            r["short"] = p["odds"] <= SHORT_MAX
        if p["kind"] == "prop" and "legs" not in g:
            r["legs"] = p["legs"]
        r["units"] = profit(p["odds"], True) if g.get("res") == "hit" else (-1.0 if g.get("res") == "miss" else 0.0)
        yp = st["price"].get(p["id"])
        if yp:
            r["your"] = {**yp, "odds": p_to_am(yp["p"])}
            r["your_units"] = (1 / yp["p"] - 1) if g.get("res") == "hit" else (-1.0 if g.get("res") == "miss" else 0.0)
        out_rows.append(r)
    graded = [r for r in out_rows if r["status"] == "graded" and r["res"] in ("hit", "miss")]
    agree_rec = {}
    for tag in ("strong", "agree", "disagree"):
        rs = [r for r in graded if r["tipster"] in AGREE_SRC and (r.get("model") or {}).get("tag") == tag]
        n, w = len(rs), sum(r["res"] == "hit" for r in rs)
        u = sum(r["units"] for r in rs)
        agree_rec[tag] = {"n": n, "w": w, "l": n - w, "u": round(u, 1), "per": round(100 * u / n) if n else None}
    # best-spot tags, point in time: each pick is judged on the tipster's record from earlier dates only
    spot_rec = {"in": [], "out": [], "none": []}
    for r in out_rows:
        prior = [x for x in graded if x["tipster"] == r["tipster"] and x["date"] < r["date"]]
        by, ju = _spot_stats(prior)
        r["spot"], r["spot_why"] = spot_tag(r, by, ju)
        if r in graded and _kind(r):
            spot_rec[r["spot"] or "none"].append(r)

    def rec(rs, key="units"):
        n = len(rs)
        if not n:
            return {"n": 0, "w": 0, "l": 0, "u": 0.0, "rate": None, "per": None, "last10": None}
        w = sum(r["res"] == "hit" for r in rs)
        u = sum(r[key] for r in rs)
        last = rs[-10:]
        return {"n": n, "w": w, "l": n - w, "u": round(u, 2), "rate": round(100 * w / n), "per": round(100 * u / n),
                "last10": f"{sum(r['res'] == 'hit' for r in last)}/{len(last)}"}
    per = []
    for k, t in TIPSTERS.items():
        rs = sorted([r for r in graded if r["tipster"] == k], key=lambda r: (r["date"], r["id"]))
        tail = [r for r in rs if "your_units" in r]
        by, ju = _spot_stats(rs)
        best = sorted([(kk, v) for kk, v in by.items() if v[0] >= SPOT_MIN and v[2] / v[0] >= 0.10], key=lambda x: -x[1][2])
        skip = sorted([(kk, v) for kk, v in by.items() if v[0] >= SPOT_MIN and v[2] < 0], key=lambda x: x[1][2])
        spots = {"best": [{"kind": kk, "w": v[1], "l": v[0] - v[1], "u": round(v[2], 1)} for kk, v in best],
                 "skip": [{"kind": kk, "w": v[1], "l": v[0] - v[1], "u": round(v[2], 1)} for kk, v in skip]}
        if ju[0] >= JUICE_MIN and ju[2] < 0:
            spots["skip"].append({"kind": f"{JUICE} or more juice", "w": ju[1], "l": ju[0] - ju[1], "u": round(ju[2], 1)})
        legrec = None
        if k == "gobpm":
            lg = [l for r in rs for l in (r.get("legs") or []) if l.get("res") in ("hit", "miss") and l.get("stat")]
            def _lr(ls):
                return {"n": len(ls), "w": sum(l["res"] == "hit" for l in ls),
                        "p": round(100 * sum(l.get("p") or 0 for l in ls) / len(ls)) if ls else None}
            legrec = {"all": _lr(lg), "over": _lr([l for l in lg if l.get("side") == "over"]),
                      "under": _lr([l for l in lg if l.get("side") == "under"]),
                      "by": {LAB.get(st, st): _lr([l for l in lg if l["stat"] == st]) for st in sorted({l["stat"] for l in lg})}}
        per.append({"key": k, **t, **rec(rs), "tail": rec(tail, "your_units"), "spots": spots, "legrec": legrec})
    tail_all = [r for r in graded if "your_units" in r]
    for t in per:
        if t["key"] == "sam":
            rs = sorted([r for r in graded if r["tipster"] == "sam" and r.get("short")], key=lambda r: r["date"])
            t["short"] = rec(rs)
    # combos
    combos, tier_rows = [], []

    def _grade_combo(legs_in, d):
        legs, res = [], []
        for l in legs_in:
            g, x, _ = grade_leg(l, d, ply, team)
            legs.append({**l, "res": g, "actual": x})
            res.append(g)
        status = "open" if any(x is None for x in res) else "graded"
        return legs, status, status == "graded" and "miss" not in res and any(x == "hit" for x in res)

    for d, c in sorted(st.get("combos", {}).items(), reverse=True):
        if c.get("v") == 2:   # update 87: three tiers (+300 / +400 / +500) from 75-85% cushion legs
            tiers = []
            for t in c["tiers"]:
                legs, status, hit = _grade_combo(t["legs"], d)
                tiers.append({**t, "legs": legs, "status": status, "hit": hit})
                tier_rows.append({"date": d, "target": t["target"], "pays": t["pays"], "status": status, "hit": hit,
                                  "locked": c.get("locked")})
            pool, _, _ = _grade_combo(c.get("pool") or [], d)
            combos.append({"date": d, **c, "tiers": tiers, "pool": pool, "legs": tiers[-1]["legs"] if tiers else pool,
                           "status": tiers[-1]["status"] if tiers else "none", "hit": tiers[-1]["hit"] if tiers else False})
            continue
        legs, status, hit = _grade_combo(c["legs"], d)
        combos.append({"date": d, **c, "legs": legs, "status": status, "hit": hit})
    cg = [c for c in combos if c["status"] == "graded" and c.get("locked") and c.get("v") != 2]
    tier_rec = {}
    for tgt in CUSH_TIERS:
        rs = [r for r in tier_rows if r["target"] == tgt and r["status"] == "graded" and r.get("locked")]
        tier_rec[tgt] = {"n": len(rs), "hits": sum(r["hit"] for r in rs),
                         "u": round(sum((r["pays"] - 1) if r["hit"] else -1 for r in rs), 2)}
    pool_legs = [l for c in combos for l in (c.get("pool") or []) if c.get("locked") and l.get("res") in ("hit", "miss")]
    pool_rec = {"n": len(pool_legs), "w": sum(l["res"] == "hit" for l in pool_legs),
                "p": round(100 * sum(l["p"] for l in pool_legs) / len(pool_legs), 1) if pool_legs else None}
    # tonight
    tonight = []
    for r in out_rows:
        if r["date"] != today:
            continue
        tip = by_team.get(r.get("team")) if r["kind"] == "spread" else None
        if r["kind"] == "spread":
            g = next((g for g in (sl or {}).get("games") or [] if r["team"] in (g["away"], g["home"])), None)
            if g:
                r["game"] = f"{g['away']} @ {g['home']}"
        if r.get("posted"):
            try:
                r["posted_et"] = datetime.fromisoformat(r["posted"].replace("Z", "+00:00")).astimezone(
                    __import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
            except ValueError:
                pass
        if r["kind"] == "prop":
            rows = _pred_rows(today)
            prow = rows.get((_norm(r["legs"][0]["player"]), r["legs"][0]["stat"])) if len(r["legs"]) == 1 else None
            tip = by_gid.get(prow.get("game_id")) if prow else None
            r["game"] = f"{prow['team']} {'vs' if prow.get('home') else '@'} {prow['opp']}" if prow else None
            r["team"] = prow["team"] if prow else None
        r["tip"] = tip.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%-I:%M %p ET") if tip else None
        tonight.append(r)
    days = {}
    for r in sorted(out_rows, key=lambda r: (r["date"], r["tipster"], r["id"])):
        if r["status"] == "graded":
            days.setdefault(r["date"], []).append(r)
    day_list = [{"date": d, "rows": rs, "w": sum(x["res"] == "hit" for x in rs), "l": sum(x["res"] == "miss" for x in rs),
                 "u": round(sum(x["units"] for x in rs), 2)} for d, rs in sorted(days.items(), reverse=True)]
    return {"tipsters": per, "tonight": tonight, "today": today, "days": day_list, "dropped": DROPPED,
            "tail": rec(sorted(tail_all, key=lambda r: r["date"]), "your_units"), "earn_n": EARN_N,
            "combos": combos, "combo_rec": {"n": len(cg), "hits": sum(c["hit"] for c in cg),
                                            "u": round(sum((c["pays"] - 1) if c["hit"] else -1 for c in cg), 2)},
            "tier_rec": tier_rec, "pool_rec": pool_rec, "cush_soft": CUSH_SOFT,
            "season_from": min((r["date"] for r in allp), default=None), "lab": LAB, "p_to_am": p_to_am,
            "spot_rec": {k: rec(sorted(v, key=lambda r: r["date"])) for k, v in spot_rec.items()}, "spot_min": SPOT_MIN,
            "juice": JUICE, "agree_rec": agree_rec, "agree_min": int(AGREE_MIN)}
