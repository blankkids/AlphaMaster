"""Per-watch paper trading account used by the realtime signal engine."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PAPER_AMOUNT = 100_000.0
DEFAULT_PAPER_COST_RATE = 0.0003
MIN_PAPER_AMOUNT = 1.0
MAX_PAPER_AMOUNT = 1_000_000_000_000.0
PAPER_MODES = {"T+0", "T+1"}
MAX_PAPER_TRADES = 200
MAX_EQUITY_POINTS = 500
PAPER_RULES_VERSION = 2


def normalize_paper_amount(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = DEFAULT_PAPER_AMOUNT
    if not math.isfinite(amount) or not MIN_PAPER_AMOUNT <= amount <= MAX_PAPER_AMOUNT:
        raise ValueError(
            f"模拟交易金额必须在 {MIN_PAPER_AMOUNT:g} 到 {MAX_PAPER_AMOUNT:g} 之间"
        )
    return amount


def normalize_paper_mode(value: Any) -> str:
    mode = str(value or "T+0").strip().upper()
    if mode not in PAPER_MODES:
        raise ValueError("模拟交易模式只能是 T+0 或 T+1")
    return mode


def normalize_cost_rate(value: Any) -> float:
    rate = _finite_float(value, DEFAULT_PAPER_COST_RATE)
    if not 0.0 <= rate <= 1.0:
        raise ValueError("模拟交易成本率必须在 0 到 1 之间")
    return rate


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    number = _finite_float(value, math.nan)
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class PaperAccount:
    """A paper account that follows the backtest engine's execution rules.

    Backtest parity:
    - continuous target exposure = tanh(factor);
    - a signal is filled at a later bar's open, never at the signal bar's close;
    - PnL uses position * log(open[next] / open[current]);
    - turnover is charged the same combined commission/slippage cost rate.

    ``T+0`` uses the backtest's one-bar execution lag. ``T+1`` adds one more
    closed-bar delay before the open-price fill.
    """

    amount: float = DEFAULT_PAPER_AMOUNT
    mode: str = "T+0"
    cost_rate: float = DEFAULT_PAPER_COST_RATE
    rules_version: int = PAPER_RULES_VERSION
    cash: float | None = None
    units: float = 0.0
    active_position: float = 0.0
    signal_queue: list[float] = field(default_factory=list)
    cum_log_return: float = 0.0
    total_cost: float = 0.0
    last_bar_ts: int | None = None
    last_price: float | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.amount = normalize_paper_amount(self.amount)
        self.mode = normalize_paper_mode(self.mode)
        self.cost_rate = normalize_cost_rate(self.cost_rate)
        self.rules_version = PAPER_RULES_VERSION
        self.cash = self.amount if self.cash is None else _finite_float(self.cash, self.amount)
        self.units = _finite_float(self.units)
        self.active_position = self._normalize_position(self.active_position) or 0.0
        self.signal_queue = [
            position
            for value in self.signal_queue
            if (position := self._normalize_position(value)) is not None
        ][-2:]
        self.cum_log_return = _finite_float(self.cum_log_return)
        self.total_cost = max(0.0, _finite_float(self.total_cost))
        self.last_bar_ts = _optional_int(self.last_bar_ts)
        self.last_price = _optional_float(self.last_price)
        self.trades = [dict(row) for row in self.trades[-MAX_PAPER_TRADES:] if isinstance(row, dict)]
        self.equity_curve = [
            dict(row) for row in self.equity_curve[-MAX_EQUITY_POINTS:] if isinstance(row, dict)
        ]
        self._sync_balance(self.last_price)

    @staticmethod
    def _normalize_position(value: Any) -> float | None:
        if value is None:
            return None
        position = _finite_float(value, math.nan)
        if not math.isfinite(position):
            return None
        return max(-1.0, min(1.0, position))

    @property
    def pending_position(self) -> float | None:
        return self.signal_queue[0] if self.signal_queue else None

    @classmethod
    def from_dict(
        cls,
        payload: Any,
        *,
        default_amount: float = DEFAULT_PAPER_AMOUNT,
        default_mode: str = "T+0",
        default_cost_rate: float = DEFAULT_PAPER_COST_RATE,
    ) -> "PaperAccount":
        if not isinstance(payload, dict):
            return cls(
                amount=default_amount,
                mode=default_mode,
                cost_rate=default_cost_rate,
            )
        try:
            amount = normalize_paper_amount(payload.get("amount", default_amount))
        except ValueError:
            amount = normalize_paper_amount(default_amount)
        try:
            mode = normalize_paper_mode(payload.get("mode", default_mode))
        except ValueError:
            mode = normalize_paper_mode(default_mode)
        try:
            cost_rate = normalize_cost_rate(payload.get("cost_rate", default_cost_rate))
        except ValueError:
            cost_rate = normalize_cost_rate(default_cost_rate)
        # Older accounts used close-price fills and cannot be mixed with the
        # backtest-compatible return series. Preserve config but start clean.
        if payload.get("rules_version") != PAPER_RULES_VERSION:
            return cls(amount=amount, mode=mode, cost_rate=cost_rate)
        return cls(
            amount=amount,
            mode=mode,
            cost_rate=cost_rate,
            cash=payload.get("cash"),
            units=payload.get("units", 0.0),
            active_position=payload.get("active_position", 0.0),
            signal_queue=(
                payload.get("signal_queue")
                if isinstance(payload.get("signal_queue"), list)
                else []
            ),
            cum_log_return=payload.get("cum_log_return", 0.0),
            total_cost=payload.get("total_cost", 0.0),
            last_bar_ts=payload.get("last_bar_ts"),
            last_price=payload.get("last_price"),
            trades=payload.get("trades") if isinstance(payload.get("trades"), list) else [],
            equity_curve=(
                payload.get("equity_curve")
                if isinstance(payload.get("equity_curve"), list)
                else []
            ),
        )

    def reset(self, *, amount: Any | None = None, mode: Any | None = None) -> None:
        if amount is not None:
            self.amount = normalize_paper_amount(amount)
        if mode is not None:
            self.mode = normalize_paper_mode(mode)
        self.cash = self.amount
        self.units = 0.0
        self.active_position = 0.0
        self.signal_queue.clear()
        self.cum_log_return = 0.0
        self.total_cost = 0.0
        self.last_bar_ts = None
        self.last_price = None
        self.trades.clear()
        self.equity_curve.clear()

    def process_signal(
        self,
        *,
        bar_ts: Any,
        price: Any,
        target_position: Any,
    ) -> bool:
        """Process a signal using this closed bar's *open* as execution/mark price."""
        try:
            timestamp = int(bar_ts)
        except (TypeError, ValueError):
            return False
        mark = _finite_float(price, math.nan)
        target = self._normalize_position(target_position)
        if (
            timestamp <= 0
            or not math.isfinite(mark)
            or mark <= 0
            or target is None
            or (self.last_bar_ts is not None and timestamp <= self.last_bar_ts)
        ):
            return False

        bar_log_return = 0.0
        if self.last_price is not None:
            bar_log_return = self.active_position * math.log(mark / self.last_price)
            self.cum_log_return += bar_log_return

        execution_delay = 1 if self.mode == "T+0" else 2
        execution_target = (
            self.signal_queue.pop(0)
            if len(self.signal_queue) >= execution_delay
            else None
        )
        if execution_target is not None:
            self._execute(timestamp, mark, execution_target)
        self.signal_queue.append(target)

        self.last_bar_ts = timestamp
        self.last_price = mark
        self._sync_balance(mark)
        equity = self._equity(mark)
        self.equity_curve.append(
            {
                "ts": timestamp,
                "equity": round(equity, 8),
                "profit": round(equity - self.amount, 8),
                "return_pct": round((equity / self.amount - 1.0) * 100.0, 8),
                "bar_log_return": round(bar_log_return, 10),
                "position": round(self.active_position, 8),
            }
        )
        if len(self.equity_curve) > MAX_EQUITY_POINTS:
            del self.equity_curve[:-MAX_EQUITY_POINTS]
        return True

    def _execute(self, timestamp: int, price: float, target_position: float) -> None:
        previous_position = self.active_position
        turnover = abs(target_position - previous_position)
        if turnover <= 1e-12:
            return

        equity_before_cost = self._equity(price)
        cost_log_return = turnover * self.cost_rate
        self.cum_log_return -= cost_log_return
        equity_after_cost = self._equity(price)
        cost_amount = max(0.0, equity_before_cost - equity_after_cost)
        self.total_cost += cost_amount
        self.active_position = target_position
        self._sync_balance(price)
        trade_amount = equity_before_cost * turnover
        equity = self._equity(price)
        self.trades.append(
            {
                "ts": timestamp,
                "side": "BUY" if target_position > previous_position else "SELL",
                "action": self._action_label(
                    previous_position,
                    target_position,
                    target_position - previous_position,
                ),
                "price": round(price, 8),
                "quantity": round(trade_amount / price, 8),
                "trade_amount": round(trade_amount, 8),
                "target_position": round(target_position, 8),
                "turnover": round(turnover, 8),
                "cost": round(cost_amount, 8),
                "cost_rate": self.cost_rate,
                "equity": round(equity, 8),
                "profit": round(equity - self.amount, 8),
            }
        )
        if len(self.trades) > MAX_PAPER_TRADES:
            del self.trades[:-MAX_PAPER_TRADES]

    @staticmethod
    def _action_label(previous_units: float, desired_units: float, delta_units: float) -> str:
        if previous_units < 0 <= desired_units:
            return "平空/买入" if desired_units > 0 else "平空"
        if previous_units > 0 >= desired_units:
            return "卖出/做空" if desired_units < 0 else "卖出"
        if delta_units > 0:
            return "买入" if desired_units >= 0 else "减空"
        return "做空" if desired_units <= 0 else "减多"

    def _equity(self, price: float | None = None) -> float:
        del price
        return self.amount * math.exp(self.cum_log_return)

    def _sync_balance(self, price: float | None) -> None:
        equity = self._equity()
        if price is None or price <= 0:
            self.units = 0.0
            self.cash = equity
            return
        self.units = equity * self.active_position / price
        self.cash = equity - self.units * price

    def to_public(self) -> dict[str, Any]:
        equity = self._equity()
        position_value = self.units * (self.last_price or 0.0)
        return {
            "amount": self.amount,
            "mode": self.mode,
            "rules_version": self.rules_version,
            "execution_rule": "next_open" if self.mode == "T+0" else "next_next_open",
            "cost_rate": self.cost_rate,
            "total_cost": round(self.total_cost, 8),
            "cash": round(float(self.cash), 8),
            "units": round(self.units, 8),
            "position_value": round(position_value, 8),
            "position_ratio": round(self.active_position, 8),
            "active_position": self.active_position,
            "pending_position": self.signal_queue[0] if self.signal_queue else None,
            "pending_signals": list(self.signal_queue),
            "last_bar_ts": self.last_bar_ts,
            "last_price": self.last_price,
            "equity": round(equity, 8),
            "profit": round(equity - self.amount, 8),
            "return_pct": round((equity / self.amount - 1.0) * 100.0, 8),
            "trade_count": len(self.trades),
            "trades": list(self.trades),
            "equity_curve": list(self.equity_curve),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "mode": self.mode,
            "rules_version": self.rules_version,
            "cost_rate": self.cost_rate,
            "cash": self.cash,
            "units": self.units,
            "active_position": self.active_position,
            "signal_queue": list(self.signal_queue),
            "cum_log_return": self.cum_log_return,
            "total_cost": self.total_cost,
            "last_bar_ts": self.last_bar_ts,
            "last_price": self.last_price,
            "trades": list(self.trades),
            "equity_curve": list(self.equity_curve),
        }
