"""
Injury news alerts: catch late scratches before the books catch up.

Every build compares tonight's OUT list (ESPN injury report + NBA.com official
inactives, update 31) with everyone seen out earlier the same day. A player
newly ruled out is NEWS. The model already hands his minutes and shots to
teammates, but spreads them thinly (mostly to the bench), so the size of the
bump comes from real history instead: how each teammate actually played in
games WITHOUT that player vs with him (last season + this one, 4+ games each,
shrunk toward 0). Teammates whose with/without gap passes a stat-sized
threshold and whose line is still the same are flagged: the book (or Kalshi /
PrizePicks) hasn't reacted yet. The play's side follows the history (OVER if
he does more without the player); "model agrees" when our pick is that side.

Each later build (until the game tips) checks whether the line has moved since
the news ("BOOK CAUGHT UP"). Every flagged play is graded from the box score
against the line and side it had AT the news, so the record shows whether
reacting to late news really pays.

Guards: the first build of a slate only records who's already out (no alerts);
someone seen out once today is never "news" again (a failed fetch that drops
and re-adds a name can't fake an alert); games that have tipped are ignored.

data/news_state.json  {date, outs: {player: {...}}, proj: {"player|stat": proj}}
data/news/DATE.json   {event_id: {...}}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

import dataio
import predictions

ROOT = Path(__file__).parent.parent / "data"
STATE = ROOT / "news_state.json"
DIR = ROOT / "news"
STATS = ("pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra")
BUMP_MIN = {"pts": 1.5, "reb": 0.8, "ast": 0.8, "fg3m": 0.4, "pra": 2.0, "pr": 1.8, "pa": 1.8, "ra": 1.2}
MAX_PER_EVENT = 8


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


def load_date(date: str) -> dict:
    return _load(DIR / f"{date}.json", {})


def _save_date(date: str, d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{date}.json").write_text(json.dumps(d, indent=1, default=str))


def all_dates() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.json")) if DIR.exists() else []


WO_MIN_GAMES = 4      # games without the player before his absence counts
WO_SHRINK = 6         # shrink the with/without gap toward 0 by n/(n + 6)
COMBO = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}


def with_without(box: pl.DataFrame, team: str, out_player: str) -> dict:
    """How each teammate actually played WITHOUT `out_player` vs with him, last season + this one,
    in games for `team` while he was on it. {teammate: {"n_wo", "n_w", stat: shrunk bump}}."""
    if box is None or not box.height:
        return {}
    cur = dataio.CURRENT_SEASON
    b = box.filter(pl.col("played") & (pl.col("team") == team) & (pl.col("season") >= cur - 1))
    on = b.filter(pl.col("player") == out_player)
    if not on.height:
        return {}
    seasons = on["season"].unique().to_list()
    b = b.filter(pl.col("season").is_in(seasons))
    with_g = set(on["game_id"].to_list())
    all_g = set(b["game_id"].to_list())
    wo_g = all_g - with_g
    if len(wo_g) < WO_MIN_GAMES:
        return {}
    out = {}
    for name, g in b.filter(pl.col("player") != out_player).group_by("player"):
        name = name[0] if isinstance(name, tuple) else name
        w = g.filter(pl.col("game_id").is_in(list(with_g)))
        wo = g.filter(pl.col("game_id").is_in(list(wo_g)))
        if wo.height < WO_MIN_GAMES or w.height < WO_MIN_GAMES:
            continue
        k = wo.height / (wo.height + WO_SHRINK)
        r = {"n_wo": wo.height, "n_w": w.height, "min": round(k * (wo["min"].mean() - w["min"].mean()), 1)}
        for s in ("pts", "reb", "ast", "fg3m"):
            r[s] = round(k * (wo[s].mean() - w[s].mean()), 2)
        for c, parts in COMBO.items():
            r[c] = round(sum(r[x] for x in parts), 2)
        out[name] = r
    return out


def _picks_now(res: dict) -> tuple[dict, dict]:
    """{"player|stat": proj}, {"player|stat": row/stat info} from this build's projections."""
    proj, info = {}, {}
    for r in res.get("players") or []:
        for s in STATS:
            st = (r.get("stats") or {}).get(s)
            if not st:
                continue
            k = f"{r['player']}|{s}"
            proj[k] = st["proj"]
            info[k] = {"row": r, "st": st}
    return proj, info


