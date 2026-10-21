"""Probe RFQ configuration (env ``PROBE_<KEY>``; credentials reuse ``POLYMARKET_*`` from .env).

PROBE_MODE off (default) | dry (build and log bodies, never send) | live (send through the gateway).
PROBE_GATEWAY requester (published host, CLOB L2 account headers only) | builder (host from builder
onboarding, account + builder headers). See docs/probe-rfq-notes.md for the credential list.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

REQUESTER_HOST = "https://combos-rfq-gateway-requester-api.polymarket.com"
REQUESTER_PATH = "/v1/requester/rfq/requests"
BUILDER_PATH = "/v1/builder/rfq/requests"
MODES = ("off", "dry", "live")
GATEWAYS = ("requester", "builder")


@dataclass(frozen=True)
class ProbeConfig:
    mode: str = "off"
    gateway: str = "requester"
    gateway_url: str = REQUESTER_HOST            # PROBE_GATEWAY_URL; the builder host comes from onboarding
    # account (CLOB L2) credentials
    poly_address: str = ""                       # POLY_ADDRESS header: the account signer that owns the API key
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    signer_address: str = ""                     # body signer_address (Deposit Wallet for signature_type 3)
    maker_address: str = ""                      # body maker_address (Deposit Wallet for signature_type 3)
    signature_type: int = 3
    # builder credentials (PROBE_GATEWAY=builder only)
    builder_api_key: str = ""
    builder_passphrase: str = ""
    builder_secret: str = ""
    requester_id: str = ""                       # PROBE_REQUESTER_ID: our requestor_public_id, learned from the first response
    # trigger and budget
    delay_sec: float = 5.0
    horizons_sec: tuple[int, ...] = (60, 300)
    max_late_sec: float = 600.0                  # a due probe older than this is dropped, not sent late
    max_per_hour: int = 20
    max_per_combo: int = 3                       # per combo per day
    min_spacing_sec: float = 30.0                # between probes on one combo
    both_directions: bool = True
    # disguise
    disguise: bool = True
    disguise_min_p: float = 0.99
    disguise_max_spread: float = 0.01            # one tick
    disguise_max_age_sec: float = 21_600.0       # the extra leg's book row must be this fresh
    # sizing
    size_usd: float = 20.0                       # BUY probes: notional; SELL probes: shares worth this at the price
    buy_safety_max: float = 0.90909
    # stores
    db: Path = Path("./data/probe.db")
    decisions_db: Path = Path("./data/decisions.db")
    rfq_db: Path = Path("./data/rfq.db")
    books_db: Path = Path("./data/books.db")
    kalshi_db: Path = Path("./data/kalshi_books.db")
    sports: tuple[str, ...] = ("nfl", "nba")
    timeout_sec: float = 30.0
    poll_sec: float = 2.0

    @property
    def path(self) -> str:
        return BUILDER_PATH if self.gateway == "builder" else REQUESTER_PATH

    def missing_credentials(self) -> list[str]:
        """Env names Ruben must supply before PROBE_MODE=live."""
        need = {"PROBE_POLY_ADDRESS (or POLYMARKET_SIGNER_ADDRESS)": self.poly_address, "POLYMARKET_API_KEY": self.api_key,
                "POLYMARKET_API_SECRET": self.api_secret, "POLYMARKET_API_PASSPHRASE": self.api_passphrase,
                "POLYMARKET_SIGNER_ADDRESS": self.signer_address, "POLYMARKET_MAKER_ADDRESS": self.maker_address}
        if self.gateway == "builder":
            need.update({"PROBE_GATEWAY_URL (builder host from onboarding)": "" if self.gateway_url == REQUESTER_HOST else self.gateway_url,
                         "PROBE_BUILDER_API_KEY": self.builder_api_key, "PROBE_BUILDER_PASSPHRASE": self.builder_passphrase,
                         "PROBE_BUILDER_SECRET": self.builder_secret})
        return [k for k, v in need.items() if not v]

    def to_json(self) -> str:
        d = asdict(self)
        for k in ("api_secret", "api_passphrase", "builder_secret", "builder_passphrase"):
            d[k] = "***" if d[k] else ""
        return json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}, sort_keys=True)


def _f(env: dict[str, str], key: str, default: float) -> float:
    v = env.get(f"PROBE_{key}")
    return float(v) if v not in (None, "") else default


def _b(env: dict[str, str], key: str, default: bool) -> bool:
    v = (env.get(f"PROBE_{key}") or "").lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


def load_probe_config(env_file: str | os.PathLike | None = ".env", env: dict[str, str] | None = None) -> ProbeConfig:
    if env is None:
        if env_file is not None and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)
    b = ProbeConfig()
    mode = (env.get("PROBE_MODE") or b.mode).lower()
    gateway = (env.get("PROBE_GATEWAY") or b.gateway).lower()
    if mode not in MODES:
        raise ValueError(f"PROBE_MODE must be one of {MODES}, got {mode!r}")
    if gateway not in GATEWAYS:
        raise ValueError(f"PROBE_GATEWAY must be one of {GATEWAYS}, got {gateway!r}")
    horizons = b.horizons_sec
    if env.get("PROBE_HORIZONS_SEC"):
        horizons = tuple(sorted(int(x) for x in env["PROBE_HORIZONS_SEC"].split(",") if x.strip()))
    return ProbeConfig(
        mode=mode, gateway=gateway, gateway_url=(env.get("PROBE_GATEWAY_URL") or b.gateway_url).rstrip("/"),
        poly_address=env.get("PROBE_POLY_ADDRESS") or env.get("POLYMARKET_SIGNER_ADDRESS") or "",
        api_key=env.get("POLYMARKET_API_KEY") or "", api_secret=env.get("POLYMARKET_API_SECRET") or "",
        api_passphrase=env.get("POLYMARKET_API_PASSPHRASE") or "",
        signer_address=env.get("POLYMARKET_SIGNER_ADDRESS") or "", maker_address=env.get("POLYMARKET_MAKER_ADDRESS") or "",
        signature_type=int(env.get("POLYMARKET_SIGNATURE_TYPE") or b.signature_type),
        builder_api_key=env.get("PROBE_BUILDER_API_KEY") or "", builder_passphrase=env.get("PROBE_BUILDER_PASSPHRASE") or "",
        builder_secret=env.get("PROBE_BUILDER_SECRET") or "", requester_id=env.get("PROBE_REQUESTER_ID") or "",
        delay_sec=_f(env, "DELAY_SEC", b.delay_sec), horizons_sec=horizons, max_late_sec=_f(env, "MAX_LATE_SEC", b.max_late_sec),
        max_per_hour=int(_f(env, "MAX_PER_HOUR", b.max_per_hour)), max_per_combo=int(_f(env, "MAX_PER_COMBO", b.max_per_combo)),
        min_spacing_sec=_f(env, "MIN_SPACING_SEC", b.min_spacing_sec), both_directions=_b(env, "BOTH_DIRECTIONS", b.both_directions),
        disguise=_b(env, "DISGUISE", b.disguise), disguise_min_p=_f(env, "DISGUISE_MIN_P", b.disguise_min_p),
        disguise_max_spread=_f(env, "DISGUISE_MAX_SPREAD", b.disguise_max_spread),
        disguise_max_age_sec=_f(env, "DISGUISE_MAX_AGE_SEC", b.disguise_max_age_sec),
        size_usd=_f(env, "SIZE_USD", b.size_usd), buy_safety_max=_f(env, "BUY_SAFETY_MAX", b.buy_safety_max),
        db=Path(env.get("PROBE_DB") or b.db), decisions_db=Path(env.get("QUOTE_DECISIONS_DB") or b.decisions_db),
        rfq_db=Path(env.get("RFQ_DB_PATH") or b.rfq_db), books_db=Path(env.get("BOOK_DB_PATH") or b.books_db),
        kalshi_db=Path(env.get("KALSHI_BOOKS_DB_PATH") or b.kalshi_db),
        sports=tuple(s.strip() for s in (env.get("PROBE_SPORTS") or env.get("QUOTE_SPORTS") or ",".join(b.sports)).split(",") if s.strip()),
        timeout_sec=_f(env, "TIMEOUT_SEC", b.timeout_sec), poll_sec=_f(env, "POLL_SEC", b.poll_sec),
    )
