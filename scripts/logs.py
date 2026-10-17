"""
Line-movement log and pipeline-health log.

Line movement: for every (player, stat, game_id, source) the first line seen
and the latest one, with times. Entries for games more than 3 days old are
dropped so the file stays small (frozen picks keep their own line).

Pipeline health: one row per build -- players projected, lines matched per
source, new picks frozen, graded, and every source's error (if any). The
last 300 builds are kept.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"
LM_PATH = DATA / "line_movement.json"
HEALTH_PATH = DATA / "pipeline_health.json"


def _now():
    return datetime.now(timezone.utc)


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def log_lines(line_book: dict, slate_date: str | None) -> dict:
    lm = _load(LM_PATH, {})
    stamp = _now().strftime("%Y-%m-%d %H:%M UTC")
    changed = 0
    for (player, stat, gid), sources in line_book.items():
        for src, rec in sources.items():
            k = f"{player}|{stat}|{gid}|{src}"
            e = lm.get(k)
            if e is None:
                lm[k] = {"date": slate_date, "first": rec["line"], "first_at": stamp, "last": rec["line"],
                         "last_at": stamp, "moves": 0}
            elif e["last"] != rec["line"]:
                e.update(last=rec["line"], last_at=stamp, moves=e["moves"] + 1)
                changed += 1
    cutoff = (_now() - timedelta(days=3)).date().isoformat()
    lm = {k: v for k, v in lm.items() if (v.get("date") or "9999") >= cutoff}
    LM_PATH.write_text(json.dumps(lm, indent=0, sort_keys=True))
    return lm


def movement(lm: dict, player: str, stat: str, gid: int, src: str) -> dict | None:
    e = lm.get(f"{player}|{stat}|{gid}|{src}")
    if not e or e["first"] == e["last"]:
        return None
    return {"from": e["first"], "to": e["last"], "since": e["first_at"]}


def log_health(entry: dict) -> list:
    h = _load(HEALTH_PATH, [])
    entry = {"at": _now().strftime("%Y-%m-%d %H:%M UTC"), **entry}
    h = (h + [entry])[-300:]
    HEALTH_PATH.write_text(json.dumps(h, indent=1))
    return h
