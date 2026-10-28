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

# 1e. EDGES were all spotted before their game tipped
E = []
for f in sorted((ROOT / "data" / "edges").glob("*.json")) if (ROOT / "data" / "edges").exists() else []:
    E += list(json.loads(f.read_text()).values())
late_e = [e for e in E if ts(e["first_seen"]) >= ts(e["tip_utc"]) or ts(e.get("last_seen") or e["first_seen"]) >= ts(e["tip_utc"])]
check("every EDGES bet recorded and updated before tip", not late_e, f"{len(late_e)} late" if late_e else f"{len(E)} checked")

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
check("recommended units are one of 0 / 0.25 / 0.5 / 0.75 / 1 / 1.5 / 2", not bad_u, f"{len(bad_u)} odd" if bad_u else f"{sum(1 for p in allp if p.get('units') is not None)} stored")

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

print()
print("ALL CHECKS PASSED" if not fails else f"{len(fails)} CHECK(S) FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
