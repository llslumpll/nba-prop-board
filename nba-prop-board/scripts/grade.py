"""
Grading, honest scoring and calibration.

Grading (runs every build, safe to repeat):
  * a pick is graded once its game's box score is in the data;
  * actual = the player's stat in that game_id; a DNP / scratch / missing
    row is VOID (never a miss); a result equal to a whole-number line is a
    PUSH; both are left out of hit rates;
  * every line seen at freeze time is scored too (|line - actual|), so the
    History page can show our average error next to each line's.

Edge vs lean (per stat): a stat is labelled "edge" only when its live record
has at least MIN_SAMPLE_CAL graded picks AND our average error is no worse
than the line's AND our hit rate beats the average break-even of the sides
we picked. Until then every pick on that stat is a "lean".

Calibration (two separate knobs, both gated by MIN_SAMPLE_CAL graded picks):
  * projection bias per stat and tier -> 30% of the average miss is added
    back to future projections in that tier;
  * chance shrink per stat -> stated chances are pulled toward 50% if picks
    that claimed 60% have been hitting less than that.
MIN_SAMPLE_CAL is about graded PICKS and has nothing to do with how many
games a player has played (that's model.Params.var_k and the tier cut-offs).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

import model
import predictions

DATA = Path(__file__).parent.parent / "data"
CAL_PATH = DATA / "calibration.json"
MIN_SAMPLE_CAL = 50
CAL_DAMPEN = 0.3
BIAS_CAP = {"pts": 2.0, "reb": 1.0, "ast": 1.0, "fg3m": 0.5, "pra": 3.0, "pr": 2.5, "pa": 2.5, "ra": 1.5}


def _now():
    return datetime.now(timezone.utc)


def _actuals(box: pl.DataFrame) -> tuple[dict, set]:
    """({(player_id, game_id): {stat: value}} for players who played,
    set of game_ids that have box scores)."""
    if not box.height:
        return {}, set()
    cur = model._with_combos(box.filter(pl.col("played")))
    vals = {}
    for row in cur.select(["player_id", "game_id"] + list(model.STATS)).iter_rows(named=True):
        vals[(row["player_id"], row["game_id"])] = row
    return vals, set(box["game_id"].unique().to_list())


def grade_all(box: pl.DataFrame, sched: pl.DataFrame) -> dict:
    vals, games_in = _actuals(box)
    now = _now()
    n_new = n_pending = 0
    for date in predictions.all_dates():
        store = predictions.load_date(date)
        changed = False
        for p in store.values():
            if p["status"] != "pending":
                continue
            if p["game_id"] not in games_in:
                n_pending += 1
                continue
            row = vals.get((p.get("player_id"), p["game_id"]))
            stamp = now.strftime("%Y-%m-%d %H:%M UTC")
            if row is None:
                p.update(status="void", void_reason="did not play", graded_at=stamp)
            else:
                a = float(row[p["stat"]])
                p["actual"] = a
                p["our_err"] = round(abs(p["projected"] - a), 2)
                p["line_errs"] = {s: round(abs(l - a), 2) for s, l in (p.get("lines") or {}).items()}
                if a == p["line"]:
                    p.update(status="push", hit=None)
                else:
                    p["hit"] = (a > p["line"]) if p["call"] == "OVER" else (a < p["line"])
                    p["status"] = "graded"
                p["graded_at"] = stamp
            n_new += 1
            changed = True
        if changed:
            predictions.mark_dirty(date)

    # team markets
    done = {}
    if sched.height:
        for r in sched.filter(pl.col("completed") & pl.col("home_score").is_not_null()).iter_rows(named=True):
            done[r["game_id"]] = (r["home_score"], r["away_score"])
    for date in predictions.all_dates(team=True):
        store = predictions.load_date(date, team=True)
        changed = False
        for p in store.values():
            if p["status"] != "pending" or p["game_id"] not in done:
                continue
            hs, as_ = done[p["game_id"]]
            if p["market"] == "moneyline":
                p["hit"] = (hs > as_) == (p["pick"] == p["home"])
            elif p["market"] == "spread":
                adj = hs - as_ + p["line"]  # line = home spread
                if adj == 0:
                    p["status"] = "push"
                else:
                    p["hit"] = (adj > 0) == (p["pick"] == p["home"])
            elif p["market"] == "total":
                tot = hs + as_
                if tot == p["line"]:
                    p["status"] = "push"
                else:
                    p["hit"] = (tot > p["line"]) == (p["pick"] == "OVER")
            if p["status"] == "pending":
                p["status"] = "graded"
            p["final"] = f"{p['away']} {as_} - {p['home']} {hs}"
            changed = True
            n_new += 1
        if changed:
            predictions.mark_dirty(date, team=True)
    predictions.save()
    return {"newly_graded": n_new, "pending": n_pending}


# ---------------------------------------------------------------------------
# Scoring summaries
# ---------------------------------------------------------------------------

def _rate(h, n):
    return round(100 * h / n, 1) if n else None


def stat_scoreboard(preds: list[dict]) -> list[dict]:
    """One row per stat x primary line source."""
    rows = {}
    for p in preds:
        k = (p["stat"], p["line_source"])
        r = rows.setdefault(k, {"stat": p["stat"], "source": p["line_source"], "n": 0, "hits": 0,
                                "push": 0, "void": 0, "pending": 0, "be_sum": 0.0, "be_n": 0,
                                "our_err": [], "line_err": []})
        if p["status"] == "pending":
            r["pending"] += 1
        elif p["status"] == "void":
            r["void"] += 1
        elif p["status"] == "push":
            r["push"] += 1
        else:
            r["n"] += 1
            r["hits"] += bool(p["hit"])
            if p.get("break_even") is not None:
                r["be_sum"] += p["break_even"]
                r["be_n"] += 1
        if p.get("our_err") is not None and (p.get("line_errs") or {}).get(p["line_source"]) is not None:
            r["our_err"].append(p["our_err"])
            r["line_err"].append(p["line_errs"][p["line_source"]])
    out = []
    for r in rows.values():
        out.append({
            "stat": r["stat"], "source": r["source"], "graded": r["n"], "hits": r["hits"],
            "hit_rate": _rate(r["hits"], r["n"]), "push": r["push"], "void": r["void"], "pending": r["pending"],
            "break_even": round(100 * r["be_sum"] / r["be_n"], 1) if r["be_n"] else None,
            "our_mae": round(sum(r["our_err"]) / len(r["our_err"]), 2) if r["our_err"] else None,
            "line_mae": round(sum(r["line_err"]) / len(r["line_err"]), 2) if r["line_err"] else None,
            "err_n": len(r["our_err"]),
        })
    order = list(model.STATS)
    return sorted(out, key=lambda x: (order.index(x["stat"]) if x["stat"] in order else 99, x["source"]))


def edge_status(preds: list[dict]) -> dict:
    """{stat: {"status": "edge"|"lean", "why": str, ...}} from the live record."""
    out = {}
    for s in model.STATS:
        g = [p for p in preds if p["stat"] == s and p["status"] == "graded"]
        errs = [(p["our_err"], p["line_errs"][p["line_source"]]) for p in g
                if p.get("our_err") is not None and (p.get("line_errs") or {}).get(p["line_source"]) is not None]
        n = len(g)
        hit = _rate(sum(bool(p["hit"]) for p in g), n)
        be = [p["break_even"] for p in g if p.get("break_even") is not None]
        be_avg = round(100 * sum(be) / len(be), 1) if be else None
        our = round(sum(e[0] for e in errs) / len(errs), 2) if errs else None
        line = round(sum(e[1] for e in errs) / len(errs), 2) if errs else None
        ok = (n >= MIN_SAMPLE_CAL and our is not None and line is not None and our <= line
              and hit is not None and be_avg is not None and hit > be_avg)
        if n < MIN_SAMPLE_CAL:
            why = f"{n}/{MIN_SAMPLE_CAL} graded picks so far"
        elif not ok:
            why = f"live record not ahead of the line yet (hit {hit}% vs break-even {be_avg}%, error {our} vs line {line})"
        else:
            why = f"live record ahead of the line (hit {hit}% vs {be_avg}%, error {our} vs {line})"
        out[s] = {"status": "edge" if ok else "lean", "why": why, "n": n, "hit_rate": hit,
                  "break_even": be_avg, "our_mae": our, "line_mae": line}
    return out


def chance_buckets(preds: list[dict]) -> list[dict]:
    """Do picks we gave a higher chance actually hit more often?"""
    g = [p for p in preds if p["status"] == "graded" and p.get("chance") is not None]
    out = []
    for lo, hi in ((50, 55), (55, 60), (60, 65), (65, 101)):
        b = [p for p in g if lo <= p["chance"] * 100 < hi]
        out.append({"bucket": f"{lo}-{hi if hi <= 100 else ''}%".replace("-%", "%+"), "n": len(b),
                    "hit_rate": _rate(sum(bool(p["hit"]) for p in b), len(b))})
    return out


def best5_record(preds: list[dict]) -> list[dict]:
    rows = {}
    for p in preds:
        for lst in p.get("best5", []):
            r = rows.setdefault((p["stat"], lst), {"stat": p["stat"], "list": lst, "n": 0, "hits": 0, "pending": 0})
            if p["status"] == "graded":
                r["n"] += 1
                r["hits"] += bool(p["hit"])
            elif p["status"] == "pending":
                r["pending"] += 1
    for r in rows.values():
        r["hit_rate"] = _rate(r["hits"], r["n"])
    return sorted(rows.values(), key=lambda r: (r["stat"], r["list"]))


def team_record(tpreds: list[dict]) -> list[dict]:
    out = []
    for m in ("moneyline", "spread", "total"):
        g = [p for p in tpreds if p["market"] == m and p["status"] == "graded"]
        out.append({"market": m, "n": len(g), "hits": sum(bool(p["hit"]) for p in g),
                    "hit_rate": _rate(sum(bool(p["hit"]) for p in g), len(g)),
                    "pending": sum(1 for p in tpreds if p["market"] == m and p["status"] == "pending")})
    return out


# ---------------------------------------------------------------------------
# Calibration (written for the NEXT build to read)
# ---------------------------------------------------------------------------

def calibrate(preds: list[dict]) -> dict:
    cal = {"min_sample": MIN_SAMPLE_CAL, "dampen": CAL_DAMPEN,
           "generated_at": _now().strftime("%Y-%m-%d %H:%M UTC"), "bias": {}, "chance_shrink": {}}
    for s in model.STATS:
        g = [p for p in preds if p["stat"] == s and p.get("actual") is not None]
        by_tier: dict[str, list] = {}
        for p in g:
            by_tier.setdefault(p.get("tier", "unknown"), []).append(p["actual"] - p["projected"])
        cal["bias"][s] = {}
        for tier, d in by_tier.items():
            if len(d) < MIN_SAMPLE_CAL:
                cal["bias"][s][tier] = {"n": len(d), "status": "building", "bias": 0.0}
            else:
                raw = sum(d) / len(d)
                b = max(-BIAS_CAP[s], min(BIAS_CAP[s], CAL_DAMPEN * raw))
                cal["bias"][s][tier] = {"n": len(d), "status": "active", "bias": round(b, 2), "raw": round(raw, 2)}
        gp = [p for p in preds if p["stat"] == s and p["status"] == "graded" and p.get("chance") is not None]
        if len(gp) < MIN_SAMPLE_CAL:
            cal["chance_shrink"][s] = {"n": len(gp), "status": "building", "shrink": 1.0}
            continue
        sxy = sum((p["chance"] - 0.5) * ((1.0 if p["hit"] else 0.0) - 0.5) for p in gp)
        sxx = sum((p["chance"] - 0.5) ** 2 for p in gp)
        raw = sxy / sxx if sxx > 0 else 1.0
        shrink = max(0.3, min(1.0, 1 + CAL_DAMPEN * (raw - 1)))
        cal["chance_shrink"][s] = {"n": len(gp), "status": "active", "shrink": round(shrink, 3), "raw": round(raw, 3)}
    CAL_PATH.write_text(json.dumps(cal, indent=1, sort_keys=True))
    return cal


def load_calibration() -> dict:
    try:
        return json.loads(CAL_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"min_sample": MIN_SAMPLE_CAL, "bias": {}, "chance_shrink": {}}


def maturity(cal: dict) -> dict:
    items = []
    for s, tiers in (cal.get("bias") or {}).items():
        for t, d in tiers.items():
            items.append({"what": f"{model.STAT_LABEL.get(s, s)} bias · {t}", "n": d["n"], "status": d["status"]})
    for s, d in (cal.get("chance_shrink") or {}).items():
        items.append({"what": f"{model.STAT_LABEL.get(s, s)} chance calibration", "n": d["n"], "status": d["status"]})
    active = sum(1 for i in items if i["status"] == "active")
    pending = [i for i in items if i["status"] != "active"]
    return {"active": active, "total": len(items), "min_sample": cal.get("min_sample", MIN_SAMPLE_CAL),
            "closest": max(pending, key=lambda i: i["n"]) if pending else None}
