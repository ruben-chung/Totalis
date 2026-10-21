# Appendix: the joint score grid for one game

Game `nfl-cin-bal-2026-10-25` (CIN at BAL), Polymarket full-game ladders as recorded on 2026-10-21 (pre-game). 800 markets in the game, 646 two-sided. Produced by `python -m rfqbot joint --game nfl-cin-bal-2026-10-25`; this page by a 60-line script over the same model object.

## 1. Inputs: what the market quotes

Each ladder is a set of lines with a market probability (mid of the two-sided book) and an error (half-spread plus thinness). Only the lines near the money are shown.

**spread (margin = BAL minus CIN; p = P(margin > line))**

| line | -1.5 | +0.0 | +1.5 | +2.5 | +3.5 | +4.5 | +5.5 |
|---|---|---|---|---|---|---|---|
| p | 0.600 | 0.575 | 0.550 | 0.515 | 0.430 | 0.400 | 0.380 |
| err | 0.020 | 0.012 | 0.013 | 0.008 | 0.013 | 0.014 | 0.012 |

**total (p = P(total > line))**

| line | 46.5 | 48.5 | 50.5 | 52.5 | 54.5 | 56.5 | 58.5 |
|---|---|---|---|---|---|---|---|
| p | 0.640 | 0.585 | 0.525 | 0.465 | 0.420 | 0.375 | 0.290 |
| err | 0.050 | 0.014 | 0.012 | 0.016 | 0.026 | 0.024 | 0.050 |

**team total BAL (p = P(BAL points > line))**

| line | 16.5 | 19.5 | 20.5 | 23.5 | 26.5 | 27.5 | 30.5 |
|---|---|---|---|---|---|---|---|
| p | 0.905 | 0.830 | 0.745 | 0.650 | 0.530 | 0.475 | 0.340 |
| err | 0.085 | 0.090 | 0.045 | 0.060 | 0.050 | 0.035 | 0.060 |

**team total CIN (p = P(CIN points > line))**

| line | 13.5 | 16.5 | 19.5 | 20.5 | 23.5 | 27.5 | 30.5 |
|---|---|---|---|---|---|---|---|
| p | 0.905 | 0.845 | 0.730 | 0.660 | 0.545 | 0.345 | 0.250 |
| err | 0.075 | 0.085 | 0.060 | 0.050 | 0.045 | 0.055 | 0.060 |

The spread ladder pins the distribution of the margin, the total ladder the distribution of the sum, the two team-total ladders the distribution of each team's points. None of them says how the two team scores move together; that is what fitting all four onto one grid recovers.

## 2. The fitted grid

Probability of each (home, away) score bucket after fitting the four ladders at once (iterative proportional fitting on an 81 x 81 score grid, shown here in 7-point buckets). Rows are BAL points, columns are CIN points, cells in % of probability.

| BAL \ CIN | 0-6 | 7-13 | 14-20 | 21-27 | 28-34 | 35-41 | 42+ | row sum |
|---|---|---|---|---|---|---|---|---|
| 0-6 | 0.4 | 0.9 | 1.2 | 0.1 | 0.6 | 2.0 | 0.2 | 5.5 |
| 7-13 | 0.9 | 0.7 | 0.7 | 0.4 | 0.2 | 1.6 | 0.3 | 4.9 |
| 14-20 | 0.9 | 0.1 | 5.4 | 7.0 | 1.2 | 0.2 | 0.3 | 15.1 |
| 21-27 | 0.3 | 1.2 | 7.7 | 8.2 | 4.7 | 4.9 | 0.0 | 27.0 |
| 28-34 | 0.5 | 0.8 | 2.6 | 7.1 | 1.1 | 1.0 | 0.1 | 13.3 |
| 35-41 | 1.8 | 1.4 | 5.1 | 8.4 | 1.1 | 5.6 | 2.9 | 26.3 |
| 42+ | 0.3 | 0.4 | 0.6 | 0.3 | 0.2 | 4.1 | 2.0 | 7.9 |
| col sum | 5.1 | 5.5 | 23.4 | 31.5 | 9.2 | 19.4 | 5.9 | 100 |

Means BAL 27.3, CIN 25.2; sd 10.8 and 10.4; correlation of the two team scores +0.240. The row and column sums are the team-total ladders; the diagonals carry the spread and total.

## 3. Where it differs from independence

Same buckets, grid minus the product of its own row and column sums, in percentage points. Positive means the market puts more weight on that score pair than two independent team scores would.

| BAL \ CIN | 0-6 | 7-13 | 14-20 | 21-27 | 28-34 | 35-41 | 42+ |
|---|---|---|---|---|---|---|---|
| 0-6 | +0.2 | +0.6 | -0.0 | -1.6 | +0.1 | +0.9 | -0.1 |
| 7-13 | +0.7 | +0.5 | -0.4 | -1.1 | -0.2 | +0.7 | +0.0 |
| 14-20 | +0.1 | -0.7 | +1.9 | +2.2 | -0.2 | -2.7 | -0.6 |
| 21-27 | -1.1 | -0.3 | +1.4 | -0.3 | +2.2 | -0.3 | -1.5 |
| 28-34 | -0.2 | +0.1 | -0.5 | +2.9 | -0.1 | -1.6 | -0.7 |
| 35-41 | +0.5 | -0.1 | -1.0 | +0.1 | -1.3 | +0.5 | +1.4 |
| 42+ | -0.1 | -0.1 | -1.3 | -2.2 | -0.5 | +2.6 | +1.6 |

## 4. What a combo costs on this grid

A combo wins on a score pair if every leg wins there. Its price is the probability mass of those pairs. Product = the two leg prices multiplied (independence); joint = read off the grid.

| leg A | leg B | p_a | p_b | product | joint | joint / product |
|---|---|---|---|---|---|---|
| BAL -2.5 | total 52.5 over | 0.519 | 0.467 | 0.242 | 0.260 | 1.07 |
| BAL -2.5 | total 52.5 under | 0.519 | 0.533 | 0.276 | 0.258 | 0.93 |
| BAL -2.5 | BAL team total 27.5 over | 0.519 | 0.476 | 0.247 | 0.362 | 1.47 |
| BAL -2.5 | CIN team total 26.5 under | 0.519 | 0.501 | 0.260 | 0.373 | 1.44 |
| total 52.5 over | BAL team total 27.5 over | 0.467 | 0.476 | 0.222 | 0.375 | 1.68 |
| BAL moneyline | BAL -8.5 | 0.580 | 0.262 | 0.152 | 0.262 | 1.72 |
| BAL moneyline | CIN moneyline | 0.580 | 0.424 | 0.246 | 0.004 | 0.02 |

Reading the table: the favourite covering and the game going over sit on the same side of the grid, so the market prices that combo above the product; favourite covers with the under sits below it. Moneyline with its own spread is nearly the spread alone; both moneylines together is only the tie (0.4%), since they are one market. The per-pair calibration layer then scales these joint-minus-product gaps by what resolved games have shown for that market-type pair.
