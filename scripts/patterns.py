"""
Patterns: what kinds of picks actually hit, and self-corrections that only
switch on once a pattern has held up on games it wasn't measured on.

Every build (after grading) splits the graded picks by game date into an
EARLIER half (where a pattern is spotted) and a LATER half (where it has to
hold up again). A correction switches on only if:
  * both halves have enough picks (MIN_TRAIN / MIN_TEST),
  * it points the same way in both halves, and
  * over all of them it is clearly more than luck (|z| >= Z_MIN).
Anything else stays "watching", or "flipped" when the later games went the
other way (that's what happened to "UNDERs win" and "follow the line move"
between Oct 22 and Oct 25).

Five kinds of correction:
  skip   a group of picks (stat + line source, or stat + source + side, or a
         game-pick market + side) whose hit rate sits below what its prices
         need in both halves: those picks become PASS (0 units) and stay off
         Best 5 / Best Value. Combos on PrizePicks are the obvious candidate.
  over   our chance of OVER for a stat + source is off in the same direction
         in both halves (e.g. the line runs high): future chances move by part
         of the gap (shrunk toward 0, capped at MAX_SHIFT).
  conf   our stated chances on one line source run too high in both halves
         (picks said 63% and hit 53%): every chance on that source is pulled
         toward 50% by the share of our lean that didn't show up in results,
         so its picks size down, mostly to PASS, and rank lower on Best 5.
  flip   a group (stat + source, stat + source + side, line-move group, or game
         market + side) where the OTHER side beat its own price in both halves:
         those picks switch sides. The new chance is (1 - ours) plus the lift
         the other side showed (shrunk). On PrizePicks both sides need 54.2%, so
         a group hitting 48% never flips (52% still loses) but one hitting 42%
         can (58%). Other side's price: saved since Oct 27, estimated (4.5% cut)
         for older FanDuel / Kalshi picks. Patterns are always measured on
         the model's own side (orig()), so a flip doesn't hide what it learned from.
  move   picks whose side agrees / disagrees with how their line has moved
         since it opened: chance moves by part of the measured gap.

Active corrections are written to data/patterns.json and read by the NEXT
build (live.run); once active, a correction is re-checked every build and
switches off if it stops holding. The History -> Patterns tab shows all of
it, with each correction's record since it switched on.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import grade
import live
import model

PATH = Path(__file__).parent.parent / "data" / "patterns.json"
MIN_TRAIN, MIN_TEST = 150, 100
Z_MIN = 2.0
MIN_GAP = 0.03       # 3 percentage points, smaller isn't worth acting on
SHRINK_N = 400       # shift = gap * n / (n + SHRINK_N)
MAX_SHIFT = 0.05
SOURCES = {"fanduel": "FanDuel", "kalshi": "Kalshi", "prizepicks": "PrizePicks",
           "prizepicks+kalshi": "PrizePicks (Kalshi-priced)"}


def sk(p: dict) -> str | None:
    """Source key for grouping: PrizePicks lines priced from Kalshi (from Oct 28) are their own group."""
    return p.get("src_key") or p.get("line_source")


def _be(p):
    return p.get("break_even") or live.DEFAULT_BE


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def active(store: dict | None = None) -> dict:
    """What live.run applies: {"skip": set of keys, "over": {(stat, src): shift}, "move": {"agree"/"against": shift}}."""
    store = store if store is not None else load()
    out = {"skip": set(), "over": {}, "move": {}, "conf": {}, "flip": {}, "proven": set(store.get("proven", []))}
    for r in store.get("rules", []):
        if r.get("status") != "active":
            continue
        if r["kind"] == "skip":
            out["skip"].add(r["key"])
        elif r["kind"] == "over":
            out["over"][tuple(r["key"].split("|"))] = r["shift"]
        elif r["kind"] == "move":
            out["move"][r["key"]] = r["shift"]
        elif r["kind"] == "conf":
            out["conf"][r["key"]] = r["factor"]
        elif r["kind"] == "flip":
            out["flip"][r["key"]] = r["lift"]
    return out


def skip_keys(p: dict) -> list[str]:
    """Every skip-rule key a pick (player or game) belongs to."""
    if "market" in p:
        side = (p.get("pick") if p["market"] == "total" else
                ("fav" if (p.get("break_even") or 0.5) >= 0.5 else "dog") if p["market"] == "moneyline" else "any")
        return [f"game|{p['market']}", f"game|{p['market']}|{side}"]
    src = sk(p) or p.get("source")
    side = p.get("call") or p.get("side")
    return [f"{p['stat']}|{src}", f"{p['stat']}|{src}|{side}"]


VIG = 0.045  # typical two-sided cut, only used when the other side's price wasn't saved (picks before Oct 27)


def other_be(p: dict) -> float:
    """Break-even of the side we did NOT pick: its saved price when we have it, else estimated."""
    if "market" in p:
        if p.get("be_other"):
            return p["be_other"]
    else:
        if p.get("line_source") == "prizepicks":
            return _be(p)  # PrizePicks: both sides need the same 54.2%
        saved = p.get("be_under") if p.get("call") == "OVER" else p.get("be_over")
        if saved:
            return saved
    return min(0.95, max(0.05, 1 + VIG - _be(p)))


def orig(p: dict) -> dict:
    """A flipped pick seen the way the model first called it, so patterns are always measured on
    the model's own side (a flip's own results are tracked separately, on the rule)."""
    f = p.get("flip")
    if not f:
        return p
    q = dict(p)
    if "market" in p:
        q.update(pick=f["orig_pick"], pick_label=f.get("orig_label"), chance=f["orig_chance"],
                 break_even=f.get("orig_be"), be_other=p.get("break_even"))
    else:
        q.update(call=f["orig_call"], chance=f["orig_chance"], break_even=f.get("orig_be"))
    if p.get("status") == "graded" and p.get("hit") is not None:
        q["hit"] = not p["hit"]
    q.pop("flip", None)
    return q


def move_dir(p: dict) -> str | None:
    """'agree' when the pick's side follows how its line moved since opening (OVER and the line went
    up, UNDER and it went down), 'against' the other way, None if unmoved or unknown."""
    op = (p.get("snaps") or {}).get("open")
    if not op or op.get("line") is None or p.get("line") is None or op.get("line_source") != p.get("line_source"):
        return None
    mv = p["line"] - op["line"]
    if mv == 0:
        return None
    return "agree" if (mv > 0) == (p.get("call") == "OVER") else "against"


# ---------------------------------------------------------------------------

def _hit_vs_need(ps):
    n = len(ps)
    h = sum(1 for p in ps if p.get("hit"))
    need = sum(_be(p) for p in ps)
    var = sum(_be(p) * (1 - _be(p)) for p in ps)
    return n, h, need, ((h - need) / math.sqrt(var)) if var > 0 else 0.0


def _over_cal(ps):
    """(n, actual overs, expected overs from our chances, z)."""
    n = len(ps)
    act = sum(1 for p in ps if p["actual"] > p["line"])
    po = [p["chance"] if p["call"] == "OVER" else 1 - p["chance"] for p in ps]
    exp = sum(po)
    var = sum(x * (1 - x) for x in po)
    return n, act, exp, ((act - exp) / math.sqrt(var)) if var > 0 else 0.0


def _reversed(ps):
    """Betting the OTHER side: (n, its hits, hits its own prices need, z)."""
    n = len(ps)
    h = sum(1 for p in ps if p.get("hit") is False)
    bes = [other_be(p) for p in ps]
    need = sum(bes)
    var = sum(b * (1 - b) for b in bes)
    return n, h, need, ((h - need) / math.sqrt(var)) if var > 0 else 0.0


def _hit_vs_chance(ps):
    n = len(ps)
    h = sum(1 for p in ps if p.get("hit"))
    exp = sum(p["chance"] for p in ps)
    var = sum(p["chance"] * (1 - p["chance"]) for p in ps)
    return n, h, exp, ((h - exp) / math.sqrt(var)) if var > 0 else 0.0


def _split(graded):
    dates = sorted({p["date"] for p in graded})
    if len(dates) < 2:
        return set(dates), set()
    # earlier half by number of picks, never splitting a date
    total, run, cut = len(graded), 0, dates[0]
    per = defaultdict(int)
    for p in graded:
        per[p["date"]] += 1
    for d in dates:
        run += per[d]
        cut = d
        if run >= total / 2:
            break
    train = {d for d in dates if d <= cut}
    test = set(dates) - train
    return train, test


def _lean(ps):
    return sum(p["chance"] - 0.5 for p in ps) / len(ps) if ps else 0.0


def _rule(kind, key, label, ps, train, test, fn, direction_needed=None):
    tr = [p for p in ps if p["date"] in train]
    te = [p for p in ps if p["date"] in test]
    n_all, a_all, e_all, z = fn(ps)
    n1, a1, e1, _ = fn(tr) if tr else (0, 0, 0, 0)
    n2, a2, e2, _ = fn(te) if te else (0, 0, 0, 0)
    gap1 = (a1 - e1) / n1 if n1 else 0.0
    gap2 = (a2 - e2) / n2 if n2 else 0.0
    gap = (a_all - e_all) / n_all if n_all else 0.0
    if n1 < MIN_TRAIN or n2 < MIN_TEST:
        status, why = "watching", f"needs {MIN_TRAIN}+ earlier and {MIN_TEST}+ later graded picks (has {n1} and {n2})"
    elif (gap1 > 0) != (gap2 > 0):
        status, why = "flipped", f"earlier games {gap1 * 100:+.1f} pts, later games {gap2 * 100:+.1f} pts: it didn't hold"
    elif direction_needed is not None and (gap < 0) != direction_needed:
        status, why = "fine", (f"hitting more than our chances said ({gap * 100:+.1f} pts), nothing to fix" if kind == "conf"
                               else f"the other side doesn't beat its price either ({gap * 100:+.1f} pts), so no flip" if kind == "flip"
                               else f"beating its prices ({gap * 100:+.1f} pts over what's needed), nothing to fix")
    elif (abs(gap) < MIN_GAP if kind != "conf" else abs(gap) < 0.25 * _lean(ps)) or abs(z) < Z_MIN:
        # (confidence fixes are judged against how far our chances lean from 50%: losing a quarter of it matters)
        status, why = "watching", f"same direction in both halves but small or not clear of luck yet ({gap * 100:+.1f} pts, z {z:+.1f})"
    else:
        status, why = "active", f"held in both halves: earlier {gap1 * 100:+.1f} pts, later {gap2 * 100:+.1f} pts (z {z:+.1f})"
    shift = round(max(-MAX_SHIFT, min(MAX_SHIFT, gap * n_all / (n_all + SHRINK_N))), 4)
    out = {"kind": kind, "key": key, "label": label, "status": status, "why": why, "n": n_all,
           "n_train": n1, "n_test": n2, "gap": round(gap, 4), "gap_train": round(gap1, 4), "gap_test": round(gap2, 4),
           "z": round(z, 2), "shift": shift if kind not in ("skip", "conf", "flip") else None}
    if kind == "flip" and ps:
        # how much more often the other side hit than (1 - our chance) said; added to it when flipping
        base = sum(1 - p["chance"] for p in ps) / len(ps)
        lift = a_all / n_all - base if n_all else 0.0
        out["lift"] = round(max(0.0, min(0.10, lift * n_all / (n_all + SHRINK_N))), 4)
    if kind == "conf" and ps:
        # how much of our lean away from 50% to keep: the share that actually showed up in results,
        # moved toward 1 (no change) on small samples
        lean = _lean(ps)
        a = min(1.0, max(0.0, 1 + gap / lean)) if lean > 0 else 1.0
        out["factor"] = round(1 - (1 - a) * n_all / (n_all + SHRINK_N), 3)
    return out


def _group_by(ps, fn):
    out = defaultdict(list)
    for p in ps:
        out[fn(p)].append(p)
    return out


def _group(ps, key):
    out = defaultdict(list)
    for p in ps:
        out[p.get(key)].append(p)
    return out


def _stat_name(s):
    return model.STAT_LABEL.get(s, s)


def compute(preds: list[dict], tpreds: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    prev = {r["kind"] + ":" + r["key"]: r for r in load().get("rules", [])}
    raw_g = [p for p in preds if p.get("status") == "graded" and p.get("actual") is not None]
    raw_gt = [t for t in tpreds if t.get("status") == "graded"]
    g, gt = [orig(p) for p in raw_g], [orig(t) for t in raw_gt]
    train, test = _split(g + gt)
    rules = []

    by_ss, by_sss = defaultdict(list), defaultdict(list)
    for p in g:
        by_ss[(p["stat"], sk(p))].append(p)
        by_sss[(p["stat"], sk(p), p["call"])].append(p)
    for (s, src), ps in sorted(by_ss.items()):
        rules.append(_rule("skip", f"{s}|{src}", f"{_stat_name(s)} on {SOURCES.get(src, src)}", ps, train, test,
                           _hit_vs_need, direction_needed=True))
        rules.append(_rule("over", f"{s}|{src}", f"Chance of OVER, {_stat_name(s)} on {SOURCES.get(src, src)}",
                           ps, train, test, _over_cal))
    for (s, src, side), ps in sorted(by_sss.items()):
        rules.append(_rule("skip", f"{s}|{src}|{side}", f"{_stat_name(s)} {side} on {SOURCES.get(src, src)}",
                           ps, train, test, _hit_vs_need, direction_needed=True))
    # flips: the other side must beat ITS OWN price in both halves (PrizePicks: 54.2% either way)
    fl = defaultdict(list)
    for p in g:
        fl[f"{p['stat']}|{sk(p)}"].append(p)
        fl[f"{p['stat']}|{sk(p)}|{p['call']}"].append(p)
        d = move_dir(p)
        if d:
            fl[f"move|{d}"].append(p)
    for t in gt:
        for k in skip_keys(t):
            fl[k].append(t)
    for k, ps in sorted(fl.items()):
        if k.startswith("move|"):
            label = "Flip: picks that " + ("follow" if k.endswith("agree") else "go against") + " their line's move"
        elif k.startswith("game|"):
            label = "Flip: game " + " ".join(x if x not in ("fav", "dog") else ("favorites" if x == "fav" else "underdogs")
                                            for x in k.split("|")[1:])
        else:
            parts = k.split("|")
            label = f"Flip: {_stat_name(parts[0])} {parts[2] + ' ' if len(parts) > 2 else ''}on {SOURCES.get(parts[1], parts[1])}"
        rules.append(_rule("flip", k, label, ps, train, test, _reversed, direction_needed=False))
    for src, ps in sorted(_group_by(g, sk).items()):
        rules.append(_rule("conf", src, f"Our confidence on {SOURCES.get(src, src)} lines", ps, train, test,
                           _hit_vs_chance, direction_needed=True))
    mv = defaultdict(list)
    for p in g:
        d = move_dir(p)
        if d:
            mv[d].append(p)
    for d, label in (("agree", "Pick follows the line's move since it opened"),
                     ("against", "Pick goes against the line's move since it opened")):
        rules.append(_rule("move", d, label, mv.get(d, []), train, test, _hit_vs_chance))
    gm = defaultdict(list)
    for t in gt:
        for k in skip_keys(t):
            gm[k].append(t)
    for k, ps in sorted(gm.items()):
        parts = k.split("|")[1:]
        label = "Game picks: " + " ".join(x if x not in ("fav", "dog") else ("favorites" if x == "fav" else "underdogs") for x in parts)
        rules.append(_rule("skip", k, label, ps, train, test, _hit_vs_need, direction_needed=True))

    # keep activation dates; record since switching on
    stamp = now.strftime("%Y-%m-%d")
    for r in rules:
        old = prev.get(r["kind"] + ":" + r["key"])
        if r["status"] == "active":
            r["active_since"] = (old or {}).get("active_since") or stamp
        elif old and old.get("active_since"):
            r["was_active"] = old["active_since"]
            r["switched_off"] = old.get("switched_off") or stamp
        if r.get("active_since") and r["kind"] == "flip":
            since = [p for p in raw_g + raw_gt if p["date"] >= r["active_since"] and (p.get("flip") or {}).get("key") == r["key"]]
            h = sum(1 for p in since if p.get("hit"))
            r["since"] = {"n": len(since), "hits": h, "units": round(sum(grade.units(p) or 0 for p in since), 2)}
        elif r.get("active_since"):
            since = [p for p in (g + gt) if p["date"] >= r["active_since"]
                     and ((r["kind"] == "skip" and r["key"] in skip_keys(p))
                          or (r["kind"] == "over" and f"{p.get('stat')}|{sk(p)}" == r["key"])
                          or (r["kind"] == "move" and move_dir(p) == r["key"])
                          or (r["kind"] == "conf" and sk(p) == r["key"]))]
            h = sum(1 for p in since if p.get("hit"))
            r["since"] = {"n": len(since), "hits": h, "units": round(sum(grade.units(p) or 0 for p in since), 2)}
    order = {"active": 0, "watching": 1, "flipped": 2, "fine": 3}
    rules.sort(key=lambda r: (order.get(r["status"], 9), -abs(r["z"])))

    # a new kind of pick (PrizePicks priced from Kalshi, from Oct 28) is "proven" once 150+ of them are graded
    # and they've at least broken even; until then live.run caps their stake at NEW_GROUP_CAP
    proven = []
    for key, ps in _group_by(g, sk).items():
        n = len(ps)
        if key and n >= MIN_TRAIN and sum(grade.units(p) or 0 for p in ps) >= 0:
            proven.append(key)
    out = {"at": now.strftime("%Y-%m-%d %H:%M UTC"), "train": sorted(train), "test": sorted(test), "proven": sorted(proven),
           "rules": rules, "tables": tables(g, gt)}
    txt = json.dumps(out, indent=1, sort_keys=True, default=str)
    if not PATH.exists() or PATH.read_text() != txt:
        PATH.write_text(txt)
    return out


def _row(label, ps):
    n = len(ps)
    h = sum(1 for p in ps if p.get("hit"))
    u = sum(grade.units(p) or 0 for p in ps)
    need = round(100 * sum(_be(p) for p in ps) / n, 1) if n else None
    rate = round(100 * h / n, 1) if n else None
    return {"label": label, "n": n, "hits": h, "rate": rate, "need": need, "units": round(u, 2),
            "roi": round(100 * u / n, 1) if n else None,
            "v": None if n < 30 else ("good" if rate > need and u > 0 else "bad" if rate < need - 1 else "ok")}


def tables(g: list[dict], gt: list[dict]) -> list[dict]:
    """Display breakdowns (no corrections attached)."""
    T = []

    def table(key, title, help_, groups):
        T.append({"key": key, "title": title, "help": help_, "rows": [_row(lab, ps) for lab, ps in groups if ps]})

    table("side", "Over vs under", "Is one side winning more for a stat? Lines that run high make UNDERs win.",
          [(f"{_stat_name(s)} {side}", [p for p in g if p["stat"] == s and p["call"] == side])
           for s in model.STATS for side in ("OVER", "UNDER")])
    table("source", "By line source", "Which book's lines we beat. PrizePicks has no prices, so its break-even is a flat 54.2%.",
          [(SOURCES[s], [p for p in g if sk(p) == s]) for s in SOURCES])
    buckets = ((0, .5, "plus money"), (.5, .53, "even to -112"), (.53, .56, "-113 to -127"),
               (.56, .6, "-128 to -150"), (.6, 1.01, "-150 or more"))
    table("price", "By price (FanDuel and Kalshi)", "The price sets what you need to hit. Heavy favorites need a lot.",
          [(lab, [p for p in g if p["line_source"] != "prizepicks" and lo <= _be(p) < hi]) for lo, hi, lab in buckets])
    table("move", "Line move since the pick opened", "Did following the market's move help, or did going against it?",
          [("pick follows the move", [p for p in g if move_dir(p) == "agree"]),
           ("pick goes against the move", [p for p in g if move_dir(p) == "against"]),
           ("line didn't move", [p for p in g if (p.get("snaps") or {}).get("open") and move_dir(p) is None])])

    def gap(p):
        return abs(p["projected"] - p["line"]) / max(p["line"], 1)
    table("gap", "How far our projection was from the line", "Big disagreements with the line are where we're most often wrong.",
          [(lab, [p for p in g if p.get("line") and lo <= gap(p) < hi])
           for lo, hi, lab in ((0, .05, "within 5%"), (.05, .15, "5-15%"), (.15, .3, "15-30%"), (.3, 99, "30%+"))])
    table("home", "Home vs road", "Whether the player was at home.",
          [("home", [p for p in g if p.get("home")]), ("road", [p for p in g if p.get("home") is False])])
    table("tier", "How much this-season data the player had", "Early in the season most players are projected from last season.",
          [(t, [p for p in g if p.get("tier") == t]) for t in sorted({p.get("tier") for p in g if p.get("tier")})])
    table("games", "Game picks by market and side", "Moneyline favorites vs underdogs, spreads, totals over vs under.",
          [(lab, [t for t in gt if f"game|{k}" in skip_keys(t) or k in skip_keys(t)])
           for k, lab in (("game|moneyline|fav", "moneyline favorites"), ("game|moneyline|dog", "moneyline underdogs"),
                          ("game|spread", "spreads"), ("game|total|OVER", "totals OVER"), ("game|total|UNDER", "totals UNDER"))])
    return T
