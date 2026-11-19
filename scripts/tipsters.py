"""Tipster tracker (update 78).

The accounts the user follows on X were checked against final box scores from opening night (Nov 18 2026 review).
Only the ones with a real, honestly posted winning record are tracked: Dr. Profit, WizBetz, byrnetts and PeakyProps
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
    "gob": {"name": "Goblin", "handle": "@PotdPrizepicks", "what": "spread legs", "honest": False,
            "note": "Only the spread legs out of his parlays, graded as -110 straights. His full parlays went 1 of 10."},
    "sam": {"name": "Sam", "handle": "@TheModelManSam", "what": "safest lotto slip", "honest": None,
            "note": "The lowest tier of his daily 3-tier lotto ladder (3 role players at low lines). Priced +210 or shorter it went 7 of 11 through Nov 17."},
}
SHORT_MAX = 210  # Sam's safest slip: the ones priced +210 or shorter are the ones that cashed
DROPPED = ("swan_wbg (36-57, -25.4U), ZiggyBetsFR (ladders, 14-14 flat), NextLevelBets (31-31, -4.0U, posts only winning days), TheModelManSam's straights (5-6, -1.7U; his safest lotto slip is tracked instead), "
           "Bearprops (13-14 legs, never posts a loss)")
LAB = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA", "pr": "P+R", "pa": "P+A", "ra": "R+A", "dd": "DOUBLE-DOUBLE"}
PARTS = {"pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",), "pra": ("pts", "reb", "ast"),
         "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}
KFEE = 0.07
CUSH_LO, CUSH_HI = 0.65, 0.82
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


def grade_leg(leg: dict, d: str, ply: dict):
    """-> (result 'hit'|'miss'|'push'|'void'|None, actual, player row)"""
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
        res, x, r = grade_leg(leg, d, ply)
        legs.append({**leg, "res": res, "actual": x, "team": r["team"] if r else None})
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


def cushion_leg(leg: dict, prow: dict | None) -> dict | None:
    """One Kalshi rung safer than their over (e.g. 3.5 AST -> 3+ or the next rung down), if it costs 65-82%."""
    if not prow or leg.get("side", "over") != "over" or leg["stat"] not in ("pts", "reb", "ast", "fg3m"):
        return None
    top = math.ceil(float(leg["line"]))
    cands = [(rung, ask) for rung, bid, ask in (prow.get("kladder") or []) if rung < top and ask]
    for rung, ask in sorted(cands, reverse=True):
        c = _kcost(ask)
        if CUSH_LO <= c <= CUSH_HI:
            return {"player": leg["player"], "stat": leg["stat"], "rung": rung, "line": rung - 0.5, "side": "over",
                    "p": round(c, 4), "game_id": prow.get("game_id")}
    return None


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
        first = min((by_gid.get(l["game_id"]) for l in cur["legs"] if by_gid.get(l["game_id"])), default=None)
        if first and now >= first:
            cur["locked"] = True
            locked = True
    if not locked:
        legs, seen = [], set()
        for p in picks:
            if p["kind"] != "prop" or len(p["legs"]) != 1:
                continue
            prow = rows.get((_norm(p["legs"][0]["player"]), p["legs"][0]["stat"]))
            tip = by_gid.get(prow.get("game_id")) if prow else None
            if not tip or now >= tip:
                continue
            c = cushion_leg(p["legs"][0], prow)
            if c and _norm(c["player"]) not in seen:
                seen.add(_norm(c["player"]))
                legs.append({**c, "from": p["id"], "tipster": p["tipster"]})
        legs = sorted(legs, key=lambda l: -l["p"])[:4]
        if len(legs) >= 2:
            pay = 1.0
            for l in legs:
                pay /= l["p"]
            st["combos"][date] = {"legs": legs, "pays": round(pay, 2), "built": now.strftime("%Y-%m-%d %H:%M UTC"), "locked": False}
        else:
            st["combos"].pop(date, None)
    DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def summary(box, sl: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    allp = _load(PICKS, {}).get("picks", [])
    st = _load(STATE, {"price": {}, "combos": {}})
    ply, team = _box_index(box)
    by_team, by_gid = _tips(sl)
    today = (sl or {}).get("date")
    out_rows = []
    for p in allp:
        g = grade_pick(p, ply, team)
        r = {**p, **g, "who": TIPSTERS[p["tipster"]]["name"]}
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
        per.append({"key": k, **t, **rec(rs), "tail": rec(tail, "your_units")})
    tail_all = [r for r in graded if "your_units" in r]
    for t in per:
        if t["key"] == "sam":
            rs = sorted([r for r in graded if r["tipster"] == "sam" and r.get("short")], key=lambda r: r["date"])
            t["short"] = rec(rs)
    # combos
    combos = []
    for d, c in sorted(st.get("combos", {}).items(), reverse=True):
        legs, res = [], []
        for l in c["legs"]:
            g, x, _ = grade_leg(l, d, ply)
            legs.append({**l, "res": g, "actual": x})
            res.append(g)
        status = "open" if any(x is None for x in res) else "graded"
        hit = status == "graded" and "miss" not in res and any(x == "hit" for x in res)
        combos.append({"date": d, **c, "legs": legs, "status": status, "hit": hit})
    cg = [c for c in combos if c["status"] == "graded" and c.get("locked")]
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
            "season_from": min((r["date"] for r in allp), default=None), "lab": LAB, "p_to_am": p_to_am}
