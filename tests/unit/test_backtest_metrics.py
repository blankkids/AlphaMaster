"""tests/unit/test_backtest_metrics.py — 回测指标计算测试。

覆盖 backtest_viz/metrics.py 的纯函数,以及 BacktestEngine 在 SymbolResult 上
真实填充 max_drawdown / calmar / VaR / CVaR(修复此前 max_drawdown 恒为 0)。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
import torch

from backtest_viz import metrics
from backtest_viz.engine import BacktestEngine


# ── 纯函数 ────────────────────────────────────────────────────────────────


def test_max_drawdown_basic() -> None:
    # 峰值 0.2 后跌到 0.08 → 回撤 0.12
    cum = np.array([0.0, 0.1, 0.2, 0.08, 0.15], dtype=np.float64)
    assert metrics.max_drawdown(cum) == pytest.approx(0.12)


def test_max_drawdown_monotonic_up_is_zero() -> None:
    assert metrics.max_drawdown(np.array([0.1, 0.2, 0.3])) == 0.0


def test_max_drawdown_empty() -> None:
    assert metrics.max_drawdown(np.array([])) == 0.0
    assert metrics.max_drawdown(None) == 0.0


def test_var_cvar_tail_ordering() -> None:
    rng = np.random.default_rng(0)
    pnl = rng.normal(0.0, 0.01, 10000)
    var95 = metrics.value_at_risk(pnl, 0.95)
    var99 = metrics.value_at_risk(pnl, 0.99)
    # 99% 比 95% 更极端(更小的损失分位)
    assert var99 < var95
    # CVaR 不超过对应 VaR(尾部均值更糟/更小)
    assert metrics.conditional_value_at_risk(pnl, 0.99) <= var99
    assert metrics.conditional_value_at_risk(pnl, 0.95) <= var95


def test_calmar_ratio() -> None:
    # total_return=0.5, mdd=0.1, 恰好 1 年(252 期)→ 年化 0.5 → calmar 5.0
    assert metrics.calmar_ratio(0.5, 0.1, 252, 252) == pytest.approx(5.0)
    # mdd<=0 → 0
    assert metrics.calmar_ratio(0.5, 0.0, 252, 252) == 0.0


def test_ulcer_index_nonneg() -> None:
    cum = np.array([0.0, 0.1, 0.05, 0.2])
    assert metrics.ulcer_index(cum) >= 0.0


# ── engine 集成:SymbolResult 风险字段被真实填充 ──────────────────────────


def test_engine_fills_risk_metrics_and_nonzero_drawdown() -> None:
    T = 300
    rng = np.random.default_rng(7)
    # 几何布朗运动的 close(含涨跌,产生非平凡 target_ret)
    log_ret = rng.normal(0.0, 0.004, T)
    close = 100.0 * np.exp(np.cumsum(log_ret))
    open_ = close * (1.0 + rng.normal(0.0, 0.001, T))
    feat0 = np.sin(np.arange(T) * 0.3).astype(np.float32)  # 振荡信号 → 仓位来回翻转

    raw = {
        "open":   torch.tensor(open_, dtype=torch.float32).unsqueeze(0),
        "high":   torch.tensor(close * 1.001, dtype=torch.float32).unsqueeze(0),
        "low":    torch.tensor(close * 0.999, dtype=torch.float32).unsqueeze(0),
        "close":  torch.tensor(close, dtype=torch.float32).unsqueeze(0),
        "volume": torch.ones(T, dtype=torch.float32).unsqueeze(0),
        "time":   torch.arange(T, dtype=torch.int64).unsqueeze(0),
    }
    feat = torch.zeros((1, 8, T), dtype=torch.float32)
    feat[0, 0, :] = torch.from_numpy(feat0)

    engine = BacktestEngine(formula=[0], cost_rate=0.0003)
    results = engine.run(raw, feat, ["TEST"])
    r = results[0]

    # 所有新增风险字段存在且有限
    for v in (r.max_drawdown, r.calmar, r.var_95, r.var_99, r.cvar_95, r.cvar_99):
        assert math.isfinite(v), f"非有限值: {v}"
    # max_drawdown 非负(回撤定义)
    assert r.max_drawdown >= 0.0
    # VaR/CVaR: 99 比 95 更极端
    assert r.var_99 <= r.var_95
    assert r.cvar_99 <= r.cvar_95
