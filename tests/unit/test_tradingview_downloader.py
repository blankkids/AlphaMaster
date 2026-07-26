from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from download_tradingview_klines import (
    default_output_path,
    infer_pro_name,
    parse_user_datetime,
    prepare_dataframe,
    validate_training_output_path,
)


def test_infer_china_exchange() -> None:
    assert infer_pro_name("159170") == "SZSE:159170"
    assert infer_pro_name("600519") == "SSE:600519"
    assert infer_pro_name("510300") == "SSE:510300"


def test_explicit_exchange_and_pro_name() -> None:
    assert infer_pro_name("aapl", "nasdaq") == "NASDAQ:AAPL"
    assert infer_pro_name("hkex:700") == "HKEX:700"


def test_date_only_end_uses_end_of_day() -> None:
    ts = parse_user_datetime(
        "2026-07-24",
        "Asia/Shanghai",
        end_of_day=True,
    )
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Shanghai"))
    assert dt.hour == 23
    assert dt.minute == 59


def test_prepare_dataframe_filters_and_deduplicates() -> None:
    rows = [
        {"time": 100, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10},
        {"time": 200, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "tick_volume": 20},
        {"time": 200, "open": 2, "high": 4, "low": 1.5, "close": 3.5, "tick_volume": 30},
        {"time": 300, "open": 3, "high": 4, "low": 2.5, "close": 3.5, "tick_volume": 40},
    ]
    df = prepare_dataframe(
        rows,
        start_ts=150,
        end_ts=250,
        target_bars=5000,
        all_history=False,
        interval_seconds=300,
        keep_forming=True,
    )
    assert list(df["time"]) == [200]
    assert float(df["high"].iloc[0]) == 4.0
    assert int(df["tick_volume"].iloc[0]) == 30


def test_default_output_is_training_compatible() -> None:
    path = default_output_path("SZSE:159170", "5m", "data")
    assert path.as_posix() == "data/159170_M5.parquet"


def test_custom_output_must_keep_training_timeframe_suffix() -> None:
    validate_training_output_path(Path("exports/my_stock_M5.parquet"), "5m")
    with pytest.raises(ValueError, match="_M5.parquet"):
        validate_training_output_path(Path("exports/my_stock.parquet"), "5m")
