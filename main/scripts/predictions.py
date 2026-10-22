"""
Frozen prediction store: one record per player / stat / game_id, written once
and never overwritten. Grading only fills in the result fields.

Why one file per slate date (data/predictions/YYYY-MM-DD.json): the NBA
posts ~800 lines a night. A single file rewritten by the 20-minute refresh
would grow the repo by megabytes a day; per-date files only change on the
days they cover.

Rules:
  * a pick is frozen at the first build within FREEZE_WINDOW of tip where a
    market line exists for it, and never after tip-off (callers pass the tip
    time; checked here too). Waiting until game day means the projection
    has that day's injury report behind it;
  * a pick frozen under an older chance method (no blend_w) is re-frozen
    once with the current one, still only before tip;
  * if a player on either team is newly ruled OUT after a pick froze but
    before tip, the pick is re-frozen with the updated projection
    (refreeze()); the original line and time are kept in first_line /
    first_frozen_at. Still always before tip;
  * the last line seen before tip (close_line) is recorded for closing line
    value: did the market move toward our side after we froze?
  * projection, line, call and chance are fixed at freeze time;
  * a DNP or scratch is graded VOID, never a miss; a result equal to a
    whole-number line is a PUSH.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).parent.parent / "data" / "predictions"
TEAM_DIR = Path(__file__).parent.parent / "data" / "team_predictions"
BEST5_SIZE = 5
FREEZE_WINDOW = timedelta(hours=8)
# fields replaced when a pick is re-frozen after new injury news
REFREEZE_FIELDS = ("tier", "proj_min", "var", "line", "chance", "break_even", "market_side", "projected", "blend_mean", "blend_w",
                   "call", "line_source", "lines", "label", "why", "out_ctx")

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
    if _now() < tip_utc - FREEZE_WINDOW:
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
    if tip_utc is None or _now() >= tip_utc or _now() < tip_utc - FREEZE_WINDOW:
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


def refreeze(rec: dict, tip_utc: datetime | None) -> bool:
    """Re-freeze an existing pending pick when the OUT list for its game has
    changed since it froze (late scratch). Only before tip. Old records
    without out_ctx just get today's list stored as their baseline."""
    if tip_utc is None or _now() >= tip_utc or rec.get("line") is None:
        return False
    store = load_date(rec["date"])
    k = key(rec["player"], rec["stat"], rec["game_id"])
    cur = store.get(k)
    if not cur or cur.get("status") != "pending":
        return False
    method_update = cur.get("blend_w") is None and rec.get("blend_w") is not None
    if cur.get("out_ctx") is None and not method_update:
        cur["out_ctx"] = rec.get("out_ctx")
        mark_dirty(rec["date"])
        return False
    if cur.get("out_ctx") == rec.get("out_ctx") and not method_update:
        return False
    cur.setdefault("first_line", cur["line"])
    cur.setdefault("first_call", cur["call"])
    cur.setdefault("first_frozen_at", cur["frozen_at"])
    for f in REFREEZE_FIELDS:
        if f in rec:
            cur[f] = rec[f]
    cur["frozen_at"] = _now().strftime("%Y-%m-%d %H:%M UTC")
    cur["refreezes"] = cur.get("refreezes", 0) + 1
    note = " Re-frozen with the updated chance method (blended with the line)." if method_update else " Re-frozen after new injury news."
    cur["why"] = (cur.get("why") or "")[:360] + note
    for f in ("close_line", "close_mkt", "close_at"):
        cur.pop(f, None)
    mark_dirty(rec["date"])
    return True


def record_close(date: str, player: str, stat: str, game_id: int, line, mkt_side, tip_utc: datetime | None) -> bool:
    """Keep the latest pre-tip line for the pick's own source. After tip this
    stops updating, so what's left is the closing line."""
    if tip_utc is None or _now() >= tip_utc:
        return False
    cur = load_date(date).get(key(player, stat, game_id))
    if not cur or cur.get("status") != "pending" or line is None:
        return False
    if cur.get("close_line") == line and cur.get("close_mkt") == mkt_side:
        return False
    cur["close_line"] = line
    cur["close_mkt"] = mkt_side
    cur["close_at"] = _now().strftime("%Y-%m-%d %H:%M UTC")
    mark_dirty(date)
    return True


