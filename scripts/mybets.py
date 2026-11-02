"""
My Bets (update 53): the user's real bets, read from a Google Sheet published as CSV, graded automatically.

Setup: Google Sheet with the header row  date, bet, stat, side, line, book, price, units
       File -> Share -> Publish to web -> (the sheet) -> Comma-separated values -> Publish -> copy the link,
       saved as the GitHub repository secret MYBETS_CSV. No secret = the page says how to set it up.

Rows (one bet each; the COPY BET buttons on the site produce them, tab-separated so they paste into columns):
  date   11/02 or 2026-11-02 (the slate's date)
  bet    player name | team (ML / SPREAD) | "CHA@WAS" or a team (TOTAL) | parlay id from the site (safe-2, news-1-w2)
  stat   PTS REB AST 3PM PRA P+R P+A R+A | ML SPREAD TOTAL | PARLAY (also 2-MAN, 3-LEG, SINGLE: legs in "bet"
         joined with " + ", e.g. "SAS ML + NYK ML" or "LeBron James OVER 24.5 PTS + LAL ML")
  side   OVER / UNDER (props, totals); blank for ML / SPREAD / PARLAY
  line   the number (SPREAD: the team's number, e.g. -5.5 or +3.5)
  book   free text (Kalshi, PrizePicks, FanDuel, ...)
  price  what was paid: 52 (= 52%, Kalshi-style), -110 / +150 (American), 3.4x (payout multiple); blank = -110
  units  stake in units; blank = 1

Grading: props from the site's graded pick for that player-stat-date, else the box score; game bets from the
final score; site parlays from the site's own graded parlay. Unmatched rows are listed so a typo can be fixed.
data/mybets.json keeps the latest graded copy (units only; dollars appear only in the Discord recap).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import unicodedata
import urllib.request
from datetime import date as Date
from pathlib import Path

ROOT = Path(__file__).parent.parent / "data"
OUT = ROOT / "mybets.json"
STAT = {"PTS": "pts", "POINTS": "pts", "REB": "reb", "REBOUNDS": "reb", "AST": "ast", "ASSISTS": "ast",
        "3PM": "fg3m", "3PT": "fg3m", "THREES": "fg3m", "FG3M": "fg3m", "PRA": "pra", "P+R+A": "pra",
        "P+R": "pr", "PR": "pr", "P+A": "pa", "PA": "pa", "R+A": "ra", "RA": "ra"}
PARTS = {"pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",), "pra": ("pts", "reb", "ast"),
         "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}
DEFAULT_PAYOUT = 1 / 0.5238   # -110


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def _date(s: str) -> str | None:
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", s)
    if m:
        mo, d = int(m[1]), int(m[2])
        y = int(m[3]) if m[3] else (2026 if mo >= 8 else 2027)   # 2026-27 season
        y = y + 2000 if y < 100 else y
        try:
            return Date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def payout(price: str) -> float | None:
    """Total return per unit staked (stake included). None = can't read it."""
    p = (price or "").strip().replace("%", "").replace("¢", "").lower()
    if not p:
        return DEFAULT_PAYOUT
    try:
        if p.endswith("x"):
            v = float(p[:-1])
            return v if v > 1 else None
        v = float(p)
    except ValueError:
        return None
    if p.startswith(("+", "-")) and abs(v) >= 100:
        return 1 + (v / 100 if v > 0 else 100 / -v)
    if 0 < v < 1:
        return 1 / v
    if 1 <= v < 100:
        return 100 / v
    return None


def fetch(url: str | None = None) -> tuple[list[dict], str | None]:
    url = url or (os.environ.get("MYBETS_CSV") or "").strip()
    if not url.startswith("https://"):
        return [], "not set up"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "prop-edge-mybets"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8-sig")
    except Exception as e:  # noqa: BLE001
        return [], f"couldn't read the sheet ({str(e)[:80]})"
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], None
    head = [h.strip().lower() for h in rows[0]]
    cols = ("date", "bet", "stat", "side", "line", "book", "price", "units")
    if "bet" in head and "date" in head:
        idx = {c: head.index(c) for c in cols if c in head}
        body = rows[1:]
    else:   # no header row: columns in the standard order
        idx = {c: i for i, c in enumerate(cols)}
        body = rows
    out = []
    for i, r in enumerate(body, start=2 if "bet" in head else 1):
        rec = {c: (r[idx[c]].strip() if c in idx and idx[c] < len(r) else "") for c in cols}
        if rec["bet"] or rec["stat"]:
            rec["row"] = i
            out.append(rec)
    return out, None


def _box_lookup(box):
    """{(date, normalized player): {stat: value}} from the box score (played games)."""
    if box is None or not getattr(box, "height", 0):
        return {}
    import polars as pl
    b = box.filter(pl.col("played")).select("date", "player", "pts", "reb", "ast", "fg3m")
    return {(str(r["date"]), _norm(r["player"])): r for r in b.iter_rows(named=True)}


