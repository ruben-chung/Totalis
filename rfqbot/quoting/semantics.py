"""Combo RFQ semantics, pinned from the Polymarket Combos docs and our recorded frames (2026-10-20).

What a combo position pays (docs, trading/positions/combinatorial): "a combinatorial YES
position pays out only if every leg wins. If any leg loses, the corresponding combinatorial NO
position pays out." YES + NO are fully collateralised (1 pUSD per share pair), so
``p(combo YES) = P(all legs' selected positions resolve YES)``.

side / direction (docs, trading/combos/requesters + market-makers): "`side` must currently be
`"YES"`"; "A quote request can currently only buy or sell the YES side of a Combo." The
requester's ``direction`` is from the requester's point of view, so the maker takes the other
side. Maker hedge table in the docs: Buy YES -> maker "Sell YES at `price`" (or buy NO at
``1 - price`` from collateral); Sell YES -> maker "Buy YES at `price`" (or sell NO). Recorded
data: every one of 514k RFQs and 674 trades has side YES (rfqs: BUY/notional 473,538, SELL/shares
40,587, 4 blank; trades BUY 534 / SELL 140).

requested_size (docs): "`notional`: the target value of the request in collateral currency ...
always and only used by BUY requests" ("A BUY sets the maximum collateral budget, including
fees"); "`shares`: the target number of Combo outcome tokens ... always and only used by SELL
requests". ``value_e6`` is 6-decimal base units of pUSD (notional) or of shares. Recorded data
agrees: over 544 BUY trades ``size_e6 * price_e6 / 1e6`` is 0.917-1.000 x the requested notional
(median 0.965; the shortfall is the fee allowance), over 143 SELL trades ``size_e6`` is 0.997-1.000 x
the requested shares. Derived share size for a BUY (docs): ``floor(value_e6 * 1e6 / price_e6)``.

Our quote (docs, market-makers): "Price the request as pUSD per YES Combo share" ->
``price_e6`` is always the price of the YES combo per share in pUSD base units, whichever
direction; ``size_e6`` is in share base units (1 share = 1,000,000). A BUY quote above the
safety maximum 0.90909 is rejected (``QUOTED_PRICE_ABOVE_SAFETY_THRESHOLD``), any price above 1
is ``QUOTED_PRICE_OUT_OF_RANGE``, zero/negative ``PRICE_E6_NOT_POSITIVE``. Tick size is not
documented; we round to ``QUOTE_TICK`` (default 0.001) away from fair. RFQ_TRADE frames carry
``price_e6`` (blended fill price) and ``size_e6`` (matched shares).

The ~0.999 SELL prints (62 of 143 SELL trades at 0.90-1.00): every leg of every such combo is a
game played on the recording day (27/27 legs dated 2026-10-20, e.g. Liverpool / Roma / Man City
to win, all already decided). Requesters are selling combos that are effectively settled but not
yet redeemable; a maker buying YES at 0.999 earns the remaining 0.1% at redemption. They are
liquidity exits, not mispricings.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..slugs import E6

SIDE_YES = "YES"
BUY, SELL = "BUY", "SELL"
MAKER_SELLS_YES, MAKER_BUYS_YES = "sell_yes", "buy_yes"
BUY_SAFETY_MAX = 0.90909      # docs: BUY quotes above this are rejected
SHARE_E6 = E6                 # share base units (1 share = 1,000,000); alias of the protocol's fixed-point scale


class UnsupportedRfq(ValueError):
    pass


@dataclass(frozen=True)
class MakerView:
    direction: str            # requester's direction
    maker_action: str         # sell_yes | buy_yes
    size_unit: str            # notional | shares

    @property
    def maker_sells(self) -> bool:
        return self.maker_action == MAKER_SELLS_YES


def maker_view(direction: str, side: str, size_unit: str) -> MakerView:
    """Which side the maker takes. Raises UnsupportedRfq for anything the docs say cannot occur."""
    if side != SIDE_YES:
        raise UnsupportedRfq(f"side {side!r} (docs: side must be YES)")
    if direction == BUY:
        if size_unit not in ("notional", ""):
            raise UnsupportedRfq(f"BUY with unit {size_unit!r} (docs: BUY uses notional)")
        return MakerView(direction, MAKER_SELLS_YES, "notional")
    if direction == SELL:
        if size_unit not in ("shares", ""):
            raise UnsupportedRfq(f"SELL with unit {size_unit!r} (docs: SELL uses shares)")
        return MakerView(direction, MAKER_BUYS_YES, "shares")
    raise UnsupportedRfq(f"direction {direction!r}")


def requested_shares_e6(unit: str, value_e6: int, price_e6: int) -> int:
    """Share size the requester asks for, given our price: notional / price for BUY, shares for SELL."""
    if price_e6 <= 0:
        raise ValueError("price must be positive")
    if unit == "notional":
        return (value_e6 * SHARE_E6) // price_e6
    return int(value_e6)


def notional_e6(shares_e6: int, price_e6: int) -> int:
    return (shares_e6 * price_e6) // SHARE_E6
