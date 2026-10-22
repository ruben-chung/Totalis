"""Settings loaded from .env / environment. Fails loudly on missing required fields."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq"
DEFAULT_RFQ_API_URL = "https://combos-rfq-api.polymarket.com"
DEFAULT_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Protocol timing (ms), from docs.polymarket.com/trading/combos
SUBMISSION_WINDOW_MS = 400
ACCEPTANCE_WINDOW_MS = 10_000
LAST_LOOK_WINDOW_MS = 1_000
# Grace after the protocol windows before an RFQ with no execution update is derived EXPIRED:
# the measured request -> trade latency reaches ~17 s. One value for the listener, the replay, the
# normalizer and the quoter's live book.
DEFAULT_EXPIRY_GRACE_MS = 20_000


def expiry_horizon_ms(grace_ms: int = DEFAULT_EXPIRY_GRACE_MS) -> int:
    """How long after its submission deadline an RFQ is still considered live: acceptance + last look + grace."""
    return ACCEPTANCE_WINDOW_MS + LAST_LOOK_WINDOW_MS + int(grace_ms)


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
    initial_capital_usd: float = 50_000.0
    catalog_refresh_sec: int = 21_600
    expiry_grace_ms: int = DEFAULT_EXPIRY_GRACE_MS
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
    # Kalshi price feed (rfqbot kalshi_books)
    kalshi_api_url: str = DEFAULT_KALSHI_API_URL
    kalshi_ws_url: str = DEFAULT_KALSHI_WS_URL
    kalshi_key_id: str = ""                       # blank = unauthenticated (public reads, slower sweep)
    kalshi_private_key_path: Path | None = None   # PEM file; read only by the signer, never logged
    kalshi_books_db_path: Path = Path("./data/kalshi_books.db")
    kalshi_log_dir: Path | None = Path("./data/kalshi")
    kalshi_sweep_sec: float = 15.0                # batch top-of-book sweep period (authenticated)
    kalshi_sweep_sec_unauth: float = 60.0         # sweep period without keys
    kalshi_depth_sec: float = 30.0                # /orderbook polls for matched tickers in recent RFQ flow
    kalshi_match_refresh_sec: float = 600.0       # leg_matches rebuild timer (also after each kalshi-sync)
    kalshi_ws: bool = True                        # ticker websocket when keys are present

    @property
    def has_auth(self) -> bool:
        return not self.missing

    @property
    def horizon_ms(self) -> int:
        return expiry_horizon_ms(self.expiry_grace_ms)

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


# -- typed environment readers: each default is written once, at the Settings field -------------
def _env(name: str, default: str | None = None) -> str:
    return os.environ.get(name, default if default is not None else "").strip()


def _str(name: str, default: str) -> str:
    return _env(name) or default


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _path(name: str, default: Path | None, *, none_word: bool = False, expand: bool = False) -> Path | None:
    """Path setting; with ``none_word`` the literal "none" (any case) turns the feature off, an
    empty value keeps the default (``_raw_log_dir`` semantics)."""
    v = _env(name)
    if none_word and v.lower() == "none":
        return None
    if not v:
        return default
    return Path(os.path.expanduser(v) if expand else v)


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

    if not _bool("PAPER_TRADING", True):
        # live quote submission is not implemented; the flag has no effect and the bot always papers
        log.warning("PAPER_TRADING=false ignored: live quote submission is not implemented, running in paper mode")

    d = Settings.__dataclass_fields__

    def default(name: str):
        return d[name].default
    s = Settings(
        api_key=required["POLYMARKET_API_KEY"],
        api_secret=required["POLYMARKET_API_SECRET"],
        api_passphrase=required["POLYMARKET_API_PASSPHRASE"],
        signer_address=required["POLYMARKET_SIGNER_ADDRESS"],
        maker_address=required["POLYMARKET_MAKER_ADDRESS"],
        signature_type=_int("POLYMARKET_SIGNATURE_TYPE", 3),
        ws_url=_str("POLYMARKET_RFQ_WS_URL", default("ws_url")),
        rfq_api_url=_str("POLYMARKET_COMBOS_RFQ_API_URL", default("rfq_api_url")),
        db_path=_path("RFQ_DB_PATH", default("db_path")),
        raw_log_dir=_raw_log_dir(_env("RAW_LOG_DIR", str(default("raw_log_dir")))),
        raw_db_types=_str("RAW_DB_TYPES", default("raw_db_types")).lower(),
        initial_capital_usd=_float("INITIAL_CAPITAL_USD", default("initial_capital_usd")),
        catalog_refresh_sec=_int("CATALOG_REFRESH_SEC", default("catalog_refresh_sec")),
        expiry_grace_ms=_int("EXPIRY_GRACE_MS", default("expiry_grace_ms")),
        log_level=_str("LOG_LEVEL", default("log_level")),
        clob_ws_url=_str("POLYMARKET_CLOB_WS_URL", default("clob_ws_url")),
        gamma_url=_str("POLYMARKET_GAMMA_URL", default("gamma_url")),
        book_db_path=_path("BOOK_DB_PATH", default("book_db_path")),
        book_log_dir=_path("BOOK_LOG_DIR", default("book_log_dir"), none_word=True),
        book_universe_hours=_float("BOOK_UNIVERSE_HOURS", default("book_universe_hours")),
        book_refresh_sec=_int("BOOK_REFRESH_SEC", default("book_refresh_sec")),
        book_assets_per_conn=_int("BOOK_ASSETS_PER_CONN", default("book_assets_per_conn")),
        book_min_interval_ms=_int("BOOK_MIN_INTERVAL_MS", default("book_min_interval_ms")),
        book_change_key=_str("BOOK_CHANGE_KEY", default("book_change_key")).lower(),
        book_raw_deltas=_bool("BOOK_RAW_DELTAS", default("book_raw_deltas")),
        book_game_sports=tuple(x.strip().lower() for x in _str("BOOK_GAME_SPORTS", ",".join(default("book_game_sports"))).split(",") if x.strip()),
        book_game_max_tokens=_int("BOOK_GAME_MAX_TOKENS", default("book_game_max_tokens")),
        book_game_lookahead_hours=_float("BOOK_GAME_LOOKAHEAD_HOURS", default("book_game_lookahead_hours")),
        missing=missing,
        kalshi_api_url=_str("KALSHI_API_URL", default("kalshi_api_url")),
        kalshi_ws_url=_str("KALSHI_WS_URL", default("kalshi_ws_url")),
        kalshi_key_id=_env("KALSHI_KEY_ID"),
        kalshi_private_key_path=_path("KALSHI_PRIVATE_KEY_PATH", None, expand=True),
        kalshi_books_db_path=_path("KALSHI_BOOKS_DB_PATH", default("kalshi_books_db_path")),
        kalshi_log_dir=_path("KALSHI_LOG_DIR", default("kalshi_log_dir"), none_word=True),
        kalshi_sweep_sec=_float("KALSHI_SWEEP_SEC", default("kalshi_sweep_sec")),
        kalshi_sweep_sec_unauth=_float("KALSHI_SWEEP_SEC_UNAUTH", default("kalshi_sweep_sec_unauth")),
        kalshi_depth_sec=_float("KALSHI_DEPTH_SEC", default("kalshi_depth_sec")),
        kalshi_match_refresh_sec=_float("KALSHI_MATCH_REFRESH_SEC", default("kalshi_match_refresh_sec")),
        kalshi_ws=_bool("KALSHI_WS", default("kalshi_ws")),
    )
    if require_auth:
        s.require_auth()
    return s
