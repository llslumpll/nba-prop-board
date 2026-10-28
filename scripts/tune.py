"""
Weekly self-tuning.

Once a week (first build on/after Monday 4 AM CT, or when data/tuning.json is
missing or 7+ days old) re-test the model's main settings against games that
have already been played, and keep a change only when it helps on games it
was NOT chosen on.

Three windows, all graded from the ESPN box scores with projections built
only from games before each one:
  A  "earlier"  this season before the last 7 days + last season's first 3 weeks
  B  "recent"   this season's last 7 days of games
  C  "steady"   a mid-season stretch of last season (a guard against overfitting)

Score = average of (miss with the setting / miss now) over minutes, points,
rebounds, assists, threes and PRA, for players projected 20+ minutes.
A setting moves one step at a time (bounded), and only if it lowers the miss
in BOTH A and B by 0.1%+ and doesn't raise C by more than 0.2%. Settings are
tried one at a time, each building on the last accepted change.

Results go to data/tuning.json: the settings in force (live.load_fit applies
them) and a history of every run with what was tried, what changed and why.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import polars as pl

import backtest
import dataio
import model

OUT = Path(__file__).parent.parent / "data" / "tuning.json"
MIN_GAIN = 0.001       # A and B must both improve by 0.1%+
MAX_C_LOSS = 0.002     # C may not get worse by more than 0.2%
EVERY_DAYS = 7
STATS = ("min", "pts", "reb", "ast", "fg3m", "pra")

# name: (label, kind, step, lo, hi). kind "add" = +/- step, "mul" = x/÷ step.
# k_scale scales every stat's "games before this season counts fully" (Params.k).
KNOBS = {
    "last_game_w":   ("weight on a player's most recent game's minutes", "add", 0.05, 0.0, 0.5),
    "min_prior_k":   ("games before last season's minutes stop mattering", "mul", 1.5, 0.5, 8.0),
    "min_half_life": ("how fast older minutes fade (games)", "add", 2.0, 4.0, 16.0),
    "k_scale":       ("how fast this season's stats take over from last season's", "mul", 1.25, 0.4, 2.5),
    "a_def":         ("strength of the opponent-defense adjustment", "add", 0.05, 0.0, 0.6),
    "def_k":         ("games of prior behind each defense rating", "mul", 1.5, 10.0, 120.0),
    "a_pace":        ("strength of the pace adjustment", "add", 0.1, 0.0, 1.0),
    "usage_b":       ("how much teammates' absences boost usage", "add", 0.1, 0.0, 0.6),
}
BT = Path(__file__).parent.parent / "data" / "backtest.json"


def base_sig() -> str:
    """Fingerprint of backtest.json's settings. The monthly retune (retune.py) starts from the
    tuned settings and saves a full new set; when it does, this changes and the weekly
    overrides are retired (they're already folded in), so they never apply twice."""
    try:
        prm = json.loads(BT.read_text()).get("params") or {}
    except Exception:  # noqa: BLE001
        prm = {}
    return json.dumps(prm, sort_keys=True, default=str)


def load() -> dict:
    try:
        return json.loads(OUT.read_text())
    except Exception:  # noqa: BLE001
        return {}


def overrides() -> dict:
    """Settings the tuner has adopted (applied on top of backtest.json + manual defaults)."""
    st = load()
    if st.get("base_sig") is not None and st["base_sig"] != base_sig():
        return {}
    return dict(st.get("params") or {})


def apply(p: model.Params, ov: dict) -> model.Params:
    ch = {k: v for k, v in ov.items() if k in model.Params.__dataclass_fields__ and k != "k"}
    if "k_scale" in ov:
        ch["k"] = {s: round(v * ov["k_scale"], 3) for s, v in p.k.items()}
    return dataclasses.replace(p, **ch) if ch else p


def current(p: model.Params, ov: dict) -> dict:
    out = {}
    for k in KNOBS:
        if k == "k_scale":
            out[k] = ov.get("k_scale", 1.0)
        else:
            out[k] = ov.get(k, getattr(p, k))
    return out


def due(now: dt.datetime, state: dict | None = None) -> bool:
    st = state if state is not None else load()
    last = st.get("at")
    if not last:
        return True
    try:
        age = now - dt.datetime.fromisoformat(last)
    except Exception:  # noqa: BLE001
        return True
    return age >= dt.timedelta(days=EVERY_DAYS) or (now.weekday() == 0 and now.hour >= 9 and age >= dt.timedelta(days=5))


def _windows(box: pl.DataFrame) -> dict:
    cur = dataio.CURRENT_SEASON
    reg = box.filter(pl.col("played") & (pl.col("season_type") == model.REGULAR))
    cur_dates = reg.filter(pl.col("season") == cur)["date"]
    prev = reg.filter(pl.col("season") == cur - 1)["date"]
    out = {}
    if cur_dates.len():
        last = cur_dates.max()
        b0 = last - dt.timedelta(days=6)
        out["B"] = [(cur, b0, last)]
        a = []
        if cur_dates.min() < b0:
            a.append((cur, cur_dates.min(), b0 - dt.timedelta(days=1)))
    else:
        a = []
    if prev.len():
        p0 = prev.min()
        a.append((cur - 1, p0, p0 + dt.timedelta(days=20)))
        mid = p0 + dt.timedelta(days=75)
        out["C"] = [(cur - 1, mid, mid + dt.timedelta(days=20))]
        if "B" not in out:  # preseason: no games yet this season, test on last season's 4th week
            out["B"] = [(cur - 1, p0 + dt.timedelta(days=21), p0 + dt.timedelta(days=27))]
    out["A"] = a
    return out


def _targets(box, windows):
    cache = {}
    out = {}
    for name, spans in windows.items():
        parts = []
        for season, d0, d1 in spans:
            if season not in cache:
                cache[season] = backtest.targets_for(box, season)
            parts.append(cache[season].filter(pl.col("date").is_between(d0, d1)))
        out[name] = pl.concat(parts) if parts else None
    return out


def _miss(box, targets, p) -> dict:
    df = backtest.run_projection(box, targets, p).filter(pl.col("proj_min") >= backtest.PROP_MIN)
    if not df.height:
        return {}
    out = {"n": df.height}
    for s in STATS:
        col = "proj_min" if s == "min" else f"proj_{s}"
        out[s] = df.select((pl.col(col) - pl.col(s)).abs().mean()).item()
    return out


def _score(m: dict, base: dict) -> float:
    rs = [m[s] / base[s] for s in STATS if base.get(s)]
    return sum(rs) / len(rs) if rs else 1.0


def _neighbors(name, val):
    _, kind, step, lo, hi = KNOBS[name]
    if kind == "add":
        cands = [round(val - step, 4), round(val + step, 4)]
    else:
        cands = [round(val / step, 4), round(val * step, 4)]
    return [c for c in cands if lo - 1e-9 <= c <= hi + 1e-9 and abs(c - val) > 1e-9]


def run(box: pl.DataFrame, p_base: model.Params, now: dt.datetime) -> dict:
    """p_base = Params from backtest.json + manual defaults, WITHOUT tuning overrides."""
    st = load()
    ov = overrides()
    windows = _windows(box)
    if not windows.get("A") or not windows.get("B"):
        return {"ran": False, "why": "not enough games"}
    tg = _targets(box, windows)
    p_now = apply(p_base, ov)
    base = {w: _miss(box, tg[w], p_now) for w in ("A", "B", "C") if tg.get(w) is not None}
    if not base.get("A") or not base.get("B"):
        return {"ran": False, "why": "no graded rows"}
    vals = current(p_base, ov)
    tried, changes = [], []
    ref = dict(base)
    for name in KNOBS:
        best = None
        for v in _neighbors(name, vals[name]):
            trial = {**ov, name: v}
            p_try = apply(p_base, trial)
            m = {w: _miss(box, tg[w], p_try) for w in ref}
            sc = {w: round(_score(m[w], ref[w]), 5) for w in ref}
            ok = sc["A"] <= 1 - MIN_GAIN and sc["B"] <= 1 - MIN_GAIN and sc.get("C", 1.0) <= 1 + MAX_C_LOSS
            tried.append({"knob": name, "from": vals[name], "to": v, "score": sc, "ok": ok})
            if ok and (best is None or sc["A"] + sc["B"] < best[1]["A"] + best[1]["B"]):
                best = (v, sc, m)
        if best:
            v, sc, m = best
            changes.append({"knob": name, "label": KNOBS[name][0], "from": vals[name], "to": v,
                            "gain_a": round(100 * (1 - sc["A"]), 2), "gain_b": round(100 * (1 - sc["B"]), 2),
                            "c": round(100 * (sc.get("C", 1.0) - 1), 2)})
            ov[name] = v
            vals[name] = v
            ref = m
    p_new = apply(p_base, ov)
    after = {w: _miss(box, tg[w], p_new) for w in ref}

    def pct(w):
        return round(100 * (1 - _score(after[w], base[w])), 2) if base.get(w) and after.get(w) else None

    def rnd(m):
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in (m or {}).items()}

    rec = {"at": now.isoformat(timespec="minutes"), "windows": {w: [[s, str(a), str(b)] for s, a, b in sp] for w, sp in windows.items()},
           "rows": {w: (base[w] or {}).get("n") for w in base}, "changes": changes,
           "tried": len(tried), "gain": {w: pct(w) for w in base},
           "miss_before": {w: rnd(base[w]) for w in base}, "miss_after": {w: rnd(after[w]) for w in after},
           "settings": current(p_base, ov)}
    hist = (st.get("history") or [])[-25:] + [rec]
    out = {"at": rec["at"], "base_sig": base_sig(), "params": ov, "settings": rec["settings"], "labels": {k: v[0] for k, v in KNOBS.items()},
           "history": hist, "last_tried": tried}
    OUT.write_text(json.dumps(out, indent=1, default=str))
    return {"ran": True, **rec}
