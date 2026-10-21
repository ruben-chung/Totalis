"""Stage 4: replay the recorded RFQ flow through the quoting path with the books as they were.

    python -m rfqbot.backtest run     --start ... --end ...        # one setting
    python -m rfqbot.backtest compare --start ... --end ...        # the design switches, one pass over the window
    python -m rfqbot.backtest settle  --run <id>                   # re-runnable as resolutions arrive
    python -m rfqbot.backtest report  --run <id>

Everything reads the live stores with mode=ro and writes only data/backtest.db. See docs/backtest-notes.md.
"""
