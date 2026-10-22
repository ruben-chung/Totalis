# NBA player-prop share model: first fit (2026-10-20)

Built by `python -m rfqbot.props fit` on `data/props.db` (box scores: ESPN public API; lines: Polymarket closing ladders from `data/history.db`, Kalshi settled props). Model `stat = s * team_points + e` with `s` from the line when a line and a team-points line exist (`line`), else the leave-one-out season share (`season`). Residual sds are by line bucket; pair correlations are on standardized residuals `z = e / sd(bucket)`. Parameters: `rfqbot/props/props_fit.json`.

## Dataset

| table | rows |
|---|---|
| games | 1,391 |
| box_team | 2,770 |
| box_player | 37,144 |
| prop_lines | 28,537 |
| game_lines | 2,678 |
| fetch_log | 1,881 |

Lines per source / stat (rows, with a pre-tip price, settled, joined to a box row):

| source | stat | lines | priced | settled | joined |
|---|---|---|---|---|---|
| kalshi | ast | 39 | 39 | 39 | 39 |
| kalshi | fg3m | 41 | 41 | 41 | 41 |
| kalshi | pts | 67 | 65 | 55 | 67 |
| kalshi | reb | 48 | 48 | 48 | 48 |
| polymarket | ast | 9,241 | 0 | 9,226 | 8,788 |
| polymarket | pts | 9,639 | 0 | 9,622 | 9,153 |
| polymarket | reb | 9,616 | 0 | 9,599 | 9,146 |

## Findings (first run, 2026-10-20)

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

Join diagnostics: 137 lines dropped for a key shared by two rostered players; 80 players seen on more than one team.

Fit sample: 1,319 games, 34,692 roster rows, 28,663 played, 32,195 rows with a team-points line; min minutes 0.0.

