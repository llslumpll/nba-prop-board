# PROP EDGE NBA

A static NBA prop board. Python (polars + Jinja) writes HTML into `docs/`. GitHub Pages serves it. GitHub Actions rebuilds it on a schedule and commits the result back. There is no server.

Every pick is frozen before tip-off, graded after the game, and published win or lose. If a source fails or has no line, the page says so and the site still builds. No number is ever invented, and no pick is shown without a market line behind it.

## One-time setup

1. **Pages:** Settings → Pages → Source: *Deploy from a branch* → Branch `main`, folder `/docs` → Save. The site appears at `https://llslumpll.github.io/nba-prop-board/`.
2. **Actions permissions:** Settings → Actions → General.
   - Choose *Allow all actions and reusable workflows*.
   - Under *Workflow permissions*, choose **Read and write permissions**, so the workflows can commit the rebuilt site.
3. **FanDuel lines (optional):** Settings → Secrets and variables → Actions → New repository secret named `ODDS_API_KEY`.
   - Until it exists, the FanDuel workflow skips itself and spends nothing.
   - Adding it is what turns the daily pull on (about 330 credits in the busiest month; see below).
4. **First run:** Actions tab → *Daily build* → *Run workflow*. Then run *Probe market sources* once, and check `data/probe.json`. It shows which Kalshi series, PrizePicks lines, injuries and rosters came back from GitHub's servers.

## Workflows

All four share the concurrency group `nba-site-repo-write`. They queue rather than overlap, and push with a rebase-and-retry loop.

| Workflow | When (UTC) | What it runs |
|---|---|---|
| `build.yml` *Daily build* | 10:40 daily | `scripts/build.py`: box scores, schedule, rosters, player variance, then everything below |
| `refresh_odds.yml` *Odds refresh* | every 20 min, 16:00–03:59 | `build.py --no-fetch`: injuries, Kalshi, PrizePicks, re-project, freeze, grade, render |
| `oddsapi.yml` *FanDuel pull* | 13:10, 15:10, 17:10, 20:10 daily | `build.py --no-fetch --fanduel-daily`: lines once per ET day, points props once per ET day (retried until posted); extra attempts cost nothing (skipped without the secret) |
| `probe.yml` | manual | `build.py --no-fetch --probe`: writes `data/probe.json`, renders nothing, no credits |
| `retune.yml` *Monthly retune* | 12:20 on the 1st | `retune.py`: re-fits settings with this season's games; adopts only if better on held-out games (see below) |

**GitHub runs scheduled jobs late:** in the first days the 10:40 build ran up to 6 hours late, the 20-minute refresh ran about every 2 hours, and a once-a-day FanDuel schedule never fired. So the FanDuel pull tries four times a day and only acts once.

**Why 10:40:** sportsdataverse's NBA job (`hoopR-nba-data/.github/workflows/daily_nba.yml`) runs at 07:00 UTC in season. The build records the box-score file's `Last-Modified` time, and Home shows it, so a late upstream run is visible.

## Data

- **Box scores and schedule:** the sportsdataverse ESPN parquet releases on GitHub.
  - `player_box_{SEASON}` and `nba_schedule_{SEASON}`. The season label is the year the season ends, so 2026-27 is `2027`.
  - History (`2024`–`2026`) is downloaded once and cached in `data/cache/`.
  - `player_box_2027` returns 404 until games are played. The build handles that and projects from the last two seasons.
  - Only regular-season rows (`season_type == 2`) feed the model.
  - All-Star rows are also tagged regular season (teams STARS, STRIPES, WORLD), so rows are filtered to the 30 real team codes.
- **Injuries:** ESPN's public injuries JSON.
  - Out and Doubtful remove a player and hand his minutes to teammates.
  - Questionable and Day-To-Day players are flagged, and they are kept out of Best 5.
- **Rosters:** ESPN's public team-roster JSON, so offseason moves are right before box scores exist. If that pull fails, each player keeps his last box-score team, and the page says so.
- **Team codes:** `scripts/teams.py` uses the NBA's own three-letter codes, with an alias map per source:
  - ESPN: `GS NO NY SA UTAH WSH`
  - Kalshi: `GSW NYK UTA`…
  - The Odds API: full names
  - PrizePicks: its own codes
  - Unmatched team or player names are written to `data/unmatched_names.json`.
- `nba_api` / stats.nba.com is not used; it commonly blocks cloud IPs like Actions runners.

## Markets

