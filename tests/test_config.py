from pathlib import Path

from rfqbot.config import _raw_log_dir


def test_raw_log_dir_empty_falls_back_and_none_turns_off():
    assert _raw_log_dir("") == Path("./data/raw") and _raw_log_dir(None) == Path("./data/raw")
    assert _raw_log_dir("NONE") is None and _raw_log_dir("none") is None
    assert _raw_log_dir(" ./x ") == Path("./x")
