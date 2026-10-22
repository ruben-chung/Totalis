import logging
from pathlib import Path

from rfqbot.config import (ACCEPTANCE_WINDOW_MS, DEFAULT_EXPIRY_GRACE_MS, DEFAULT_KALSHI_API_URL, LAST_LOOK_WINDOW_MS, Settings,
                           _bool, _float, _int, _path, _raw_log_dir, expiry_horizon_ms, load_settings)


def test_raw_log_dir_empty_falls_back_and_none_turns_off():
    assert _raw_log_dir("") == Path("./data/raw") and _raw_log_dir(None) == Path("./data/raw")
    assert _raw_log_dir("NONE") is None and _raw_log_dir("none") is None
    assert _raw_log_dir(" ./x ") == Path("./x")


def test_typed_env_helpers(monkeypatch):
    monkeypatch.setenv("T_INT", " 7 "); monkeypatch.setenv("T_EMPTY", "")
    monkeypatch.setenv("T_F", "2.5"); monkeypatch.setenv("T_B", "off"); monkeypatch.setenv("T_P", "none"); monkeypatch.setenv("T_H", "~/k.pem")
    assert _int("T_INT", 1) == 7 and _int("T_EMPTY", 1) == 1 and _int("T_MISSING", 3) == 3
    assert _float("T_F", 1.0) == 2.5 and _float("T_EMPTY", 1.0) == 1.0
    assert _bool("T_B", True) is False and _bool("T_EMPTY", True) is True and _bool("T_MISSING", False) is False
    assert _path("T_P", Path("x"), none_word=True) is None and _path("T_P", Path("x")) == Path("none")
    assert _path("T_EMPTY", Path("d")) == Path("d") and str(_path("T_H", None, expand=True)).endswith("k.pem") and "~" not in str(_path("T_H", None, expand=True))


def test_one_expiry_horizon_everywhere(monkeypatch, tmp_path, caplog):
    from rfqbot.normalizer import Normalizer
    from rfqbot.quoting.inventory import DEFAULT_HORIZON_MS
    from rfqbot.store import Store
    assert expiry_horizon_ms() == ACCEPTANCE_WINDOW_MS + LAST_LOOK_WINDOW_MS + DEFAULT_EXPIRY_GRACE_MS == 31_000
    assert DEFAULT_HORIZON_MS == expiry_horizon_ms()
    st = Store(None)
    assert Normalizer(st).horizon_ms == expiry_horizon_ms() and Normalizer(st, expiry_grace_ms=5_000).horizon_ms == 16_000
    st.close()
    for k in ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE", "POLYMARKET_SIGNER_ADDRESS",
              "POLYMARKET_MAKER_ADDRESS", "EXPIRY_GRACE_MS", "PAPER_TRADING", "KALSHI_API_URL"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(None)
    assert s.expiry_grace_ms == DEFAULT_EXPIRY_GRACE_MS and s.horizon_ms == 31_000 and s.kalshi_api_url == DEFAULT_KALSHI_API_URL
    assert not hasattr(s, "paper_trading") and s.missing and not s.has_auth
    monkeypatch.setenv("EXPIRY_GRACE_MS", "5000"); monkeypatch.setenv("PAPER_TRADING", "false")
    with caplog.at_level(logging.WARNING, logger="rfqbot.config"):
        s2 = load_settings(None)                       # accepted with a warning, never an error
    assert s2.horizon_ms == 16_000 and any("PAPER_TRADING=false ignored" in r.message for r in caplog.records)
    assert Settings.__dataclass_fields__["expiry_grace_ms"].default == DEFAULT_EXPIRY_GRACE_MS
