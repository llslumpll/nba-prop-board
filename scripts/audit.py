"""
Consistency audit: recomputes every number from the raw pick files and checks
it against the rules and the built pages. Run: python scripts/audit.py
Prints one line per check; exit code 1 if anything fails.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dataio  # noqa: E402
import grade  # noqa: E402
import model  # noqa: E402
import predictions  # noqa: E402

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
fails, notes = [], []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        fails.append(name)


def ts(s):
    return datetime.fromisoformat(s.replace(" UTC", "+00:00")) if "UTC" in s else datetime.fromisoformat(s)


P = predictions.load_all()
T = predictions.load_all(team=True)
print(f"{len(P)} player picks over {len(set(p['date'] for p in P))} days, {len(T)} game picks")

# 1. frozen before tip
late = [p for p in P + T if ts(p["frozen_at"]) >= ts(p["tip_utc"])]
check("every pick frozen before tip", not late, f"{len(late)} late" if late else f"{len(P) + len(T)} checked")

# 1b. checkpoint snapshots and updates all happened before tip
late_s = [(p.get("player") or p.get("pick_label"), k) for p in P + T for k, sn in (p.get("snaps") or {}).items()
          if sn.get("at") and ts(sn["at"]) >= ts(p["tip_utc"])]
check("every snapshot (open / 10 AM / 1 PM / 5 PM) taken before tip", not late_s, f"{len(late_s)} late: {late_s[:2]}" if late_s
      else f"{sum(len(p.get('snaps') or {}) for p in P + T)} checked")

# 1c. pattern skips: a skipped pick carries no stake and never sits on an open Best 5 / Best Value list
bad_skip = [p for p in P + T if p.get("skip") and (p.get("units") or 0) > 0]
open_skip = [p for p in P + T if p.get("skip") and p.get("status") == "pending" and p.get("best5")
             and ts(p["frozen_at"]) < ts(p["tip_utc"]) and datetime.now(ts(p["tip_utc"]).tzinfo) < ts(p["tip_utc"])]
check("pattern-skipped picks have no stake and aren't on open Best 5 / Value lists", not bad_skip and not open_skip,
      f"{len(bad_skip)} staked, {len(open_skip)} listed" if bad_skip or open_skip else "")

# 1d. flips: a flipped pick really is on the other side
bad_flip = [p for p in P if p.get("flip") and p["flip"].get("orig_call") == p.get("call")]
bad_flip += [t for t in T if t.get("flip") and t["flip"].get("orig_pick") == t.get("pick")]
check("flipped picks are really on the other side", not bad_flip,
      f"{len(bad_flip)} wrong" if bad_flip else f"{sum(1 for p in P + T if p.get('flip'))} flipped")

# 1d2. one side per pick: a recorded chance is never below 50% (that would mean the other side was the lean)
low = [p for p in P if p.get("chance") is not None and p["chance"] < 0.5 - 1e-9]
check("every pick's chance is for the side shown (50% or more)", not low,
      f"{len(low)} under 50%: {[p.get('player') for p in low[:3]]}" if low else f"{len(P)} checked")

# 1e. EDGES were all spotted before their game tipped
E = []
for f in sorted((ROOT / "data" / "edges").glob("*.json")) if (ROOT / "data" / "edges").exists() else []:
    E += list(json.loads(f.read_text()).values())
late_e = [e for e in E if ts(e["first_seen"]) >= ts(e["tip_utc"]) or ts(e.get("last_seen") or e["first_seen"]) >= ts(e["tip_utc"])]
check("every EDGES bet recorded and updated before tip", not late_e, f"{len(late_e)} late" if late_e else f"{len(E)} checked")

# 1f. PARLAYS: legs set before the first tip, one per game, and results that follow from the legs
PZ = []
for f in sorted((ROOT / "data" / "parlays").glob("*.json")) if (ROOT / "data" / "parlays").exists() else []:
    PZ += list(json.loads(f.read_text()).values())
late_p = [r["id"] + " " + r["date"] for r in PZ if ts(r["updated_at"]) >= ts(r["first_tip"])
          or ts(r["first_tip"]) != min(ts(l["tip_utc"]) for l in r["legs"])]
dup_g = [r["id"] + " " + r["date"] for r in PZ if len({l["game_id"] for l in r["legs"]}) != len(r["legs"]) or len(r["legs"]) != r["size"]]
check("every parlay's legs set before its first tip, one leg per game, right size", not late_p and not dup_g,
      f"late {late_p[:2]}, shape {dup_g[:2]}" if late_p or dup_g else f"{len(PZ)} checked")
shared = []
for d in {r["date"] for r in PZ}:
    seen = Counter((l["player"] if l["kind"] == "player" else l["game_id"]) for r in PZ if r["date"] == d for l in r["legs"])
    shared += [f"{d} {k}" for k, v in seen.items() if v > 1]
check("no player or moneyline in two parlays on the same day", not shared, str(shared[:3]) if shared else f"{len(PZ)} checked")
bad_p = []
for r in PZ:
    res = [l.get("result") for l in r["legs"]]
    if r["status"] == "graded" and r["hit"] and ("miss" in res or None in res):
        bad_p.append(r["id"] + " " + r["date"])
    if r["status"] == "graded" and r["hit"] is False and "miss" not in res:
        bad_p.append(r["id"] + " " + r["date"])
    if r["status"] == "pending" and "miss" in res:
        bad_p.append(r["id"] + " " + r["date"])
check("parlay results match their legs (won = every played leg hit, lost = a leg missed)", not bad_p,
      str(bad_p[:3]) if bad_p else f"{sum(1 for r in PZ if r['status'] != 'pending')} graded")

# 1g. INJURY NEWS plays were all spotted before their game tipped
NE = []
for f in sorted((ROOT / "data" / "news").glob("*.json")) if (ROOT / "data" / "news").exists() else []:
    NE += list(json.loads(f.read_text()).values())
late_n = [e["id"] for e in NE if ts(e["seen_at"]) >= ts(e["tip_utc"])]
check("every injury-news play spotted before its game tipped", not late_n, str(late_n[:2]) if late_n else f"{len(NE)} events checked")

# 1g2. one news play per player-stat per game (absences combined, net side), and news parlays
#       carry no stake until news plays have earned one
import news as _news
dup_n = []
for f in sorted((ROOT / "data" / "news").glob("*.json")) if (ROOT / "data" / "news").exists() else []:
    day = json.loads(f.read_text())
    _news.mark_counted(day)
    c = Counter((e["game_id"], b["player"], b["stat"]) for e in day.values() for b in e["plays"] if b.get("counted"))
    dup_n += [f"{f.stem} {k[1]} {k[2]}" for k, v in c.items() if v > 1]
earned = _news.summary(None).get("earned")
paid_news = [r["id"] + " " + r["date"] for r in PZ if r["family"] == "news" and (r.get("stake") or 0) > 0 and not earned]
check("one injury-news play per player-stat per game; news parlays unstaked until earned", not dup_n and not paid_news,
      f"repeats {dup_n[:2]}, staked {paid_news[:2]}" if dup_n or paid_news else "")

# 1h. KALSHI LAGS (market.py): recorded before tip, one per player-stat-game, prices are real chances, and
#     graded results follow from the actual stat
LG = []
for f in sorted((ROOT / "data" / "market").glob("*.json")) if (ROOT / "data" / "market").exists() else []:
    LG += list(json.loads(f.read_text()).values())
bad_lg = [r["label"] for r in LG if ts(r["first_seen"]) >= ts(r["tip_utc"]) or ts(r["last_seen"]) >= ts(r["tip_utc"])
          or (r.get("cost") is not None and not 0 < r["cost"] < 1)
          or (r["status"] == "graded" and r.get("actual") is not None
              and r["hit"] != ((r["actual"] > r["kalshi_line"]) == (r["side"] == "OVER")))]
dup_lg = [k for k, v in Counter((r["game_id"], r["player"], r["stat"]) for r in LG).items() if v > 1]
check("Kalshi-lag bets recorded before tip, one per player-stat, real prices, graded right", not bad_lg and not dup_lg,
      f"bad {bad_lg[:2]}, dup {dup_lg[:2]}" if bad_lg or dup_lg else f"{len(LG)} checked")

# 2. statuses and grading arithmetic
bad = []
for p in P:
    if p["status"] == "graded":
        exp = (p["actual"] > p["line"]) if p["call"] == "OVER" else (p["actual"] < p["line"])
        if p["actual"] is None or p["hit"] != exp or p["actual"] == p["line"]:
            bad.append(p)
    elif p["status"] == "push" and p["actual"] != p["line"]:
        bad.append(p)
    elif p["status"] not in ("pending", "graded", "void", "push"):
        bad.append(p)
check("player picks graded correctly (hit = actual vs line, pushes exact)", not bad, f"{len(bad)} wrong" if bad else "")

# 3. nothing left pending after its box score landed
box = dataio.load_box()
games_in = set(box["game_id"].unique().to_list()) if box.height else set()
stuck = [p for p in P if p["status"] == "pending" and p["game_id"] in games_in]
check("no pick still pending after its game's box score landed", not stuck, f"{len(stuck)} stuck" if stuck else "")

# 4. game picks graded from finals
finals = {}
if box.height:
    import polars as pl
    for r in box.filter(pl.col("home") & pl.col("team_score").is_not_null()).select("game_id", "team_score", "opp_score").unique().iter_rows():
        finals[r[0]] = (r[1], r[2])
badt = []
for t in T:
    if t["status"] != "graded" or t["game_id"] not in finals:
        continue
    hs, as_ = finals[t["game_id"]]
    if t["market"] == "moneyline":
        exp = (hs > as_) == (t["pick"] == t["home"])
    elif t["market"] == "spread":
        exp = ((hs - as_ + t["line"]) > 0) == (t["pick"] == t["home"])
    else:
        exp = ((hs + as_) > t["line"]) == (t["pick"] == "OVER")
    if t["hit"] != exp:
        badt.append(t)
check("game picks graded correctly from final scores", not badt, f"{len(badt)} wrong" if badt else f"{sum(1 for t in T if t['status'] == 'graded')} graded")

# 5. Best 5 / Best Value: at most 5 per list per day, one per player (one per game for game lists)
over = []
for d in set(p["date"] for p in P):
    tags = defaultdict(list)
    for p in P:
        if p["date"] == d:
            for t in p.get("best5", []):
                tags[t].append(p["player"])
    for t, pl_ in tags.items():
        if len(pl_) > 5 or len(set(pl_)) != len(pl_):
            over.append((d, t, len(pl_)))
for d in set(t["date"] for t in T):
    tags = defaultdict(list)
    for t in T:
        if t["date"] == d:
            for tg in t.get("best5", []):
                tags[tg].append(t["game_id"])
    for tg, gs in tags.items():
        if len(gs) > 5 or len(set(gs)) != len(gs):
            over.append((d, tg, len(gs)))
check("Best 5 / Best Value lists: max 5, one per player / game", not over, str(over[:3]) if over else "")

# 6. game Best lists only contain picks that beat their price
neg = [t for t in T if any(x.startswith("game:") for x in t.get("best5", [])) and t.get("value") is not None and t["value"] < 0]
check("game Best 5 / Value only picks with chance >= break-even", not neg, f"{len(neg)}" if neg else "")
negm = [t for t in T if any(x in ("moneyline:value", "spread:value", "total:value") for x in t.get("best5", []))
        and t.get("value") is not None and t["value"] < 0]
check("moneyline / spread / total Best Value only picks that beat their price", not negm, f"{len(negm)}" if negm else "")
wrongm = [t for t in T if any(x.split(":")[0] in ("moneyline", "spread", "total") and x.split(":")[0] != t["market"] for x in t.get("best5", []))]
check("per-market game lists only hold their own market", not wrongm, f"{len(wrongm)}" if wrongm else "")

# 7. chance rules on picks frozen with the current method
cap_bad = [p for p in P if p.get("blend_w") is not None and p["chance"] > 0.80 + 1e-9]
check("player chances capped at 80% (current method)", not cap_bad, f"{len(cap_bad)} above" if cap_bad else "")


# 8. pages show the same numbers as the raw files
def page(name):
    f = DOCS / name
    return f.read_text() if f.exists() else ""


def rec(ps):
    g = [p for p in ps if p["status"] == "graded"]
    h = sum(1 for p in g if p["hit"])
    return h, len(g) - h, sum(1 for p in ps if p["status"] == "pending")


home = page("index.html")
dates = sorted(set(p["date"] for p in P))
boards = re.findall(r'class="board-h"><span>([^<]+)</span><a href="history-([\d-]+)\.html".*?rec-hit"><b>(\d+)</b>.*?rec-miss"><b>(\d+)</b>.*?rec-pend"><b>(\d+)</b>', home, re.S)
check("Home shows two day boards", len(boards) == 2 or (len(dates) < 2 and len(boards) == 1), f"{len(boards)} found")
for label, d, h, m, pe in boards:
    exp = rec([p for p in P if p["date"] == d])
    check(f"Home board {label.strip()} {d} = raw files", (int(h), int(m), int(pe)) == exp, f"page {h}-{m} ({pe} pending), raw {exp[0]}-{exp[1]} ({exp[2]} pending)")

hist = page("history.html")
allp = P + T
g_all = [p for p in allp if p["status"] == "graded"]
h_all = sum(1 for p in g_all if p["hit"])
rec_all = round(sum(grade.staked_pl(p) or 0 for p in g_all), 2)
flat_all = round(sum(grade.units(p) or 0 for p in g_all), 2)
top = hist.split('class="hv-top"', 1)[1].split("</section>", 1)[0] if 'class="hv-top"' in hist else ""
m_rec = re.search(r'hv-tile"><b>(\d+)&ndash;(\d+)</b>', top)
m_u = re.findall(r'hv-tile"><b><span class="[^"]*">([+-][\d.]+)U</span></b><span>(at our units|flat 1U each)', top)
uv = {k: float(v) for v, k in m_u}
check("History short version record = raw files (props + game picks)",
      bool(m_rec) and (int(m_rec.group(1)), int(m_rec.group(2))) == (h_all, len(g_all) - h_all),
      f"page {m_rec.groups() if m_rec else None}, raw {h_all}-{len(g_all) - h_all}")
check("History units (recommended and flat) = raw files",
      abs(uv.get("at our units", 1e9) - rec_all) < 0.006 and abs(uv.get("flat 1U each", 1e9) - flat_all) < 0.006,
      f"page {uv}, raw {rec_all:+.2f} / {flat_all:+.2f}")
# every pick that froze after units existed carries its stake, and stakes follow the steps
import live  # noqa: E402
steps = set(live.UNIT_NAMES)
bad_u = [p for p in allp if p.get("units") is not None and float(p["units"]) not in steps]
check("recommended units are one of 0 / 0.25 / 0.5 / 0.75 / 1 / 1.5 / 2 / 3", not bad_u, f"{len(bad_u)} odd" if bad_u else f"{sum(1 for p in allp if p.get('units') is not None)} stored")

for d in dates:
    dp = page(f"history-{d}.html")
    if not dp:
        check(f"day page {d} exists", False)
        continue
    m_ = re.search(r'class="day-total"><b[^>]*>(\d+)&ndash;(\d+)</b>', dp)
    exp = rec([p for p in P if p["date"] == d])
    check(f"day page {d} total = raw files", bool(m_) and (int(m_.group(1)), int(m_.group(2))) == exp[:2], f"page {m_.groups() if m_ else None}, raw {exp[0]}-{exp[1]}")
    n_hit, n_miss = dp.count("res res-hit"), dp.count("res res-miss")
    tp_ = [t for t in T if t["date"] == d]
    exp_h = exp[0] + sum(1 for t in tp_ if t["status"] == "graded" and t["hit"])
    exp_m = exp[1] + sum(1 for t in tp_ if t["status"] == "graded" and not t["hit"])
    check(f"day page {d} rows: HIT/MISS badges = raw files", (n_hit, n_miss) == (exp_h, exp_m), f"page {n_hit}/{n_miss}, raw {exp_h}/{exp_m}")
    check(f"day page {d}: every pick shows its units", dp.count('class="u-tag u-') >= len(tp_) + sum(1 for p in P if p["date"] == d),
          f"{dp.count('class=\"u-tag u-')} tags for {len(tp_) + sum(1 for p in P if p['date'] == d)} picks")

# 9. SANITY: projections and data make sense (added Oct 29 after the Isaiah Stewart 0-minute bug)
try:
    snap = json.loads((ROOT / "data" / "projections_today.json").read_text())
except Exception:  # noqa: BLE001
    snap = {}
if snap.get("players"):
    SP = snap["players"]
    open_g = {g for g, v in (snap.get("games") or {}).items() if not v.get("started")}
    sums = defaultdict(float)
    for r in SP:
        sums[(r["game_id"], r["team"])] += r["proj_min"] or 0
    bad_sum = [f"{t} {m:.0f}" for (g, t), m in sums.items() if str(g) in open_g and not 225 <= m <= 247]
    check("every team's projected minutes add up to a game (225-247)", not bad_sum, str(bad_sum[:4]) if bad_sum else f"{len(sums)} teams")
    zero = [f"{r['player']} ({r['team']}) {r['proj_min']:.1f} min" for r in SP if str(r["game_id"]) in open_g and r.get("has_line")
            and (r.get("games") or 0) >= 1 and (r.get("recent_min") or 0) >= 10 and (r["proj_min"] or 0) < 5]
    check("no player with a posted line who's been playing is projected near 0 minutes", not zero, str(zero[:4]) if zero else f"{len(SP)} players")
    dupes = [k for k, v in Counter((r["id"], r["game_id"]) for r in SP).items() if v > 1]
    names = defaultdict(set)
    for r in SP:
        names[r["player"]].add(r["team"])
    two = [n for n, ts in names.items() if len(ts) > 1]
    check("every player projected once, for one team", not dupes and not two, f"dupes {dupes[:3]}, two teams {two[:3]}" if dupes or two else f"{len(SP)} players")
    low_reg = [f"{r['player']} ({r['team']}) {r['proj_min']:.1f} of ~{r['recent_min']:.0f}" for r in SP if str(r["game_id"]) in open_g
               and (r.get("games") or 0) >= 3 and (r.get("recent_min") or 0) >= 15 and (r["proj_min"] or 0) < 8]
    if low_reg:
        print(f"NOTE regulars projected under 8 min (team-minutes cap; no line posted): {low_reg[:6]}")
# data freshness: every game final for 12+ hours is in the box scores
try:
    import polars as pl
    sch = dataio.load_schedule()
    bx = dataio.load_box()
    have = set(bx["game_id"].unique().to_list()) if bx.height else set()
    cutoff = datetime.now(sch["tip_utc"][0].tzinfo) - __import__("datetime").timedelta(hours=15)
    since = cutoff - __import__("datetime").timedelta(days=30)
    old_final = sch.filter(pl.col("completed") & (pl.col("tip_utc") < cutoff) & (pl.col("tip_utc") > since))
    miss = [r["game_id"] for r in old_final.iter_rows(named=True) if r["game_id"] not in have]
    miss = [g for g in miss if g]  # ignore unknown ids
    check("every game finished 15+ hours ago has box scores", not miss, f"{len(miss)} missing: {miss[:3]}" if miss else f"{old_final.height} games")
except Exception as e:  # noqa: BLE001
    print(f"NOTE freshness check skipped: {e}")

print()
print("ALL CHECKS PASSED" if not fails else f"{len(fails)} CHECK(S) FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
