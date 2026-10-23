
## Stage 1: listener and storage

- Websocket listener, verbatim raw log, SQLite store, derived expiry, replay. ~190 RFQ/s recorded


## Stage 2: pricing and correlation (NFL and NBA only)

**Fair Value Estimation**: 
- Each Polymarket leg is matched to its Kalshi market. We calculate the marginal fairs using a precision weighted price (of Poly and Kalshi). 
- Essentially we track change in variance of both venues (these are both tracking the same true probability) - in any window the change in midprice = true change + venues noise. So comparing the two venues gives each venues noise/variance. 
- The error on each fair becomes part of the margin: less confident price, wider quote.

**Dependence:** 
- Legs in different games are independent, so the combo price is just the product (with different markets like Politics, Economics etc these assumptions cannot be made). 
- Legs in the same game are priced off a joint distribution of the final score, reconstructed from the market: the spread ladder gives the margin, the total ladder gives the points, the team-total ladders tie the two team scores together. Fitting all of them at once onto a home-away score grid gives one joint that reproduces every quoted line.
- The fitted grid is a probability table over scores. Full-game combos are priced exactly on it (the mass of cells where every leg wins); combos with a half-game leg are priced on 50k scores drawn from it. 

Comparing our dependence model against resolved games (by combo type: spread x total, ML x spread, etc..) we apply shrinkage towards independence if our dependence is statistically overestimated. The calibration map is continuous in the leg probability (hinge tails with variance floors), so a quote never jumps at a bucket edge, which a requester could otherwise exploit by asking twice.

Validation on 93 resolved NFL games: Brier 0.130 vs 0.140 for assumed independence

## Stage 3: inventory and risk

**Exposure:** 
- Every position, filled/pending, is run through the same 50k scores (above). For each outcome/game-state (team wins, over hits) we define Delta "how much our P&L moves if event happens or not". A ML has delta 1 to its own team winning. From the same scores we can calculate drawdown and risk metrics. 

**Sizing:** 
- Min(Volume Asked, Exposure Budget Remaining, Quarter Kelly)

**Margin:** 
- We want the margin that maximises expected profit per RFQ = P(fill at margin m) x EV(fill at m). The optimiser picks m to maximise this and updates dynamically. 
- P(fill) is the fill curve: we compare our logged paper quotes to the actual prints and count how often we would have won at each margin. See Appendix B. 

- EV per fill is margin minus two costs. (1) Winner's curse: we get filled more precisely when our price is wrong. So conditional on a fill our fair is wrong against us, by more when it is less certain. The cost is that expected error given a fill. (2) Price Drift (Adverse Selection): Implied Fair price moving away from us after fill. So EV = Margin - curse - drift.

**Wallets:** 
- each requester gets a profit score from our fills against them. Consistently -EV ones get a margin premium, or are blocked.

**Skew:** 
- If a quote is +corr to our exposure the margin widens; and vice versa. This skew cannot make us cross FV. 


## Stage 4: evaluation

**Validation:** The dependence engine is scored on resolved games. On 93 NFL games Brier is 0.130 vs 0.140 assuming 100% independence. 

**Venues:** On 4.4h of matched pre-game books, Kalshi leads price discovery in both sports. This is expected as Kalshi is more liquid. 

**What the prints say:** P(fill) = 0.135%. Margin makers charge (print vs product of leg fairs at trade time) and the drift of the fair after the fill:

**Wallet block rule:** 3.5% false positives at 95% confidence (we would rather wrongly block neutral wallets than let sharps trade w us)

**Probe RFQs (built, dry-run only):** request our own quote on a combo we just filled, read the price, never accept. Gives the combo's price seconds after our fill and the best competing quote per RFQ. Dry-run on 3 h of prints: 26 triggers, 20 request bodies built, 18 disguised with a 0.99 leg from another game, 0 sent. Live needs the API passphrase and deposit-wallet addresses; never run in production here.

**Backtest (paper, replayed on 13 h of recorded flow on Wednesday):**
The model trades all NFL and any NBA that isnt SGP. 

| | |
|---|---|
| NFL/NBA RFQs evaluated | 56,270 over 58 games |
| fill rate on quoted | 0.09% |
| shares filled | 1,675 |
| capital deployed | $997 |
| EV per contract = margin + drift | ~1.5ct |
| worst-case loss (1% tail) | $6.3k, 12.5% of capital, under the 15% cap |

## Design decisions 

- Dependence from the market's own joint, not from a fundamental sports model.
- Independence is structural (different game, not backed out of a model output)
- Confidence on every fair is expressed in margin; margin terms are logged per RFQ, so attribution is possible.
- Caps are on instantaneous exposure including pending quotes, not on quote count.
- Realized-outcome evidence counted in games, standard errors clustered by game.


## Next Steps in this project (I dont have time)

The five I would do first:

1. **NBA same-game engine.** same dependence modelling we did for NFL on NBA + Also expand to more different markets. And also maybe work on a more fundamental model to model dependence which can be blended with the current one. 
2. **Adverse selection on the fair.** Kalshi leads price discovery, so tilt the fair toward Kalshi when venues disagree instead of only widening. Implement a "Last-Look" feature: re-price at selection, decline if the fair moved against us by more than the margin.
3. **Probe RFQs live, and the per-RFQ curse.** Each probe gives the combo's price right after our fill and the best competing quote. This gives us very useful data for analysis. This can also be disguised combining w a 0.99 leg.
4. **Hedging with singles.** The outcome deltas in the risk layer are already the per-leg hedge ratios. Hedge a fill on the CLOB or Kalshi when spread plus fees is below the drop in marginal tail risk; the dependence residual stays. This also raises capacity, since ~half of intended quotes were refused by the risk budget.
5. **Dynamic Fill Curves** due to game theory our fill curve will change over time and our optimization function needs to account for this. 