LEG_PROP = re.compile(r"^(.+?)\s+(OVER|UNDER)\s+([\d.]+)\s+([A-Z0-9+]+)$", re.I)
LEG_ML = re.compile(r"^([A-Z]{2,4})\s+ML$", re.I)


def _parse_leg(txt: str):
    m = LEG_ML.match(txt)
    if m:
        return ("ml", m[1].upper())
    m = LEG_PROP.match(txt)
    if m and STAT.get(m[4].upper()):
        return ("prop", m[1], STAT[m[4].upper()], m[2].upper(), float(m[3]))
    return None


def _grade_leg(leg, d, preds_d, tps_d, boxd, played_dates) -> str:
    """'hit' | 'miss' | 'push' | 'void' | 'pending' | 'unmatched' for one parsed parlay leg."""
    if leg[0] == "ml":
        rec = next((t for t in tps_d.values() if leg[1] in (t["home"], t["away"])), None)
        if not rec:
            return "unmatched" if d in played_dates else "pending"
        if not rec.get("scores"):
            return "pending"
        hs, as_ = rec["scores"]
        return "hit" if (hs > as_) == (leg[1] == rec["home"]) else "miss"
    _, name, st, side, line = leg
    nm = _norm(name)
    p = next((v for v in preds_d.values() if v.get("stat") == st and _norm(v["player"]) == nm), None)
    if p and p.get("status") == "void":
        return "void"
    actual = p["actual"] if p and p.get("actual") is not None else None
    if actual is None:
        bx = boxd.get((d, nm))
        if bx:
            actual = sum(bx[x] or 0 for x in PARTS[st])
    if actual is None:
        if d in played_dates and not p and not any(_norm(v["player"]) == nm for v in preds_d.values()):
            return "unmatched"
        return "pending" if not (d in played_dates and p and p.get("status") == "graded") else "void"
    return "push" if actual == line else ("hit" if (actual > line) == (side == "OVER") else "miss")


def grade_rows(rows: list[dict], box=None) -> list[dict]:
    import parlays
    import predictions
    boxd = _box_lookup(box)
    played_dates = {k[0] for k in boxd}
    preds, tps, pzs = {}, {}, {}
    out = []
    for r in rows:
        g = dict(r)
        d = _date(r["date"])
        stat = (r["stat"] or "").strip().upper().replace(" ", "")
        side = (r["side"] or "").strip().upper()
        pay = payout(r["price"])
        try:
            units = float(r["units"]) if r["units"] else 1.0
        except ValueError:
            units = None
        try:
            line = float(r["line"].replace("+", "")) if r["line"] else None
        except ValueError:
            line = None
        g.update(date_iso=d, units_f=units, payout=round(pay, 4) if pay else None, result="pending", kind="prop")
        problem = (None if d else "date") or (None if pay else "price") or (None if units is not None else "units")
        if problem:
            g.update(result="unmatched", note=f"can't read the {problem}")
            out.append(g)
            continue
        if d not in preds:
            preds[d] = predictions.load_date(d)
            tps[d] = predictions.load_date(d, team=True)
            pzs[d] = parlays.load_date(d)
        multi = re.match(r"^(PARLAY|SINGLE|STRAIGHT|\d+-?(MAN|LEGS?|PICKS?|TEAM))$", stat) or (" + " in r["bet"] and stat not in STAT)
        if multi:
            # "ml-2: SAS ML + NYK ML": the legs you copied are graded themselves (a site parlay can still change legs
            # before its first tip); a bare id ("ml-2") falls back to the site's own graded parlay
            g["kind"] = "parlay"
            pid, colon, legtxt = r["bet"].partition(":")
            legtxt = legtxt if colon else r["bet"]          # "SAS ML + NYK ML" with no id works too
            legs = [x.strip() for x in legtxt.split("+") if x.strip()]
            parsed = [_parse_leg(x) for x in legs]
            if legs and all(parsed):
                res = [_grade_leg(x, d, preds[d], tps[d], boxd, played_dates) for x in parsed]
                bad = [legs[i] for i, x in enumerate(res) if x == "unmatched"]
                if bad:
                    g.update(result="unmatched", note=f"can't find leg: {bad[0]}")
                elif "miss" in res:
                    g["result"] = "miss"
                elif "pending" in res:
                    g["result"] = "pending"
                elif all(x in ("void", "push") for x in res):
                    g["result"] = "void"
                else:
                    g["result"] = "hit"   # void / pushed legs drop out, like most books (payout as entered)
                g["legs"] = [{"label": legs[i], "result": x} for i, x in enumerate(res)]
                g["label"] = " + ".join(legs)
                if len(legs) == 1:   # a single written as "ATL ML" / "Name OVER 14.5 PTS" counts with game bets / props
                    g["kind"] = "game" if parsed[0][0] == "ml" else "prop"
            else:
                pz = pzs[d].get(pid.strip().lower())
                if not pz:
                    g.update(result="unmatched", note="no site parlay with that id on that date (or a leg couldn't be read)")
                elif pz["status"] == "graded":
                    g["result"] = "hit" if pz["hit"] else "miss"
                    if pz["hit"] and not r["price"]:
                        g["payout"] = pz.get("pays_final") or pz.get("pays")   # the site's own payout when none was entered
                elif pz["status"] == "void":
                    g["result"] = "void"
                g["label"] = " + ".join(l["label"] for l in (pz or {}).get("legs", [])) or r["bet"]
        elif stat in ("ML", "SPREAD", "TOTAL"):
            g["kind"] = "game"
            team = re.sub(r"[^A-Z@]", "", r["bet"].upper())
            rec = next((t for t in tps[d].values() if team and (t["home"] in team or t["away"] in team)), None)
            if not rec:
                g.update(result="unmatched", note="no game for that team on that date")
            elif rec.get("scores"):
                hs, as_ = rec["scores"]
                pick = team.split("@")[0] if "@" not in team else None
                if stat == "ML":
                    won = (hs > as_) == (pick == rec["home"])
                    g["result"] = "hit" if won else "miss"
                elif stat == "SPREAD" and line is not None:
                    m = (hs - as_) if pick == rec["home"] else (as_ - hs)
                    g["result"] = "push" if m + line == 0 else ("hit" if m + line > 0 else "miss")
                elif stat == "TOTAL" and line is not None and side in ("OVER", "UNDER"):
                    t = hs + as_
                    g["result"] = "push" if t == line else ("hit" if (t > line) == (side == "OVER") else "miss")
                else:
                    g.update(result="unmatched", note="needs a line (and OVER / UNDER for totals)")
            g["label"] = f"{r['bet']} {stat}" + (f" {side}" if side else "") + (f" {r['line']}" if r["line"] else "")
        else:
            st = STAT.get(stat)
            g["label"] = f"{r['bet']} {side} {r['line']} {stat}"
            if not st or side not in ("OVER", "UNDER") or line is None:
                g.update(result="unmatched", note="needs a stat, OVER / UNDER and a line")
                out.append(g)
                continue
            nm = _norm(r["bet"])
            p = next((v for k, v in preds[d].items() if v.get("stat") == st and _norm(v["player"]) == nm), None)
            actual = None
            if p and p.get("status") == "void":
                g["result"] = "void"
            elif p and p.get("actual") is not None:
                actual = p["actual"]
            else:
                bx = boxd.get((d, nm))
                if bx:
                    actual = sum(bx[x] or 0 for x in PARTS[st])
                elif not p and d in played_dates and not any(_norm(v["player"]) == nm for v in preds[d].values()):
                    # that day's games are in, and no one by that name played or had a pick: likely a typo
                    g.update(result="unmatched", note="player not found on that date (check the spelling)")
            if actual is not None:
                g["actual"] = actual
                g["result"] = "push" if actual == line else ("hit" if (actual > line) == (side == "OVER") else "miss")
        out.append(g)
    for g in out:
        u = g.get("units_f") or 0
        g["pl"] = (round(u * (g["payout"] - 1), 2) if g["result"] == "hit" else -u if g["result"] == "miss"
                   else 0.0) if g.get("payout") else 0.0
    return out


