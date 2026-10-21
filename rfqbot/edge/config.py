"""Margin optimiser configuration (env ``EDGE_<KEY>``; see docs/margin-optimizer-brief.md)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class EdgeConfig:
    decisions_db: Path = Path("./data/decisions.db")    # quote assembly's decision log (QUOTE_DECISIONS_DB)
    rfq_db: Path = Path("./data/rfq.db")                # recording listener: trades, rfqs, requesters, markets
    books_db: Path = Path("./data/books.db")
    kalshi_db: Path = Path("./data/kalshi_books.db")
    edge_db: Path = Path("./data/edge.db")              # fills, curves, markouts, wallet scores
    wallet_list: Path = Path("./data/edge_wallets.json")  # manual allow / deny list
    table_path: Path = HERE / "margin_table.json"       # the optimiser's output, read by margin_for()
    sports: tuple[str, ...] = ("nfl", "nba")            # sports quote assembly prices
    grid_max: float = 0.10                              # markup grid 0 .. grid_max in grid_step (probability points)
    grid_step: float = 0.0025
    sigma_cuts: tuple[float, ...] = (0.005, 0.009)      # sigma = u_m + u_d buckets: s0 < cut0 <= s1 < cut1 <= s2
    n_min: int = 30                                     # decisions a cell needs before it stops borrowing from its parent
    min_printed: int = 5                                # printed decisions a node needs for a delta estimate
    horizons_min: tuple[int, ...] = (1, 5, 15, 30)
    block_min_fills: int = 8                            # EDGE_BLOCK_MIN_FILLS
    block_days: float = 7.0                             # EDGE_BLOCK_DAYS
    wallet_k: float = 1.0                               # EDGE_WALLET_K: premium = k * max(0, -expected profit)
    shrink_n: float = 8.0                               # pseudo-fills pulling a wallet's mean toward the population
    block_conf: float = 0.90                            # one-sided confidence of the block rule's upper bound
    min_fill_rate: float = 0.0                          # EDGE_MIN_FILL_RATE: F(m*) >= this (0 = unconstrained)
    min_fill_basis: str = "print"                       # print: F among RFQs that traded | request: F per request
    curse_source: str = "model"                         # model | measured: E[error | win] used by the optimiser
    default_edge: float = 0.005                         # QUOTE_BASE_EDGE fallback when no table / node applies
    refresh_sec: float = 300.0                          # monitor loop period
    kalshi_mode: str = "auto"                           # passed to marginals.compute_game

    def grid(self) -> list[float]:
        n = int(round(self.grid_max / self.grid_step))
        return [round(i * self.grid_step, 6) for i in range(n + 1)]

    def to_json(self) -> str:
        return json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items()}, sort_keys=True)


def _f(env: dict[str, str], key: str, default: float) -> float:
    v = env.get(f"EDGE_{key}")
    return float(v) if v not in (None, "") else default


def _p(env: dict[str, str], key: str, default: Path) -> Path:
    v = env.get(f"EDGE_{key}")
    return Path(v) if v else default


def load_edge_config(env_file: str | os.PathLike | None = ".env", env: dict[str, str] | None = None) -> EdgeConfig:
    if env is None:
        if env_file is not None and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)
    b = EdgeConfig()
    cuts = b.sigma_cuts
    if env.get("EDGE_SIGMA_CUTS"):
        cuts = tuple(sorted(float(x) for x in env["EDGE_SIGMA_CUTS"].split(",") if x.strip()))
    horizons = b.horizons_min
    if env.get("EDGE_HORIZONS_MIN"):
        horizons = tuple(sorted(int(x) for x in env["EDGE_HORIZONS_MIN"].split(",") if x.strip()))
    return EdgeConfig(
        decisions_db=Path(env.get("QUOTE_DECISIONS_DB") or env.get("EDGE_DECISIONS_DB") or b.decisions_db),
        rfq_db=_p(env, "RFQ_DB", Path(env.get("RFQ_DB_PATH") or b.rfq_db)),
        books_db=_p(env, "BOOKS_DB", Path(env.get("BOOK_DB_PATH") or b.books_db)),
        kalshi_db=_p(env, "KALSHI_DB", Path(env.get("KALSHI_BOOKS_DB_PATH") or b.kalshi_db)),
        edge_db=_p(env, "DB", b.edge_db), wallet_list=_p(env, "WALLET_LIST", b.wallet_list),
        table_path=_p(env, "TABLE_PATH", b.table_path),
        sports=tuple(s.strip() for s in (env.get("EDGE_SPORTS") or env.get("QUOTE_SPORTS") or ",".join(b.sports)).split(",") if s.strip()),
        grid_max=_f(env, "GRID_MAX", b.grid_max), grid_step=_f(env, "GRID_STEP", b.grid_step), sigma_cuts=cuts,
        n_min=int(_f(env, "N_MIN", b.n_min)), min_printed=int(_f(env, "MIN_PRINTED", b.min_printed)), horizons_min=horizons,
        block_min_fills=int(_f(env, "BLOCK_MIN_FILLS", b.block_min_fills)), block_days=_f(env, "BLOCK_DAYS", b.block_days),
        wallet_k=_f(env, "WALLET_K", b.wallet_k), shrink_n=_f(env, "SHRINK_N", b.shrink_n), block_conf=_f(env, "BLOCK_CONF", b.block_conf),
        min_fill_rate=_f(env, "MIN_FILL_RATE", b.min_fill_rate),
        min_fill_basis=(env.get("EDGE_MIN_FILL_BASIS") or b.min_fill_basis).lower(),
        curse_source=(env.get("EDGE_CURSE_SOURCE") or b.curse_source).lower(),
        default_edge=_f(env, "DEFAULT_EDGE", float(env.get("QUOTE_BASE_EDGE") or b.default_edge)),
        refresh_sec=_f(env, "REFRESH_SEC", b.refresh_sec), kalshi_mode=(env.get("EDGE_KALSHI_MODE") or b.kalshi_mode),
    )
