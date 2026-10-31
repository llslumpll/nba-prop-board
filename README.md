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

## Consistency audit (Oct 21)

`scripts/audit.py` recomputes everything from the raw pick files and checks it: every pick frozen before tip, grading arithmetic (props and game picks against final scores), nothing left pending after its box score, Best 5 / Best Value rules, chance cap, and that Home's two boards, History's Profit totals and every day page show exactly the raw numbers. It runs at the end of every build; the result is saved to `data/audit.json` and shown on History → Pipeline (green when all pass, red with the failing checks). Run it by hand with `python scripts/audit.py`.

Game picks also record a closing line now (FanDuel spread / total, no-vig moneyline for the picked side), so their CLV counts like the props'. Day pages' GAMES tab lists every game's final score with our moneyline / spread / total results.

## Live tracker, safer saving, self-repairing Best 5 (Oct 21)

* **Games → LIVE** (`docs/static/live.js`): runs in the visitor's browser and pulls ESPN's public scoreboard + game summary every 30 s while the tab is open (no site rebuild needed). Shows the score / clock, our game picks' live status, every frozen player pick vs the live box score (current, pace at our projected minutes, ✔ hit / ✘ lost / on track / behind; ★ Best 5 first), the box score and play-by-play. `games.html#live-<game_id>` opens a game; each game in ALL GAMES has a LIVE TRACKER button. If ESPN can't be reached it says so and links to ESPN's gamecast.
* **Saving:** workflows now use `scripts/push_or_rebuild.sh`. If another run pushed while one was working, it resets to the new main, restores its freshly fetched inputs and rebuilds instead of merging generated files (a line-by-line merge of two runs once produced a 6-pick 3PM Best 5 on Oct 21).
* **Best 5 lists** record when each pick was tagged (`best5_at`); a list over 5 keeps the earliest-tagged picks. `predictions.FIXES` holds one-time corrections, applied idempotently and noted on the pick (`corrections`), shown on the day page.

## Same-night grading, live slate hold, recommended units, History rebuild (Oct 23)

**Graded as soon as a game ends (`scripts/espn_finals.py`).** Every build (light or heavy) asks ESPN's public scoreboard about today, yesterday and any date with pending picks. For each FINAL game the daily sportsdataverse file doesn't have yet, it downloads that game's box score once and keeps it in `data/espn_box.json`. Those rows are added to the box table in memory (ESPN game and athlete ids are our ids, no name matching), finals are marked completed with their scores, and grading runs as usual: player props and moneyline/spread/total picks are graded within one refresh of the final buzzer. When the daily file catches up, its rows win and the ESPN copy is dropped. If ESPN is unreachable nothing changes except waiting for the daily file; the reason shows under History → Model & pipeline. Tonight's own results never feed tonight's projections (the slate's games are left out of the model input).

**Slate holds until the last game is final (`live.slate`).** The site stays on tonight's date (Home, Picks, Games, the LIVE tracker) until every game on it is final; a game nobody has marked final drops off 5 hours after tip (`LIVE_HOLD_H`). It used to flip to tomorrow as soon as the last game tipped.

**Recommended units (`live.unit_rec`).** Edge = our chance − the price's break-even. 0 or less → PASS; 0–2 pts 0.25U (low), 2–4 0.5U (medium), 4–6 0.75U (lean), 6–9 1U (confident), 9–13 1.5U (high), 13+ 2U (extreme). A big gap vs the line (likely role change) caps at 0.25U; questionable players cap at 0.5U. Units are frozen with the pick (`units`, plus `units_raw` without the trust shrink); picks frozen before units existed are sized from their own chance and price by the same rule (`grade.stake`). The tag shows next to every projection: Picks cards, Players cells, player pages (each line), Games picks, the LIVE tracker and every day page, which also shows profit at that stake.

**Sizing trust (`grade.unit_trust`).** Every build looks at the last 400 graded sized picks at their raw stakes. Under 150 of them: full sizing. ROI ≥ 0: full sizing; between −5% and 0: every edge × 0.75 before sizing; below −5%: × 0.5. So stakes shrink while the sizing loses and come back when results earn it. Shown on History → Units.

