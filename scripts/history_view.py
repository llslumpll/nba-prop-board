"""
Everything the History page shows, organised the way the page reads:
a plain-English summary first, then one tab per question, each split into
sub-sections (one per stat, list or market) with its own record, verdict,
charts and pick log.

Every number here is computed from the frozen pick files (player picks and
game picks); nothing is typed in by hand.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import grade
import live
import model
import svgchart

GREEN, RED, YELLOW, CYAN, DIM = "#3dff8f", "#ff3d57", "#ffc531", "#4f86ff", "#ab9cc8"
MIN_VERDICT = 20  # graded picks before a section gets a verdict
GROUPS = [("pts", "Points", ("pts",)), ("reb", "Rebounds", ("reb",)), ("ast", "Assists", ("ast",)),
          ("fg3m", "3-Pointers", ("fg3m",)), ("combos", "PRA & combos", ("pra", "pr", "pa", "ra"))]
MARKETS = [("moneyline", "Moneyline"), ("spread", "Spread"), ("total", "Total")]
CHECKS = [("open", "Opening line"), ("10am", "10 AM CT"), ("1pm", "1 PM CT"), ("5pm", "5 PM CT"), ("final", "Locked at tip")]


def when_rows(ps: list[dict], only_lists: bool = False) -> list[dict]:
    """One row per checkpoint: how the picks as they stood at that moment did."""
    rows = []
    for ck, label in CHECKS:
        n = h = 0
        flat = rec = staked = 0.0
        be, clvs, flipped, pend = [], [], 0, 0
        for p in ps:
            if ck == "final":
                team = "market" in p
                sn = ({k: p.get(k) for k in ("pick", "line", "chance", "break_even", "market_side", "units")} if team else
                      {k: p.get(k) for k in ("line", "call", "chance", "break_even", "market_side", "units", "line_source")})
                sn["best5"] = p.get("best5") or []
                if "snaps" not in p:
                    continue  # compare like with like: only picks made under the follow-the-line rules
            else:
                sn = (p.get("snaps") or {}).get(ck)
                if not sn:
                    continue
            if only_lists and not sn.get("best5"):
                continue
            if p.get("status") == "pending":
                pend += 1
                continue
            res = grade.snap_result(p, sn)
            if res is None:
                continue
            f, r = grade.snap_units(p, sn)
            n += 1
            h += bool(res)
            flat += f
            rec += r
            staked += float(sn.get("units") or 0)
            be.append(sn.get("break_even") or live.DEFAULT_BE)
            side = sn.get("pick") if "market" in p else sn.get("call")
            final_side = p.get("pick") if "market" in p else p.get("call")
            flipped += side != final_side
            if ck != "final":
                c = grade.snap_clv(p, sn)
                if c and c["beat"]:
                    clvs.append(c["beat"] > 0)
        rows.append({"key": ck, "label": label, "graded": n, "hits": h, "misses": n - h, "pending": pend,
                     "rate": round(100 * h / n, 1) if n else None, "be": round(100 * sum(be) / len(be), 1) if be else None,
                     "flat": round(flat, 2), "rec": round(rec, 2), "rec_roi": round(100 * rec / staked, 1) if staked else None,
                     "clv": round(100 * sum(clvs) / len(clvs), 1) if clvs else None, "clv_n": len(clvs),
                     "flipped": flipped})
    best = [r for r in rows if r["graded"] >= MIN_VERDICT]
    if best:
        top = max(best, key=lambda r: (r["flat"] / r["graded"]))
        top["top"] = True
    return rows


def _dl(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%-m/%-d")


def _be(p) -> float:
    return p.get("break_even") or live.DEFAULT_BE


def stats(ps: list[dict]) -> dict:
    g = [p for p in ps if p.get("status") == "graded"]
    h = sum(1 for p in g if p.get("hit"))
    flat = sum(grade.units(p) or 0 for p in g)
    staked = sum(grade.stake(p) for p in g)
    rec = sum(grade.staked_pl(p) or 0 for p in g)
    be = round(100 * sum(_be(p) for p in g) / len(g), 1) if g else None
    rate = round(100 * h / len(g), 1) if g else None
    out = {"n": len(ps), "graded": len(g), "hits": h, "misses": len(g) - h, "rate": rate, "be": be,
           "flat": round(flat, 2), "roi": round(100 * flat / len(g), 1) if g else None,
           "staked": round(staked, 2), "rec": round(rec, 2), "rec_roi": round(100 * rec / staked, 1) if staked else None,
           "pending": sum(1 for p in ps if p.get("status") == "pending"),
           "void": sum(1 for p in ps if p.get("status") == "void"), "push": sum(1 for p in ps if p.get("status") == "push")}
    out.update(verdict(out))
    return out


def verdict(s: dict) -> dict:
    n, rate, be = s["graded"], s["rate"], s["be"]
    if not n:
        return {"v": "none", "vtext": "Nothing graded yet."}
    gap = round(rate - be, 1)
    if n < MIN_VERDICT:
        return {"v": "early", "vtext": f"Too early to judge: {n} graded (needs {MIN_VERDICT}+). "
                                       f"So far {rate}% vs {be}% needed to profit."}
    if gap >= 2 and s["flat"] > 0:
        return {"v": "good", "vtext": f"Hits {rate}%, {gap} points above the {be}% needed to profit."}
    if gap >= -1:
        return {"v": "ok", "vtext": f"Hits {rate}% vs {be}% needed. Not an edge yet."}
    return {"v": "bad", "vtext": f"Hits {rate}%, {abs(gap)} points below the {be}% needed to profit."}


def charts(ps: list[dict], name: str, h: int = 260) -> dict:
    """Daily hit rate (with break-even) + running units (flat and at the recommended stake)."""
    g = [p for p in ps if p.get("status") == "graded"]
    dates = sorted({p["date"] for p in g})
    if len(dates) < 1:
        return {"hit": None, "units": None, "days": 0}
    labels = [_dl(d) for d in dates]
    rates, run_f, run_r, f, r = [], [], [], 0.0, 0.0
    for d in dates:
        day = [p for p in g if p["date"] == d]
        rates.append(round(100 * sum(1 for p in day if p.get("hit")) / len(day), 1))
        f += sum(grade.units(p) or 0 for p in day)
        r += sum(grade.staked_pl(p) or 0 for p in day)
        run_f.append(round(f, 2))
        run_r.append(round(r, 2))
    be = round(100 * sum(_be(p) for p in g) / len(g), 1)
    many = len(dates) > 10
    lo_r = max(0, min(rates + [be, 50]) - 12)
    hi_r = min(100, max(rates + [be, 50]) + 12)
    hit = svgchart.line_chart([{"points": rates, "color": YELLOW}], height=h, y_min=lo_r, y_max=hi_r, y_suffix="%",
                              references=[{"y": be, "color": "#ff6a13", "label": f"needed {be}%", "below": True},
                                          {"y": 50, "color": DIM, "label": "50%", "below": True, "left": True}],
                              point_labels=labels, show_values="last" if many else True, value_fmt="{:.0f}%", big=True,
                              x_labels=(labels[0], labels[-1]), label=f"{name}: hit rate by day")
    lo, hi = min(run_f + run_r + [0]), max(run_f + run_r + [0])
    pad = max(1.0, (hi - lo) * 0.15)
    units = svgchart.line_chart([{"points": run_f, "color": YELLOW}, {"points": run_r, "color": CYAN}], height=h,
                                y_min=lo - pad, y_max=hi + pad, fill_zero=True, point_labels=labels,
                                show_values="last", value_fmt="{:+.1f}", big=True,
                                x_labels=(labels[0], labels[-1]), label=f"{name}: running units")
    return {"hit": hit, "units": units, "days": len(dates)}


def _mark(p):
    if p.get("status") == "graded":
        return "hit" if p.get("hit") else "miss"
    return p.get("status") or "pending"


def log_rows(ps: list[dict], limit: int = 60) -> list[dict]:
    rows = []
    for p in sorted(ps, key=lambda p: (p["date"], -(p.get("chance") or 0)), reverse=True)[:limit]:
        team_pick = "market" in p
        rows.append({
            "date": _dl(p["date"]), "who": f"{p['away']} @ {p['home']}" if team_pick else p["player"],
            "pid": None if team_pick else p.get("player_id"), "team": None if team_pick else p.get("team"),
            "what": (p.get("pick_label") or p["pick"]) if team_pick else f"{p['call']} {p['line']:g} {model.STAT_LABEL.get(p['stat'], p['stat'])}",
            "side": ("under" if p.get("pick") == "UNDER" else "over") if team_pick else p["call"].lower(),
            "actual": p.get("final") if team_pick else p.get("actual"), "chance": p.get("chance"),
            "u": grade.stake(p), "pl": grade.staked_pl(p), "flat": grade.units(p), "mark": _mark(p)})
    return rows


def section(ps: list[dict], name: str, key: str, limit: int = 60) -> dict:
    return {"key": key, "name": name, "s": stats(ps), "c": charts(ps, name), "log": log_rows(ps, limit)}


def build(preds: list[dict], tpreds: list[dict], health: list, page_ids: set, trust: dict | None) -> dict:
    allp = preds + tpreds
    out = {"overall": stats(allp), "players_all": stats(preds), "games_all": stats(tpreds),
           "overall_c": charts(allp, "Every pick")}

    # when to bet: picks as they stood at each checkpoint
    out["when"] = {"props": when_rows(preds), "games": when_rows(tpreds),
                   "props_b5": when_rows(preds, True), "games_b5": when_rows(tpreds, True),
                   "n": sum(1 for p in allp if p.get("snaps"))}

    # what's working / what isn't: stat x line source and game markets, 15+ graded
    segs = []
    by = defaultdict(list)
    for p in preds:
        by[(model.STAT_LABEL.get(p["stat"], p["stat"]), p.get("line_source"))].append(p)
    for (stat, src), ps in by.items():
        segs.append({"name": stat, "src": src, **stats(ps)})
    for m, label in MARKETS:
        segs.append({"name": f"Game {label.lower()}", "src": None, **stats([t for t in tpreds if t["market"] == m])})
    ranked = sorted([s for s in segs if s["graded"] >= 15], key=lambda s: -(s["roi"] or -999))
    out["working"] = [s for s in ranked if (s["roi"] or 0) > 0][:4]
    out["not_working"] = [s for s in reversed(ranked) if (s["roi"] or 0) < 0][:4]

    # units
    out["tiers"] = grade.unit_tiers(preds, tpreds)
    mx = max([abs(t["pl"]) for t in out["tiers"]] + [1])
    for t in out["tiers"]:
        t["bar"] = round(100 * abs(t["pl"]) / mx)
    out["trust"] = trust or {}
    out["steps"] = [{"u": u, "name": live.UNIT_NAMES[u], "edge": round(100 * e)} for e, u in live.UNIT_STEPS]

    # Best 5 / Best Value: one sub-section per list, player stats and game picks alike
    b5 = []
    for key, name, _stats in GROUPS:
        lists = []
        for lst, lname in (("best5", "Best 5"), ("value", "Best Value")):
            ps = [p for p in preds if f"{key}:{lst}" in (p.get("best5") or [])]
            lists.append(section(ps, f"{name} {lname}", f"{key}-{lst}", 80) | {"list": lname})
        b5.append({"key": key, "name": name, "lists": lists})
    lists = []
    for lst, lname in (("best5", "Best 5"), ("value", "Best Value")):
        ps = [t for t in tpreds if f"game:{lst}" in (t.get("best5") or [])]
        lists.append(section(ps, f"Game picks (all) {lname}", f"games-{lst}", 80) | {"list": lname})
    b5.append({"key": "games", "name": "Game picks (all)", "lists": lists})
    for m, label in MARKETS:
        lists = []
        for lst, lname in (("best5", "Best 5"), ("value", "Best Value")):
            ps = [t for t in tpreds if f"{m}:{lst}" in (t.get("best5") or [])]
            lists.append(section(ps, f"{label} {lname}", f"{m}-{lst}", 80) | {"list": lname})
        b5.append({"key": m, "name": label, "lists": lists})
    every5 = [p for p in allp if any(t.endswith(":best5") for t in (p.get("best5") or []))]
    everyv = [p for p in allp if any(t.endswith(":value") for t in (p.get("best5") or []))]
    out["b5_all"] = [section(every5, "Every Best 5 list", "all-best5", 0) | {"list": "Best 5"},
                     section(everyv, "Every Best Value list", "all-value", 0) | {"list": "Best Value"}]
    out["b5"] = b5
    out["b5_table"] = [{"name": g["name"], "list": l["list"], **l["s"]} for g in b5 for l in g["lists"]]

    # by stat
    sb = grade.stat_scoreboard(preds)
    money_by = {(r["stat"], r["source"]): r for r in grade.money_summary(preds)["by"]}
    st = []
    for key, name, stat_keys in GROUPS:
        ps = [p for p in preds if p["stat"] in stat_keys]
        rows = [r | {"m": money_by.get((r["stat"], r["source"]), {})} for r in sb if r["stat"] in stat_keys]
        errs = [(p["our_err"], (p.get("line_errs") or {}).get(p.get("line_source"))) for p in ps
                if p.get("our_err") is not None and (p.get("line_errs") or {}).get(p.get("line_source")) is not None]
        st.append(section(ps, name, key, 0) | {"rows": rows, "multi": len(stat_keys) > 1,
                                                "our_err": round(sum(e[0] for e in errs) / len(errs), 2) if errs else None,
                                                "line_err": round(sum(e[1] for e in errs) / len(errs), 2) if errs else None,
                                                "over": stats([p for p in ps if p.get("call") == "OVER"]),
                                                "under": stats([p for p in ps if p.get("call") == "UNDER"])})
    out["by_stat"] = st

    # game picks
    out["markets"] = [section([t for t in tpreds if t["market"] == m], label, m, 60) for m, label in MARKETS]

    # players
    pl = defaultdict(list)
    for p in preds:
        pl[p.get("player_id")].append(p)
    prow = []
    for pid, ps in pl.items():
        s = stats(ps)
        if not s["graded"]:
            continue
        best = defaultdict(lambda: [0, 0])
        for p in ps:
            if p.get("status") == "graded":
                best[p["stat"]][0] += 1
                best[p["stat"]][1] += bool(p.get("hit"))
        bs = max(best.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[1][0]))
        prow.append({"pid": pid, "player": ps[-1]["player"], "team": ps[-1]["team"], "page": f"p-{pid}.html" in page_ids,
                     "best_stat": model.STAT_LABEL.get(bs[0], bs[0]), "best_rec": f"{bs[1][1]}/{bs[1][0]}", **s})
    prow.sort(key=lambda r: (-r["graded"], -(r["rate"] or 0)))
    out["players"] = prow
    hot = [r for r in prow if r["graded"] >= 4]
    out["hot"] = sorted(hot, key=lambda r: (-(r["rate"] or 0), -r["graded"]))[:6]
    out["cold"] = sorted(hot, key=lambda r: ((r["rate"] or 0), -r["graded"]))[:6]

    # confidence: does a higher chance / more value hit more?
    g = [p for p in allp if p.get("status") == "graded" and p.get("chance") is not None]
    cb = []
    for lo, hi in ((50, 55), (55, 60), (60, 65), (65, 101)):
        b = [p for p in g if lo <= 100 * p["chance"] < hi]
        cb.append({"label": f"{lo}-{hi}%" if hi <= 100 else f"{lo}%+", "mid": (lo + min(hi, 75)) / 2, **stats(b)})
    vb = []
    for lo, hi, lab in ((-100, 0, "below break-even"), (0, 3, "0-3 pts"), (3, 6, "3-6 pts"), (6, 10, "6-10 pts"), (10, 101, "10+ pts")):
        b = [p for p in g if lo <= 100 * (p["chance"] - _be(p)) < hi]
        vb.append({"label": lab, **stats(b)})
    out["chance_b"], out["value_b"] = cb, vb
    return out
