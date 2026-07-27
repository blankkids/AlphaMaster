from __future__ import annotations

from unittest.mock import patch

import pytest

from web.data_sources.base import Bar
from web.realtime_manager import (
    RealtimeManager,
    WatchTask,
    _build_data_snapshot,
    _normalize_refresh_seconds,
)


def _bars() -> list[Bar]:
    return [
        Bar(ts=1_700_000_000, open=10.0, high=11.0, low=9.5, close=10.5, volume=100.0),
        Bar(ts=1_700_000_300, open=10.5, high=12.0, low=10.0, close=11.5, volume=250.0),
    ]


def _task() -> WatchTask:
    return WatchTask(
        id="tongdaxin:159170:5m:best_159170",
        source="tongdaxin",
        symbol="159170",
        timeframe="5m",
        strategy_file="strategies/best_159170.json",
        strategy_name="best_159170",
        formula=[0],
        vocab_version=None,
        strategy_symbol="159170",
        strategy_timeframe="5m",
        best_score=1.0,
        cadence_s=5,
    )


def test_build_data_snapshot_contains_range_and_latest_ohlcv() -> None:
    snapshot = _build_data_snapshot(_bars(), read_at=1_700_000_400)

    assert snapshot == {
        "bar_count": 2,
        "first_bar_ts": 1_700_000_000,
        "last_bar_ts": 1_700_000_300,
        "read_at": 1_700_000_400.0,
        "latest_bar": {
            "ts": 1_700_000_300,
            "open": 10.5,
            "high": 12.0,
            "low": 10.0,
            "close": 11.5,
            "volume": 250.0,
        },
    }


def test_realtime_refresh_seconds_default_and_validation() -> None:
    assert _normalize_refresh_seconds(None) == 5
    assert _normalize_refresh_seconds("12") == 12
    with pytest.raises(ValueError, match="刷新秒数"):
        _normalize_refresh_seconds(0)
    with pytest.raises(ValueError, match="刷新秒数"):
        _normalize_refresh_seconds(3601)


def test_watch_exposes_and_persists_refresh_seconds() -> None:
    task = _task()
    assert task.to_public()["refresh_seconds"] == 5
    assert task.persist_dict()["refresh_seconds"] == 5


def test_realtime_status_exposes_snapshot_even_when_history_is_insufficient() -> None:
    manager = RealtimeManager()
    task = _task()
    manager._tasks[task.id] = task
    manager._get_bars = lambda *_args: _bars()

    with patch(
        "web.realtime_manager.evaluate_signal",
        return_value={
            "state": "insufficient",
            "bars_used": 2,
            "message": "历史 bar 不足（2/3000），无法稳定计算特征",
        },
    ):
        manager._evaluate_task(task)

    watch = manager.status()["watches"][0]
    assert watch["state"] == "insufficient"
    assert watch["data_snapshot"]["bar_count"] == 2
    assert watch["data_snapshot"]["latest_bar"]["close"] == 11.5
