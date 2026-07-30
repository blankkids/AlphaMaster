from __future__ import annotations

import math

import pytest

from web.paper_trading import PaperAccount


def test_t0_executes_at_next_bar_open_and_ignores_duplicate_poll() -> None:
    account = PaperAccount(amount=10_000, mode="T+0", cost_rate=0)

    assert account.process_signal(bar_ts=100, price=10, target_position=0.5)
    assert account.units == 0
    assert account.pending_position == pytest.approx(0.5)
    assert len(account.trades) == 0
    assert len(account.equity_curve) == 1

    assert account.process_signal(bar_ts=200, price=12, target_position=-1)
    assert account.units == pytest.approx(10_000 * 0.5 / 12)
    assert len(account.trades) == 1

    assert not account.process_signal(bar_ts=200, price=11, target_position=-1)
    assert len(account.trades) == 1


def test_t1_adds_one_more_bar_before_open_price_execution() -> None:
    account = PaperAccount(amount=10_000, mode="T+1", cost_rate=0)

    assert account.process_signal(bar_ts=100, price=10, target_position=0.5)
    assert account.units == 0
    assert account.pending_position == pytest.approx(0.5)
    assert account.trades == []

    assert account.process_signal(bar_ts=200, price=20, target_position=-0.25)
    assert account.units == 0
    assert account.trades == []

    assert account.process_signal(bar_ts=300, price=25, target_position=0.1)
    assert account.units == pytest.approx(200)
    assert account.pending_position == pytest.approx(-0.25)
    assert account.trades[0]["target_position"] == pytest.approx(0.5)


def test_backtest_log_return_and_turnover_cost_are_reused() -> None:
    account = PaperAccount(amount=10_000, mode="T+0", cost_rate=0.001)

    account.process_signal(bar_ts=100, price=9, target_position=0.5)
    account.process_signal(bar_ts=200, price=10, target_position=-0.25)
    account.process_signal(bar_ts=300, price=12, target_position=0.1)

    # First target earns open[200] -> open[300], then the second target is charged.
    expected_log_return = (
        -0.5 * 0.001
        + 0.5 * math.log(12 / 10)
        - abs(-0.25 - 0.5) * 0.001
    )
    assert account.cum_log_return == pytest.approx(expected_log_return)
    assert account.to_public()["equity"] == pytest.approx(
        10_000 * math.exp(expected_log_return)
    )
    assert account.trades[0]["cost_rate"] == 0.001
    assert account.total_cost > 0

def test_paper_accounts_are_independent() -> None:
    first = PaperAccount(amount=10_000, mode="T+0", cost_rate=0)
    second = PaperAccount(amount=50_000, mode="T+0", cost_rate=0)

    first.process_signal(bar_ts=100, price=10, target_position=1)
    second.process_signal(bar_ts=100, price=10, target_position=-0.5)
    first.process_signal(bar_ts=200, price=10, target_position=1)
    second.process_signal(bar_ts=200, price=10, target_position=-0.5)
    first.process_signal(bar_ts=300, price=12, target_position=1)

    assert first.to_public()["profit"] == pytest.approx(2_000)
    assert second.to_public()["profit"] == pytest.approx(0)
    assert first.units > 0
    assert second.units < 0
    assert len(first.trades) == 1
    assert len(second.trades) == 1


def test_paper_state_round_trip_preserves_curve_and_pending_signal() -> None:
    original = PaperAccount(amount=20_000, mode="T+1", cost_rate=0.0007)
    original.process_signal(bar_ts=100, price=25, target_position=0.75)

    restored = PaperAccount.from_dict(original.to_dict())

    assert restored.amount == 20_000
    assert restored.mode == "T+1"
    assert restored.pending_position == pytest.approx(0.75)
    assert restored.cost_rate == 0.0007
    assert restored.last_bar_ts == 100
    assert restored.equity_curve == original.equity_curve


def test_reset_changes_config_and_clears_only_this_account() -> None:
    account = PaperAccount(amount=10_000, mode="T+0")
    account.process_signal(bar_ts=100, price=10, target_position=1)

    account.reset(amount=30_000, mode="T+1")

    assert account.amount == 30_000
    assert account.mode == "T+1"
    assert account.cash == 30_000
    assert account.units == 0
    assert account.trades == []
    assert account.equity_curve == []
