"""
Frozen prediction store: one record per player / stat / game_id, written once
and never overwritten. Grading only fills in the result fields.

Why one file per slate date (data/predictions/YYYY-MM-DD.json): the NBA
posts ~800 lines a night. A single file rewritten by the 20-minute refresh
would grow the repo by megabytes a day; per-date files only change on the
days they cover.

Rules:
  * a pick is frozen at the first build where a market line exists for it,
    and never after tip-off (callers pass the tip time; checked here too);
  * projection, line, call and chance are fixed at freeze time;
  * a DNP or scratch is graded VOID, never a miss; a result equal to a
    whole-number line is a PUSH.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).parent.parent / "data" / "predictions"
TEAM_DIR = Path(__file__).parent.parent / "data" / "team_predictions"
BEST5_SIZE = 5

_cache: dict[str, dict] = {}
_dirty: set[str] = set()


def _now():
    return datetime.now(timezone.utc)


def key(player: str, stat: str, game_id: int) -> str:
    return f"{player}|{stat}|{game_id}"


def _path(date: str, team=False) -> Path:
    return (TEAM_DIR if team else DIR) / f"{date}.json"


def load_date(date: str, team=False) -> dict:
    ck = ("T" if team else "P") + date
    if ck not in _cache:
        p = _path(date, team)
        try:
            _cache[ck] = json.loads(p.read_text()) if p.exists() else {}
        except Exception:  # noqa: BLE001
            _cache[ck] = {}
    return _cache[ck]


def mark_dirty(date: str, team=False):
    _dirty.add(("T" if team else "P") + date)


def save():
    for ck in sorted(_dirty):
        team, date = ck[0] == "T", ck[1:]
        d = TEAM_DIR if team else DIR
        d.mkdir(parents=True, exist_ok=True)
        _path(date, team).write_text(json.dumps(_cache[ck], indent=1, sort_keys=True, default=str))
    _dirty.clear()


def all_dates(team=False) -> list[str]:
    d = TEAM_DIR if team else DIR
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def load_all(team=False) -> list[dict]:
    out = []
    for date in all_dates(team):
        out += list(load_date(date, team).values())
    return out


def freeze(rec: dict, tip_utc: datetime | None) -> bool:
    """rec must carry player, stat, game_id, date (slate date, ET). Returns
    True if written. Refuses after tip-off or without a line."""
    if tip_utc is None or _now() >= tip_utc or rec.get("line") is None:
        return False
    store = load_date(rec["date"])
    k = key(rec["player"], rec["stat"], rec["game_id"])
    if k in store:
        return False
    rec = dict(rec)
    rec.update({"frozen_at": _now().strftime("%Y-%m-%d %H:%M UTC"), "tip_utc": tip_utc.isoformat(),
                "status": "pending", "actual": None, "hit": None, "best5": []})
    store[k] = rec
    mark_dirty(rec["date"])
    return True


def freeze_team(rec: dict, tip_utc: datetime | None) -> bool:
    if tip_utc is None or _now() >= tip_utc:
        return False
    store = load_date(rec["date"], team=True)
    k = f"{rec['game_id']}|{rec['market']}"
    if k in store:
        return False
    rec = dict(rec)
    rec.update({"frozen_at": _now().strftime("%Y-%m-%d %H:%M UTC"), "tip_utc": tip_utc.isoformat(),
                "status": "pending", "hit": None})
    store[k] = rec
    mark_dirty(rec["date"], team=True)
    return True


def get(date: str, player: str, stat: str, game_id: int) -> dict | None:
    return load_date(date).get(key(player, stat, game_id))


def sync_best5(date: str, group: str, list_name: str, ranked: list[dict]) -> dict:
    """Keep one slate's Best 5 list at 5 picks. `group` is a stat ("pts") or
    "combos" for the PRA page's mixed list; the tag stored is
    "<group>:<list_name>". A tagged pick whose game has tipped is locked in;
    open slots go to the best-ranked frozen picks whose games haven't
    started, one per player. Tags only -- never touches the pick itself."""
    store = load_date(date)
    now = _now()
    tag = f"{group}:{list_name}"

    def started(p):
        try:
            return now >= datetime.fromisoformat(p["tip_utc"])
        except Exception:  # noqa: BLE001
            return True

    tagged = [k for k, p in store.items() if tag in p.get("best5", [])]
    locked = [k for k in tagged if started(store[k]) or store[k]["status"] != "pending"]
    open_slots = max(BEST5_SIZE - len(locked), 0)
    keep, players = [], {store[k]["player"] for k in locked}
    for r in ranked:
        if len(keep) >= open_slots:
            break
        k = key(r["player"], r["stat"], r["game_id"])
        if k in locked or k not in store or started(store[k]) or r["player"] in players:
            continue
        keep.append(k)
        players.add(r["player"])
    final = set(locked) | set(keep)
    changed = False
    for k in tagged:
        if k not in final:
            store[k]["best5"].remove(tag)
            changed = True
    for k in final:
        if tag not in store[k]["best5"]:
            store[k]["best5"].append(tag)
            changed = True
    if changed:
        mark_dirty(date)
    return {"size": len(final), "locked": len(locked), "keys": sorted(final)}
