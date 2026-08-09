"""
backtest_viz/metrics.py — 回测绩效指标(纯 numpy,无外部依赖)。

供 backtest_viz/engine.py 与 run_backtest.py 共用,统一 max_drawdown / Calmar /
VaR / CVaR / Ulcer 口径,修复此前 SymbolResult.max_drawdown 恒为 0 的问题。

sharpe / sortino / kelly 仍由 run_backtest.py 的 calc_sharpe / calc_sortino /
calc_kelly_fraction 维持原实现,避免大范围改动既有导入与口径。
"""
from __future__ import annotations

import numpy as np


def max_drawdown(cum_pnl: np.ndarray) -> float:
    """最大回撤(基于累计 PnL 序列)。

    cum_pnl 为逐 bar 累计的 log 收益序列;回撤 = 历史峰值 - 当前值。
    空序列返回 0.0(非负,值越大越差)。
    """
    if cum_pnl is None or len(cum_pnl) == 0:
        return 0.0
    running_max = np.maximum.accumulate(cum_pnl)
    drawdown = running_max - cum_pnl
    return float(drawdown.max())


def calmar_ratio(
    total_return: float,
    mdd: float,
    periods_per_year: int,
    n_periods: int,
) -> float:
    """Calmar = 年化收益 / 最大回撤。

    total_return 为累计 log 收益,按 periods_per_year/n_periods 线性年化
    (与项目 sharpe 的 sqrt 年化风格保持简单一致)。mdd<=0 或无样本时返回 0.0。
    """
    if mdd <= 1e-12 or n_periods <= 0 or periods_per_year <= 0:
        return 0.0
    ann = total_return * (periods_per_year / n_periods)
    return float(ann / mdd)


def value_at_risk(pnl: np.ndarray, alpha: float = 0.95) -> float:
    """历史 VaR(返回损失分位数,通常为负值)。

    alpha=0.95 表示在 95% 置信下,单期最坏损失不超过该值。
    pnl 为逐 bar 收益序列(log 收益,小数量纲)。
    """
    if pnl is None or len(pnl) == 0:
        return 0.0
    return float(np.quantile(pnl, 1.0 - alpha))


def conditional_value_at_risk(pnl: np.ndarray, alpha: float = 0.95) -> float:
    """CVaR / Expected Shortfall:最坏 (1-alpha) 尾部的平均损失。"""
    if pnl is None or len(pnl) == 0:
        return 0.0
    var = np.quantile(pnl, 1.0 - alpha)
    tail = pnl[pnl <= var]
    if len(tail) == 0:
        return float(var)
    return float(tail.mean())


def ulcer_index(cum_pnl: np.ndarray) -> float:
    """Ulcer 指数 = sqrt(mean(回撤深度^2)),衡量回撤的深度与持续时间。"""
    if cum_pnl is None or len(cum_pnl) == 0:
        return 0.0
    running_max = np.maximum.accumulate(cum_pnl)
    drawdown = running_max - cum_pnl
    return float(np.sqrt(np.mean(drawdown ** 2)))
