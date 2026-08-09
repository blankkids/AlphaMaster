"""tests/unit/test_akshare_source.py — AKShare 数据源测试(不联网,mock _fetch)。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from web.data_sources.akshare_source import (
    AkshareSource,
    _CST,
    _classify,
    _normalize_df,
)
from web.data_sources.factory import SOURCE_KINDS, get_source


# ── 分类 ─────────────────────────────────────────────────────────────────


def test_classify_stock_index_futures() -> None:
    assert _classify("600519") == "stock"
    assert _classify("000001") == "stock"
    assert _classify("159170") == "stock"  # ETF
    assert _classify("IF0") == "futures"
    assert _classify("rb0") == "futures"
    assert _classify("sh000001") == "index"
    assert _classify("sz399001") == "index"


# ── 归一化:中英文列名 ────────────────────────────────────────────────────


def test_normalize_chinese_daily_columns() -> None:
    df = pd.DataFrame({
        "日期": ["2026-08-06", "2026-08-07"],
        "开盘": [10.0, 11.0],
        "收盘": [10.5, 11.5],
        "最高": [11.0, 12.0],
        "最低": [9.9, 10.9],
        "成交量": [1000, 2000],
    })
    bars = _normalize_df(df, daily=True)
    assert len(bars) == 2
    assert bars[0].ts < bars[1].ts
    assert bars[0].close == 10.0 + 0.5
    # 日线 ts = 当日 15:00 CST
    assert bars[0].ts == int(datetime(2026, 8, 6, 15, 0, tzinfo=_CST).timestamp())


def test_normalize_english_intraday_columns() -> None:
    df = pd.DataFrame({
        "datetime": ["2026-08-07 14:55:00", "2026-08-07 15:00:00"],
        "open": [1.0, 2.0],
        "high": [1.5, 2.5],
        "low": [0.9, 1.9],
        "close": [1.2, 2.2],
        "volume": [100, 200],
    })
    bars = _normalize_df(df, daily=False)
    assert len(bars) == 2
    assert bars[0].close == 1.2
    assert bars[0].ts == int(datetime(2026, 8, 7, 14, 55, tzinfo=_CST).timestamp())


def test_normalize_dedups_by_timestamp_and_sorts() -> None:
    # 同一 ts 出现两次（去重），且乱序输入应升序输出
    df = pd.DataFrame({
        "datetime": ["2026-08-07 15:00:00", "2026-08-07 14:55:00", "2026-08-07 15:00:00"],
        "open": [1.0, 2.0, 3.0], "high": [1.5, 2.5, 3.5],
        "low": [0.9, 1.9, 2.9], "close": [1.2, 2.2, 3.2], "volume": [100, 200, 300],
    })
    bars = _normalize_df(df, daily=False)
    assert len(bars) == 2
    assert bars[0].ts < bars[1].ts


def test_normalize_missing_column_raises() -> None:
    import pytest
    df = pd.DataFrame({"date": ["2026-08-07"], "open": [1.0]})
    with pytest.raises(Exception):
        _normalize_df(df, daily=True)


# ── fetch_bars:drop_forming 与截断(mock _fetch)────────────────────────


def _fake_df() -> pd.DataFrame:
    now = datetime.now(_CST)
    past = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    future = (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    return pd.DataFrame({
        "datetime": [past, future],
        "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.9],
        "close": [1.2, 2.2], "volume": [100, 200],
    })


def test_fetch_bars_drops_forming_bar(monkeypatch) -> None:
    src = AkshareSource()
    monkeypatch.setattr(src, "_fetch", lambda *a, **k: _fake_df())
    bars = src.fetch_bars("IF0", "5m", 10, drop_forming=True)
    assert len(bars) == 1  # 未来那根被剔除


def test_fetch_bars_keeps_forming_when_disabled(monkeypatch) -> None:
    src = AkshareSource()
    monkeypatch.setattr(src, "_fetch", lambda *a, **k: _fake_df())
    bars = src.fetch_bars("IF0", "5m", 10, drop_forming=False)
    assert len(bars) == 2


def test_fetch_bars_truncates_to_n(monkeypatch) -> None:
    now = datetime.now(_CST)
    times = [(now - timedelta(days=10) + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
             for i in range(20)]
    df = pd.DataFrame({
        "datetime": times,
        "open": range(20), "high": range(20), "low": range(20),
        "close": range(20), "volume": range(20),
    })
    src = AkshareSource()
    monkeypatch.setattr(src, "_fetch", lambda *a, **k: df)
    bars = src.fetch_bars("600519", "1d", 5, drop_forming=False)
    assert len(bars) == 5


def test_fetch_bars_wraps_errors_as_unavailable(monkeypatch) -> None:
    import pytest
    from web.data_sources.base import DataSourceUnavailable

    def boom(*a, **k):
        raise RuntimeError("proxy down")

    src = AkshareSource()
    monkeypatch.setattr(src, "_fetch", boom)
    with pytest.raises(DataSourceUnavailable):
        src.fetch_bars("600519", "1d", 10)


# ── 工厂注册 ─────────────────────────────────────────────────────────────


def test_akshare_registered_in_source_kinds() -> None:
    assert ("akshare", "AKShare") in SOURCE_KINDS


def test_factory_builds_akshare_singleton() -> None:
    src = get_source("akshare")
    assert src.kind == "akshare"
    assert get_source("akshare") is src  # 单例