def process(sl: dict, inj: dict, res: dict, now: datetime | None = None, box: pl.DataFrame | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    date = sl.get("date")
    if not date:
        return {"events": 0, "new": 0}
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    games = {g["game_id"]: g for g in sl.get("games") or []}
    team_game = {}
    for g in games.values():
        team_game[g["away"]] = g
        team_game[g["home"]] = g
    outs_now = {p["player"]: p for p in inj.get("players") or []
                if p.get("class") == "out" and p.get("player") and p.get("team") in team_game}
    proj_now, info = _picks_now(res)
    state = _load(STATE, {})
    events = load_date(date)
    new = []
    if state.get("date") == date:
        for name, p in outs_now.items():
            if name in state.get("outs", {}):
                continue
            g = team_game[p["team"]]
            if now >= _dt(g["tip_utc"]):
                continue
            new.append((name, p, g))
    else:
        state = {"date": date, "outs": {}, "proj": {}}
    prev = state.get("proj") or {}
    for name, p, g in new:
        bens = []
        ww = with_without(box, p["team"], name)
        for k, now_v in proj_now.items():
            pl_, s = k.split("|")
            row = info[k]["row"]
            if row["team"] != p["team"] or pl_ == name or pl_ not in ww:
                continue
            bump = ww[pl_][s]
            if abs(bump) < BUMP_MIN[s]:
                continue
            st = info[k]["st"]
            pk = st.get("pick")
            if not pk or pk.get("line") is None:
                continue
            call = "OVER" if bump > 0 else "UNDER"
            ch = pk.get("chance")
            ch = ch if pk["side"] == call else (1 - ch if ch is not None else None)
            bens.append({"player": pl_, "player_id": row.get("player_id"), "team": row["team"], "stat": s,
                         "bump": bump, "n_wo": ww[pl_]["n_wo"], "n_w": ww[pl_]["n_w"], "min_bump": ww[pl_]["min"],
                         "proj_before": prev.get(k), "proj_after": now_v,
                         "line": pk["line"], "call": call, "src": pk.get("source"), "anchor": pk.get("anchor"),
                         "chance": ch, "be": pk.get("break_even") if pk["side"] == call else None,
                         "units": pk.get("units") if pk["side"] == call else 0.0,
                         "aligned": True, "model_agrees": pk["side"] == call,
                         "key": predictions.key(pl_, s, row["game_id"]),
                         "line_now": pk["line"], "moved": False, "result": None})
        bens.sort(key=lambda b: -abs(b["bump"]) / BUMP_MIN[b["stat"]])
        # one play per teammate (his biggest bump), so a single player can't fill the list
        seen, top = set(), []
        for b in bens:
            if b["player"] not in seen:
                seen.add(b["player"])
                top.append(b)
        bens = top
        eid = f"{name}|{stamp}"
        events[eid] = {"id": eid, "date": date, "player": name, "team": p["team"], "status": p.get("status"),
                       "comment": (p.get("comment") or "")[:200], "seen_at": stamp, "game_id": g["game_id"],
                       "tip_utc": str(g["tip_utc"]), "opp": g["home"] if g["away"] == p["team"] else g["away"],
                       "plays": bens[:MAX_PER_EVENT]}
    # follow each open event's lines until tip: has the book caught up?
    for ev in events.values():
        if now >= _dt(ev["tip_utc"]):
            continue
        for b in ev["plays"]:
            st = (info.get(f"{b['player']}|{b['stat']}") or {}).get("st") or {}
            pk = st.get("pick")
            if pk and pk.get("line") is not None:
                b["line_now"] = pk["line"]
                b["moved"] = pk["line"] != b["line"]
                b["proj_now"] = st.get("proj")
    for name, p in outs_now.items():
        state["outs"].setdefault(name, {"team": p["team"], "status": p.get("status"), "at": stamp})
    state["proj"] = proj_now
    STATE.write_text(json.dumps(state, indent=1, default=str))
    if events:
        _save_date(date, events)
    return {"events": len(events), "new": len(new),
            "plays": sum(1 for e in events.values() for b in e["plays"] if b["aligned"])}


def _dt(x):
    return x if isinstance(x, datetime) else datetime.fromisoformat(str(x))


def grade() -> int:
    n = 0
    for date in all_dates():
        events = load_date(date)
        ps = predictions.load_date(date)
        changed = False
        for ev in events.values():
            for b in ev["plays"]:
                if b.get("result") is not None:
                    continue
                p = ps.get(b["key"])
                if not p:
                    continue
                if p.get("status") == "void":
                    b["result"] = "void"
                elif p.get("actual") is not None:
                    a = p["actual"]
                    b["actual"] = a
                    b["result"] = "void" if a == b["line"] else ("hit" if (a > b["line"]) == (b["call"] == "OVER") else "miss")
                else:
                    continue
                changed = True
                n += 1
        if changed:
            _save_date(date, events)
    return n


def summary(today: str | None) -> dict:
    allp, evs = [], []
    for d in all_dates():
        for ev in load_date(d).values():
            evs.append(ev)
            allp += [b | {"date": d, "out": ev["player"]} for b in ev["plays"] if b["aligned"]]
    done = [b for b in allp if b.get("result") in ("hit", "miss")]
    hits = sum(1 for b in done if b["result"] == "hit")
    units = sum(((1 / (b.get("be") or 0.5238)) - 1) if b["result"] == "hit" else -1 for b in done)
    still = [b for b in done if not b.get("moved")]
    return {"n": len(done), "hits": hits, "rate": round(100 * hits / len(done), 1) if done else None,
            "units": round(units, 2), "pending": sum(1 for b in allp if b.get("result") is None),
            "unmoved": {"n": len(still), "hits": sum(1 for b in still if b["result"] == "hit")},
            "today": sorted([e for e in evs if e["date"] == today], key=lambda e: e["seen_at"], reverse=True),
            "recent": sorted(done, key=lambda b: (b["date"], b["player"]), reverse=True)[:40], "events_total": len(evs)}