**History rebuilt (`scripts/history_view.py`, `templates/history.html`).** A "short version" on top (record, hit rate vs what's needed to profit, units at our stakes and flat, a plain verdict, what's working / what isn't), a glossary, then tabs, each opening with "what this tells you": Overview, Units (record and profit per stake size), Best 5 (sub-tab per stat **and game picks**, each list with record, verdict, hit-rate and running-units charts and its full pick log), By stat (per stat, per line source, our miss vs the line's, over vs under), Game picks (moneyline / spread / total), Players (hot/cold and a searchable record for everyone), Confidence (does more chance / value hit more), Day by day, Model & pipeline. The audit checks the short version's record and units against the raw files and that every pick on every day page shows its units.

**Games page market tabs (Oct 23).** Games now has TOP PICKS (all markets together, the original Best 5 / Best Value, unchanged), MONEYLINE, SPREAD and TOTAL tabs, each with its own BEST 5 (highest chance, one per game, same lock-at-tip rules as the player lists; units show PASS when the chance doesn't beat the price) and BEST VALUE (only picks that beat their price, biggest edge first), then ALL GAMES and LIVE as before. Tags `moneyline:best5`, `moneyline:value`, `spread:…`, `total:…` are tracked from Oct 23 and graded on History → Best 5 (one sub-tab per market). The audit checks each per-market list holds only its own market and that its Best Value never holds a pick below its price.

## Early board, picks follow the line until tip, When to bet, second FanDuel pull (Oct 23)

**Picks follow the line until their own tip.** A pick is recorded as soon as a line exists for it (`predictions.FREEZE_WINDOW` = 36 h before tip), so Best 5 / Best Value fill in the night before or first thing in the morning (the early board, cards marked UPDATES TILL TIP). Every build after that updates it with the latest projection, line, side, chance and units (`predictions.update`, `update_team`) until its game tips; the last pre-tip version is what gets graded. A 3 PM game locks at 3 while the evening games keep updating. The opening version stays in `first_line` / `first_call` (cards show "opened UNDER 10.5") and `snaps.open`. Closing line value is now measured from the opening version.

**Checkpoints.** The first build after 10 AM, 1 PM and 5 PM Central on game day saves every untipped pick as it stands (`predictions.snapshot`, `snaps.10am / 1pm / 5pm`, Best 5 tags included); one missed by more than 3 hours, or after tip, is skipped. History → **When to bet** grades every copy (record, hit % vs needed, units at that moment's stake, flat, beat-the-close, how often the side changed later), for all picks and for Best 5 / Best Value picks only, so the site shows which moment's picks do best. The audit checks every snapshot was taken before tip.

**FanDuel.** Free plan: game lines twice a day (morning, and again after 3 PM ET, ~422 credits in the busiest month); points props once a day. Paid plan: set the repository variable `ODDS_PLAN` (Settings → Secrets and variables → Actions → Variables) to `20k` and trigger the FanDuel workflow hourly; every run then pulls game lines plus points, rebounds, assists and threes props (`oddsapi_client.PAID`), still stopping at the 60-credit reserve.

**Same-night finals without ESPN.** Finals already stored in `data/espn_box.json` mark their games completed even on a build that can't reach ESPN, so the slate never falls back to a finished night.

## Patterns, self-correction and faster minutes (Oct 26)

**Patterns (`scripts/patterns.py`, History → Patterns).** After grading, every build splits the graded picks by date into an earlier and a later half. A correction switches on only when a pattern has 150+ earlier and 100+ later picks, points the same way in both halves and is clearly more than luck (|z| ≥ 2, gap ≥ 3 points). It's re-checked every build and switches off when it stops holding. Kinds: **skip** (a stat + line source, stat + source + side, or game market + side whose hit rate is below what its prices need: picks become SKIP, no stake, off Best 5 / Best Value), **over** (shift the OVER chance for a stat + source by part of the measured gap, max 5 points) and **move** (shift the chance of picks that follow / go against their line's move since opening). Active ones are read by the next build (`patterns.active()` → `live.run(pat=…, open_lines=…)`, `live.GAME_SKIP`). Each pick stores `skip`, `move`, `move_dir` and `adj`; cards show SKIP and LINE ▲/▼. Display breakdowns (over/under, source, price, line move, projection-vs-line gap, home/road, data tier, game markets) are on the same tab. The audit checks skipped picks carry no stake and aren't on open lists.

On the first run (Oct 20–23 vs Oct 24–25): on — skip points on FanDuel, P+A and R+A on PrizePicks, and +2.8 points for picks that go against their line's move. Flipped (held earlier, reversed later) — the "UNDERs win" and over-bias patterns seen on Oct 22.

**Minutes (`live.load_fit`).** `min_prior_k` 4 → 1 and a new `last_game_w` = 0.25 (extra weight on this season's latest game). Out-of-sample: minutes miss 4.75 → 4.31 (first three weeks of 2025-26), 4.97 → 4.62 (this season), 4.18 → 4.13 (mid-season); points and PRA misses down too.

**Confidence fix per line source (Oct 26, `patterns.py` kind `conf`).** Each source's graded picks are checked for hit rate vs the chance we stated. If our chances ran too high in both halves (and lost at least a quarter of their lean from 50%, |z| ≥ 2), every chance on that source is pulled toward 50%, keeping the share of the lean that actually showed up (shrunk toward no change on small samples). First run: PrizePicks (said 63%, hit 53% on 58%+ picks; keeps 25% of the lean) and FanDuel (keeps 46%) switched on; Kalshi flipped (well calibrated overall), so it's untouched. Effect: PrizePicks picks fall below its 54.2% break-even and become PASS, and Best 5 / Best Value lean toward Kalshi lines, the part of Best 5 that has been winning.

**Flips and both sides' prices (Oct 27).** Every pick now saves both sides' break-evens (`be_over` / `be_under`; game picks `be_other`). A new Patterns kind **flip** checks, for each stat + source, stat + source + side, line-move group and game market + side, whether the OTHER side beat its own price in both halves (same 150 / 100 / |z| ≥ 2 rules). When on, those picks switch sides (chance = 1 − ours + the measured lift, shrunk), show a ↺ FLIP tag, and are graded on the flipped side; patterns keep measuring the model's own side (`patterns.orig()`), and each flip rule tracks its own record since switching on. PrizePicks can flip too, but only when the other side clears 54.2% (a group hitting 48% never does). Older picks without a saved other-side price use a 4.5% cut estimate. On Oct 27 nothing qualifies yet: "picks that follow their line's move" (other side +3.5 pts) needs 150 earlier-half picks and a clearer result; FanDuel points OVER (+3.1 pts, z 1.0) isn't clear of luck. Updates now also refresh a pick when only its skip / flip status changes.

## Kalshi fees, Kalshi-priced PrizePicks, priced parlays (Oct 28)

From the Monahan lecture (market-based betting):

- **Kalshi fees in every break-even** (`kalshi_client.with_fee`): Kalshi charges about 7% × price × (1 − price) per contract (rounded up per order; ~1.75¢ at 50¢). Every Kalshi break-even (props and moneylines) is now ask + fee, so an edge is an edge after fees. Picks graded before Oct 28 keep the break-even they were frozen with.
- **Kalshi check for PrizePicks** (`markets.build_lines`): PrizePicks posts no price, so its lines used to start from 50/50. When Kalshi has a rung at the same number (PrizePicks 1.5 = Kalshi "2+"), the PrizePicks line now starts from Kalshi's market chance (`anchor = "kalshi"`), then our projection is blended in as usual. These picks form their own group, `prizepicks+kalshi` ("PrizePicks (Kalshi-priced)"), for every pattern (skip, flip, confidence, over) so the old PrizePicks lessons don't carry over to them blindly. Being new, their stake is capped at 0.5U (`live.NEW_GROUP_CAP`) until 150+ are graded and they've broken even (`patterns.json → proven`). Cards show KALSHI-PRICED.
- **Priced parlay in the slip** (`docs/static/slip.js`): a third mode next to POWER / FLEX multiplies each leg's own price (Kalshi incl. fee, FanDuel) and shows the all-hit chance, the payout, the expected value, and each leg's edge vs its price, with a warning for every leg below its price ("−5% a leg is −10% for two") and for PrizePicks legs (which pay by PrizePicks' own table).

## Teams page and matchup context (Oct 28)

**Teams (`scripts/rankings.py`, `templates/rankings.html`, nav "Teams").** Every build: standings (W–L, win %, home/road, last 10, streak, PPG, opponent PPG, differential, offensive / defensive / net rating per 100 possessions, pace) and what every defense allows per game for points, rebounds, assists, threes and the combos, overall and to guards / forwards / centers, ranked 1–30 (1 = allows the most) with % vs league. Tabs: Standings (tap a column to sort), Allowed (per stat), Team view (one team, every stat by position) and Last season (steadier reference early on).

**Matchup line on every projection.** WHY & STATS shows, per stat, what tonight's opponent allows to the player's position, with rank and % vs league (`rankings.matchup`).

**Opponent defense in the projections (`live.load_fit`).** The model's defense-vs-position factor, off since launch, is on at low strength (`use_def` True, `a_def` 0.25, `def_k` 40 games of prior). Re-tested on games it wasn't fitted on (first weeks and mid 2025-26, this season): average miss lower in 14 of 15 stat / period checks, each by about 0.1% — a small but consistent help; the market already prices most matchups.

## EDGES: market-based +EV, arbitrage and middles (Oct 28)

**`scripts/edges.py`, Games → EDGES.** Every build compares Kalshi's prices with FanDuel's for the same outcome: moneylines, spreads and totals at the same number, and FanDuel points props vs the Kalshi rung at the same number. FanDuel's two prices have their cut removed with the power method; when Kalshi's cost (ask + fee) is below that fair chance by 2%+, it's listed as +EV with "buy at or below" (the most to pay before the fee) and our model's chance as a second opinion. Arbitrage (Kalshi side + FanDuel's opposite side under $1) is flagged, and the closest pair is shown when there's none. Middles (FanDuel and Kalshi on different totals / spread numbers) show the chance both bets win (normal curve around FanDuel's number) when the pair is +EV. Every +EV edge is recorded in `data/edges/DATE.json` when first seen, kept up to date until tip, and graded from final scores / box scores at 1U per edge at the Kalshi cost; the tab shows the record overall and when our model agreed. FanDuel is a single, soft reference book (paid Odds API plans could add sharper books), so these are leads to check, not certainties. The audit checks every edge was recorded before tip. Any error in this step is caught so it can never stop a build.

## Update 30: price check, no cents

- Every open pick now carries a plain-English price check next to its units:
  **GOOD PRICE** (our chance beats what the price needs; same picks that get units),
  **WAIT** (within 3 points of break-even: check back, the price or line may move your way),
  **PRICE TOO HIGH** (could still win, but you'd pay too much). Locked and graded picks show **NO BET** instead of PASS.
- EDGES tab: no cent prices. Each +EV bet shows GOOD PRICE, "about X% better than fair", and the real (devigged) chance;
  arbitrage shows the guaranteed % instead of cents.
- Best 5 lists unchanged on purpose: through Oct 27 the Best 5 picks priced *below* break-even went 81–38 (+6.8U flat)
  while the ones above it went 43–36 (−7.0U), mostly PrizePicks picks that were overconfident before the
  confidence fix. Revisit once the fixed chances have a few weeks of results.

## Update 31: weekly self-tuning + lineup final check

- `scripts/tune.py`: once a week (first build 7+ days after the last run, or Monday after 5+ days) the site
  re-tests 8 model settings (recent-minutes weight, last season's minutes prior, minutes fade, how fast this
  season's stats take over, defense strength and prior, pace strength, usage boost) one step at a time on played
  games. A change is kept only if it lowers the average projection miss on BOTH earlier games (this season before
  the last 7 days + last season's first 3 weeks) and the last 7 days by 0.1%+, without hurting a mid-season stretch
  of last season by more than 0.2%. Saved in `data/tuning.json`, applied by `live.load_fit`, shown on
  History -> Model & pipeline -> Learning. Takes ~5 seconds. When the monthly retune adopts a full new set, the
  weekly overrides retire automatically (they were its starting point). First run Oct 28: 14 nearby settings
  tried, none beat the current ones.
- `scripts/lineups_client.py`: NBA.com daily starting lineups every build. Official inactives on a team whose
  lineup is CONFIRMED are treated as out (minutes go to teammates); starters get a STARTING tag on Players/Picks.
  Builds run every 20 minutes, so one lands 0-20 minutes before every tip, after lineups are usually confirmed.
  Status shown on History -> Model & pipeline -> Checks. Fail-soft (8 s timeout).

## Update 32: Parlays page (parlays.html, nav between Teams and History)

- `scripts/parlays.py` builds the site's own parlays every build from picks it has already recorded:
  Safest props (2/3/4 legs, highest chance), Good price (2/3 legs, only GOOD PRICE legs), Moneyline (2/3/4 legs).
  One leg per game, one per player, never a SKIP, only games not started. Each parlay is rebuilt until its FIRST
  leg tips, then locked; graded from the locked legs (player final stat, game final score). A leg that doesn't
  play or pushes drops out and the parlay pays as the smaller one; LOST as soon as one leg misses.
  Stored in `data/parlays/DATE.json`.
- Page: TONIGHT (cards with chance all hit, pays about = legs' prices multiplied, price check, ALL LEGS ON KALSHI
  and the exact Kalshi market per leg), TRACK RECORD (per parlay type: W-L, hit % vs expected, units at 1U, ROI,
  % of legs hit), DAY BY DAY, HOW IT WORKS.
- Audit: 2 new checks (legs set before first tip / one per game / right size; results match legs).

## Update 33: proven picks, Best single, Books got it wrong, parlay stakes and streaks

- `scripts/proven.py`: every graded pick is grouped by source + 5-point chance band; each pick shows its group's
  real record ("52/91"). A group that has cashed 80%+ over 20+ picks, with the 95% Wilson lower bound still above
  THIS pick's break-even, lifts the pick to 2U ("PROVEN"); 85%+ over 30+ with the lower bound 3+ points above
  break-even lifts it to 3U ("PROVEN+"). SKIPs and injury-listed players never get lifted.
- Parlays: no player or moneyline in two parlays a day (32b). New types: Best single (1 leg: GOOD PRICE plays
  ranked by group record blended with chance) and Books got it wrong (1/2/3 legs where our chance beats the
  book's own devigged chance by 3+ points). Recommended stake per parlay (singles: the pick's units; 2/3/4 legs:
  1 / 0.5 / 0.25U, doubled after 10+ graded in profit, halved if down 25%+, max 3U). Scoreboard: parlays cashed
  X/Y, singles X/Y, units at recommended stakes, current streak, last 10. Retired parlay types that never locked
  are dropped.

## Update 35: injury news alerts (Home)

- `scripts/news.py`: every build compares tonight's OUT list (ESPN + NBA.com official inactives) with everyone seen
  out earlier that day. A newly ruled-out player is NEWS; for each teammate with a line, the bump comes from real
  history: how he played in games WITHOUT that player vs with him (last season + this one, 4+ games each, shrunk by
  n/(n+6)). Teammates past a stat threshold (pts 1.5, reb/ast 0.8, threes 0.4, combos 1.2-2) are flagged with
  the line at the moment of news; later builds show LINE HASN'T MOVED or BOOK MOVED until tip.
- Every news play is graded against its line/side at the news; Home shows the record (and the record when the
  line hadn't moved by tip). Guards: no alerts on a slate's first build; a name seen out once that day is never
  news again; tipped games ignored. Audit: news spotted before tip.
- Note: in testing the projection model spread a star's minutes mostly to the bench (starters +0.5 pts), which is
  why the bump uses with/without history. Feeding with/without into projections themselves is the mid-November item.

## Update 36: with/without absences (tested, kept OFF, re-tested weekly)

- `scripts/wowy.py` + `model.py`: per-minute production of each player in games a star teammate (12+ ppg, fresh
  absence) missed vs games they shared, and what an opponent allowed without its missing players; both from
  games before the projected date only. Params `wowy_w`, `opp_out_w` (default 0 = off).
- Backtest Oct 29 (2025-26 Nov / Jan / Mar + this season): every weight made the average miss slightly WORSE on
  last season (e.g. Jan: 14.64 -> 14.68 at wowy 0.5); the model's own minutes redistribution + team-total pull
  already capture most of it. So both stay off, and the weekly self-tuning (tune.py) now tries a 0.25 step on
  each every week and switches one on only if it beats the current settings on earlier AND recent games.
- requirements.txt: numpy (used by wowy.py).

## Update 37: role-based scoring rate (ON) + role signals on cards

- `model.py` `role_rate_w` / `role_early` / `role_band` / `role_k`: a player's per-minute rate is blended with his
  rate in past games (this + last season) where he played within 5 minutes of tonight's projected minutes
  (n / (n + 10) shrink), weighted most early in the season (x 5 / (games this season + 5)). From the Dr. Profit
  writeups: players produce at a higher rate in bigger roles (Watson: 0.45 pts/min overall vs ~0.60 in 25+ min games).
- Backtest Oct 29 (games the setting wasn't fitted on): PRA miss 6.416 -> 6.411 (Nov 2025), 6.215 -> 6.212 (Jan),
  6.240 -> 6.238 (Mar), 6.562 -> 6.487 this season; reb / ast / threes better in every window, points in 3 of 4.
  ON by default (live.load_fit); both knobs added to the weekly self-tuning.
- Pick cards: "IN 25-35 MIN GAMES: 15/19" (how often he cleared tonight's line in games with minutes like
  tonight's projection) and a MINUTES RISING / FALLING tag when last game's minutes differ from his recent
  average by 6+.

## Update 38: early-season rotation fix (players who haven't been playing)

- Bug (Oct 29): Isaiah Stewart projected 0.0 minutes. Memphis players who hadn't played a game this season
  (Mashack, Small) or only one (Murray) were counted at last season's minutes, filled the ~240 team minutes
  ahead of him, and pushed him to "fringe" (0 after the team cap).
- Data: once a team has played 2+ games, a player with 0 appearances plays in 41% of games for ~10 min.
- Fix (model.py appear_*): in a team's first 10 games, a player who appeared in under half of them is ordered by
  minutes x his appearance share and treated as fringe; his minutes no longer push rotation players out.
  Stewart: 0.0 -> ~18 min.
- Test on live-like rosters (everyone with a box row, DNPs = 0 min): this season minutes miss 6.01 -> 5.60, PRA
  miss 6.02 -> 5.83 (20+ min players 6.76 -> 6.72); first 4 weeks of 2025-26 overall 5.59 -> 5.47 min (20+ min
  players slightly worse, 6.64 -> 6.72 PRA). No effect after a team's 10th game.
- Also in update 38 (full data sweep, Oct 29):
  - Sweep: live-style projections for every game day this season + tonight, checking zero-minute regulars,
    phantom minutes, team minute totals, roster vs box-score teams, names, duplicates, out players with picks and
    stale box scores. Feeds were clean (no missing finals, no team or name mismatches); two projection bugs found.
  - DeAndre Jordan (NOP) projected 0.0 min with a 4.5-rebound line: the team-minutes cap left the 11th man at 0.
    New safety net (live.py MIN_FLOOR_*): a player with a posted line who has played this season (10+ recent min)
    and is projected under 8 min is floored at 80% of his recent minutes; his teammates shrink (max 15%) so the
    team still adds up to one game. Historically for such players the miss drops 8.3 -> 5.7 min (Nov 2025),
    7.1 -> 5.6 (Jan), 9.3 -> 6.5 (this season).
  - New permanent audit checks on every build (data/projections_today.json snapshot): every team's projected
    minutes add up to a game (225-247); no player with a posted line who's been playing projected near 0; every
    player projected once, for one team; every game final 15+ hours has box scores. Plus a printed NOTE listing
    regulars the cap trims under 8 min with no line posted.

## Update 39: "prices checked" stamp on parlay cards

- Every open parlay shows "PRICES CHECKED 5:40 PM CDT · 12 min ago" (the viewer's own time zone; turns amber
  after 25 minutes, i.e. a build was missed). Parlays re-price every build; the stamp shows how fresh that is.

## Update 40: optional TERMINAL style (switchable, ARCADE stays the default)

- `docs/static/terminal.css` (+ IBM Plex Mono fonts in docs/static/fonts): a black / amber / monospace look, every
  rule scoped to `html[data-theme="terminal"]`, so the arcade look is untouched.
- A "STYLE: ARCADE / TERMINAL" button in the header switches it; the choice is saved per browser/device
  (localStorage `pe_theme`) and applied before the page paints. To remove the theme: delete terminal.css and the
  button in base.html.

## Update 42: PRIMETIME style + BKN minutes fix (the style was removed again in 43)

- Fix (live.py, still in use): when several players on one team get the posted-line minutes floor, and shrinking
  teammates (max 15%) isn't enough, the whole team is scaled evenly so it still adds up to one game (seen in a
  test: BKN 261 -> 241).
- A switchable PRIMETIME look (`court.css`); dropped in update 43.

## Update 43: ARCADE theme pack (one look again; the STYLE switch is gone)

- The STYLE button and the alternate looks are removed; the site is ARCADE only. `court.css`, `terminal.css`
  and the Inter / Barlow Condensed / IBM Plex Mono fonts in `docs/static` are no longer used (safe to delete).
- Arcade-game touches added to the end of `docs/static/style.css` (all CSS, no data changes):
  marquee light bulbs around the logo, the tagline scrolls like an LED ticker, faint neon "arcade carpet"
  shapes in the background, a bouncing basketball on the home title, a blinking cursor after section titles,
  "ON FIRE!" pixel flames on any pick staked 1U or more, a combo counter on parlays ("3x COMBO", singles
  say "SOLO SHOT"), and graded parlays say "WINNER!" or "GAME OVER". Animations stop for visitors whose
  device asks for reduced motion.

## Update 44: lit-up court background (Sunset colors)

- The neon "arcade carpet" shapes behind the site are replaced by a full NBA court drawn in glowing lines,
  in warm Sunset colors: orange sidelines, pink 3-point arcs, yellow keys and center circle, red rims, with a
  soft orange glow on the floor. It stands upright on phones and lies sideways on wide screens, and stays put
  while the page scrolls. CSS only (end of `docs/static/style.css`); no data changes.

## Update 45: one side per number, COIN FLIP, no stakes on coin flips

- `live.py` `align_sides`: when two sources post the same line for a player-stat but our chances point opposite
  ways (different prices, or a pattern that fired on only one source, e.g. Giannis REB 11.5: PrizePicks OVER 50%
  after the line-move pattern, Kalshi UNDER 53%), every line at that number takes the side of the most confident
  one; the others keep the leftover chance (shown as 50% at least) and get no stake.
- Player props under 52% get no stake and show **COIN FLIP** (new tag + legend line). Staked props under 52%
  went 23-31 (42.6%, -1.56U) through Oct 29: Kalshi's cheap price was right, our sliver of a lean wasn't.
  Game picks (moneyline underdogs) are unaffected.
- WHY & STATS now says "we give UNDER 53%" (the side shown and its chance) instead of always quoting the OVER %.
- New audit check: every recorded pick's chance is for the side shown (50% or more).

## Update 46: injury news moves the projections, combined news record, News tab, news parlays

- The news now changes the projection itself (`news.projection_adj` -> `live.run(news_adj=...)`): every
  teammate of a player ruled out today gets half (NEWS_W 0.5) of his real with/without difference added to
  every stat (combos = sum of parts), summed over each absence that day. The build re-projects the moment news
  lands, so that build's picks, Best 5 / Value lists and open parlays already use it. WHY & STATS says
  "injury news: +1.9 with X ruled out today". Tested on the 39 news situations so far: the model alone moved
  those projections +0.4 on average while the history said ~3; half the history put the projection on the
  right side of the line 68% of the time (vs 55%) with the same average miss; the full history overshot.
  (The update-36 with/without boost for EVERY game, not just same-day news, stays off: it made projections
  worse; the weekly tuner keeps re-testing it.)
- `news.mark_counted`: one news play per game + player + stat, using every absence combined. When a second
  teammate is ruled out and flags the same player-stat again, the bumps add up (net) and the play follows the
  net; if they offset to below the stat's threshold there is nothing left to call (Oct 29 Derik Queen REB:
  +1.7 and -1.4 = +0.3). Applied to old days too: 28/43 -> 24/37 (64.9%). Home tags COMBINED plays.
- History -> NEWS PLAYS tab: record, hit rate vs 52.4% needed, flat units, record when the line hadn't moved by
  tip, games that came out ahead, by day, every graded play, and progress toward a stake (40+ graded and the
  cautious end of the hit rate above 52.4%; once earned, news plays show 0.5U).
- `parlays.py` family "Injury news" (single, 2, 3 and 4 legs, each only when news hit that many games): counted
  news plays whose lines haven't moved, biggest bump first; same rules as other parlays. TRACKING ONLY (0 stake) until news plays earn a
  stake, kept out of the main parlay record. A 0 stake now stays 0 when graded (was treated as 1U).
- Audit: one counted news play per player-stat per game; news parlays unstaked until earned.

## Update 47: injury-news singles and parlays in waves, following tip times

- The "Injury news" family builds a single plus 2-, 3- and 4-leg parlays from news plays whose lines haven't
  moved, each only when news has hit that many games (one leg per game, no player in two parlays).
- WAVES: news lands at different times and games tip at different times. Each news single / parlay locks at its
  first leg's tip; after that, a NEW one of the same size can be built later the same day from news on games that
  haven't started (ids news-2, news-2-w2, ... up to 6 per size). Example (tested): Embiid out at 4 PM CT ->
  news single on a PHI teammate, locked at the 5 PM tip; Mitchell + Trae Young out at 5:20 PM -> a new single
  and a 2-leg from the later games, while the early single stays locked.
- All TRACKING ONLY until news plays earn a stake (then the single shows 0.5U, parlays 1 / 0.5 / 0.25U); kept
  out of the main parlay record; every news play is also graded on its own (History -> NEWS PLAYS).

## Update 48: market reading (which lines to trust, Kalshi lagging the books, prices saved for learning)

- Tested first (walk-forward, each day learning only from earlier days): a "trusted situations" table by
  source x stat x side x price x disagreement x line move did NOT predict next-day results with 11 days of data
  (TRUSTED -6.6% vs AVOID -3.9%), so it is not used for stakes. What the data does show: the prices are sharper
  than our disagreements with them (the bigger our chance above Kalshi / FanDuel, the worse), and picks whose
  line later moved past our number (beat the close) hit 64%.
- `market.py` KALSHI LAGGING THE BOOKS: when PrizePicks and FanDuel both have a prop line 1+ points from Kalshi's
  even-money rung (same side), the Kalshi bet toward their number hit 62.5% over 849 past picks (both sides,
  10 of 11 days 55%+). Past prices on those rungs weren't saved (some "even-money" rungs cost 60%+), so every lag
  is now recorded with the real Kalshi price (ask / 1 - bid, fee included), kept fresh until tip, graded from
  the player's pick, voided after 36 h if the player has no pick. TRACKING ONLY until 100+ graded and the
  cautious end of the hit rate beats the average price paid; then 0.5U. Games -> EDGES lists tonight's; History
  -> MARKET has the record.
- History -> MARKET: every graded pick by line source and Kalshi by price of the side picked: hit rate vs needed,
  cautious end, flat units, TRUSTED / CLOSE / NOT YET.
- Every pick record now also keeps each book's number and break-evens (`mkt`) and Kalshi's ladder
  (`kladder`: [rung, bid, ask]) so future market ideas can be tested with real prices.
- Audit: lag bets recorded before tip, one per player-stat, real prices, graded correctly.

## Update 49: more charts

- `svgchart.py`: `hbar_vs_need` (one bar per row: hit rate, green when it beats what it needs, white tick at the
  needed rate, record on the right) and `running_units` (cumulative units by day on a clean scale with 0 on a
  gridline, green above / red below).
- Home: "Last 10 days" daily hit rate of player picks vs what their prices needed on average.
- Parlays -> Track Record: running units at the recommended stakes (main record only) and hit rate by type vs
  the chance the site gave, with each type's record.
- History -> News Plays: running units at 1U. History -> Market: hit rate vs what the price needs, by line
  source and by Kalshi price.

## Update 50: phone alerts (Discord) + faster checks around tip time

- `scripts/alerts.py`, called at the end of every build: posts to a Discord channel when a player is newly
  ruled out (with the teammate plays whose lines haven't moved), when a new injury-news single / parlay is built,
  Kalshi lags once that tracker has earned a stake, and a morning recap (first build after 9 AM CT) of last
  night's results in units and dollars (1U = $100). Each message is sent once (`data/alerts_state.json`, kept
  through a push-conflict rebuild). Needs the repository secret `DISCORD_WEBHOOK`; without it nothing is sent.
- `.github/workflows/build.yml`: passes `DISCORD_WEBHOOK` to the build and keeps `data/alerts_state.json` on a
  rebuild.
- Faster checks: add a second cron-job.org job that triggers the same "Daily build" workflow every 5 minutes
  from 4:00 to 9:55 PM CT (`*/5 16-21 * * *`, America/Chicago), when injury news lands. Builds take ~1-2 min
  and the workflow queues instead of overlapping; Kalshi / ESPN / PrizePicks are free, so no Odds API credits.

## Update 51: stakes only where they've been earned

- `proven.py` group records now also add up what each pick's price needed (`b`). `proven.earned(rec)`: a regular
  pick carries a stake only when its group (source + 5-point chance band) has 30+ graded picks hitting at least
  2 points above what their prices needed (EARN_N, EARN_MARGIN). Otherwise units 0, a "no stake yet: picks like
  this are H/N ..." note in WHY & STATS, and a NOT EARNED tag (new legend line) instead of a stake.
- Applies to player props and game picks; PROVEN 2U / 3U unchanged (stricter); news plays and Kalshi lags keep
  their own earn rules. Parlays' GOOD PRICE legs and Best Single now mean "chance beats the price" (not "has
  units"), so parlays and Best Value lists build as before.
- Walk-forward backtest Oct 20-30 (each day judged only on earlier days): every recommended stake 670 bets,
  -14.66U on 172U (-8.5%); earned-only 94 bets, -0.43U on 25U (-1.7%). Groups earn and lose it automatically.
