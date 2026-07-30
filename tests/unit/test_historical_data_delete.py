from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

import web.app as app_module


def _install_delete_context(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    data_file: Path,
    *,
    training_active: bool = False,
) -> dict:
    settings = {
        "last_data_file": str(data_file),
        "recent_data_files": [str(data_file)],
        "training_records": [{
            "data_file": str(data_file),
            "symbol": "TEST",
            "timeframe": "M5",
        }],
    }

    monkeypatch.setattr(app_module, "ROOT", root)
    monkeypatch.setattr(
        app_module,
        "_list_historical_data_files",
        lambda: [{"data_file": str(data_file.resolve())}],
    )
    monkeypatch.setattr(app_module, "load_settings", lambda: dict(settings))

    def fake_save(changes: dict) -> dict:
        settings.update(changes)
        return dict(settings)

    monkeypatch.setattr(app_module, "save_settings", fake_save)
    monkeypatch.setattr(
        app_module.training_manager,
        "status",
        lambda: {
            "active": training_active,
            "job": {"data_file": str(data_file)} if training_active else None,
        },
    )
    monkeypatch.setattr(
        app_module.backtest_manager,
        "status",
        lambda: {"active": False, "job": None},
    )
    return settings


def test_delete_managed_history_removes_parquet_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    data_file = root / "data" / "uploads" / "batch" / "TEST_M5.parquet"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"PAR1")
    settings = _install_delete_context(monkeypatch, root, data_file)

    response = app_module.api_delete_data_file(
        app_module.DeleteDataFileRequest(data_file=str(data_file))
    )

    assert response["file_deleted"] is True
    assert response["history_removed"] is True
    assert not data_file.exists()
    assert settings["last_data_file"] == ""
    assert settings["recent_data_files"] == []
    assert settings["training_records"] == []


def test_delete_external_history_also_removes_source_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    data_file = tmp_path / "external" / "TEST_H1.parquet"
    data_file.parent.mkdir()
    data_file.write_bytes(b"PAR1")
    settings = _install_delete_context(monkeypatch, root, data_file)

    response = app_module.api_delete_data_file(
        app_module.DeleteDataFileRequest(data_file=str(data_file))
    )

    assert response["file_deleted"] is True
    assert not data_file.exists()
    assert settings["recent_data_files"] == []


def test_delete_history_rejects_file_used_by_training(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    data_file = root / "data" / "TEST_M15.parquet"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"PAR1")
    _install_delete_context(monkeypatch, root, data_file, training_active=True)

    with pytest.raises(HTTPException) as exc_info:
        app_module.api_delete_data_file(
            app_module.DeleteDataFileRequest(data_file=str(data_file))
        )

    assert exc_info.value.status_code == 409
    assert data_file.exists()


def test_history_list_marks_all_files_for_source_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    managed = root / "data" / "uploads" / "batch" / "TEST_M5.parquet"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"PAR1")
    external = tmp_path / "external" / "OTHER_H1.parquet"
    external.parent.mkdir()
    external.write_bytes(b"PAR1")

    monkeypatch.setattr(app_module, "ROOT", root)
    monkeypatch.setattr(app_module, "DATA_UPLOAD_DIR", root / "data" / "uploads")
    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {
            "last_data_file": str(managed),
            "recent_data_files": [str(managed), str(external)],
        },
    )

    rows = app_module._list_historical_data_files()

    assert [row["data_file"] for row in rows] == [
        str(managed.resolve()),
        str(external.resolve()),
    ]
    assert all(row["delete_mode"] == "file" for row in rows)


def test_history_list_scans_entire_project_data_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    data_file = root / "data" / "nested" / "TEST_M30.parquet"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"PAR1")

    monkeypatch.setattr(app_module, "ROOT", root)
    monkeypatch.setattr(app_module, "DATA_UPLOAD_DIR", root / "data" / "uploads")
    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {
            "last_data_file": "",
            "recent_data_files": [],
            "last_strategy_file": "",
        },
    )

    rows = app_module._list_historical_data_files()

    assert [row["data_file"] for row in rows] == [str(data_file.resolve())]
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["timeframe"] == "M30"