def get(date: str, player: str, stat: str, game_id: int) -> dict | None:
    return load_date(date).get(key(player, stat, game_id))


def _trim_overflow(store: dict, final: set, tag: str) -> set:
    """A list can only exceed BEST5_SIZE if two runs' results were ever
    combined; keep the earliest-tagged picks and drop the most recently
    tagged (never chosen by results)."""
    if len(final) <= BEST5_SIZE:
        return final
    order = sorted(final, key=lambda k: ((store[k].get("best5_at") or {}).get(tag) or "", k))
    for k in order[BEST5_SIZE:]:
        store[k].setdefault("best5_dropped", {})[tag] = "list had more than 5 picks; the most recently tagged was removed"
    return set(order[:BEST5_SIZE])


def _apply_tags(store: dict, tagged: list, final: set, tag: str, now) -> bool:
    changed = False
    for k in tagged:
        if k not in final:
            store[k]["best5"].remove(tag)
            (store[k].get("best5_at") or {}).pop(tag, None)
            changed = True
    for k in final:
        if tag not in store[k]["best5"]:
            store[k]["best5"].append(tag)
            store[k].setdefault("best5_at", {})[tag] = now.strftime("%Y-%m-%d %H:%M:%S UTC")
            changed = True
    return changed


# One-time corrections, applied idempotently by apply_fixes() and noted on the pick itself.
FIXES = [
    {"date": "2026-10-21", "key": "Amen Thompson|fg3m|401909840", "remove_tag": "fg3m:best5",
     "note": "Removed from 3PM Best 5 on Oct 21: two automatic runs saved at the same moment and their lists merged into 6 "
             "picks. The 6:21 PM CT list (5 picks) was complete without this one; its tag came in through the merge."},
]


def apply_fixes() -> int:
    n = 0
    for f in FIXES:
        rec = load_date(f["date"]).get(f["key"])
        if rec and f["remove_tag"] in rec.get("best5", []):
            rec["best5"].remove(f["remove_tag"])
            rec.setdefault("corrections", []).append(f["note"])
            mark_dirty(f["date"])
            n += 1
    return n


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
    final = _trim_overflow(store, final, tag)
    changed = _apply_tags(store, tagged, final, tag, now)
    if changed:
        mark_dirty(date)
    return {"size": len(final), "locked": len(locked), "keys": sorted(final)}


def sync_team_best(date: str, list_name: str, ranked: list[dict]) -> None:
    """Game Best 5 / Best Value: same rules as the player lists (tipped
    picks lock, open slots go to the best-ranked frozen untipped picks),
    one pick per game. Tag stored is "game:<list_name>"."""
    store = load_date(date, team=True)
    now = _now()
    tag = f"game:{list_name}"

    def started(r):
        try:
            return now >= datetime.fromisoformat(r["tip_utc"])
        except Exception:  # noqa: BLE001
            return True

    for r in store.values():
        r.setdefault("best5", [])
    tagged = [k for k, r in store.items() if tag in r["best5"]]
    locked = [k for k in tagged if started(store[k]) or store[k]["status"] != "pending"]
    games = {store[k]["game_id"] for k in locked}
    keep = []
    for r in ranked:
        if len(locked) + len(keep) >= BEST5_SIZE:
            break
        k = f"{r['game_id']}|{r['market']}"
        if k in locked or k not in store or started(store[k]) or r["game_id"] in games:
            continue
        keep.append(k)
        games.add(r["game_id"])
    final = set(locked) | set(keep)
    final = _trim_overflow(store, final, tag)
    if _apply_tags(store, tagged, final, tag, now):
        mark_dirty(date, team=True)


def record_close_team(date: str, game_id: int, market: str, close_line, close_mkt, tip_utc: datetime | None) -> bool:
    """Latest pre-tip FanDuel line (spread: home point; total: points) or
    no-vig chance for the picked side (moneyline). Stops at tip."""
    if tip_utc is None or _now() >= tip_utc:
        return False
    cur = load_date(date, team=True).get(f"{game_id}|{market}")
    if not cur or cur.get("status") != "pending":
        return False
    if cur.get("close_line") == close_line and cur.get("close_mkt") == close_mkt:
        return False
    cur.update(close_line=close_line, close_mkt=close_mkt, close_at=_now().strftime("%Y-%m-%d %H:%M UTC"))
    mark_dirty(date, team=True)
    return True
