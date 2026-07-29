from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from backtest_viz import BacktestEngine
from run_backtest import build_kelly_backtest, calc_kelly_fraction


def test_calc_kelly_fraction_clamps_invalid_and_leveraged_values() -> None:
    assert calc_kelly_fraction(0.60, 2.0) == pytest.approx(0.40)
    assert calc_kelly_fraction(0.20, 1.0) == 0.0
    assert calc_kelly_fraction(1.0, 2.0) == 1.0
    assert calc_kelly_fraction(None, 2.0) == 0.0
    assert calc_kelly_fraction(0.60, 0.0) == 0.0


def test_build_kelly_backtest_replaces_signal_size_and_recalculates_costs() -> None:
    result = SimpleNamespace(
        symbol="TEST",
        win_rate=0.60,
        profit_loss_ratio=2.0,
        position=np.array([0.8, 0.6, -0.7, -0.9], dtype=np.float32),
        open=np.array([100.0, 101.0, 102.0, 101.0], dtype=np.float32),
        times=np.array([1, 2, 3, 4], dtype=np.int64),
    )
    engine = BacktestEngine(formula=[0], cost_rate=0.001, periods_per_year=252)

    kelly = build_kelly_backtest(result, engine, periods_per_year=252)

    assert kelly["fraction"] == pytest.approx(0.40)
    np.testing.assert_allclose(
        kelly["position"],
        np.array([0.4, 0.4, -0.4, -0.4], dtype=np.float32),
    )

    target_ret = np.array(
        [np.log(102.0 / 101.0), np.log(101.0 / 102.0), 0.0, 0.0],
        dtype=np.float32,
    )
    previous = np.array([0.0, 0.4, 0.4, -0.4], dtype=np.float32)
    expected_pnl = kelly["position"] * target_ret - np.abs(kelly["position"] - previous) * 0.001
    np.testing.assert_allclose(kelly["pnl"], expected_pnl, rtol=1e-5, atol=5e-8)
    assert kelly["n_trades"] == 2


def test_kelly_fraction_below_live_threshold_stays_flat() -> None:
    result = SimpleNamespace(
        symbol="TEST",
        win_rate=0.51,
        profit_loss_ratio=1.0,
        position=np.ones(4, dtype=np.float32),
        open=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float32),
        times=np.array([1, 2, 3, 4], dtype=np.int64),
    )
    engine = BacktestEngine(formula=[0], cost_rate=0.001, periods_per_year=252)

    kelly = build_kelly_backtest(result, engine, periods_per_year=252)

    assert kelly["raw_fraction"] == pytest.approx(0.02)
    assert kelly["fraction"] == 0.0
    assert np.count_nonzero(kelly["position"]) == 0
    assert kelly["n_trades"] == 0
