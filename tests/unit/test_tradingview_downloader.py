from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from download_tradingview_klines import (
    _BROWSER_FETCH_JS,
    _browser_context_is_authenticated,
    _extract_auth_token,
    _frame,
    default_output_path,
    infer_pro_name,
    load_configured_auth_token,
    main,
    parse_user_datetime,
    prepare_dataframe,
    replay_symbol_fallback,
    validate_training_output_path,
)


def test_infer_china_exchange() -> None:
    assert infer_pro_name("159170") == "SZSE:159170"
    assert infer_pro_name("600519") == "SSE:600519"
    assert infer_pro_name("510300") == "SSE:510300"


def test_explicit_exchange_and_pro_name() -> None:
    assert infer_pro_name("aapl", "nasdaq") == "NASDAQ:AAPL"
    assert infer_pro_name("hkex:700") == "HKEX:700"


def test_replay_symbol_uses_dly_feed() -> None:
    assert replay_symbol_fallback("SSE:520840") == "SSE_DLY:520840"
    assert replay_symbol_fallback("NASDAQ:AAPL") == "NASDAQ_DLY:AAPL"
    assert replay_symbol_fallback("SSE_DLY:520840") == "SSE_DLY:520840"


def test_browser_fetch_contains_replay_history_protocol() -> None:
    for method in (
        "replay_create_session",
        "replay_get_depth",
        "replay_reset",
        "replay_add_series",
        "replay_step",
    ):
        assert method in _BROWSER_FETCH_JS
    assert 'message.m === "du"' in _BROWSER_FETCH_JS


def test_date_only_end_uses_end_of_day() -> None:
    ts = parse_user_datetime(
        "2026-07-24",
        "Asia/Shanghai",
        end_of_day=True,
    )
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Shanghai"))
    assert dt.hour == 23
    assert dt.minute == 59


def test_prepare_dataframe_filters_and_deduplicates() -> None:
    rows = [
        {"time": 100, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10},
        {"time": 200, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "tick_volume": 20},
        {"time": 200, "open": 2, "high": 4, "low": 1.5, "close": 3.5, "tick_volume": 30},
        {"time": 300, "open": 3, "high": 4, "low": 2.5, "close": 3.5, "tick_volume": 40},
    ]
    df = prepare_dataframe(
        rows,
        start_ts=150,
        end_ts=250,
        target_bars=5000,
        all_history=False,
        interval_seconds=300,
        keep_forming=True,
    )
    assert list(df["time"]) == [200]
    assert float(df["high"].iloc[0]) == 4.0
    assert int(df["tick_volume"].iloc[0]) == 30


def test_default_output_is_training_compatible() -> None:
    path = default_output_path("SZSE:159170", "5m", "data")
    assert path.as_posix() == "data/159170_M5.parquet"


def test_custom_output_must_keep_training_timeframe_suffix() -> None:
    validate_training_output_path(Path("exports/my_stock_M5.parquet"), "5m")
    with pytest.raises(ValueError, match="_M5.parquet"):
        validate_training_output_path(Path("exports/my_stock.parquet"), "5m")


def test_extracts_authenticated_token_from_tradingview_frame() -> None:
    frame = _frame("set_auth_token", ["signed-in-token"])
    assert _extract_auth_token(frame) == "signed-in-token"
    assert (
        _extract_auth_token(
            _frame("set_auth_token", ["unauthorized_user_token"])
        )
        is None
    )


def test_auth_token_prefers_environment_then_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "web_settings.json"
    settings_path.write_text(
        '{"tradingview_auth_token": "settings-token"}',
        encoding="utf-8",
    )

    monkeypatch.setenv("TRADINGVIEW_AUTH_TOKEN", "environment-token")
    assert load_configured_auth_token(settings_path) == (
        "environment-token",
        "环境变量 TRADINGVIEW_AUTH_TOKEN",
    )

    monkeypatch.delenv("TRADINGVIEW_AUTH_TOKEN")
    assert load_configured_auth_token(settings_path) == (
        "settings-token",
        "web_settings.json",
    )


def test_login_rejects_websocket_only_transport() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["520840", "--login", "--transport", "websocket"])
    assert exc_info.value.code == 2


def test_show_browser_rejects_websocket_only_transport() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["520840", "--show-browser", "--transport", "websocket"])
    assert exc_info.value.code == 2


def test_browser_login_detects_tradingview_session_cookie() -> None:
    class FakeContext:
        pages = []

        @staticmethod
        def cookies(_url: str) -> list[dict[str, str]]:
            return [{"name": "sessionid", "value": "signed-in"}]

    assert _browser_context_is_authenticated(FakeContext()) is True


def test_login_timeout_range_is_validated() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["520840", "--login-timeout", "10"])
    assert exc_info.value.code == 2