Did-not-play mass on the roster: 6,029 / 34,692 = 0.174 (ESPN lists inactive and coach's-decision players); a further 451 lined player-games had no roster row at all (player out, not listed) and count as did-not-play below.

## pts

Rows with a line 9,202, of which did not play 618 (DNP mass 0.067); fitted rows 28,453.

| share source | n | mean e | sd e | skew | p10 | p90 |
|---|---|---|---|---|---|---|
| line | 8,572 | 0.54 | 6.92 | 0.41 | -7.7 | 9.3 |
| season | 19,881 | -0.16 | 5.44 | 0.64 | -6.3 | 6.9 |

Line share vs season share on 8,554 rows: corr 0.871, means 0.136 / 0.137.

Residual by line bucket (sd is the engine's default sd(line)):

| line | n | mean e | sd e | skew |
|---|---|---|---|---|
| 0-10 | 15,427 | 0.22 | 5.09 | 1.12 |
| 10-15 | 6,692 | -0.13 | 6.22 | 0.41 |
| 15-20 | 3,508 | 0.04 | 6.95 | 0.33 |
| 20-25 | 1,701 | -0.38 | 7.59 | 0.45 |
| 25-30 | 917 | -0.46 | 8.07 | 0.02 |
| 30+ | 208 | -1.05 | 8.53 | -0.05 |

Residual sd by position and line bucket (n in brackets):

| position | 0-10 | 10-15 | 15-20 | 20-25 | 25-30 | 30+ |
|---|---|---|---|---|---|---|
| C | 4.67 (2,426) | 5.86 (1,110) | 6.86 (491) | 8.42 (152) | 8.44 (89) | - |
| F | 4.93 (5,595) | 6.13 (2,341) | 6.90 (1,108) | 7.16 (891) | 7.47 (279) | - |
| G | 5.34 (7,406) | 6.40 (3,241) | 7.01 (1,909) | 7.93 (658) | 8.30 (549) | 8.55 (181) |

Blowout (starters, n 13,168): slope of e on |margin| -0.0676/pt (corr -0.098), on signed margin -0.0028/pt (corr -0.007).

| abs margin | n | mean e | sd e |
|---|---|---|---|
| 0-5 | 2,564 | 1.16 | 6.74 |
| 5-10 | 3,069 | 1.52 | 6.86 |
| 10-15 | 2,640 | 1.06 | 6.70 |
| 15-20 | 1,879 | 0.72 | 6.80 |
| 20-30 | 1,947 | 0.04 | 6.82 |
| 30+ | 1,069 | -0.87 | 6.67 |

Vs the game (n 28,453): corr(stat, team pts) 0.127, corr(stat, total) 0.112, corr(stat, margin) 0.065; residual: corr(e, total) -0.003, corr(e, margin) -0.009, corr(e, opp pts) 0.004.

Teammates residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 2,203 | -0.103 |
| C-F | 15,380 | -0.026 |
| C-G | 22,972 | -0.015 |
| F-F | 17,783 | -0.021 |
| F-G | 51,824 | -0.013 |
| G-G | 32,300 | -0.017 |
| all | 142,462 | -0.018 |

Opponents residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 3,482 | 0.046 |
| C-F | 16,855 | 0.022 |
| C-G | 22,698 | 0.018 |
| F-F | 19,910 | 0.030 |
| F-G | 54,889 | 0.023 |
| G-G | 37,051 | 0.019 |
| all | 154,885 | 0.022 |

## reb

Rows with a line 9,260, of which did not play 617 (DNP mass 0.067); fitted rows 28,448.

| share source | n | mean e | sd e | skew | p10 | p90 |
|---|---|---|---|---|---|---|
| line | 8,632 | 0.22 | 2.96 | 0.50 | -3.3 | 4.0 |
| season | 19,816 | -0.06 | 2.36 | 0.79 | -2.6 | 3.0 |

Line share vs season share on 8,619 rows: corr 0.845, means 0.048 / 0.048.

Residual by line bucket (sd is the engine's default sd(line)):

| line | n | mean e | sd e | skew |
|---|---|---|---|---|
| 0-4 | 16,557 | 0.15 | 2.16 | 1.25 |
| 4-6 | 7,096 | -0.10 | 2.68 | 0.60 |
| 6-8 | 2,782 | -0.11 | 3.22 | 0.53 |
| 8-10 | 1,264 | -0.30 | 3.59 | 0.41 |
| 10-12 | 578 | -0.23 | 3.96 | 0.17 |
| 12+ | 171 | -1.10 | 4.18 | -0.07 |

Residual sd by position and line bucket (n in brackets):

| position | 0-4 | 4-6 | 6-8 | 8-10 | 10-12 | 12+ |
|---|---|---|---|---|---|---|
| C | 3.25 (912) | 2.93 (1,061) | 3.55 (920) | 3.61 (833) | 4.04 (407) | 3.95 (143) |
| F | 2.27 (5,007) | 2.74 (3,288) | 3.02 (1,396) | 3.58 (344) | 3.78 (170) | 5.21 (28) |
| G | 1.97 (10,638) | 2.51 (2,747) | 3.05 (466) | 3.25 (87) | - | - |

Blowout (starters, n 13,163): slope of e on |margin| -0.0301/pt (corr -0.103), on signed margin -0.0051/pt (corr -0.030).

| abs margin | n | mean e | sd e |
|---|---|---|---|
| 0-5 | 2,564 | 0.58 | 2.84 |
| 5-10 | 3,067 | 0.61 | 2.94 |
| 10-15 | 2,638 | 0.41 | 2.89 |
| 15-20 | 1,879 | 0.14 | 2.76 |
| 20-30 | 1,946 | 0.08 | 2.85 |
| 30+ | 1,069 | -0.38 | 2.84 |

Vs the game (n 28,448): corr(stat, team pts) 0.021, corr(stat, total) -0.017, corr(stat, margin) 0.052; residual: corr(e, total) -0.157, corr(e, margin) -0.032, corr(e, opp pts) -0.098.

Teammates residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 2,203 | -0.020 |
| C-F | 15,380 | 0.047 |
| C-G | 22,969 | 0.042 |
| F-F | 17,783 | 0.036 |
| F-G | 51,808 | 0.034 |
| G-G | 32,268 | 0.034 |
| all | 142,411 | 0.037 |

Opponents residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 3,482 | 0.111 |
| C-F | 16,855 | 0.051 |
| C-G | 22,688 | 0.054 |
| F-F | 19,910 | 0.051 |
| F-G | 54,871 | 0.044 |
| G-G | 37,024 | 0.046 |
| all | 154,830 | 0.050 |

## ast

Rows with a line 8,133, of which did not play 528 (DNP mass 0.065); fitted rows 28,449.

| share source | n | mean e | sd e | skew | p10 | p90 |
|---|---|---|---|---|---|---|
| line | 7,595 | 0.25 | 2.26 | 0.50 | -2.4 | 3.2 |
| season | 20,854 | -0.05 | 1.57 | 1.07 | -1.7 | 1.9 |

Line share vs season share on 7,581 rows: corr 0.885, means 0.034 / 0.035.

Residual by line bucket (sd is the engine's default sd(line)):

| line | n | mean e | sd e | skew |
|---|---|---|---|---|
| 0-2 | 14,957 | 0.09 | 1.32 | 1.61 |
| 2-4 | 8,630 | -0.06 | 1.91 | 0.88 |
| 4-6 | 3,054 | -0.01 | 2.42 | 0.59 |
| 6-8 | 1,408 | 0.03 | 2.80 | 0.47 |
| 8-10 | 309 | 0.05 | 3.25 | 0.35 |
| 10+ | 91 | -0.32 | 3.40 | -0.38 |

Residual sd by position and line bucket (n in brackets):

| position | 0-2 | 2-4 | 4-6 | 6-8 | 8-10 | 10+ |
|---|---|---|---|---|---|---|
| C | 1.21 (3,064) | 1.87 (928) | 2.55 (175) | 2.64 (38) | - | 3.40 (52) |
| F | 1.26 (6,478) | 1.79 (2,795) | 2.48 (702) | 2.84 (202) | 3.14 (53) | - |
| G | 1.44 (5,415) | 1.99 (4,907) | 2.39 (2,177) | 2.79 (1,168) | 3.27 (239) | 3.33 (39) |

Blowout (starters, n 13,164): slope of e on |margin| -0.0052/pt (corr -0.024), on signed margin 0.0034/pt (corr 0.027).

| abs margin | n | mean e | sd e |
|---|---|---|---|
| 0-5 | 2,564 | 0.26 | 2.04 |
| 5-10 | 3,068 | 0.30 | 2.09 |
| 10-15 | 2,640 | 0.29 | 2.07 |
| 15-20 | 1,878 | 0.28 | 2.15 |
| 20-30 | 1,945 | 0.23 | 2.10 |
| 30+ | 1,069 | 0.06 | 2.20 |

Vs the game (n 28,449): corr(stat, team pts) 0.117, corr(stat, total) 0.099, corr(stat, margin) 0.065; residual: corr(e, total) 0.008, corr(e, margin) 0.016, corr(e, opp pts) -0.004.

Teammates residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 2,200 | -0.006 |
| C-F | 15,373 | -0.000 |
| C-G | 22,961 | -0.007 |
| F-F | 17,769 | 0.005 |
| F-G | 51,817 | 0.004 |
| G-G | 32,303 | -0.014 |
| all | 142,423 | -0.002 |

Opponents residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 3,479 | 0.051 |
| C-F | 16,841 | 0.012 |
| C-G | 22,687 | 0.000 |
| F-F | 19,902 | 0.012 |
| F-G | 54,879 | 0.026 |
| G-G | 37,055 | 0.020 |
| all | 154,843 | 0.018 |

## fg3m

Rows with a line 8, of which did not play 0 (DNP mass 0.000); fitted rows 28,435.

| share source | n | mean e | sd e | skew | p10 | p90 |
|---|---|---|---|---|---|---|
| line | 0 | - | - | - | - | - |
| season | 28,435 | 0.00 | 1.21 | 1.08 | -1.3 | 1.6 |

Residual by line bucket (sd is the engine's default sd(line)):

| line | n | mean e | sd e | skew |
|---|---|---|---|---|
| 0-1 | 12,854 | 0.02 | 0.79 | 2.00 |
| 1-2 | 10,109 | -0.01 | 1.33 | 1.05 |
| 2-3 | 4,366 | -0.05 | 1.63 | 0.70 |
| 3-4 | 1,005 | -0.05 | 1.88 | 0.68 |
| 4+ | 101 | -0.02 | 2.24 | 0.67 |

Residual sd by position and line bucket (n in brackets):

| position | 0-1 | 1-2 | 2-3 | 3-4 | 4+ |
|---|---|---|---|---|---|
| C | 0.51 (3,055) | 1.28 (1,100) | 1.50 (117) | - | - |
| F | 0.77 (4,731) | 1.32 (3,825) | 1.57 (1,434) | 1.90 (229) | - |
| G | 0.92 (5,068) | 1.35 (5,184) | 1.66 (2,815) | 1.87 (776) | 2.26 (94) |

Blowout (starters, n 13,152): slope of e on |margin| -0.0066/pt (corr -0.047), on signed margin 0.0012/pt (corr 0.014).

| abs margin | n | mean e | sd e |
|---|---|---|---|
| 0-5 | 2,561 | 0.12 | 1.38 |
| 5-10 | 3,064 | 0.13 | 1.41 |
| 10-15 | 2,637 | 0.12 | 1.36 |
| 15-20 | 1,877 | 0.11 | 1.44 |
| 20-30 | 1,944 | 0.03 | 1.35 |
| 30+ | 1,069 | -0.10 | 1.31 |

Vs the game (n 28,435): corr(stat, team pts) 0.111, corr(stat, total) 0.090, corr(stat, margin) 0.066; residual: corr(e, total) 0.018, corr(e, margin) 0.011, corr(e, opp pts) 0.007.

Teammates residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 2,197 | -0.025 |
| C-F | 15,364 | -0.007 |
| C-G | 22,942 | -0.012 |
| F-F | 17,755 | -0.031 |
| F-G | 51,769 | -0.009 |
| G-G | 32,262 | -0.004 |
| all | 142,289 | -0.011 |

Opponents residual correlation by position pair:

| pair | n pairs | corr |
|---|---|---|
| C-C | 3,475 | 0.013 |
| C-F | 16,824 | 0.002 |
| C-G | 22,662 | 0.005 |
| F-F | 19,884 | 0.006 |
| F-G | 54,836 | 0.004 |
| G-G | 37,011 | 0.003 |
| all | 154,692 | 0.004 |

## Cross-stat

Same player (z of one stat vs z of another, same game) and teammate pairs (stat a of i vs stat b of j):

| stats | same player n / corr | teammate pairs n / corr |
|---|---|---|
| ast-fg3m | 28435 / 0.116 | 284712 / 0.031 |
| pts-ast | 28448 / 0.233 | 284885 / 0.040 |
| pts-fg3m | 28435 / 0.647 | 284751 / -0.022 |
| pts-reb | 28448 / 0.351 | 284873 / 0.027 |
| reb-ast | 28443 / 0.272 | 284834 / 0.016 |
| reb-fg3m | 28435 / 0.146 | 284700 / 0.004 |

## Review fixes (2026-10-22)

- Season-share rows were bucketed on `s * realized team points`, while lined rows used the pre-game line; they are now bucketed on `s * team_pts_line` (else the team's leave-one-out season average) and `by_line_bucket_by_source` reports the `line` and `season` bucket tables separately. The sds in `props-fit-findings.md` come from the first run and need a refit (`python -m rfqbot.props fit`, which also rewrites `props_fit.json` and this file's tables).
- Prices whose last trade is more than 120 min before the 15-min pre-tip offset are dropped from the implied line and counted in `join.stale_priced_lines`.
- `props_fit.json` moved to `claude-sessions/` (nothing imports it); the FINDINGS prose moved to `claude-sessions/props-fit-findings.md`; `load_rows` returns `(rows, diagnostics)`.
