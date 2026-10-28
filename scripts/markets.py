"""
Merges every market source into one lookup keyed by (player, stat, game_id).

  FanDuel (Odds API)  priced over/under; no-vig chance from the two prices;
                      break-even = the price's own implied chance (e.g. -110 = 52.4%)
  PrizePicks          line only, no price; break-even 54.2% (6-pick flex), labelled
  Kalshi              "N+" ladder; main line = rung priced nearest 50%;
                      break-even = the ask you'd pay for that side plus Kalshi's
                      fee (about 7% x price x (1 - price)); PrizePicks lines that
                      match a Kalshi rung take Kalshi's market chance (anchor)

The primary line for a pick (the one it is graded against) is the first
source that has one, in PRIMARY_ORDER. All the other sources' lines are kept
alongside so History can compare every line's own error to ours.

Player names are matched to ESPN's spelling with normalize_name(); a name is
only remapped when exactly one known player on that team shares the
normalised form. Anything unmatched is logged (data/unmatched_names.json).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import kalshi_client
import oddsapi_client
import prizepicks_client
import teams

ET = ZoneInfo("America/New_York")
PRIMARY_ORDER = ("fanduel", "prizepicks", "kalshi")
SOURCE_LABEL = {"fanduel": "FanDuel", "prizepicks": "PrizePicks", "kalshi": "Kalshi",
                "prizepicks+kalshi": "PrizePicks (Kalshi-priced)"}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[.'’`]", "", s.lower()).replace("-", " ")
    toks = [t for t in s.split() if t]
    while toks and toks[-1] in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


class NameMatcher:
    """Maps a source's player name (+ team when known) to our player name."""

    def __init__(self, roster_rows):
        # roster_rows: iterable of (player, team)
        self.by_norm: dict[str, set] = {}
        self.by_norm_team: dict[tuple, set] = {}
        for player, team in roster_rows:
            n = normalize_name(player)
            self.by_norm.setdefault(n, set()).add(player)
            self.by_norm_team.setdefault((n, team), set()).add(player)
        self.unmatched: dict[str, int] = {}

    def match(self, name: str, team: str | None, source: str) -> str | None:
        n = normalize_name(name)
        if team:
            c = self.by_norm_team.get((n, team))
            if c and len(c) == 1:
                return next(iter(c))
        c = self.by_norm.get(n)
        if c and len(c) == 1:
            return next(iter(c))
        key = f"{source}: {name}" + (f" ({team})" if team else "")
        self.unmatched[key] = self.unmatched.get(key, 0) + 1
        return None


def _et_date(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(ET).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def build_lines(slate_games, matcher: NameMatcher, roster_team: dict,
                fd_props: list, pp: dict, kalshi: dict) -> dict:
    """
    slate_games: [{game_id, away, home, date (ET iso)}] -- games we project.
    roster_team: {player: team} for the slate.
    Returns {(player, stat, game_id): {source: {...line record...}}}.
    """
    by_team_date = {}
    for g in slate_games:
        for side in ("away", "home"):
            by_team_date[(g[side], g["date"])] = g["game_id"]
    by_matchup = {(g["away"], g["home"]): g["game_id"] for g in slate_games}
    out: dict = {}

    def put(player, stat, gid, source, rec):
        if rec.get("line") is None or rec["line"] <= 0:
            return  # a 0 or negative line is a parsing problem, never a real prop
        out.setdefault((player, stat, gid), {})[source] = rec

    # FanDuel
    for ln in fd_props:
        gid = by_matchup.get((ln.get("away"), ln.get("home")))
        if gid is None:
            continue
        who = matcher.match(ln["player"], None, "fanduel")
        if not who or roster_team.get(who) not in (ln.get("away"), ln.get("home")):
            continue
        po_raw = oddsapi_client.american_to_prob(ln.get("over_price"))
        pu_raw = oddsapi_client.american_to_prob(ln.get("under_price"))
        nv = oddsapi_client.no_vig_pair(ln.get("over_price"), ln.get("under_price"))[0]
        put(who, ln["stat"], gid, "fanduel", {
            "line": float(ln["line"]), "over_price": ln.get("over_price"), "under_price": ln.get("under_price"),
            "over_str": oddsapi_client.fmt_price(ln.get("over_price")),
            "under_str": oddsapi_client.fmt_price(ln.get("under_price")),
            "market_over": nv, "be_over": po_raw, "be_under": pu_raw,
        })

    # PrizePicks
    for ln in pp.get("lines", []):
        team = teams.canon(ln.get("team"), "prizepicks") if ln.get("team") else None
        who = matcher.match(ln["player"], team, "prizepicks")
        if not who:
            continue
        team = team or roster_team.get(who)
        gid = by_team_date.get((team, _et_date(ln.get("start_time"))))
        if gid is None:
            continue
        be = prizepicks_client.BREAK_EVEN_PCT / 100
        put(who, ln["stat"], gid, "prizepicks", {"line": ln["line"], "market_over": None,
                                                  "be_over": be, "be_under": be})

    # Kalshi ladders
    for (name, stat, away, home), rungs in kalshi_client.player_ladders(kalshi).items():
        gid = by_matchup.get((away, home))
        if gid is None:
            continue
        who = matcher.match(name, None, "kalshi")
        if not who or roster_team.get(who) not in (away, home):
            continue
        main = kalshi_client.main_line(rungs)
        if not main:
            continue
        put(who, stat, gid, "kalshi", {
            "line": main["line"], "threshold": main["threshold"], "market_over": main["mid"],
            "be_over": kalshi_client.with_fee(main.get("yes_ask")),  # ask + Kalshi's fee
            "be_under": kalshi_client.with_fee(1 - main["yes_bid"]) if main.get("yes_bid") is not None else None,
            "ladder": [{"n": r["threshold"], "mid": r["mid"]} for r in rungs if r.get("mid") is not None],
        })
    # Kalshi check for PrizePicks: PrizePicks posts no price (we'd assume 50/50). When Kalshi has
    # a rung at the same number (PrizePicks 1.5 = Kalshi "2+"), take Kalshi's market chance instead,
    # so the PrizePicks pick starts from what the sharper market thinks.
    for srcs in out.values():
        pp, ka = srcs.get("prizepicks"), srcs.get("kalshi")
        if not pp or not ka or pp["line"] % 1 != 0.5:
            continue
        n = int(pp["line"] + 0.5)
        rung = next((r for r in ka.get("ladder") or [] if r["n"] == n and r.get("mid") is not None), None)
        if rung and 0.03 <= rung["mid"] <= 0.97:
            pp.update(market_over=rung["mid"], anchor="kalshi", anchor_n=n)
    return out


def primary(sources: dict) -> tuple[str, dict] | tuple[None, None]:
    for s in PRIMARY_ORDER:
        if s in sources:
            return s, sources[s]
    return None, None
