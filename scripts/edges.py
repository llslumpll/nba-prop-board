"""
EDGES: market-based bets, the way the Monahan / OddsJam approach works.

No projection involved: take a reference market's two-sided price (FanDuel),
remove its cut with the power method (fairer to favorites than the simple
proportional split), and compare that fair chance with what Kalshi charges
for the same outcome, fee included.

  +EV         Kalshi cost (ask + fee) below the fair chance: EV = fair / cost - 1.
  arbitrage   Kalshi side + FanDuel's opposite side cost under $1 together:
              a profit whatever happens (rare, gone in seconds).
  middle      FanDuel and Kalshi use different numbers for a game total or spread
              (FanDuel over 228.5, Kalshi under 231.5): both win if the result
              lands in between. Chance from a normal curve around the market's
              number (the same widths the game picks use); shown when the two
              bets together are +EV.

Matched markets: moneylines, spreads and totals at the same number, and
FanDuel points props vs the Kalshi rung at the same number. FanDuel is a single,
soft reference book, so every edge is labelled that way; the Odds API paid plan
could add sharper books. Every edge is recorded (data/edges/DATE.json), kept up
to date until tip like the picks, and graded, so the EDGES record shows whether
market edges actually pay.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

import kalshi_client
import teams

DIR = Path(__file__).parent.parent / "data" / "edges"
MIN_EV = 0.02            # show / record edges of 2%+ after Kalshi's fee
MAX_EV = 0.20            # above this it's treated as a stale / thin price to check by hand
TOTAL_SD, MARGIN_SD = 18.0, 12.5
ND = NormalDist()


def implied(american):
    if american is None:
        return None
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def devig_power(p1, p2):
    """Fair chances from two implied (with-vig) chances: p1^k + p2^k = 1."""
    if not p1 or not p2:
        return None, None
    lo, hi = 0.5, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        if p1 ** k + p2 ** k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return p1 ** k, p2 ** k


def _ev(fair, cost):
    return fair / cost - 1 if fair and cost else None


def _row(kind, g, label, side_desc, kalshi_side, cost_raw, fair, ref_desc, line=None, model=None, extra=None):
    cost = kalshi_client.with_fee(cost_raw)
    ev = _ev(fair, cost)
    r = {"kind": kind, "game_id": g["game_id"], "date": g["date"], "tip_utc": g["tip_utc"].isoformat(),
         "away": g["away"], "home": g["home"], "label": label, "side": side_desc, "kalshi": kalshi_side,
         "ask": round(cost_raw, 3), "cost": cost, "fair": round(fair, 4), "ev": round(ev, 4) if ev is not None else None,
         "max_price": round(fair / (1 + kalshi_client.FEE_RATE * (1 - fair)), 3) if fair else None,
         "ref": ref_desc, "line": line, "model": round(model, 4) if model is not None else None,
         "model_agrees": model is not None and model > cost}
    r.update(extra or {})
    return r


def _kalshi_rows(kal, kind, away, home):
    return [r for r in kal.get("game", []) if r.get("kind") == kind and r.get("away") == away and r.get("home") == home]


def _team(r):
    suf = r.get("team_suffix") or ""
    code = "".join(ch for ch in suf if ch.isalpha())
    return teams.canon(code, "kalshi")


def find(sl: dict, kal: dict, line_book: dict, res: dict) -> dict:
    """{"edges": [...], "arbs": [...], "middles": [...], "closest_arb": float|None}"""
    edges, arbs, middles, closest = [], [], [], None
    for g in sl.get("games", []):
        if g.get("started"):
            continue
        fd = g.get("fanduel") or {}
        gp = {p["market"]: p for p in g.get("game_picks") or []}
        tag = f"{g['away']} @ {g['home']}"
        # --- moneyline
        ml = fd.get("moneyline")
        if ml:
            ph, pa = devig_power(implied(ml["home_price"]), implied(ml["away_price"]))
            for r in _kalshi_rows(kal, "moneyline", g["away"], g["home"]):
                t = _team(r)
                if t not in (g["home"], g["away"]) or not r.get("yes_ask"):
                    continue
                fair = ph if t == g["home"] else pa
                other_raw = implied(ml["away_price"] if t == g["home"] else ml["home_price"])
                p = gp.get("moneyline")
                model = (p["chance"] if p["pick"] == t else 1 - p["chance"]) if p else None
                e = _row("moneyline", g, f"{tag}: {t} wins", f"{t} ML", f"YES {t} wins", r["yes_ask"], fair,
                         f"FanDuel {ml['home_str']}/{ml['away_str']}", model=model)
                both = e["cost"] + other_raw
                closest = both if closest is None else min(closest, both)
                if both < 1:
                    arbs.append({**e, "both": round(both, 4), "other": f"FanDuel {g['away'] if t == g['home'] else g['home']} ML"})
                edges.append(e)
        # --- spread at the same number
        sp = fd.get("spread")
        if sp and sp.get("home_price") is not None:
            ph, pa = devig_power(implied(sp["home_price"]), implied(sp["away_price"]))
            fav, num = (g["home"], -float(sp["home_point"])) if float(sp["home_point"]) < 0 else (g["away"], float(sp["home_point"]))
            p_fav = ph if fav == g["home"] else pa
            for r in _kalshi_rows(kal, "spread", g["away"], g["home"]):
                if _team(r) != fav or r.get("floor_strike") is None:
                    continue
                x = float(r["floor_strike"])
                if abs(x - num) < 1e-6:
                    if r.get("yes_ask"):
                        edges.append(_row("spread", g, f"{tag}: {fav} -{num:g}", f"{fav} -{num:g}", f"YES {fav} wins by over {num:g}",
                                          r["yes_ask"], p_fav, f"FanDuel spread {fav} -{num:g}", line=num))
                    if r.get("yes_bid"):
                        dog = g["away"] if fav == g["home"] else g["home"]
                        edges.append(_row("spread", g, f"{tag}: {dog} +{num:g}", f"{dog} +{num:g}", f"NO {fav} wins by over {num:g}",
                                          1 - r["yes_bid"], 1 - p_fav, f"FanDuel spread {dog} +{num:g}", line=num))
                elif r.get("yes_bid") and r.get("yes_ask"):
                    # middle: FanDuel favorite -num plus Kalshi NO on "fav by over x" (x > num), or the reverse
                    _middle(middles, g, tag, "spread", num, x, fav, r, sp, mu=num, sd=MARGIN_SD)
        # --- total at the same number
        tot = fd.get("total")
        if tot and tot.get("over_price") is not None:
            po, pu = devig_power(implied(tot["over_price"]), implied(tot["under_price"]))
            T = float(tot["point"])
            p = gp.get("total")
            for r in _kalshi_rows(kal, "total", g["away"], g["home"]):
                if r.get("floor_strike") is None:
                    continue
                x = float(r["floor_strike"])
                if abs(x - T) < 1e-6:
                    mo = (p["chance"] if p["pick"] == "OVER" else 1 - p["chance"]) if p and p.get("line") == T else None
                    if r.get("yes_ask"):
                        edges.append(_row("total", g, f"{tag}: OVER {T:g}", f"OVER {T:g}", f"YES over {T:g}", r["yes_ask"], po,
                                          f"FanDuel total {T:g} ({tot['over_str']}/{tot['under_str']})", line=T, model=mo))
                    if r.get("yes_bid"):
                        edges.append(_row("total", g, f"{tag}: UNDER {T:g}", f"UNDER {T:g}", f"NO over {T:g}", 1 - r["yes_bid"], pu,
                                          f"FanDuel total {T:g} ({tot['over_str']}/{tot['under_str']})", line=T,
                                          model=(1 - mo) if mo is not None else None))
                elif r.get("yes_bid") and r.get("yes_ask"):
                    _middle(middles, g, tag, "total", T, x, None, r, tot, mu=T, sd=TOTAL_SD)
    # --- player points: FanDuel two-sided price vs the Kalshi rung at the same number
    gid_game = {g["game_id"]: g for g in sl.get("games", []) if not g.get("started")}
    model_po = {}  # our model's chance of OVER at FanDuel's number, as a second opinion
    for plist in (res.get("picks") or {}).values():
        for p in plist:
            fl = (p.get("lines") or {}).get("fanduel")
            if fl and fl.get("p_over") is not None:
                model_po[(p["player"], p["stat"], p["game_id"])] = fl["p_over"]
    for (player, stat, gid), srcs in line_book.items():
        g = gid_game.get(gid)
        fdl, ka = srcs.get("fanduel"), srcs.get("kalshi")
        if not g or not fdl or not ka or fdl["line"] % 1 != 0.5:
            continue
        po, pu = devig_power(fdl.get("be_over"), fdl.get("be_under"))
        if po is None:
            continue
        n = int(fdl["line"] + 0.5)
        rung = next((x for x in ka.get("ladder") or [] if x["n"] == n), None)
        if not rung:
            continue
        lab = f"{player} {stat.upper()}"
        mpo = model_po.get((player, stat, gid))
        if rung.get("ask"):
            edges.append(_row("prop", g, f"{lab} OVER {fdl['line']:g}", f"OVER {fdl['line']:g}", f"YES {player} {n}+",
                              rung["ask"], po, f"FanDuel {fdl.get('over_str')}/{fdl.get('under_str')}", line=fdl["line"], model=mpo,
                              extra={"player": player, "stat": stat}))
        if rung.get("bid"):
            edges.append(_row("prop", g, f"{lab} UNDER {fdl['line']:g}", f"UNDER {fdl['line']:g}", f"NO {player} {n}+",
                              1 - rung["bid"], pu, f"FanDuel {fdl.get('over_str')}/{fdl.get('under_str')}", line=fdl["line"],
                              model=(1 - mpo) if mpo is not None else None, extra={"player": player, "stat": stat}))
    # a real market gap is a few percent; one far bigger is almost always a stale or thin Kalshi price
    # (or a mismatched market), so it's listed separately to check by hand and never recorded
    good = sorted([e for e in edges if e["ev"] is not None and MIN_EV <= e["ev"] <= MAX_EV], key=lambda e: -e["ev"])
    suspect = sorted([e for e in edges if e["ev"] is not None and e["ev"] > MAX_EV], key=lambda e: -e["ev"])[:10]
    middles.sort(key=lambda m: -m["ev"])
    return {"edges": good, "suspect": suspect, "arbs": arbs, "middles": [m for m in middles if m["ev"] > 0][:12],
            "closest_arb": round(closest, 4) if closest is not None else None, "checked": len(edges)}


def _middle(out, g, tag, market, fd_num, k_num, fav, r, fd, mu, sd):
    """FanDuel at fd_num vs Kalshi at k_num (different numbers). Two bets, $1 each:
    the side that wins above the lower number and the side that wins below the higher one."""
    lo, hi = sorted((fd_num, k_num))
    if hi - lo < 1:
        return
    cdf = lambda v: ND.cdf((v - mu) / sd)
    p_above_lo, p_below_hi = 1 - cdf(lo), cdf(hi)
    p_both = max(0.0, cdf(hi) - cdf(lo))
    if market == "total":
        if fd_num < k_num:   # FanDuel OVER fd_num + Kalshi NO on over k_num
            fd_leg, fd_price, k_leg, k_cost = f"FanDuel OVER {fd_num:g}", fd["over_price"], f"Kalshi NO over {k_num:g}", 1 - r["yes_bid"]
        else:                # Kalshi YES over k_num + FanDuel UNDER fd_num
            fd_leg, fd_price, k_leg, k_cost = f"FanDuel UNDER {fd_num:g}", fd["under_price"], f"Kalshi YES over {k_num:g}", r["yes_ask"]
    else:
        if fd_num < k_num:   # FanDuel favorite -fd_num + Kalshi NO "fav by over k_num"
            fd_leg = f"FanDuel {fav} -{fd_num:g}"
            fd_price = fd["home_price"] if fav == g["home"] else fd["away_price"]
            k_leg, k_cost = f"Kalshi NO {fav} by over {k_num:g}", 1 - r["yes_bid"]
        else:                # Kalshi YES "fav by over k_num" + FanDuel underdog +fd_num
            dog = g["away"] if fav == g["home"] else g["home"]
            fd_leg = f"FanDuel {dog} +{fd_num:g}"
            fd_price = fd["away_price"] if fav == g["home"] else fd["home_price"]
            k_leg, k_cost = f"Kalshi YES {fav} by over {k_num:g}", r["yes_ask"]
    if fd_price is None or not k_cost:
        return
    dec = 1 + (fd_price / 100 if fd_price > 0 else 100 / -fd_price)
    cost = kalshi_client.with_fee(k_cost)
    ev = p_above_lo * dec - 1 + p_below_hi / cost - 1   # $1 on each leg
    out.append({"kind": f"middle-{market}", "game_id": g["game_id"], "date": g["date"], "tip_utc": g["tip_utc"].isoformat(),
                "label": f"{tag}: {fd_leg} + {k_leg}", "both": round(p_both, 3), "ev": round(ev / 2, 4),
                "window": f"{lo:g} to {hi:g}", "fd_leg": fd_leg, "k_leg": k_leg, "k_cost": cost})


# ---------------------------------------------------------------------------
# Tracking: record, keep fresh until tip, grade

def _load(date):
    try:
        return json.loads((DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(date, d):
    DIR.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(d, indent=1, sort_keys=True)
    f = DIR / f"{date}.json"
    if not f.exists() or f.read_text() != txt:
        f.write_text(txt)


def record(found: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    by_date, n = {}, 0
    for e in found.get("edges", []):
        if now >= datetime.fromisoformat(e["tip_utc"]):
            continue
        d = by_date.setdefault(e["date"], _load(e["date"]))
        k = f"{e['game_id']}|{e['kind']}|{e['label']}"
        if k not in d:
            d[k] = {**e, "first_seen": stamp, "first_ev": e["ev"], "status": "pending"}
            n += 1
        elif d[k]["status"] == "pending":
            d[k].update({x: e[x] for x in ("ask", "cost", "fair", "ev", "max_price", "model", "model_agrees")}, last_seen=stamp)
    for date, d in by_date.items():
        _save(date, d)
    return n


def grade(box, sched) -> int:
    """Grade recorded edges from final scores (game markets) and box scores (props)."""
    import polars as pl
    done = {}
    if sched.height:
        for r in sched.filter(pl.col("completed") & pl.col("home_score").is_not_null()).iter_rows(named=True):
            done[r["game_id"]] = (r["home_score"], r["away_score"])
    pts = {}
    if box.height:
        for r in box.filter(pl.col("played")).select("player", "game_id", "pts").iter_rows(named=True):
            pts[(r["player"], r["game_id"])] = r["pts"]
    games_in = set(box["game_id"].unique().to_list()) if box.height else set()
    n = 0
    for f in sorted(DIR.glob("*.json")) if DIR.exists() else []:
        d = json.loads(f.read_text())
        changed = False
        for e in d.values():
            if e["status"] != "pending" or e["game_id"] not in done:
                continue
            hs, as_ = done[e["game_id"]]
            win = None
            side = e["side"]
            if e["kind"] == "moneyline":
                win = (hs > as_) == (side.split()[0] == e["home"])
            elif e["kind"] == "spread":
                team, num = side.split()[0], float(side.split()[1])
                margin = (hs - as_) if team == e["home"] else (as_ - hs)
                win = margin + num > 0 if margin + num != 0 else None
            elif e["kind"] == "total":
                tot = hs + as_
                win = None if tot == e["line"] else ((tot > e["line"]) == side.startswith("OVER"))
            elif e["kind"] == "prop":
                if e["game_id"] not in games_in:
                    continue
                a = pts.get((e.get("player"), e["game_id"]))
                win = None if a is None else ((a > e["line"]) == side.startswith("OVER"))
            e["status"] = "void" if win is None else "graded"
            e["hit"] = win
            e["units"] = 0.0 if win is None else round((1 / e["cost"] - 1) if win else -1.0, 4)
            changed = True
            n += 1
        if changed:
            f.write_text(json.dumps(d, indent=1, sort_keys=True))
    return n


def summary() -> dict:
    allr = []
    for f in sorted(DIR.glob("*.json")) if DIR.exists() else []:
        allr += list(json.loads(f.read_text()).values())
    g = [e for e in allr if e["status"] == "graded"]
    h = sum(1 for e in g if e["hit"])
    u = sum(e["units"] for e in g)
    by = {}
    for e in g:
        b = by.setdefault(e["kind"], {"n": 0, "hits": 0, "units": 0.0})
        b["n"] += 1
        b["hits"] += bool(e["hit"])
        b["units"] += e["units"]
    agree = [e for e in g if e.get("model_agrees")]
    return {"n": len(g), "hits": h, "units": round(u, 2), "roi": round(100 * u / len(g), 1) if g else None,
            "pending": sum(1 for e in allr if e["status"] == "pending"),
            "fair_avg": round(100 * sum(e["fair"] for e in g) / len(g), 1) if g else None,
            "rate": round(100 * h / len(g), 1) if g else None,
            "by": {k: {**v, "units": round(v["units"], 2)} for k, v in by.items()},
            "agree": {"n": len(agree), "hits": sum(1 for e in agree if e["hit"]),
                      "units": round(sum(e["units"] for e in agree), 2)},
            "recent": sorted(allr, key=lambda e: (e["date"], e.get("first_seen", "")), reverse=True)[:40]}
