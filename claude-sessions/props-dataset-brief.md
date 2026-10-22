# Brief: NBA player-prop dataset for the dependence extension

Written 2026-10-20 by the planning session (`totalis-e0`). Owner: Ruben.
Send a summary to `totalis-e0` when done.

## Why

Same-game dependence for team markets comes from the joint score distribution
(`rfqbot/pricing`). Player props are not functions of the score, so they need a second layer:
a player's stat modelled as a share of their team's points plus a residual, with the share
implied by the prop line and the residual shape, and the residual correlations between
players, fitted from history. That fit needs a dataset nobody has built yet: player-game
outcomes joined to the pre-game lines. NBA first, because that is where the props are
(hundreds per game on Polymarket; Kalshi `KXNBAPTS/REB/AST/3PT` ladders).

## Deliverables (new package `rfqbot/props/`, you own it; sqlite `data/props.db`)

1. **Box scores**: per player per game for the 2025-26 season to date (and 2024-25 if cheap),
   from a public source (`stats.nba.com` via the `nba_api` package, or the NBA's public CDN
   JSON; pick what works unauthenticated and document it): player id, name, team, opponent,
   game date (ET), minutes, points, rebounds, assists, threes, starter flag, plus team points,
   opponent points, final margin, overtime periods. Table `box_player`, `box_team`.
2. **Pre-game lines**: for each player-game, the closing prop lines. Sources, in order of
   preference: (a) Kalshi settled markets: `GET /markets?series_ticker=KXNBAPTS&status=settled`
   (and REB/AST/3PT) with `floor_strike` and `result`, plus the last pre-tip price from the
   public candlesticks endpoint (`/series/{series}/markets/{ticker}/candlesticks`, verify it
   is public and its period parameters); (b) Polymarket history once the Data Engineer's
   `rfqbot/history` package exists (player-prop events are `nba-...-player-props`; coordinate,
   do not build a second Gamma puller). Table `prop_lines(player_key, game_key, stat, line, p_over,
   source, t)`. Player identity across sources: initial + normalized surname + team, the same
   rule as `rfqbot/sports/nba_players.py` (reuse it; do not copy).
3. **Join and first fit** `python -m rfqbot.props fit`: for points, rebounds and assists:
   - share model: `stat = s * team_points + e` where `s` is the player's implied share from the
     line (line / team-total line when both exist, else the season share); report the residual
     distribution per stat (sd as a function of the line, skew, and the did-not-play mass);
   - the blowout effect: residual vs |final margin| for starters;
   - teammate residual correlation by position pair and opponent-pair correlation (pace);
   - player-vs-team-total and player-vs-spread correlations implied by the data, which are
     what the extension will need as defaults.
   Output a short markdown report `claude-sessions/props-fit-notes.md` with the numbers, plus the
   parameters in a JSON file the engine extension can load.
4. Tests on small fixtures (box-score parsing, line joining, the share fit on synthetic data).

## Ownership

You own `rfqbot/props/*`, `tests/test_props_*.py`, `claude-sessions/props-fit-notes.md`. Shared files are
owned as before (NFL session: pricing, quoting, store, __main__; Data Engineer: books, ladders,
history; NBA session: kalshi_books, clusters, sports/nba*). Not a git repo. Suite is 236 green.

## Report back

Rows per table, coverage of lines per player-game by source, the headline residual sds and
correlations, and anything that blocks a clean join (name collisions, trades mid-season).
