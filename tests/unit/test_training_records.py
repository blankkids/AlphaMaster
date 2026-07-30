from __future__ import annotations

from pathlib import Path

import pytest

import web.app as app_module


def test_upsert_training_record_preserves_config_and_adds_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_file = tmp_path / "TEST_M5.parquet"
    data_file.write_bytes(b"PAR1")
    settings = {"training_records": []}

    monkeypatch.setattr(app_module, "load_settings", lambda: dict(settings))

    def fake_save(changes: dict) -> dict:
        settings.update(changes)
        return dict(settings)

    monkeypatch.setattr(app_module, "save_settings", fake_save)
    data_info = {
        "data_file": str(data_file),
        "symbol": "TEST",
        "timeframe": "M5",
    }
    config = app_module._training_config_snapshot(
        mode="ftmo",
        from_scratch=True,
    )

    app_module._upsert_training_record(
        data_info,
        training_config=config,
        updated_at="2026-07-30T10:00:00+00:00",
    )
    app_module._upsert_training_record(
        data_info,
        strategy_info={
            "strategy_file": str(tmp_path / "best_TEST_M5.json"),
            "best_score": 2.5,
            "vocab_version": "v1",
            "formula_decoded": "OPEN → CLOSE",
            "mode": "parquet_file",
        },
        updated_at="2026-07-30T11:00:00+00:00",
    )

    record = settings["training_records"][0]
    assert record["data_file"] == str(data_file.resolve())
    assert record["symbol"] == "TEST"
    assert record["timeframe"] == "M5"
    assert record["training_config"] == config
    assert record["strategy"]["best_score"] == 2.5
    assert record["strategy"]["strategy_file"].endswith("best_TEST_M5.json")
    assert record["strategy"]["mode"] == "parquet_file"
    assert record["training_config"]["mode"] == "ftmo"
    assert record["updated_at"] == "2026-07-30T11:00:00+00:00"
