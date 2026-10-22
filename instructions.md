# Polymarket RFQ Paper-Trading Bot

Build a paper-trading bot that listens for Polymarket RFQs, prices combos, manages inventory, and evaluates its performance through historical replay.

## 1. RFQ listener and storage

- Listen for new RFQs and lifecycle updates.
- Reconnect safely after a disconnect.
- Store raw messages and normalized records.
- Capture RFQs, legs, timestamps, draft quote revisions, cancellations, expirations, executions, and settlements.

## 2. Pricing and correlation

- Estimate a fair probability for every leg.
- Model dependence between legs rather than assuming independence.
- Explain how correlations are estimated.
- Produce a fair combo price, quoted price, size, expected edge, and a short explanation of each pricing adjustment.

## 3. Inventory and risk management

Assume $50k of initial capital.

- Track pending and executed exposure across individual markets and events, as well as total portfolio risk.
- The bot should widen, skew, reduce, or reject quotes as inventory grows.

## 4. Backtest

Replay the collected RFQ dataset without using future information.

Report:

- RFQs received, quoted, rejected, expired, and executed
- Quote and execution rates
- Expected and realized P&L
- Maximum downswing, maximum upswing
- Inventory and correlated exposure over time
- Results by market type and combo size
- Sensitivity to correlation
