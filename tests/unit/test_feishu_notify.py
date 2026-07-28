from __future__ import annotations

import web.feishu_notify as feishu


def _capture_messages(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(
        feishu,
        "load_settings",
        lambda: {
            "feishu_enabled": True,
            "feishu_webhook_url": "https://example.test/hook",
        },
    )

    def fake_send_text(text: str, **_kwargs):
        messages.append(text)
        return True, "ok"

    monkeypatch.setattr(feishu, "send_text", fake_send_text)
    return messages


def test_training_lifecycle_messages_include_key_context(monkeypatch) -> None:
    messages = _capture_messages(monkeypatch)
    started_at = "2026-07-28T01:00:00+00:00"
    finished_at = "2026-07-28T02:02:03+00:00"
    common = {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "data_file": r"D:\market-data\XAUUSD_H1.parquet",
        "from_scratch": True,
    }

    assert feishu.notify_training_started(
        **common,
        started_at=started_at,
    ) == (True, "ok")
    assert feishu.notify_training_completed(
        **common,
        started_at=started_at,
        finished_at=finished_at,
        log_path="logs/train_XAUUSD.log",
    ) == (True, "ok")
    assert feishu.notify_training_failed(
        **common,
        started_at=started_at,
        finished_at=finished_at,
        error="训练进程异常退出",
        exit_code=2,
        log_path="logs/train_XAUUSD.log",
    ) == (True, "ok")

    assert "【AlphaMaster 训练开始】" in messages[0]
    assert "品种：XAUUSD · H1" in messages[0]
    assert "方式：重新训练" in messages[0]
    assert "数据：XAUUSD_H1.parquet" in messages[0]

    assert "【AlphaMaster 训练完成】" in messages[1]
    assert "耗时：1小时2分钟3秒" in messages[1]
    assert "日志：logs/train_XAUUSD.log" in messages[1]

    assert "【AlphaMaster 训练失败】" in messages[2]
    assert "退出码：2" in messages[2]
    assert "原因：训练进程异常退出" in messages[2]


def test_training_notification_respects_disabled_setting(monkeypatch) -> None:
    monkeypatch.setattr(
        feishu,
        "load_settings",
        lambda: {
            "feishu_enabled": False,
            "feishu_webhook_url": "https://example.test/hook",
        },
    )
    monkeypatch.setattr(
        feishu,
        "send_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled notifications must not be sent")
        ),
    )

    ok, message = feishu.notify_training_started(
        symbol="BTCUSDT",
        timeframe="1h",
        data_file="BTCUSDT_H1.parquet",
        from_scratch=False,
    )

    assert ok is False
    assert message == "飞书通知未启用"
