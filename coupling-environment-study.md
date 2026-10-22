# Idea, not developed: environment-stratified coupling study
**Question.** The engine reads each game's margin-to-total coupling from its ladders. Validating
that against realized outcomes on 93 games is underpowered for a correlation of ~0.1.

**Study.** Final scores plus closing spread and total for every NFL game over many seasons.
Stratify by spread bucket x total bucket (and favourite side). Per bucket: realized correlation of
margin and total, joint frequency of (favourite covers, over) against the product of marginals,
same for team totals and halves; one game is one observation.

**Output.** A table coupling(spread, total) with intervals, used to flag live games whose fitted
coupling departs from their environment, as a prior for thin team-total ladders, and as the halves
prior, which today is ours rather than the market's.

**Why deferred.** Coupling varies with environment and era, half and quarter line history is hard
to source, and it is its own project. The bot keeps market-pinned coupling at lambda 1.0 unless
game-clustered evidence rejects it.
