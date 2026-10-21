## High Level Goal:
Calculate Fair value of each marginal, then estimate dependence to find the joint. Quote this FV + margin. 

## More Detailed Implementation Description: 

### Stage 1: listener and storage

- Websocket listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded
- Feed facts: 0.135% of requests trade (fill rate); SELL fills 3.4x more than BUY; median 133s from first request to print; no requester concentration (top 10 = 7%).


### Stage 2: pricing and correlation (NFL and NBA only)

**Fair Value Estimation**: Each Polymarket leg is matched to its Kalshi market. We calcalate the marginal fairs using a precision weighted price (of Poly and Kalshi). Essentially we track change in variance of both venues (these are both tracking the same true probability) - in any window the change in midprice = true change + venues noise. So comparing the two venues gives each venues noise/variance. The error on each fair becomes part of the margin: less confident price, wider quote.

**Dependence:** 
- Legs in different games are independent, so the combo price is just the product (with different markets like Politics, Economics etc these assumptions cannot be made). 
- Legs in the same game are priced off a joint distribution of the final score, reconstructed from the market: the spread ladder gives the margin, the total ladder gives the points, the team-total ladders tie the two team scores together. Fitting all of them at once onto a home-away score grid gives one joint that reproduces every quoted line.
- The fitted grid is a probability table over scores. We draw 50k scores from it and price any combo as the fraction of draws where every leg wins (our dependece value). Worked example of one game's grid, inputs and pair prices: `docs/appendix-joint-grid.md`

Comparing our dependence model against resolved games (by combo type: spread x total, ML x spread, etc..) we apply shrinkage towards independence if our dependence is statistically overestimated.



- Validation on 93 resolved NFL games: Brier 0.130 vs 0.140 for independence; direction right, magnitude ~25% overstated; tails calibrated; 1H coupling overstated. 

### Stage 3: inventory and risk

**Exposure:** Every position, filled/pending, is run through the same 50k scores (above). For each outcome/game-state (team wins, over hits) we define Delta "how much our P&L moves if it happens vs not". A ML has delta 1 to its own team winning. From the same scores we can calculate drawdown and risk metrics. 

**Sizing:** Min(Volume Asked, Exposure Budget Remaining, Quarter Kelly)

**Margin:** We want the margin that maximises expected profit per RFQ = P(fill at margin m) x EV(fill at m). The optimzer picks m to maximise this. 
P(fill) is the fill curve: we compare our logged paper quotes to the actual prints and count how often we would have won at each margin. 

EV per fill is margin minus two costs. (1) Winner's curse: we get filled more precisely when our price is wrong. So conditional on a fill our fair is wrong against us, by more when it is less certain. The cost is that expected error given a fill. (2) Price Drift (Adverse Selection): Implied Fair price moving away from us after fill. 
So EV = Margin - curse - drift.

**Wallets:** on top, each requester gets a profit score from our fills against them. Consistently -EV ones get a premium, or are blocked.

**Skew:** If a quote is +corr to our exposure the margin widens; and vice versa. This skew cannot make us cross FV though. 


### Stage 4: evaluation

**Validation:** The dependence engine is scored on resolved games. On 93 NFL games Brier is 0.130 vs 0.140 asuuming 100% independence. 

**Venues:** On 4.4h of matched pre-game books, Kalshi leads price discovery in both sports. This is expected as Kalshi is more liquid. 

**What the prints say:** P(fill) = 0.135%. Margin makers charge (print vs product of leg fairs at trade time) and the drift of the fair after the fill:

| flow | margin at fill (pt) | drift after fill (pt, positive is good) | net (mean) |
|---|---|---|---|
| we sell, 3+ legs independent | 0.5-0.75  | 0-0.4 | ~+0.8 |
| we sell, same-game | 2.7 | +0.5 | ~+3.2 |
| we buy (requester SELL) | 1.2 | -0.2 to -0.6 (first hour, n 65); -1.4 to -3.0 (7 h, n 16-25) | +0.8 on the first hour; negative on the longer sample |

**Wallet block rule:** 3.5% flase positives at 95% confidence ( we would rather wrongly block neutral wallets than let sharps trade w us)

**Probe RFQs (built, dry-run only):** request our own quote on a combo we just filled, read the price, never accept. Gives the combo's price seconds after our fill and the best competing quote per RFQ. Dry-run on 3 h of prints: 26 triggers, 20 request bodies built, 18 disguised with a 0.99 leg from another game, 0 sent. Live needs the API passphrase and deposit-wallet addresses; never run in production here.

**Paper runs:** one-hour run we wanted to quote ~2,000 RFQs, quoted 947, refused 1,062 for risk. Worst drawdown (bottom 1% of outcomes) less than 7.5k.

**Backtest:** The recorded 4 h window (2.28M RFQs) replayed through the live quoting code on a pinned clock, books read as-of each RFQ, fills from the recorded prints, settled against resolutions. Base run: 19,236 quoted, 21 filled, 17 resolved. Realized P&L +$260 on 17 combos, +$241 mark-to-model, of which +$183 is pricing luck (fair vs outcome) and +$59 margin earned. Only 141 NFL/NBA prints exist in the window and the fills fall in 2 independent game clusters, so nothing here is statistically separable; the mechanical results are:

| setting | filled | model P&L $ | max tail loss $ |
|---|---|---|---|
| base (0.5 pt edge, caps on) | 21 | 241 | 7,661 (15%) |
| independence pricing | 20 | 231 | 7,675 |
| caps off | 19 | 342 | 68,490 (137%) |
| edge 0 / 1 pt / 2 pt | 39 / 10 / 1 | 205 / 384 / -40 | ~7,600 |
| margin table (in-sample) | 13 | 188 | 7,625 |

Fills fall steeply with the edge (39, 21, 10, 1 at 0, 0.5, 1, 2 pt). Caps cost nothing in fills and cut the worst-case loss 9x. Joint vs independence is indistinguishable at this sample. Replayed fairs match the live log (91% within 0.5 pt). Corrections from the replay: trade rate over 4 h is 0.05% of requests (0.135% was the first hour); SELL prints per request are 1.2x BUY, not 3.4x. Needs a second day of flow for an out-of-sample table and any verdict on dependence.



## Design decisions worth stating

- Dependence from the market's own joint, not from a fundamental sports model.
- Independence is certified structurally (different game) before any product pricing
- Confidence on every fair becomes margin; margin terms are logged per RFQ, so attribution is possible.
- Caps are on instantaneous exposure including pending quotes, not on quote count.
- Realized-outcome evidence counted in games, standard errors clustered by game.


## Next Steps in this project (I dont have time)

1. Backtest compare table: joint vs independence, caps on/off, fixed edge vs table.
2. Realized P&L as resolutions arrive over the week.
3. NBA same-game engine.
4. Adverse selection: tilt the fair toward Kalshi when venues disagree (it leads)
5. Last look: makers with ~$2,500 combo volume can apply (form plus a contact at Polymarket) for a confirmation step after their quote is selected: an RFQ_CONFIRMATION_REQUEST arrives and the maker answers CONFIRM or DECLINE before a confirm_by deadline (1 s in the overview). Re-price at selection, decline if the fair moved against us by more than the margin. Declining over 15% in an hour can pause quoting. The standard adverse-selection defence; not available in paper mode.
6. Probe RFQs live once credentials exist; per-RFQ competitor gap into the optimiser.
7. Hedging with singles; strategic feedback damping.
8. Full list: `docs/future-work.md`.
