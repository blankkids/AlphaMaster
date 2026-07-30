"""Training artifacts must be isolated by both symbol and timeframe."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import torch

from model_core.engine import _strategy_file_for_symbol
from model_core.vocab import FORMULA_VOCAB
from utils.training_identity import (
    checkpoint_filename,
    history_filename,
    strategy_filename,
)
from web import progress
from web import training_package
from web import training_time


def test_timeframes_produce_distinct_artifact_names() -> None:
    assert strategy_filename("159170", "M1") == "best_159170_M1.json"
    assert strategy_filename("159170", "M5") == "best_159170_M5.json"
    assert history_filename("159170", "M1") == "training_history_159170_M1.json"
    assert history_filename("159170", "M5") == "training_history_159170_M5.json"
    assert (
        checkpoint_filename("159170", "M1", 20)
        == "ckpt_159170_M1_step_0020.pt"
    )
    assert (
        checkpoint_filename("159170", "M5", 20)
        == "ckpt_159170_M5_step_0020.pt"
    )
    assert Path(_strategy_file_for_symbol("159170", "M1")).name == (
        "best_159170_M1.json"
    )
    assert Path(_strategy_file_for_symbol("159170", "M5")).name == (
        "best_159170_M5.json"
    )


def test_web_progress_keeps_m1_and_m5_independent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    strategies = tmp_path / "strategies"
    checkpoints = tmp_path / "checkpoints"
    strategies.mkdir()
    checkpoints.mkdir()
    monkeypatch.setattr(progress, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(progress, "STRATEGIES_DIR", strategies)
    monkeypatch.setattr(progress, "CHECKPOINT_DIR", checkpoints)

    for timeframe, score, step in (("M1", 1.25, 11), ("M5", 2.75, 37)):
        (strategies / strategy_filename("159170", timeframe)).write_text(
            json.dumps(
                {
                    "symbol": "159170",
                    "timeframe": timeframe,
                    "formula": [0],
                    "best_score": score,
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / history_filename("159170", timeframe)).write_text(
            json.dumps(
                {
                    "step": [step - 1],
                    "best_score": [score],
                }
            ),
            encoding="utf-8",
        )

    m1 = progress.get_symbol_progress("159170", "M1")
    m5 = progress.get_symbol_progress("159170", "M5")

    assert m1.timeframe == "M1"
    assert m1.current_step == 11
    assert m1.best_score == 1.25
    assert m5.timeframe == "M5"
    assert m5.current_step == 37
    assert m5.best_score == 2.75


def test_legacy_strategy_is_only_used_for_matching_timeframe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    monkeypatch.setattr(progress, "STRATEGIES_DIR", strategies)
    (strategies / "best_159170.json").write_text(
        json.dumps(
            {
                "symbol": "159170",
                "timeframe": "M1",
                "formula": [0],
                "best_score": 1.0,
            }
        ),
        encoding="utf-8",
    )

    assert progress._load_strategy("159170", "M1") is not None
    assert progress._load_strategy("159170", "M5") is None


def test_training_duration_is_isolated_by_timeframe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(training_time, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(training_time, "LOG_DIR", logs)
    training_time._backfilled.clear()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    training_time.record_training_session(
        symbol="159170",
        timeframe="M1",
        started_at=started.isoformat(),
        finished_at=(started + timedelta(minutes=10)).isoformat(),
        log_path="logs/train_159170_M1_20260101_000000.log",
    )
    training_time.record_training_session(
        symbol="159170",
        timeframe="M5",
        started_at=started.isoformat(),
        finished_at=(started + timedelta(minutes=25)).isoformat(),
        log_path="logs/train_159170_M5_20260101_000000.log",
    )

    assert (
        training_time.get_training_time_summary(
            "159170",
            "M1",
        ).history_total_seconds
        == 600
    )
    assert (
        training_time.get_training_time_summary(
            "159170",
            "M5",
        ).history_total_seconds
        == 1500
    )


def test_training_export_contains_only_selected_timeframe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    strategies = tmp_path / "strategies"
    checkpoints = tmp_path / "checkpoints"
    strategies.mkdir()
    checkpoints.mkdir()
    monkeypatch.setattr(progress, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(progress, "STRATEGIES_DIR", strategies)
    monkeypatch.setattr(progress, "CHECKPOINT_DIR", checkpoints)
    monkeypatch.setattr(training_package, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(training_package, "CHECKPOINT_DIR", checkpoints)

    for timeframe in ("M1", "M5"):
        torch.save(
            {
                "step": 20,
                "symbol": "159170",
                "timeframe": timeframe,
                "vocab_version": FORMULA_VOCAB.version,
                "best_score": 1.0,
            },
            checkpoints / checkpoint_filename(
                "159170",
                timeframe,
                20,
            ),
        )
        (strategies / strategy_filename("159170", timeframe)).write_text(
            json.dumps(
                {
                    "symbol": "159170",
                    "timeframe": timeframe,
                    "formula": [0],
                    "best_score": 1.0,
                }
            ),
            encoding="utf-8",
        )

    body, name = training_package.build_training_export_zip("159170", "M1")

    assert "159170_M1" in name
    with zipfile.ZipFile(BytesIO(body)) as archive:
        names = archive.namelist()
        assert any("ckpt_159170_M1_" in member for member in names)
        assert "strategies/best_159170_M1.json" in names
        assert not any("159170_M5" in member for member in names)