- **FanDuel, via The Odds API** (`basketball_nba`): priced over/under lines.
  - Break-even is the price's own implied chance (−110 = 52.4%).
  - The vig-removed chance is shown for comparison.
- **Kalshi** (public API, no key).
  - Each series below was confirmed to exist on Oct 16, 2026 via `/series/{ticker}`; a made-up ticker returns an error.
  - Before the season, only `KXNBAGAME` had open events (31). The player series list their events on game days.
  - Each build records per-series counts in `data/kalshi_raw.json` and on Home. `probe.yml` shows the raw title format.

  | Series | Used for |
  |---|---|
  | `KXNBAGAME` | moneyline (ticker `KXNBAGAME-26OCT20BOSDET` = BOS at DET) |
  | `KXNBASPREAD`, `KXNBATOTAL`, `KXNBATEAMTOTAL` | fetched and logged; not used in picks yet |
  | `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | player "N+" ladders |

  - **Only tight quotes count as prices:** a bid/ask gap of 10¢ or less. Kalshi lists player ladders days early with near-empty books (e.g. 6¢ bid / 94¢ ask), whose midpoint looks like 50% but isn't a price.
  - **Ladders:** a rung "N+" is the same bet as OVER N−0.5. A rung only counts as a line to pick against when its OVER price is between 25% and 80%; lopsided rungs (e.g. "8+ assists" at 6%) are shown in the ladder but never picked. Of those, the rung priced nearest 50% is the main line, and the full ladder is shown in the "why".
  - **Break-even:** the ask for that side.
  - **Cap:** 400 markets **per series**, by volume. Requests are spaced out and retried on HTTP 429 (the first Actions run was rate-limited on 3 series). On the NFL site, a shared cap let spreads and totals push the moneylines off the page.
- **PrizePicks: experimental.** It has no official API.
  - The NBA league is found by the exact name `NBA` from `/leagues`; it was id 7 on Oct 16, but the id isn't hardcoded. NBA1H, NBAP and similar leagues are excluded.
  - Only standard lines are kept; alternate lines are dropped.
  - There's no price, so History uses a 54.2% break-even, which assumes a 6-pick flex entry. Other entry types differ.

**Which line a pick is graded against:** FanDuel if it has one, else PrizePicks, else Kalshi. Every other source's line seen at freeze time is stored too, so History can compare each line's own error with ours.

### The Odds API budget (500 free credits/month)

Cost is markets × regions. `bookmakers=fanduel` counts as one region, `/events` is free, and empty responses aren't charged.

| Pull | Cost |
|---|---|
| Game lines (h2h + spreads + totals), once a day | 3 → ~93/month |
| Points props (`player_points`) for games tipping within 14 h, once a day | 1 per game |

| Month | Games | Credits (3 × days pulled + games) |
|---|---|---|
| Oct 2026 (from the 17th) | 88 | ~133 |
| Nov | 215 | ~305 |
| Dec | 178 | ~271 |
| **Jan 2027 (busiest)** | 236 | **~329** |
| Feb | 169 | ~253 |
| Mar | 232 | ~325 |
| Apr (regular season; playoff games add 1 each) | 88+ | ~178+ |

- The props loop keeps a 60-credit reserve, using the API's own `x-requests-remaining` header.
- A failed or empty pull never overwrites the last good cache. If the game-lines call fails, props are skipped.
- The key is read only from the secret, and is scrubbed from every logged or saved message.
- Real usage is logged to `data/oddsapi_usage_log.json`.
- Rebounds, assists, threes and combos come from PrizePicks and Kalshi only. Adding them from FanDuel would cost 3–4× as much.

## The model (`scripts/model.py`)

The same code runs the backtest and the live site. Every feature uses only games before the target date.

1. **Minutes first.**
   - The base is recency-weighted minutes (half-life 5 games, tuned).
   - Early in a season that average still reflects the end of last season (rest days, tanking, limits), so it's blended with last season's per-game minutes until this season has 4 games.
   - Rotation players are scaled toward the team's ~241 minutes (pull 0.5, tuned). This hands out minutes freed by absent teammates and trims the excess when everyone is healthy.
   - A live roster lists ~20 players. Players beyond the first 250 rotation minutes are "fringe" and get 35% of their recent minutes.
   - A hard cap keeps team minutes at or below 241, taking the excess from the **deepest players first** (stars keep their minutes).
   - Back-to-backs cut 4% for players averaging 28+ minutes.
   - Blowout risk cuts starters 0.15 min per point of expected margin beyond 8, capped at −3. The margin comes from FanDuel's spread when available, otherwise from season net ratings.
2. **Per-minute rates, shrunk by sample size:**
   - `rate = (n·this_season + k·prior) / (n + k)`, where `n` is games this season.
   - `prior` weights the last two seasons 1.0 / 0.5, and is itself shrunk to the position rate (15 games).
   - `k` per stat was tuned by walk-forward: points 5, rebounds 12, assists 5, threes 3, PRA 3, P+R 3, P+A 3, R+A 8.
3. **Context, each dampened and capped at ±6%:**
   - opponent pace
   - home ±1%
   - rest (back-to-back −2%, 3+ days +1%)
   - usage freed by absent teammates (tuned to 0, so off)
   - **Opponent defense by position was tested and dropped:** it made projections slightly worse in the backtest.
4. **Shared production:**
   - Each player's rate was earned in his own role. When several lead options share a roster (an offseason super-team, say), their projections add up to more points, rebounds or assists than a team produces.
   - Each team's summed projection is compared with a norm (half league average, half that team's last season), and every player is moved halfway toward it (strength 0.5, tuned; capped at ±12%).
5. **Ranges:**
   - Rebounds, assists and threes use a negative binomial. Points and combos use a normal curve with a continuity correction.
   - Variance is `phi·mu + kappa·mu²`, fit on 2024-25.
   - Each player's own past miss size scales it, shrunk toward 1 over 20 games.
   - Whole-number lines exclude the push from both sides.
6. **Game picks:** team scores are summed player points × 1.028. Their margin has missed by about 16 points (standard deviation), and they picked 64% of 2025-26 winners. They are always labeled "lean", and **none are made until both teams have played 10 games this season**: on opening week the summed scores ran 12–18 points above FanDuel's totals (players who changed teams still project like their old role).

**Opening-night check:** the 144 PrizePicks lines collected on the first Actions run were used as a market reference. Projections landed above the line 47% of the time, with an average gap of −0.4. Before the minutes and shared-production fixes, only 17% landed above, with a gap of −2.3. Picks frozen before those fixes, and before the Kalshi tight-quote rule, were deleted, since no game had been played.

**Backtest** (`scripts/backtest.py`, results in `data/backtest.json`): tuned on 2024-25, then scored without changes on 2025-26. Players projected for 20+ minutes, about 16,000 player-games. Numbers are average miss:

| Stat | Model | Season avg | Last 10 | NFL-site formula | In 50% range | In 80% range |
|---|---|---|---|---|---|---|
| Points | **5.10** | 5.31 | 5.30 | 5.65 | 52.0% | 82.0% |
| Rebounds | **2.00** | 2.04 | 2.07 | 2.09 | 50.4% | 79.4% |
| Assists | **1.54** | 1.58 | 1.59 | 1.66 | 49.1% | 80.4% |
| 3PM | **1.04** | 1.05 | 1.06 | 1.06 | 50.9% | 81.2% |
| PRA | **6.42** | 6.77 | 6.72 | 7.40 | 52.5% | 83.0% |

No free source has historical prop lines, so this doesn't show we beat a sportsbook. That is measured live.

## How it learns

- **Daily, from box scores:** each player's rates shift toward this season (after ~5 games it's mostly this season), and his range is re-measured from the model's past misses for him.
- **From graded picks, per stat, after 50 graded picks:** a projection-bias correction and a confidence correction switch on (see below), and the stat can move from "lean" to "edge".
- **Monthly retune** (`scripts/retune.py`, `retune.yml`, 1st of each month):
  - Re-runs the settings search (k per stat, minutes half-life, team-minutes pull, usage, defense strength, shared-production pull) on last season plus the first 70% of this season's game days.
  - Tests the result on the latest 30% of game days, which the search never saw.
  - Adopts it only if the average miss on points, rebounds, assists and threes is at least 0.5% lower and none of them is more than 1% worse. Otherwise it keeps the current settings and says why.
  - Skips itself until there are 1,500 held-out player-games (about 3–4 weeks in).
  - Every run is logged in `data/retune_log.json` and on History. An adoption keeps the old settings in the log, so it can be undone.
  - On/off switches for context factors are not changed by the retune; that needs more than a month of evidence.

## Freezing, grading and honest scoring

- **One record per player / stat / `game_id`**, frozen at the first build where a line exists, and never after tip-off.
  - Records are stored as one file per slate date in `data/predictions/`, so the 20-minute refresh doesn't keep rewriting one large file.
- **Grading:**
  - A pick is graded once its game's box score lands, usually the morning after.
  - A DNP or scratch is **void**, not a miss. A result exactly on a whole-number line is a **push**.
- **"Edge" vs "lean":** every stat starts as "lean". It becomes "edge" only after at least **`MIN_SAMPLE_CAL = 50`** graded picks show both:
  - our average error is no worse than the line's
  - our hit rate is above the average break-even of the sides we picked
- **History** shows, per stat and line source, the hit rate next to its break-even, and our error next to the line's error.
- **Calibration** (`scripts/grade.py`), also gated at 50 graded picks:
  - projection bias per stat and tier: 30% of the average miss is added back
  - a chance shrink per stat
  - `MIN_SAMPLE_CAL` counts graded *picks*. It is separate from the per-player sample settings (`Params.var_k`, the tier cut-offs).
- **Best 5 / Best Value** (per page, one pick per player):
  - Best 5 ranks by our chance for the picked side. Best Value ranks by chance minus break-even.
  - A pick locks when its game tips. Until then, a stronger pick can replace it.
  - Lists flag correlated picks (same team or same game).
- **Logs:**
  - `data/line_movement.json`: first vs latest line per source, last 3 days.
  - `data/pipeline_health.json`: per build, the last 300.
- **History pages** are split: `history.html` is the summary, and `history-YYYY-MM.html` holds each month's picks.

## Visuals

- **Pick cards** (Best 5 / Best Value, NFL-site style): the player's last 10 games as bars against today's line (green = this pick would have hit, red = would have missed, white tick = the line we graded that game against), a dotted season-average line and a dashed projection bar, then factor tiles (projected minutes, averages, typical miss, pace, home/road, rest, shared production) and a "vs this opponent" box.
- **Projections tab:** each player's "why" shows his last 10 games as green/red boxes against the pick.
- **History** (MLB-site style): section tabs; daily accuracy charts per stat with coin-flip and break-even lines; stated chance vs actual over time; "does more value mean anything" table; every Best 5 / Best Value list with its daily hit-rate chart and day-by-day ✅/❌ picks; a pipeline-health chart.
- **Day by day:** one small page per month (a row per day) and one page per day with every pick. A finished day's page is never rewritten.
- All charts are plain SVG drawn by `scripts/svgchart.py`; no JavaScript library.

## Files

```
scripts/
  build.py            orchestrator (flags: --no-fetch --pull-lines --pull-props --probe)
  dataio.py           download/cache/clean parquet, schedule, season status
  teams.py            canonical codes, per-source aliases, readable badges
  injuries_client.py  rosters_client.py  kalshi_client.py  prizepicks_client.py  oddsapi_client.py
  markets.py          name matching, one line book per player/stat/game, break-evens
  model.py            the projection model and distributions
  backtest.py         walk-forward tuning + test
  retune.py           monthly re-fit, adopted only if better on held-out games
  live.py             slate, roster rows, projections, picks, game-level chances
  predictions.py      frozen store, Best 5 tagging
  grade.py            grading, scoreboard, edge/lean, calibration
  logs.py             line movement + pipeline health
  pages.py            page contexts + rendering; templates/ holds the Jinja files
  svgchart.py         server-side SVG bar and line charts
  errs.py             short error text