def summary(graded: list[dict]) -> dict:
    done = [g for g in graded if g["result"] in ("hit", "miss")]
    h = sum(1 for g in done if g["result"] == "hit")
    need = [1 / g["payout"] for g in done if g.get("payout")]
    by_day, kinds = {}, {}
    for g in done:
        by_day[g["date_iso"]] = by_day.get(g["date_iso"], 0.0) + g["pl"]
        k = {"parlay": "Parlays", "game": "Game bets"}.get(g["kind"], "Player props")
        b = kinds.setdefault(k, {"n": 0, "h": 0, "u": 0.0})
        b["n"] += 1
        b["h"] += g["result"] == "hit"
        b["u"] += g["pl"]
    return {"n": len(done), "hits": h, "rate": round(100 * h / len(done), 1) if done else None,
            "need": round(100 * sum(need) / len(need), 1) if need else None,
            "units": round(sum(g["pl"] for g in done), 2), "staked": round(sum(g["units_f"] or 0 for g in done), 2),
            "pending": sum(1 for g in graded if g["result"] == "pending"),
            "unmatched": [g for g in graded if g["result"] == "unmatched"],
            "by_day": dict(sorted(by_day.items())),
            "kinds": [{"name": k, "n": v["n"], "h": v["h"], "u": round(v["u"], 2)} for k, v in kinds.items()],
            "rows": sorted([g for g in graded if g["result"] != "unmatched"], key=lambda g: (g["date_iso"], g["row"]), reverse=True)}


def run(box=None) -> dict:
    rows, err = fetch()
    if err == "not set up":
        return {"setup": False}
    if err:
        try:   # keep showing the last good copy if the sheet can't be read this build
            last = json.loads(OUT.read_text())
            return {**last, "error": err}
        except Exception:  # noqa: BLE001
            return {"setup": True, "error": err}
    s = {"setup": True, **summary(grade_rows(rows, box))}
    OUT.write_text(json.dumps({k: v for k, v in s.items() if k != "error"}, indent=1, default=str))
    return s
