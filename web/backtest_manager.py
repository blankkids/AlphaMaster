"""Subprocess manager for run_backtest.py jobs.

镜像 training_manager 的设计：用子进程运行 run_backtest.py，
把 stdout 写入 logs/backtest_*.log，前端通过轮询读取尾部日志与阶段进度。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.train_logging import strip_ansi

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
BACKTEST_RUNS_DIR = PROJECT_ROOT / "backtest_output" / "runs"
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

# 回测阶段：用日志关键字推断当前进行到哪一步，用于前端进度展示
BACKTEST_PHASES: list[tuple[str, str]] = [
    ("init", "初始化"),
    ("cost", "交易成本"),
    ("strategy", "加载策略"),
    ("data", "加载行情数据"),
    ("compute", "回测计算"),
    ("chart", "生成图表"),
    ("done", "完成"),
]
_PHASE_KEYS = [p[0] for p in BACKTEST_PHASES]


class JobState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class BacktestJob:
    run_id: str
    strategy_file: str
    symbol: str
    output_dir: str
    data_file: str
    timeframe: str | None = None
    commission_pct: float = 0.02
    slippage_pct: float = 0.01
    state: JobState = JobState.RUNNING
    pid: int | None = None
    log_path: str = ""
    started_at: str = ""
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy_file": self.strategy_file,
            "strategy_name": Path(self.strategy_file).name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "data_file": self.data_file,
            "output_dir": self.output_dir,
            "commission_pct": self.commission_pct,
            "slippage_pct": self.slippage_pct,
            "state": self.state.value,
            "pid": self.pid,
            "log_path": self.log_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
        }


def resolve_backtest_run_dir(run_id: str) -> Path:
    """Resolve a persisted run directory without allowing path traversal."""
    value = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("非法回测批次 ID")
    path = (BACKTEST_RUNS_DIR / value).resolve()
    try:
        path.relative_to(BACKTEST_RUNS_DIR.resolve())
    except ValueError:
        raise ValueError("非法回测批次路径") from None
    return path


def list_persisted_backtest_runs(limit: int = 100) -> list[dict[str, Any]]:
    """Load newest persisted backtest run metadata from disk."""
    if not BACKTEST_RUNS_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for metadata_path in BACKTEST_RUNS_DIR.glob("*/run.json"):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        run_id = str(data.get("run_id") or metadata_path.parent.name)
        try:
            output_dir = resolve_backtest_run_dir(run_id)
        except ValueError:
            continue
        data["run_id"] = run_id
        data["available"] = (output_dir / "multi_factor_report.json").exists()
        rows.append(data)
    rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
    return rows[: max(1, int(limit))]


class BacktestManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._job: BacktestJob | None = None
        self._log_fp = None
        self._stopped_by_user = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_state()
            job_dict = self._job.to_dict() if self._job else None
        phase_key, phase_label, phase_idx = self._current_phase()
        return {
            "active": self._job is not None and self._job.state == JobState.RUNNING,
            "job": job_dict,
            "phase": phase_key,
            "phase_label": phase_label,
            "phase_index": phase_idx,
            "phase_total": len(BACKTEST_PHASES),
        }

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_state()
        return list_persisted_backtest_runs(limit)

    def start(
        self,
        strategy_file: str,
        data_file: str | None = None,
        commission_pct: float = 0.02,
        slippage_pct: float = 0.01,
        engine: str = "vectorized",
    ) -> BacktestJob:
        with self._lock:
            self._refresh_state()
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("已有回测任务在运行")

            from web.strategy_file import inspect_strategy_file

            info = inspect_strategy_file(strategy_file)
            symbol = info.get("symbol") or ""
            timeframe = info.get("timeframe")

            if not data_file:
                raise RuntimeError(
                    "回测必须使用本地 Parquet（策略未记录 data_file，且未传入数据文件）"
                )

            now = datetime.now(timezone.utc)
            run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            output_dir = BACKTEST_RUNS_DIR / run_id
            output_dir.mkdir(parents=True, exist_ok=False)
            log_path = LOG_DIR / f"backtest_{run_id}.log"

            cmd = [
                sys.executable,
                "-u",
                "run_backtest.py",
                "--strategy-file",
                strategy_file,
                "--commission",
                str(commission_pct),
                "--slippage",
                str(slippage_pct),
                "--output-dir",
                str(output_dir),
            ]
            cmd.extend(["--data-file", data_file])
            if engine and engine != "vectorized":
                cmd.extend(["--engine", engine])

            self._log_fp = open(log_path, "w", encoding="utf-8", buffering=1)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["LOGURU_COLORIZE"] = "0"

            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            self._stopped_by_user = False
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=PROJECT_ROOT,
                    stdout=self._log_fp,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=creationflags,
                )
            except Exception:
                self._log_fp.close()
                self._log_fp = None
                raise
            self._job = BacktestJob(
                run_id=run_id,
                strategy_file=strategy_file,
                symbol=symbol,
                timeframe=str(timeframe) if timeframe else None,
                data_file=data_file,
                output_dir=str(output_dir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                commission_pct=float(commission_pct),
                slippage_pct=float(slippage_pct),
                pid=self._proc.pid,
                log_path=str(log_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                started_at=now.isoformat(),
            )
            self._persist_job()
            return self._job

    def stop(self) -> bool:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._stopped_by_user = True
            try:
                self._proc.terminate()
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            return True

    def _current_phase(self) -> tuple[str, str, int]:
        """根据日志内容推断当前回测阶段。"""
        lines = self.tail_log(200)
        if not lines:
            return ("init", "初始化", 0)
        text = "\n".join(lines)
        # 从后往前匹配最靠后的阶段关键字
        detected = "init"
        if "交易成本" in text or "手续费=" in text:
            detected = "cost"
        if "加载各品种策略" in text or re.search(r"score=", text) or "模式:" in text:
            detected = "strategy"
        if "正在加载数据" in text:
            detected = "data"
        if re.search(r"品种:\s*\[", text) or "多因子回测报告" in text:
            detected = "compute"
        if "生成 K 线图" in text or "张缩放图" in text:
            detected = "chart"
        if "完成。" in text or "JSON 报告已保存" in text:
            detected = "done"
        idx = _PHASE_KEYS.index(detected) if detected in _PHASE_KEYS else 0
        label = BACKTEST_PHASES[idx][1]
        return (detected, label, idx)

    def tail_log(self, lines: int = 200) -> list[str]:
        with self._lock:
            if not self._job or not self._job.log_path:
                return []
            path = PROJECT_ROOT / self._job.log_path
            if not path.exists():
                return []
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
            return [strip_ansi(line) for line in content.splitlines()[-lines:]]

    def _refresh_state(self) -> None:
        if self._proc is None or self._job is None:
            return
        code = self._proc.poll()
        if code is None:
            return
        self._job.exit_code = code
        self._job.finished_at = datetime.now(timezone.utc).isoformat()
        if self._job.state == JobState.RUNNING:
            if self._stopped_by_user:
                self._job.state = JobState.STOPPED
            elif code == 0:
                self._job.state = JobState.COMPLETED
            elif code < 0:
                self._job.state = JobState.STOPPED
            else:
                self._job.state = JobState.FAILED
        if self._job.state == JobState.FAILED and self._job.error is None:
            self._job.error = f"回测进程异常退出 (exit_code={code})"
            try:
                if self._job.log_path:
                    path = PROJECT_ROOT / self._job.log_path
                    with path.open("a", encoding="utf-8") as fp:
                        fp.write(f"\n[Web] 回测进程已结束，退出码: {code}\n")
            except OSError:
                pass
        if self._log_fp:
            try:
                self._log_fp.flush()
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None
        self._persist_job()
        self._proc = None

    def _persist_job(self) -> None:
        if self._job is None or not self._job.output_dir:
            return
        output_dir = (PROJECT_ROOT / self._job.output_dir).resolve()
        try:
            output_dir.relative_to(BACKTEST_RUNS_DIR.resolve())
        except ValueError:
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = output_dir / "run.json"
            tmp_path = output_dir / "run.json.tmp"
            tmp_path.write_text(
                json.dumps(self._job.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(metadata_path)
        except OSError:
            pass


backtest_manager = BacktestManager()
