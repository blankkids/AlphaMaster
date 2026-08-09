"""
backtest_viz/akquant_engine.py — 基于 akquant 的事件驱动回测(并行可选引擎)。

定位:不替换训练侧向量化 reward(model_core/backtest.py 不动),仅作事后独立
验证与专业报告生成。把本项目算出的 tanh(factor) 连续仓位映射成 akquant 的
order_target_percent 目标仓位,跑事件驱动回测,产出 Plotly HTML 报告与全量
风险指标 JSON。

口径说明:akquant 按自身撮合/成本模型成交(默认 next-bar close),与训练用的
next-bar open + 向量化 PnL 不完全一致,故本引擎仅用于验证/展示,不参与训练
选优,也不改变 strategies/best_*.json。

akquant 为可选依赖:模块顶层不 import akquant,仅在调用时延迟导入,避免未安装
时影响 backtest_viz 包的其它组件。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from model_core.vm import StackVM
from strategy_manager.signal import compute_target_positions_stateless

# 从 result.metrics 提取的指标(akquant 返回百分点形式,原样转储并在 JSON 注明)
_METRIC_ATTRS = (
    "total_return_pct", "annualized_return", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "max_drawdown_pct", "win_rate", "profit_factor",
    "var_95", "var_99", "cvar_95", "cvar_99", "ulcer_index", "upi",
    "avg_trade_bars", "exposure_time_pct",
)


def _metrics_to_dict(result) -> dict:
    """从 akquant BacktestResult.metrics 提取已知指标(缺失项跳过)。"""
    m = result.metrics
    out: dict = {}
    for attr in _METRIC_ATTRS:
        val = getattr(m, attr, None)
        if isinstance(val, (int, float)):
            out[attr] = float(val)
    return out


def run_akquant_backtest_symbol(
    formula: list[int],
    raw_1sym: dict,
    feat_1sym,
    symbol: str,
    cost_rate: float,
    initial_cash: float = 100_000.0,
):
    """单品种:formula + 该品种 raw_dict[1,T] / feat[1,F,T] -> (BacktestResult, position[T])。

    仓位口径与训练/回测完全一致:StackVM 执行公式 -> compute_target_positions_stateless
    (tanh + MIN_TRADE_EXPOSURE 归零)。
    """
    import akquant as aq
    from akquant import Strategy

    vm = StackVM()
    factor = vm.execute(formula, feat_1sym)  # [1, T]
    if factor is None:
        raise RuntimeError(f"akquant: StackVM 无法执行公式 {formula}")
    position = compute_target_positions_stateless(factor)  # [1, T]
    pos_arr = position[0].detach().double().numpy().astype(float)

    class FormulaTargetStrategy(Strategy):
        """每根 bar 把目标仓位调到预计算的 tanh(factor) 序列对应值。

        targets 通过类属性注入：akquant 接收类（非实例）并在内部无参实例化，
        故 on_bar 惰性初始化计数器，targets 走类属性。
        """
        targets: dict[str, np.ndarray] = {symbol: pos_arr}

        def on_bar(self, bar) -> None:  # noqa: ANN001 - akquant 注入 bar
            if not hasattr(self, "_idx"):
                self._idx: dict[str, int] = {}
            sym = bar.symbol
            i = self._idx.get(sym, 0)
            self._idx[sym] = i + 1
            arr = type(self).targets.get(sym)
            if arr is None or i >= len(arr):
                return
            self.order_target_percent(target_percent=float(arr[i]), symbol=sym)

    times = np.asarray(raw_1sym["time"].long().numpy()).reshape(-1)
    df = pd.DataFrame({
        "date": pd.to_datetime(times, unit="s"),
        "open": np.asarray(raw_1sym["open"].float().numpy()).reshape(-1),
        "high": np.asarray(raw_1sym["high"].float().numpy()).reshape(-1),
        "low": np.asarray(raw_1sym["low"].float().numpy()).reshape(-1),
        "close": np.asarray(raw_1sym["close"].float().numpy()).reshape(-1),
        "volume": np.asarray(raw_1sym["volume"].float().numpy()).reshape(-1),
        "symbol": symbol,
    })

    result = aq.run_backtest(
        data=df,
        strategy=FormulaTargetStrategy,  # 传类：akquant 内部无参实例化
        symbols=symbol,
        initial_cash=float(initial_cash),
        commission_rate=float(cost_rate),
    )
    return result, pos_arr


def run_akquant_from_loaded(
    *,
    symbol_formulas: dict[str, list[int]],
    raw_dict: dict,
    feat,
    syms: list[str],
    cost_rates: dict[str, float],
    output_dir: str | Path,
    initial_cash: float = 100_000.0,
) -> dict:
    """对已加载的多品种数据逐品种跑 akquant 回测,输出 metrics JSON + 每品种 HTML 报告。

    返回汇总 dict(同时写入 output_dir/akquant_metrics.json)。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict] = {}
    returns: list[float] = []
    for i, sym in enumerate(syms):
        formula = symbol_formulas.get(sym)
        if not formula:
            print(f"  [跳过] {sym}(无策略)")
            continue
        cost_rate = cost_rates.get(sym, 0.0003)
        raw_i = {k: v[i:i + 1] for k, v in raw_dict.items()}
        feat_i = feat[i:i + 1]
        try:
            result, _pos = run_akquant_backtest_symbol(
                formula, raw_i, feat_i, sym, cost_rate, initial_cash
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {sym}: {exc}")
            continue

        metrics = _metrics_to_dict(result)
        summary[sym] = {"cost_rate": cost_rate, **metrics}
        if "total_return_pct" in metrics:
            returns.append(metrics["total_return_pct"])

        # 每品种 Plotly 报告
        try:
            report_path = output_dir / f"akquant_report_{sym}.html"
            result.viz.report(filename=str(report_path), show=False)
            summary[sym]["report"] = report_path.name
            print(f"  {sym}: ret={metrics.get('total_return_pct', float('nan')):.3f}% "
                  f"sharpe={metrics.get('sharpe_ratio', float('nan')):+.3f} "
                  f"mdd={metrics.get('max_drawdown_pct', float('nan')):.3f}% "
                  f"-> {report_path.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [报告失败] {sym}: {exc}")

    portfolio = {
        "n_symbols": len(returns),
        "avg_total_return_pct": float(np.mean(returns)) if returns else 0.0,
        "note": "等权各品种 total_return_pct 的简单平均(akquant 单品种回测口径)",
    }

    payload = {
        "engine": "akquant",
        "note": ("akquant 事件驱动回测(默认 next-bar close 成交),口径与训练向量化"
                 " reward 不完全一致,仅作独立验证/展示,不参与训练选优。"
                 "指标值为 akquant 原始口径,百分比类字段为百分点形式。"),
        "symbols": summary,
        "portfolio": portfolio,
    }
    out_json = output_dir / "akquant_metrics.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n  akquant 指标已保存 -> {out_json}")
    return payload
