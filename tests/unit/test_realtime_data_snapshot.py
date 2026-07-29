from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from web.data_sources.base import Bar
from web.realtime_manager import (
    MAX_REFRESH_SECONDS,
    RealtimeManager,
    WatchTask,
    _build_data_snapshot,
    _load_backtest_metrics,
    _load_strategy_meta,
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
        win_rate=0.56,
        profit_loss_ratio=1.7,
        n_trades=114,
        performance_source="latest_backtest",
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
    assert _normalize_refresh_seconds(3 * 60 * 60) == 10_800
    with pytest.raises(ValueError, match="刷新秒数"):
        _normalize_refresh_seconds(0)
    with pytest.raises(ValueError, match="刷新秒数"):
        _normalize_refresh_seconds(MAX_REFRESH_SECONDS + 1)


def test_watch_exposes_and_persists_refresh_seconds() -> None:
    task = _task()
    public = task.to_public()
    assert public["refresh_seconds"] == 5
    assert public["win_rate"] == 0.56
    assert public["profit_loss_ratio"] == 1.7
    assert public["n_trades"] == 114
    assert task.persist_dict()["refresh_seconds"] == 5


def test_backtest_metrics_require_same_symbol_and_formula(tmp_path: Path) -> None:
    report_path = tmp_path / "multi_factor_report.json"
    report_path.write_text(
        json.dumps(
            {
                "symbols": {
                    "159170": {
                        "formula": [1, 2, 3],
                        "win_rate": 0.56,
                        "profit_loss_ratio": 1.7,
                        "n_trades": 114,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert _load_backtest_metrics("159170", [9, 9, 9], report_path) == {}
    assert _load_backtest_metrics("other", [1, 2, 3], report_path) == {}
    assert _load_backtest_metrics("159170", [1, 2, 3], report_path) == {
        "win_rate": 0.56,
        "profit_loss_ratio": 1.7,
        "n_trades": 114,
        "performance_source": "latest_backtest",
    }


def test_strategy_meta_includes_matching_backtest_metrics(tmp_path: Path) -> None:
    strategy_path = tmp_path / "best_159170.json"
    strategy_path.write_text(
        json.dumps({"symbol": "159170", "formula": [1, 2, 3], "best_score": 2.0}),
        encoding="utf-8",
    )
    report_path = tmp_path / "multi_factor_report.json"
    report_path.write_text(
        json.dumps(
            {
                "symbols": {
                    "159170": {
                        "formula": [1, 2, 3],
                        "win_rate": 0.625,
                        "profit_loss_ratio": 1.5,
                        "n_trades": 80,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("web.realtime_manager.BACKTEST_REPORT_PATH", report_path):
        meta = _load_strategy_meta(str(strategy_path))

    assert meta["win_rate"] == 0.625
    assert meta["profit_loss_ratio"] == 1.5
    assert meta["n_trades"] == 80
    assert meta["performance_source"] == "latest_backtest"


def test_countdown_uses_refresh_interval_instead_of_bar_close() -> None:
    task = _task()
    task.next_evaluation_at = 1_700_000_005.0

    with patch("web.realtime_manager.time.time", return_value=1_700_000_002.0):
        watch = task.to_public()

    assert watch["next_evaluation_at"] == 1_700_000_005.0
    assert watch["seconds_to_next"] == 3


def test_next_refresh_is_scheduled_after_data_and_judgment_finish() -> None:
    manager = RealtimeManager()
    task = _task()
    events = []

    def fetch_bars(*_args):
        events.append("refresh")
        return _bars()

    def judge(*_args):
        events.append("judge")
        return {
            "state": "insufficient",
            "bars_used": 2,
            "message": "历史 bar 不足",
        }

    manager._get_bars = fetch_bars
    with (
        patch("web.realtime_manager.evaluate_signal", side_effect=judge),
        patch("web.realtime_manager.time.monotonic", return_value=2_000.0),
        patch("web.realtime_manager.time.time", return_value=1_000.0),
    ):
        manager._evaluate_task(task)

    assert events == ["refresh", "judge"]
    assert task.next_due == 2_005.0
    assert task.next_evaluation_at == 1_005.0
    assert task.evaluating is False


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