docs/                 the built site (static/style.css is the source stylesheet)
data/                 caches, frozen picks, logs, backtest results
```

## Run locally

```bash
pip install -r requirements.txt
python scripts/backtest.py          # ~10 s: retune + rescore, writes data/backtest.json
python scripts/build.py             # heavy build
python scripts/build.py --no-fetch  # light build from cache
```

## Known gaps

- **Untested until the first Actions run:** ESPN injuries/rosters, Kalshi player-prop titles and PrizePicks NBA lines. They were checked by hand on Oct 16 but not from a runner. Run *Probe market sources* and read `data/probe.json`.
- **Kalshi team markets:** spread, total and team-total markets are fetched and logged, but not used in picks.
- **Tip times:** opening-night tip times come from ESPN's schedule file. Freezing stops at that time, so if ESPN's time is early, picks lock early, never late.
- **Playoffs and play-in** aren't projected yet; regular season only.
- **Steals and blocks** aren't built yet. They are noisy.
- **Fonts:** load from Google Fonts. If blocked, the pages fall back to a monospace font.

## Freeze timing, closing line value and profit (added Oct 20)

* **Freeze window:** a pick freezes at the first build within **8 hours of tip** that has a line for it (`predictions.FREEZE_WINDOW`), so projections freeze with that day's injury report behind them, not days early.
* **Late scratches:** every build compares the OUT list for both teams with the list stored on each pending pick (`out_ctx`). If someone new is ruled out before tip, the game's picks are re-frozen with the updated projection; the original line and time are kept in `first_line` / `first_frozen_at`, and `refreezes` counts how often. Never after tip.
* **Closing line value:** every build stores the latest pre-tip line for the pick's own source (`close_line`, plus the no-vig chance `close_mkt` where the source has prices). After tip it stops updating, so what remains is the close. History shows the % of picks where the line moved our way.
* **Units:** 1 unit per graded pick at its own line's price (`1/break_even − 1` on a win, −1 on a loss, 0 for push/void). PrizePicks uses the 54.2% flex break-even, so its units are an approximation. Shown on History (with a running-units chart) and on the Home record box.

## Player pages and slip builder (added Oct 20)

* **Player pages** (`docs/p-{player_id}.html`, template `player.html`): one per player on the current slate (rows shown on Players). Tonight's projection and pick per stat, last 15 games vs tonight's line per stat, splits (this/last season, last 5/15, home/away and vs tonight's opponent over 3 seasons), a 20-game log with our graded picks marked, and our record on him (hits, units, CLV). Written only when their content changes; pages for players no longer on the slate are removed. Player names across the site link here; Players has a search box.
* **Slip builder** (`docs/static/slip.js`, runs in the browser only): "+ SLIP" on every open pick adds a leg; the slip shows Power or Flex, the chance of each result (legs treated as independent), editable PrizePicks multipliers (standard defaults), the average return per $10 and expected value, plus warnings for the same player, teammates and same-game legs. The slip lives in the visitor's browser (localStorage) and is never sent anywhere.
* `cache_bust` is now a hash of `style.css`, so pages only change when their content does.

## Chance method v2 (Oct 21): blend with the market, role-gap guard, cap

After the first graded night (Oct 20, 324 picks: raw chances scored worse than a coin flip, Brier 0.262; Best 5 went 6/24 on large disagreements with the line), chances are computed differently (`live.py`):

* **Market-implied mean:** for each line, the average the market implies (the mean at which our distribution gives the market's own over-chance: no-vig price for FanDuel/Kalshi, 50/50 for PrizePicks; `implied_mean`). For points that's about the line; for a 0.5 threes line it's ~0.7–0.9.
* **Blend:** the chance uses `market_mean + BLEND_W × (our projection − market_mean)`, `BLEND_W = 0.35`. `grade.fit_blend` re-fits the weight (minimum average miss, dampened halfway to the default) once 1000 graded picks exist.
* **Early-season range:** variance × up to `EARLY_VAR = 1.3`, fading out over a player's first 10 games this season.
* **Role-gap guard:** if our projection is more than 50% away from the market mean for a player with < 10 games this season, the model weight shrinks further and the pick is kept out of Best 5 / Best Value (flagged "big gap vs line").
* **Cap:** no chance above 80%.
* Picks frozen under the old method are re-frozen once with the new one, only before tip. Replay on Oct 20 (in-sample, so optimistic): Brier 0.248 vs 0.262.

## Game picks, Home day boards, tabbed History (Oct 21)

* **Game picks (moneyline / spread / total)** replace the old "wait 10 games" gate (`live.game_picks`). Each starts from the market (FanDuel spread → margin, FanDuel total, no-vig moneyline) and moves toward our summed-player score by `GAME_BLEND_W` (0.25) × **agreement**: the correlation between our numbers and the market's, pooled over every game so far (`live.agreement`, 0 until 8 games). Shifts are converted to chances on the market's scale (margin σ 12.5, total σ 18). Totals first remove the slate-wide average gap (our sums run high early). Spread/total chances capped at 75%, moneylines 5–95%.
* Frozen in `data/team_predictions/` within 8 h of tip, graded from final scores; units at the FanDuel price. **Game Best 5 / Best Value** (`predictions.sync_team_best`, one per game) only take picks whose chance beats their price. Shown on Games (BEST 5 / BEST VALUE / ALL GAMES tabs; `games.html#<game_id>` opens a game) and on History → Game picks.
* **Home** shows two boards: tonight's slate and the previous day's results (props; game picks in the note line), plus an all-time line.
* **History** is tabbed (Profit & CLV, Scoreboard, Accuracy, Confidence & value, Best 5 record, Game picks, Model, Pipeline, Day by day); `history.html#<tab>` opens a tab.
