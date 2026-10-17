"""
One canonical team code per franchise, plus an alias map per data source.

Every source spells teams differently:
  ESPN / sportsdataverse   GS, NO, NY, SA, UTAH, WSH
  Kalshi tickers           GSW, NYK, SAS, UTA, PHX ...   (KXNBAGAME-26OCT20BOSDET)
  The Odds API             full names ("Golden State Warriors")
  PrizePicks               team abbreviations, usually the NBA's own

The site uses the NBA's own three-letter codes as canonical. Anything a
source sends that doesn't resolve is recorded with `note_unmatched()` and
written to data/unmatched_names.json by build.py, so a new spelling shows
up in the log instead of silently dropping a game.

Team colors are each franchise's published primary color (a fact, not
artwork). No logos are used anywhere: they are trademarked.
"""

from __future__ import annotations

TEAMS = {
    # code: (city / short name, full name, primary color)
    "ATL": ("Atlanta", "Atlanta Hawks", "#C8102E"),
    "BKN": ("Brooklyn", "Brooklyn Nets", "#000000"),
    "BOS": ("Boston", "Boston Celtics", "#008348"),
    "CHA": ("Charlotte", "Charlotte Hornets", "#008CA8"),
    "CHI": ("Chicago", "Chicago Bulls", "#CE1141"),
    "CLE": ("Cleveland", "Cleveland Cavaliers", "#860038"),
    "DAL": ("Dallas", "Dallas Mavericks", "#0064B1"),
    "DEN": ("Denver", "Denver Nuggets", "#0E2240"),
    "DET": ("Detroit", "Detroit Pistons", "#1D428A"),
    "GSW": ("Golden State", "Golden State Warriors", "#FDB927"),
    "HOU": ("Houston", "Houston Rockets", "#CE1141"),
    "IND": ("Indiana", "Indiana Pacers", "#0C2340"),
    "LAC": ("LA Clippers", "LA Clippers", "#12173F"),
    "LAL": ("LA Lakers", "Los Angeles Lakers", "#552583"),
    "MEM": ("Memphis", "Memphis Grizzlies", "#5D76A9"),
    "MIA": ("Miami", "Miami Heat", "#98002E"),
    "MIL": ("Milwaukee", "Milwaukee Bucks", "#00471B"),
    "MIN": ("Minnesota", "Minnesota Timberwolves", "#266092"),
    "NOP": ("New Orleans", "New Orleans Pelicans", "#0A2240"),
    "NYK": ("New York", "New York Knicks", "#1D428A"),
    "OKC": ("Oklahoma City", "Oklahoma City Thunder", "#007AC1"),
    "ORL": ("Orlando", "Orlando Magic", "#0150B5"),
    "PHI": ("Philadelphia", "Philadelphia 76ers", "#1D428A"),
    "PHX": ("Phoenix", "Phoenix Suns", "#29127A"),
    "POR": ("Portland", "Portland Trail Blazers", "#E03A3E"),
    "SAC": ("Sacramento", "Sacramento Kings", "#5A2D81"),
    "SAS": ("San Antonio", "San Antonio Spurs", "#000000"),
    "TOR": ("Toronto", "Toronto Raptors", "#D91244"),
    "UTA": ("Utah", "Utah Jazz", "#4E008E"),
    "WAS": ("Washington", "Washington Wizards", "#E31837"),
}
CODES = tuple(TEAMS)

# Source spelling -> canonical. Only entries that DIFFER need listing;
# a code that already equals a canonical code resolves on its own.
ALIASES = {
    "espn": {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"},
    "kalshi": {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS",
               "BRK": "BKN", "PHO": "PHX", "CHO": "CHA"},
    "prizepicks": {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS",
                   "BRK": "BKN", "PHO": "PHX", "NOR": "NOP"},
    "oddsapi": {full: code for code, (_c, full, _col) in TEAMS.items()} | {
        "Los Angeles Clippers": "LAC",
    },
}

# Canonical -> how each source spells it (the reverse direction, needed to
# build Kalshi ticker fragments and Odds API name lookups).
KALSHI_CODE = {code: code for code in CODES}  # Kalshi uses NBA codes; verified on live tickers (GSW, UTA, NYK, PHX)

_unmatched: dict[str, dict[str, int]] = {}


def canon(name: str | None, source: str) -> str | None:
    """Canonical code for a source's team spelling, or None (and logged)."""
    if not name:
        return None
    raw = str(name).strip()
    key = raw.upper() if source != "oddsapi" else raw
    if source != "oddsapi" and key in TEAMS:
        return key
    hit = ALIASES.get(source, {}).get(key)
    if hit:
        return hit
    note_unmatched(source, raw)
    return None


def note_unmatched(source: str, raw: str) -> None:
    bucket = _unmatched.setdefault(source, {})
    bucket[raw] = bucket.get(raw, 0) + 1


def unmatched_report() -> dict:
    return {src: dict(sorted(names.items())) for src, names in _unmatched.items()}


def full_name(code: str) -> str:
    return TEAMS.get(code, (code, code, ""))[1]


def city(code: str) -> str:
    return TEAMS.get(code, (code, code, ""))[0]


# ---------------------------------------------------------------------------
# Badges that stay readable on the dark background
# ---------------------------------------------------------------------------

def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def badge(code: str) -> dict:
    """Color chip for a team: background = team color, text color picked by
    contrast, and a light outline whenever the color itself is too dark to
    separate from the page background (#0a0a0f)."""
    color = TEAMS.get(code, (None, None, "#5f6862"))[2]
    lum = _luminance(color)
    text = "#0a0a0f" if lum > 0.22 else "#ffffff"
    # Contrast of the chip against the page background; under ~2:1 the chip
    # edge disappears, so it gets a visible outline.
    bg_lum = _luminance("#0a0a0f")
    contrast = (max(lum, bg_lum) + 0.05) / (min(lum, bg_lum) + 0.05)
    outline = "#c8c8d8" if contrast < 2.2 else "transparent"
    return {"abbr": code, "color": color, "text": text, "outline": outline}
