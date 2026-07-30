"""Recover invalid last_data_file paths in web settings."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import web.settings as settings_mod
from web.settings import load_settings, save_settings


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    monkeypatch.setattr(settings_mod, "STRATEGIES_DIR", strategies)
    return tmp_path


def test_load_settings_recovers_missing_temp_path(project: Path) -> None:
    parquet = project / "XAUUSD_H1.parquet"
    parquet.write_bytes(b"PAR1")
    strat = project / "strategies" / "best_XAUUSD.json"
    strat.write_text(
        json.dumps({"symbol": "XAUUSD", "data_file": str(parquet.resolve())}),
        encoding="utf-8",
    )
    settings_path = settings_mod.SETTINGS_PATH
    settings_path.write_text(
        json.dumps(
            {
                "last_data_file": r"C:\Users\x\AppData\Local\Temp\2\pytest-of-x\test0\XAUUSD_H1.parquet",
                "last_strategy_file": str(strat.resolve()),
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings()
    assert loaded["last_data_file"] == str(parquet.resolve())
    if settings_mod._is_production_settings_path():
        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        assert persisted["last_data_file"] == str(parquet.resolve())


def test_save_settings_ignores_ephemeral_path(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod_settings = project / "web_settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", prod_settings)
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", project)
    save_settings({"last_data_file": "D:\\real\\XAUUSD_H1.parquet"})
    save_settings(
        {
            "last_data_file": r"C:\Users\x\AppData\Local\Temp\2\pytest-of-x\test0\X.parquet"
        }
    )
    loaded = load_settings()
    assert loaded["last_data_file"] == "D:\\real\\XAUUSD_H1.parquet"


def test_recent_data_files_are_persisted_in_mru_order(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = project / "web_settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(settings_mod, "_is_production_settings_path", lambda: False)
    first = project / "XAUUSD_H1.parquet"
    second = project / "BTCUSDT_M5.parquet"
    first.write_bytes(b"PAR1")
    second.write_bytes(b"PAR1")

    save_settings({"last_data_file": str(first)})
    save_settings({"last_data_file": str(second)})
    saved = save_settings({"last_data_file": str(first)})

    assert saved["recent_data_files"] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    assert load_settings()["recent_data_files"] == saved["recent_data_files"]


def test_recent_data_files_have_no_count_limit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = project / "web_settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(settings_mod, "_is_production_settings_path", lambda: False)
    files = [project / f"SYMBOL{i:02d}_H1.parquet" for i in range(25)]

    for path in files:
        path.write_bytes(b"PAR1")
        save_settings({"last_data_file": str(path)})

    saved = load_settings()["recent_data_files"]
    assert len(saved) == 25
    assert saved == [str(path.resolve()) for path in reversed(files)]


def test_training_records_are_persisted_per_data_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = project / "web_settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(settings_mod, "_is_production_settings_path", lambda: False)
    data_file = project / "TEST_M5.parquet"
    data_file.write_bytes(b"PAR1")

    saved = save_settings({
        "training_records": [{
            "data_file": str(data_file),
            "symbol": "TEST",
            "timeframe": "M5",
            "training_config": {
                "train_steps": 100,
                "batch_size": 64,
                "mode": "ftmo",
                "ignored": "value",
            },
            "strategy": {
                "strategy_file": "strategies/best_TEST_M5.json",
                "best_score": 1.25,
                "ignored": "value",
            },
            "updated_at": "2026-07-30T10:00:00+00:00",
        }]
    })

    assert saved["training_records"] == [{
        "data_file": str(data_file.resolve()),
        "symbol": "TEST",
        "timeframe": "M5",
        "training_config": {
            "train_steps": 100,
            "batch_size": 64,
            "mode": "ftmo",
        },
        "strategy": {
            "strategy_file": "strategies/best_TEST_M5.json",
            "best_score": 1.25,
        },
        "updated_at": "2026-07-30T10:00:00+00:00",
    }]


def test_realtime_watch_refresh_seconds_are_persisted_with_default(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = project / "web_settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", project)
    base_watch = {
        "source": "tongdaxin",
        "symbol": "159170",
        "timeframe": "5m",
        "strategy_file": "strategies/best_159170.json",
    }

    saved = save_settings(
        {"realtime_watches": [{**base_watch, "refresh_seconds": 3 * 60 * 60}]}
    )
    assert saved["realtime_watches"][0]["refresh_seconds"] == 10_800

    settings_path.write_text(
        json.dumps({"realtime_watches": [base_watch]}), encoding="utf-8"
    )
    loaded = load_settings()
    assert loaded["realtime_watches"][0]["refresh_seconds"] == 5
