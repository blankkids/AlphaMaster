"""Regression tests for batched single-symbol formula scoring."""

from __future__ import annotations

import pytest
import torch

from model_core.backtest import MT5Backtest
from model_core.config import ModelConfig
from model_core.engine import AlphaEngine
from model_core.vocab import FORMULA_VOCAB


@pytest.mark.parametrize("reward_mode", ["ftmo", "standard", "forex"])
def test_batch_fold_matches_scalar_scores(
    monkeypatch: pytest.MonkeyPatch,
    reward_mode: str,
) -> None:
    monkeypatch.setattr(ModelConfig, "REWARD_MODE", reward_mode)
    torch.manual_seed(31)
    factors = torch.randn(12, 1, 500)
    target_ret = torch.randn(1, 500) * 0.001
    fold = (0, 300, 320, 500)
    backtest = MT5Backtest(periods_per_year=11549)

    batch_train, batch_val = backtest.evaluate_fold_batch(
        factors,
        target_ret,
        *fold,
    )
    scalar = [
        backtest.evaluate_fold(factors[i], target_ret, *fold)
        for i in range(factors.shape[0])
    ]
    scalar_train = torch.stack([pair[0] for pair in scalar])
    scalar_val = torch.stack([pair[1] for pair in scalar])

    torch.testing.assert_close(batch_train, scalar_train, rtol=1e-6, atol=1e-6)
    torch.testing.assert_close(batch_val, scalar_val, rtol=1e-6, atol=1e-6)


@pytest.mark.parametrize("symbols", [1, 3])
def test_batch_ic_matches_scalar_ic(symbols: int) -> None:
    torch.manual_seed(47 + symbols)
    factors = torch.randn(9, symbols, 320)
    target_ret = torch.randn(symbols, 320) * 0.001

    batch_mean, batch_stability = AlphaEngine._compute_ic_batch(
        factors,
        target_ret,
    )
    scalar = [
        AlphaEngine._compute_ic(factors[i], target_ret)
        for i in range(factors.shape[0])
    ]
    scalar_mean = torch.stack([pair[0].reshape(()) for pair in scalar])
    scalar_stability = torch.stack([pair[1].reshape(()) for pair in scalar])

    torch.testing.assert_close(batch_mean, scalar_mean, rtol=1e-6, atol=1e-6)
    torch.testing.assert_close(
        batch_stability,
        scalar_stability,
        rtol=1e-5,
        atol=1e-5,
    )


def test_formula_batch_matches_original_formula_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelConfig, "REWARD_MODE", "ftmo")
    torch.manual_seed(59)
    features = torch.randn(1, FORMULA_VOCAB.feature_count, 420)
    target_ret = torch.randn(1, 420) * 0.001
    operator_names = list(FORMULA_VOCAB.operator_names)
    ema5 = FORMULA_VOCAB.operator_offset + operator_names.index("EMA_5")
    ema20 = FORMULA_VOCAB.operator_offset + operator_names.index("EMA_20")
    formulas = [
        [0, ema5],
        [5, ema20],
        [10, ema5, ema20],
        [15, ema20, ema5],
    ]
    folds = [
        {
            "train_start": 0,
            "train_end": 180,
            "gap": 20,
            "val_start": 200,
            "val_end": 300,
        },
        {
            "train_start": 100,
            "train_end": 280,
            "gap": 20,
            "val_start": 300,
            "val_end": 420,
        },
    ]
    engine = AlphaEngine(data_manager=None, target_symbol="TEST")

    batch = engine._eval_formula_batch(
        formulas,
        features,
        target_ret,
        folds,
    )
    scalar = [
        engine._eval_formula_task(
            idx,
            formula,
            features,
            target_ret,
            folds,
            True,
            [],
        )
        for idx, formula in enumerate(formulas)
    ]

    assert [item["status"] for item in batch] == [
        item["status"] for item in scalar
    ]
    for batch_item, scalar_item in zip(batch, scalar):
        assert batch_item["reward"] == pytest.approx(
            scalar_item["reward"],
            abs=1e-6,
        )
        assert batch_item["val_score"] == pytest.approx(
            scalar_item["val_score"],
            abs=1e-6,
        )
        assert batch_item["ic_full"] == pytest.approx(
            scalar_item["ic_full"],
            abs=1e-7,
        )


def test_formula_batch_evaluates_duplicate_formula_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelConfig, "REWARD_MODE", "ftmo")
    torch.manual_seed(71)
    features = torch.randn(1, FORMULA_VOCAB.feature_count, 360)
    target_ret = torch.randn(1, 360) * 0.001
    ema5 = (
        FORMULA_VOCAB.operator_offset
        + list(FORMULA_VOCAB.operator_names).index("EMA_5")
    )
    formula = [0, ema5]
    formulas = [formula, list(formula), [5, ema5], list(formula)]
    folds = [
        {
            "train_start": 0,
            "train_end": 180,
            "gap": 20,
            "val_start": 200,
            "val_end": 360,
        },
    ]
    engine = AlphaEngine(data_manager=None, target_symbol="TEST")
    original_execute = engine.vm.execute
    execute_count = 0

    def counted_execute(tokens, feat):
        nonlocal execute_count
        execute_count += 1
        return original_execute(tokens, feat)

    monkeypatch.setattr(engine.vm, "execute", counted_execute)
    results = engine._eval_formula_batch(
        formulas,
        features,
        target_ret,
        folds,
    )

    assert execute_count == 2
    assert [result["idx"] for result in results] == list(range(len(formulas)))
    assert [result["fml"] for result in results] == formulas
    assert results[0]["reward"] == pytest.approx(results[1]["reward"])
    assert results[0]["reward"] == pytest.approx(results[3]["reward"])
