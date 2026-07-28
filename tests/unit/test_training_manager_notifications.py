from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest

import web.training_manager as training


class FakeProcess:
    def __init__(self, *, pid: int = 4321, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.finished = Event()
        if exit_code is not None:
            self.finished.set()

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = -15
        self.finished.set()

    def kill(self) -> None:
        self.exit_code = -9
        self.finished.set()

    def wait(self) -> int | None:
        self.finished.wait(timeout=2)
        return self.exit_code

    def finish(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.finished.set()


def _prepare_manager(
    monkeypatch,
    tmp_path,
    process: FakeProcess,
    *,
    enable_watcher: bool = False,
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(training, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(training, "LOG_DIR", log_dir)
    monkeypatch.setattr(training.subprocess, "Popen", lambda *_args, **_kwargs: process)

    events = []
    monkeypatch.setattr(
        training,
        "_dispatch_training_notification",
        lambda event, job: events.append((event, replace(job))),
    )
    manager = training.TrainingManager()
    monkeypatch.setattr(manager, "_record_session_time", lambda: None)
    if not enable_watcher:
        monkeypatch.setattr(manager, "_start_process_watcher", lambda _process: None)
    return manager, events


def test_training_start_and_completion_dispatch_notifications(
    monkeypatch,
    tmp_path,
) -> None:
    process = FakeProcess()
    manager, events = _prepare_manager(monkeypatch, tmp_path, process)

    job = manager.start(
        str(tmp_path / "XAUUSD_H1.parquet"),
        "XAUUSD",
        "H1",
        from_scratch=True,
    )

    assert job.from_scratch is True
    assert [event for event, _job in events] == ["started"]
    assert events[0][1].pid == process.pid

    process.exit_code = 0
    status = manager.status()

    assert status["job"]["state"] == "completed"
    assert [event for event, _job in events] == ["started", "completed"]
    assert events[-1][1].finished_at is not None


def test_training_nonzero_exit_dispatches_failure(
    monkeypatch,
    tmp_path,
) -> None:
    process = FakeProcess()
    manager, events = _prepare_manager(monkeypatch, tmp_path, process)
    manager.start(str(tmp_path / "BTCUSDT_H1.parquet"), "BTCUSDT", "H1")

    process.exit_code = 2
    status = manager.status()

    assert status["job"]["state"] == "failed"
    assert status["job"]["exit_code"] == 2
    assert "exit_code=2" in status["job"]["error"]
    assert [event for event, _job in events] == ["started", "failed"]


def test_watcher_dispatches_completion_without_status_polling(
    monkeypatch,
    tmp_path,
) -> None:
    process = FakeProcess()
    manager, events = _prepare_manager(
        monkeypatch,
        tmp_path,
        process,
        enable_watcher=True,
    )
    notification_sent = Event()

    def capture_event(event, job) -> None:
        events.append((event, replace(job)))
        if event == "completed":
            notification_sent.set()

    monkeypatch.setattr(training, "_dispatch_training_notification", capture_event)
    manager.start(str(tmp_path / "SILVER_H1.parquet"), "SILVER", "H1")

    process.finish(0)

    assert notification_sent.wait(timeout=2)
    assert [event for event, _job in events] == ["started", "completed"]
    assert manager.status()["job"]["state"] == "completed"


def test_training_launch_failure_dispatches_failure(
    monkeypatch,
    tmp_path,
) -> None:
    process = FakeProcess()
    manager, events = _prepare_manager(monkeypatch, tmp_path, process)
    monkeypatch.setattr(
        training.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot spawn")),
    )

    with pytest.raises(OSError, match="cannot spawn"):
        manager.start(str(tmp_path / "EURUSD_H1.parquet"), "EURUSD", "H1")

    status = manager.status()
    assert status["job"]["state"] == "failed"
    assert "cannot spawn" in status["job"]["error"]
    assert [event for event, _job in events] == ["failed"]


def test_user_stop_does_not_report_failure(monkeypatch, tmp_path) -> None:
    process = FakeProcess()
    manager, events = _prepare_manager(monkeypatch, tmp_path, process)
    manager.start(str(tmp_path / "GBPUSD_H1.parquet"), "GBPUSD", "H1")

    assert manager.stop() is True
    status = manager.status()

    assert status["job"]["state"] == "stopped"
    assert [event for event, _job in events] == ["started"]
