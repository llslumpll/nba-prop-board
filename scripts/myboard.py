"""Your board tonight (update 84, Nov 19 2026): one home-page list of what fits how the user bets.

Pulls from what the build already made, nothing new is fetched:
  1. favorites 75%+ tonight (favorites.summary)            -> the user's moneyline rule
  2. proven tipsters' picks tonight (Dr. Profit, WizBetz)   -> full season, honest recaps
  3. newer tracked tipsters' single props with a price you can get (Kalshi / Polymarket / FanDuel same number)
  4. safe lines at a GOOD PRICE (safelines.summary)
  5. tonight's cushion combo, if one was built (tipsters.summary)
A pick on a player the injury report lists as Out is flagged PLAYER OUT (it will void).
Tracking only: no stakes are added here.
"""
from __future__ import annotations

import re
import unicodedata

PROVEN = ("doc", "wiz")
NEWER = ("byrn", "peaky", "diogo", "izzo", "samb")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s.replace("-", " "))
    for suf in (" jr", " iii", " ii", " sr"):
        s = s.replace(suf, "")
    return " ".join(s.split())


def build(tp: dict | None, sf: dict | None, fv: dict | None, inj: dict | None) -> dict:
    out_now = {_norm(p.get("player")): p for p in (inj or {}).get("players") or []
               if str(p.get("status") or "").lower() == "out"}

    def is_out(r: dict) -> bool:
        legs = r.get("legs") or []
        return len(legs) == 1 and _norm(legs[0].get("player")) in out_now

    names = {t["key"]: t for t in (tp or {}).get("tipsters") or []}
    tonight = (tp or {}).get("tonight") or []
    proven, newer = [], []
    for r in tonight:
        if r.get("kind") != "prop" or len(r.get("legs") or []) != 1:
            continue
        row = {**r, "out": is_out(r), "who": names.get(r["tipster"], {})}
        if r["tipster"] in PROVEN:
            proven.append(row)
        elif r["tipster"] in NEWER and r.get("your"):
            newer.append(row)
    proven.sort(key=lambda r: (r["out"], r["tipster"], r.get("id", "")))
    newer.sort(key=lambda r: (r["out"], -(names.get(r["tipster"], {}).get("u") or 0), r.get("id", "")))

    safe = [r for r in (sf or {}).get("tonight") or [] if r.get("tag") == "good"]
    for r in safe:
        r["out"] = _norm(r.get("player")) in out_now

    today = (tp or {}).get("today")
    combo = next((c for c in (tp or {}).get("combos") or [] if c.get("date") == today), None)

    favs = (fv or {}).get("tonight") or []
    return {"favs": favs, "fav_rec": (fv or {}).get("rec"), "proven": proven, "proven_names": [names[k] for k in PROVEN if k in names],
            "newer": newer, "safe": safe, "safe_n": len((sf or {}).get("tonight") or []), "combo": combo,
            "listed_safe": bool((sf or {}).get("listed_today")),
            "lab": (tp or {}).get("lab") or {}, "p_to_am": (tp or {}).get("p_to_am"),
            "n": len(favs) + len(proven) + len(newer) + len(safe) + (1 if combo else 0)}
