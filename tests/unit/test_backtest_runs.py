from __future__ import annotations

import json
from pathlib import Path

import pytest

import web.backtest_manager as backtest_module


class FakeProcess:
    def __init__(self, cmd: list[str], **kwargs) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4321
        self.return_code: int | None = None

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.return_code = -15

    def kill(self) -> None:
        self.return_code = -9


def test_backtest_run_uses_independent_output_and_persists_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    runs_dir = tmp_path / "backtest_output" / "runs"
    strategy_file = tmp_path / "strategies" / "best_TEST_M5.json"
    data_file = tmp_path / "data" / "TEST_M5.parquet"

    monkeypatch.setattr(backtest_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(backtest_module, "LOG_DIR", logs_dir)
    monkeypatch.setattr(backtest_module, "BACKTEST_RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        "web.strategy_file.inspect_strategy_file",
        lambda _: {"symbol": "TEST", "timeframe": "M5"},
    )
    processes: list[FakeProcess] = []

    def fake_popen(cmd: list[str], **kwargs) -> FakeProcess:
        process = FakeProcess(cmd, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(backtest_module.subprocess, "Popen", fake_popen)

    manager = backtest_module.BacktestManager()
    job = manager.start(str(strategy_file), str(data_file))

    assert job.run_id
    assert job.output_dir == f"backtest_output/runs/{job.run_id}"
    output_flag = processes[0].cmd.index("--output-dir")
    assert Path(processes[0].cmd[output_flag + 1]) == runs_dir / job.run_id

    metadata_path = runs_dir / job.run_id / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "running"
    assert metadata["symbol"] == "TEST"
    assert metadata["timeframe"] == "M5"

    (metadata_path.parent / "multi_factor_report.json").write_text("{}", encoding="utf-8")
    processes[0].return_code = 0
    status = manager.status()
    runs = manager.list_runs()

    assert status["job"]["state"] == "completed"
    assert runs[0]["run_id"] == job.run_id
    assert runs[0]["available"] is True


@pytest.mark.parametrize("run_id", ["", "../escape", "20260730T120000Z-nothex"])
def test_resolve_backtest_run_dir_rejects_invalid_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    monkeypatch.setattr(backtest_module, "BACKTEST_RUNS_DIR", tmp_path / "runs")

    with pytest.raises(ValueError, match="非法回测批次"):
        backtest_module.resolve_backtest_run_dir(run_id)


def test_backtest_report_and_equity_can_read_a_selected_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web.app as app_module

    run_dir = tmp_path / "backtest_output" / "runs" / "selected"
    run_dir.mkdir(parents=True)
    report = {
        "symbols": {
            "TEST": {
                "total_return": 0.12,
                "sharpe": 1.5,
                "sortino": 2.0,
                "n_trades": 8,
                "win_rate": 0.625,
            }
        },
        "portfolio": {"total_return": 0.12, "sharpe": 1.5},
    }
    equity = {
        "symbols": {"TEST": {"equity": [0.0, 0.12]}},
        "portfolio": {"equity": [0.0, 0.12]},
    }
    (run_dir / "multi_factor_report.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )
    (run_dir / "equity_curve.json").write_text(
        json.dumps(equity),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "resolve_backtest_run_dir", lambda _: run_dir)

    report_response = app_module.api_backtest_report(run_id="selected")
    equity_response = app_module.api_backtest_equity(run_id="selected")

    assert report_response["available"] is True
    assert report_response["focus_symbol"] == "TEST"
    assert report_response["report"]["symbols"]["TEST"]["total_return"] == 0.12
    assert equity_response["available"] is True
    assert equity_response["focus_symbol"] == "TEST"
    assert equity_response["data"]["symbols"]["TEST"]["equity"] == [0.0, 0.12]
