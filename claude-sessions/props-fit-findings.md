## Findings (first run, 2026-10-20)

_Note (2026-10-22): the first run bucketed `season` rows on `s * realized team points`; the fit now buckets them on `s * team_pts_line` (else the team's leave-one-out season average) and reports the bucket tables per share source (`by_line_bucket_by_source`). The bucket sds below are from the first run and should be re-read after a refit. Prices whose last trade is more than 120 min before the 15-min pre-tip offset are dropped from the implied line (`join.stale_priced_lines`)._

* **Sources.** stats.nba.com times out and cdn.nba.com answers 403 from this machine, so box scores come
  from ESPN's public site API (one scoreboard call per day, one summary per game; raw JSON cached under
  `data/props/raw/espn`). Full 2025-26: 1,231 regular-season, 6 play-in and 81 playoff games, plus the
  2026-27 preseason and opening night; 4 games answer 404 on ESPN. Team points equal the sum of player
  points in every game. ESPN lists inactive and coach's-decision players as did-not-play rows but omits
  some injured players entirely, which is why lined player-games without a roster row count as DNP.
* **Lines.** Polymarket props exist only from 2025-12-07 (none in Oct-Nov 2025), one line per player-stat
  (a second line on ~5%); they come from the Data Engineer's `history.db` (read-only, one writer). Rows in
  the older "X: Points Over 13.5" title grammar (Dec-Apr) are recovered from the slug. Pre-tip prices
  (`hist_prices`, 15-min offset) were still being pulled when this ran, so `priced` is 0 for Polymarket
  and the share comes from the line itself; the implied-median logic kicks in once prices exist. Kalshi
  KXNBAPTS/REB/AST/3PT settled markets are public with the actual stat in `expiration_value`, but the
  series only started 2026-10-06, so they cover a few hundred preseason / opening-night player-games.
  Polymarket has no threes ladder (20 markets all season), so `fg3m` is season-share only.
* **Share model.** `s = line / team_pts_line` and the leave-one-out season share agree (corr 0.86-0.88,
  same means), so the line is a fair share proxy. Residuals are right-skewed at low lines (median line
  below the mean) and widen with the line: points sd 5.1 at lines under 10 to 8.3 at 30+, rebounds 2.2
  to 4.2, assists 1.3 to 3.4. Lined rows show a positive mean residual (+0.5 pts) because the line is a
  median of a skewed stat; the engine should centre on the mean, not the line.
* **Blowouts.** Starters' points residual falls from about +1.2 in games decided by 5 or less to -0.9
  at 30+ margins (slope -0.07 pts per point of |margin|); rebounds -0.03 per point; assists flat.
  The effect is symmetric in the sign of the margin (winning big and losing big both cut minutes).
* **Dependence.** Teammate residuals are nearly independent for points (-0.02; centres -0.10 with each
  other) and assists (0.00), slightly positive for rebounds (+0.04). Opponent-pair residuals are
  positive everywhere (points +0.02, rebounds +0.05, centre-centre +0.11): pace. Rebound residuals are
  negatively tied to the game total (-0.16: made shots are not rebounded) and to opponent points;
  points and assists residuals are orthogonal to the total and margin by construction of the share.
  These are the defaults the extension should start from; the share already carries the player-vs-team
  dependence (corr(stat, team pts) 0.13 for points).
* **Join problems.** (1) Name key collisions on one roster (`j.williams` on OKC: Jalen and Jaylin
  Williams): the fit keys players by ESPN id and drops lines whose key matches two rostered players in
  that game (`join.ambiguous_lines`). (2) Mid-season trades: the leave-one-out share is per (team, player)
  and restarts after a trade (`join.players_multi_team`). (3) Rescheduled games: Polymarket slugs carry the
  original date; props for a game moved to another day do not join (counted under lines whose game is not
  final). (4) ESPN exhibition games against non-NBA teams (Guangzhou, Melbourne United) are skipped.
