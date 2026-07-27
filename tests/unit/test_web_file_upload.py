from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

import web.app as web_app


def test_browser_uploads_training_parquet(
    tmp_path,
    monkeypatch,
) -> None:
    upload_root = tmp_path / "data"
    monkeypatch.setattr(web_app, "DATA_UPLOAD_DIR", upload_root)
    monkeypatch.setattr(web_app, "save_settings", lambda payload: payload)

    parquet = BytesIO()
    pd.DataFrame(
        {
            "time": range(1_700_000_000, 1_700_003_000),
            "open": [1.0] * 3000,
            "high": [1.0] * 3000,
            "low": [1.0] * 3000,
            "close": [1.0] * 3000,
            "tick_volume": [1] * 3000,
        }
    ).to_parquet(parquet, index=False)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/data-file/upload",
        files={
            "file": (
                "REMOTE_M5.parquet",
                parquet.getvalue(),
                "application/octet-stream",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["uploaded"] is True
    assert payload["symbol"] == "REMOTE"
    assert payload["timeframe"] == "M5"
    assert payload["bars"] == 3000
    assert upload_root in Path(payload["data_file"]).parents


def test_browser_uploads_strategy_json(
    tmp_path,
    monkeypatch,
) -> None:
    upload_root = tmp_path / "strategies"
    monkeypatch.setattr(web_app, "STRATEGY_UPLOAD_DIR", upload_root)
    monkeypatch.setattr(web_app, "save_settings", lambda payload: payload)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/strategy-file/upload",
        files={
            "file": (
                "remote_strategy.json",
                json.dumps(
                    {
                        "symbol": "REMOTE",
                        "formula": [1, 2, 3],
                        "best_score": 0.5,
                    }
                ).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["uploaded"] is True
    assert payload["symbol"] == "REMOTE"
    assert payload["best_score"] == 0.5


def test_browser_upload_rejects_wrong_extension(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_app, "DATA_UPLOAD_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app, "save_settings", lambda payload: payload)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/data-file/upload",
        files={"file": ("not-parquet.txt", b"bad", "text/plain")},
    )

    assert response.status_code == 400
    assert ".parquet" in response.json()["detail"]
