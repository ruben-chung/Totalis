"""Settings loaded from .env / environment. Fails loudly on missing required fields."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_WS_URL = "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq"
DEFAULT_RFQ_API_URL = "https://combos-rfq-api.polymarket.com"
DEFAULT_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"

# Protocol timing (ms), from docs.polymarket.com/trading/combos
SUBMISSION_WINDOW_MS = 400
ACCEPTANCE_WINDOW_MS = 10_000
LAST_LOOK_WINDOW_MS = 1_000


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    api_key: str
    api_secret: str
    api_passphrase: str
    signer_address: str
    maker_address: str
    signature_type: int
    ws_url: str = DEFAULT_WS_URL
    rfq_api_url: str = DEFAULT_RFQ_API_URL
    db_path: Path = Path("./data/rfq.db")
    raw_log_dir: Path | None = Path("./data/raw")
    raw_db_types: str = "non_request"   # "non_request" | "all" | "none"
    paper_trading: bool = True
    initial_capital_usd: float = 50_000.0
    catalog_refresh_sec: int = 21_600
    expiry_grace_ms: int = 20_000  # measured request->trade latency reaches ~17s
    log_level: str = "INFO"
    # leg order-book recorder (rfqbot books)
    clob_ws_url: str = DEFAULT_CLOB_WS_URL
    gamma_url: str = DEFAULT_GAMMA_URL
    book_db_path: Path = Path("./data/books.db")
    book_log_dir: Path | None = Path("./data/books")
    book_universe_hours: float = 6.0
    book_refresh_sec: int = 60
    book_assets_per_conn: int = 500
    book_min_interval_ms: int = 1_000
    book_change_key: str = "top"
    book_raw_deltas: bool = False
    book_game_sports: tuple[str, ...] = ("nfl", "nba")   # whole-game ladders for these; others leg-level
    book_game_max_tokens: int = 20_000
    book_game_lookahead_hours: float = 72.0
    missing: list[str] = field(default_factory=list)
    # ---- Kalshi price feed (rfqbot kalshi_books; owned by the NBA/Kalshi-feed session) -------------------
    kalshi_api_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    kalshi_key_id: str = ""                       # blank = unauthenticated (public reads, slower sweep)
    kalshi_private_key_path: Path | None = None   # PEM file; read only by the signer, never logged
    kalshi_books_db_path: Path = Path("./data/kalshi_books.db")
    kalshi_log_dir: Path | None = Path("./data/kalshi")
    kalshi_sweep_sec: float = 15.0                # batch top-of-book sweep period (authenticated)
    kalshi_sweep_sec_unauth: float = 60.0         # sweep period without keys
    kalshi_depth_sec: float = 30.0                # /orderbook polls for matched tickers in recent RFQ flow
    kalshi_match_refresh_sec: float = 600.0       # leg_matches rebuild timer (also after each kalshi-sync)
    kalshi_ws: bool = True                        # ticker websocket when keys are present
    # ---- end Kalshi price feed block ---------------------------------------------------------------------

    @property
    def has_auth(self) -> bool:
        return not self.missing

    @property
    def auth_message(self) -> dict | None:
        """The auth frame, or None when credentials are incomplete (feed is readable unauthenticated)."""
        if not self.has_auth:
            return None
        return {
            "type": "auth",
            "auth": {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            },
            "identity": {
                "signer_address": self.signer_address,
                "maker_address": self.maker_address,
                "signature_type": self.signature_type,
            },
        }

    def require_auth(self) -> None:
        """Raise ConfigError naming every field needed to authenticate that is blank."""
        if self.missing:
            raise ConfigError(
                "Missing required .env values: " + ", ".join(self.missing)
                + ". Fill them in .env (see .env.example)."
            )


def _env(name: str, default: str | None = None) -> str:
    return os.environ.get(name, default if default is not None else "").strip()


def _raw_log_dir(value: str | None) -> Path | None:
    """RAW_LOG_DIR: "none" (any case) turns the raw log off; empty falls back to ./data/raw like the other fields."""
    v = (value or "").strip()
    if v.lower() == "none":
        return None
    return Path(v or "./data/raw")


def load_settings(env_file: str | os.PathLike | None = ".env", *, require_auth: bool = False) -> Settings:
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    required = {
        "POLYMARKET_API_KEY": _env("POLYMARKET_API_KEY"),
        "POLYMARKET_API_SECRET": _env("POLYMARKET_API_SECRET"),
        "POLYMARKET_API_PASSPHRASE": _env("POLYMARKET_API_PASSPHRASE"),
        "POLYMARKET_SIGNER_ADDRESS": _env("POLYMARKET_SIGNER_ADDRESS"),
        "POLYMARKET_MAKER_ADDRESS": _env("POLYMARKET_MAKER_ADDRESS"),
    }
    missing = [k for k, v in required.items() if not v]

    paper = _env("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
    if not paper:
        raise ConfigError("PAPER_TRADING=false is not supported yet: live quote submission is not implemented.")

    s = Settings(
        api_key=required["POLYMARKET_API_KEY"],
        api_secret=required["POLYMARKET_API_SECRET"],
        api_passphrase=required["POLYMARKET_API_PASSPHRASE"],
        signer_address=required["POLYMARKET_SIGNER_ADDRESS"],
        maker_address=required["POLYMARKET_MAKER_ADDRESS"],
        signature_type=int(_env("POLYMARKET_SIGNATURE_TYPE", "3") or 3),
        ws_url=_env("POLYMARKET_RFQ_WS_URL", DEFAULT_WS_URL) or DEFAULT_WS_URL,
        rfq_api_url=_env("POLYMARKET_COMBOS_RFQ_API_URL", DEFAULT_RFQ_API_URL) or DEFAULT_RFQ_API_URL,
        db_path=Path(_env("RFQ_DB_PATH", "./data/rfq.db") or "./data/rfq.db"),
        raw_log_dir=_raw_log_dir(_env("RAW_LOG_DIR", "./data/raw")),
        raw_db_types=_env("RAW_DB_TYPES", "non_request").lower() or "non_request",
        paper_trading=paper,
        initial_capital_usd=float(_env("INITIAL_CAPITAL_USD", "50000") or 50000),
        catalog_refresh_sec=int(_env("CATALOG_REFRESH_SEC", "21600") or 21600),
        expiry_grace_ms=int(_env("EXPIRY_GRACE_MS", "20000") or 20000),
        log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        clob_ws_url=_env("POLYMARKET_CLOB_WS_URL", DEFAULT_CLOB_WS_URL) or DEFAULT_CLOB_WS_URL,
        gamma_url=_env("POLYMARKET_GAMMA_URL", DEFAULT_GAMMA_URL) or DEFAULT_GAMMA_URL,
        book_db_path=Path(_env("BOOK_DB_PATH", "./data/books.db") or "./data/books.db"),
        book_log_dir=(Path(_env("BOOK_LOG_DIR", "./data/books")) if _env("BOOK_LOG_DIR", "./data/books").lower() != "none" else None),
        book_universe_hours=float(_env("BOOK_UNIVERSE_HOURS", "6") or 6),
        book_refresh_sec=int(_env("BOOK_REFRESH_SEC", "60") or 60),
        book_assets_per_conn=int(_env("BOOK_ASSETS_PER_CONN", "500") or 500),
        book_min_interval_ms=int(_env("BOOK_MIN_INTERVAL_MS", "1000") or 1000),
        book_change_key=_env("BOOK_CHANGE_KEY", "top").lower() or "top",
        book_raw_deltas=_env("BOOK_RAW_DELTAS", "false").lower() in ("1", "true", "yes"),
        book_game_sports=tuple(x.strip().lower() for x in _env("BOOK_GAME_SPORTS", "nfl,nba").split(",") if x.strip()),
        book_game_max_tokens=int(_env("BOOK_GAME_MAX_TOKENS", "20000") or 20000),
        book_game_lookahead_hours=float(_env("BOOK_GAME_LOOKAHEAD_HOURS", "72") or 72),
        missing=missing,
        # ---- Kalshi price feed block (append only; see Settings) ----
        kalshi_api_url=_env("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2") or "https://api.elections.kalshi.com/trade-api/v2",
        kalshi_ws_url=_env("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2") or "wss://api.elections.kalshi.com/trade-api/ws/v2",
        kalshi_key_id=_env("KALSHI_KEY_ID"),
        kalshi_private_key_path=(Path(os.path.expanduser(_env("KALSHI_PRIVATE_KEY_PATH"))) if _env("KALSHI_PRIVATE_KEY_PATH") else None),
        kalshi_books_db_path=Path(_env("KALSHI_BOOKS_DB_PATH", "./data/kalshi_books.db") or "./data/kalshi_books.db"),
        kalshi_log_dir=(Path(_env("KALSHI_LOG_DIR", "./data/kalshi")) if _env("KALSHI_LOG_DIR", "./data/kalshi").lower() != "none" else None),
        kalshi_sweep_sec=float(_env("KALSHI_SWEEP_SEC", "15") or 15),
        kalshi_sweep_sec_unauth=float(_env("KALSHI_SWEEP_SEC_UNAUTH", "60") or 60),
        kalshi_depth_sec=float(_env("KALSHI_DEPTH_SEC", "30") or 30),
        kalshi_match_refresh_sec=float(_env("KALSHI_MATCH_REFRESH_SEC", "600") or 600),
        kalshi_ws=_env("KALSHI_WS", "true").lower() in ("1", "true", "yes"),
        # ---- end Kalshi price feed block ----
    )
    if require_auth:
        s.require_auth()
    return s
