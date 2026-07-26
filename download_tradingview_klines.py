"""
从 TradingView 下载股票/ETF K 线并保存为 AlphaMaster 可训练的 Parquet。

示例：
    # 中国股票/ETF 可根据六位代码自动推断 SSE/SZSE
    python download_tradingview_klines.py 159170 -t 5m --all

    # 指定交易所、日期范围
    python download_tradingview_klines.py AAPL -e NASDAQ -t 1h \
        --start 2024-01-01 --end 2025-12-31

    # 下载最近 3000 根
    python download_tradingview_klines.py SZSE:159170 -t 5m --bars 3000

输出列固定为：
    time, open, high, low, close, tick_volume

说明：
    - 默认优先使用本机 Chrome/Edge 中的浏览器 WebSocket，因为某些网络会阻断
      Python websocket-client，但允许浏览器访问 TradingView。
    - --transport websocket 可强制使用 Python WebSocket。
    - 匿名访问的历史深度、品种和交易所覆盖范围由 TradingView 决定。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import string
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


TV_WS_URL = (
    "wss://data.tradingview.com/socket.io/websocket"
    "?from=www.tradingview.com%2Fchart%2F"
)

TIMEFRAMES: dict[str, tuple[str, str, int]] = {
    # CLI token: (TradingView interval, AlphaMaster filename tag, nominal seconds)
    "1m": ("1", "M1", 60),
    "5m": ("5", "M5", 5 * 60),
    "15m": ("15", "M15", 15 * 60),
    "30m": ("30", "M30", 30 * 60),
    "1h": ("1H", "H1", 60 * 60),
    "4h": ("4H", "H4", 4 * 60 * 60),
    "1d": ("1D", "D1", 24 * 60 * 60),
    "1w": ("1W", "W1", 7 * 24 * 60 * 60),
    "1M": ("1M", "MN1", 31 * 24 * 60 * 60),
}

TIMEFRAME_ALIASES = {
    "m1": "1m",
    "1min": "1m",
    "m5": "5m",
    "5min": "5m",
    "m15": "15m",
    "15min": "15m",
    "m30": "30m",
    "30min": "30m",
    "h1": "1h",
    "60m": "1h",
    "60min": "1h",
    "h4": "4h",
    "240m": "4h",
    "240min": "4h",
    "d1": "1d",
    "daily": "1d",
    "w1": "1w",
    "weekly": "1w",
    "mn1": "1M",
    "monthly": "1M",
}

_BROWSER_FETCH_JS = r"""
async (opts) => {
  const randomId = (prefix) =>
    prefix + Math.random().toString(36).slice(2, 14);
  const chartSession = randomId("cs_");

  const wrap = (method, params) => {
    const payload = JSON.stringify({m: method, p: params});
    return `~m~${payload.length}~m~${payload}`;
  };

  const decodeFrames = (text) => {
    const frames = [];
    let pos = 0;
    while (true) {
      const first = text.indexOf("~m~", pos);
      if (first < 0) break;
      const second = text.indexOf("~m~", first + 3);
      if (second < 0) break;
      const length = Number(text.slice(first + 3, second));
      if (!Number.isFinite(length)) break;
      const start = second + 3;
      frames.push(text.slice(start, start + length));
      pos = start + length;
    }
    return frames;
  };

  return await new Promise((resolve, reject) => {
    const bars = new Map();
    let lastCompletedCount = -1;
    let pages = 0;
    let settled = false;
    let timer = null;

    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket.close(); } catch (_) {}
      const output = [...bars.values()].sort((a, b) => a.time - b.time);
      resolve({bars: output, pages});
    };

    const fail = (message) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket.close(); } catch (_) {}
      reject(new Error(message));
    };

    const needMore = () => {
      if (bars.size >= opts.maxBars) return false;
      const timestamps = [...bars.keys()];
      if (!timestamps.length) return false;
      let earliest = Infinity;
      for (const timestamp of timestamps) {
        if (timestamp < earliest) {
          earliest = timestamp;
        }
      }

      if (opts.allHistory) return true;
      if (opts.startTs !== null) return earliest > opts.startTs;

      const eligible = timestamps.filter(
        (ts) => opts.endTs === null || ts <= opts.endTs
      ).length;
      return eligible < opts.targetBars;
    };

    const socket = new WebSocket(opts.wsUrl);
    const armTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(
        () => fail(`TradingView WebSocket 超时（${opts.timeoutSeconds}s）`),
        opts.timeoutSeconds * 1000
      );
    };
    armTimeout();

    socket.onopen = () => {
      const symbolSpec =
        `={"symbol":"${opts.proName}",` +
        `"adjustment":"${opts.adjustment}","session":"${opts.session}"}`;
      const messages = [
        ["set_auth_token", ["unauthorized_user_token"]],
        ["chart_create_session", [chartSession, ""]],
        ["resolve_symbol", [chartSession, "symbol_1", symbolSpec]],
        [
          "create_series",
          [
            chartSession, "s1", "s1", "symbol_1",
            opts.interval, opts.initialBars
          ]
        ],
        ["switch_timezone", [chartSession, "exchange"]],
      ];
      for (const [method, params] of messages) {
        socket.send(wrap(method, params));
      }
    };

    socket.onerror = () => fail("TradingView WebSocket 连接失败");

    socket.onmessage = (event) => {
      const text = String(event.data);
      if (text.includes("~h~")) {
        socket.send(text);
        return;
      }

      for (const payload of decodeFrames(text)) {
        let message;
        try {
          message = JSON.parse(payload);
        } catch (_) {
          continue;
        }

        if (message.m === "symbol_error" || message.m === "critical_error") {
          fail(`TradingView 品种解析失败：${opts.proName}`);
          return;
        }

        if (message.m === "timescale_update") {
          const rows = message.p?.[1]?.s1?.s || [];
          for (const item of rows) {
            const values = item.v || [];
            if (values.length < 6) continue;
            const ts = Number(values[0]);
            if (!Number.isFinite(ts)) continue;
            bars.set(ts, {
              time: Math.trunc(ts),
              open: Number(values[1]),
              high: Number(values[2]),
              low: Number(values[3]),
              close: Number(values[4]),
              tick_volume: Number(values[5] || 0),
            });
          }
        }

        if (message.m === "no_data") {
          finish();
          return;
        }

        if (message.m === "series_completed") {
          pages += 1;
          const noGrowth = bars.size === lastCompletedCount;
          lastCompletedCount = bars.size;
          if (noGrowth || !needMore()) {
            finish();
          } else {
            const remaining = Math.max(1, opts.maxBars - bars.size);
            armTimeout();
            socket.send(
              wrap(
                "request_more_data",
                [chartSession, "s1", Math.min(opts.chunkSize, remaining)]
              )
            );
          }
        }
      }
    };
  });
}
"""


@dataclass(frozen=True)
class DownloadRequest:
    pro_name: str
    interval: str
    start_ts: int | None
    end_ts: int | None
    target_bars: int
    all_history: bool
    max_bars: int
    chunk_size: int
    timeout_seconds: int
    session: str
    adjustment: str


def normalize_timeframe(value: str) -> str:
    raw = str(value or "").strip()
    if raw in TIMEFRAMES:
        return raw
    alias = TIMEFRAME_ALIASES.get(raw.lower())
    if alias:
        return alias
    raise argparse.ArgumentTypeError(
        f"不支持周期 {value!r}；可选: {', '.join(TIMEFRAMES)}"
    )


def infer_pro_name(symbol: str, exchange: str | None = None) -> str:
    """返回 TradingView 的 EXCHANGE:CODE 标识。

    六位中国证券代码可自动推断交易所；其他品种应显式传入交易所。
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        raise ValueError("品种代码不能为空")
    if ":" in raw:
        ex, code = raw.split(":", 1)
        if not ex or not code:
            raise ValueError(f"无效 TradingView 品种: {symbol}")
        return f"{ex}:{code}"
    if exchange:
        return f"{str(exchange).strip().upper()}:{raw}"
    if re.fullmatch(r"\d{6}", raw):
        # 沪市股票/ETF/基金通常以 5、6、9 开头，其余六位代码默认深市。
        inferred = "SSE" if raw[0] in {"5", "6", "9"} else "SZSE"
        return f"{inferred}:{raw}"
    raise ValueError(
        f"无法自动推断 {symbol!r} 的交易所；请使用 EXCHANGE:CODE，"
        "或添加 --exchange，例如 AAPL --exchange NASDAQ"
    )


def parse_user_datetime(
    value: str | None,
    tz_name: str,
    *,
    end_of_day: bool = False,
) -> int | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"无法解析日期 {value!r}；请使用 YYYY-MM-DD 或 ISO 8601"
        ) from exc

    if parsed.tzinfo is None:
        tz = ZoneInfo(tz_name)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            clock = dt_time.max if end_of_day else dt_time.min
            parsed = datetime.combine(parsed.date(), clock, tzinfo=tz)
        else:
            parsed = parsed.replace(tzinfo=tz)
    return int(parsed.timestamp())


def _session_id(prefix: str) -> str:
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(12))
    return prefix + suffix


def _frame(method: str, params: list[Any]) -> str:
    payload = json.dumps(
        {"m": method, "p": params},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"~m~{len(payload)}~m~{payload}"


def _decode_frames(text: str) -> list[str]:
    frames: list[str] = []
    pos = 0
    while True:
        first = text.find("~m~", pos)
        if first < 0:
            break
        second = text.find("~m~", first + 3)
        if second < 0:
            break
        try:
            length = int(text[first + 3 : second])
        except ValueError:
            break
        start = second + 3
        frames.append(text[start : start + length])
        pos = start + length
    return frames


def _need_more(
    timestamps: list[int],
    request: DownloadRequest,
) -> bool:
    if not timestamps or len(timestamps) >= request.max_bars:
        return False
    if request.all_history:
        return True
    if request.start_ts is not None:
        return min(timestamps) > request.start_ts
    eligible = sum(
        1
        for ts in timestamps
        if request.end_ts is None or ts <= request.end_ts
    )
    return eligible < request.target_bars


def fetch_with_websocket(request: DownloadRequest) -> tuple[list[dict], int]:
    """使用 websocket-client 直接连接 TradingView。"""
    try:
        from websocket import create_connection
    except ImportError as exc:
        raise RuntimeError("缺少 websocket-client，请先安装 requirements.txt") from exc

    chart_session = _session_id("cs_")
    bars: dict[int, dict[str, int | float]] = {}
    pages = 0
    last_completed_count = -1

    try:
        ws = create_connection(
            TV_WS_URL,
            origin="https://www.tradingview.com",
            timeout=request.timeout_seconds,
        )
    except Exception as exc:
        raise RuntimeError(f"Python WebSocket 无法连接 TradingView: {exc}") from exc

    symbol_spec = json.dumps(
        {
            "symbol": request.pro_name,
            "adjustment": request.adjustment,
            "session": request.session,
        },
        separators=(",", ":"),
    )
    initial = min(request.chunk_size, request.max_bars)
    messages = [
        ("set_auth_token", ["unauthorized_user_token"]),
        ("chart_create_session", [chart_session, ""]),
        ("resolve_symbol", [chart_session, "symbol_1", "=" + symbol_spec]),
        (
            "create_series",
            [
                chart_session,
                "s1",
                "s1",
                "symbol_1",
                request.interval,
                initial,
            ],
        ),
        ("switch_timezone", [chart_session, "exchange"]),
    ]
    for method, params in messages:
        ws.send(_frame(method, params))

    deadline = time.monotonic() + request.timeout_seconds
    try:
        while time.monotonic() < deadline:
            try:
                text = str(ws.recv())
            except Exception as exc:
                raise RuntimeError(f"读取 TradingView 数据失败: {exc}") from exc
            if "~h~" in text:
                ws.send(text)
                continue

            for payload in _decode_frames(text):
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                method = message.get("m")
                if method in {"symbol_error", "critical_error"}:
                    raise RuntimeError(f"TradingView 品种解析失败: {request.pro_name}")
                if method == "timescale_update":
                    series = ((message.get("p") or [None, {}])[1] or {}).get("s1", {})
                    for item in series.get("s") or []:
                        values = item.get("v") or []
                        if len(values) < 6:
                            continue
                        ts = int(float(values[0]))
                        bars[ts] = {
                            "time": ts,
                            "open": float(values[1]),
                            "high": float(values[2]),
                            "low": float(values[3]),
                            "close": float(values[4]),
                            "tick_volume": float(values[5] or 0),
                        }
                if method == "no_data":
                    return sorted(bars.values(), key=lambda row: row["time"]), pages
                if method == "series_completed":
                    pages += 1
                    no_growth = len(bars) == last_completed_count
                    last_completed_count = len(bars)
                    if no_growth or not _need_more(list(bars), request):
                        return (
                            sorted(bars.values(), key=lambda row: row["time"]),
                            pages,
                        )
                    remaining = max(1, request.max_bars - len(bars))
                    ws.send(
                        _frame(
                            "request_more_data",
                            [
                                chart_session,
                                "s1",
                                min(request.chunk_size, remaining),
                            ],
                        )
                    )
                    deadline = time.monotonic() + request.timeout_seconds
        raise RuntimeError("等待 TradingView 数据超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _browser_channels() -> list[tuple[str, str | None]]:
    """按优先级列出 Playwright 浏览器 channel/显式路径。"""
    candidates: list[tuple[str, str | None]] = [
        ("chrome", None),
        ("msedge", None),
    ]
    explicit = [
        ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    candidates.extend((channel, path) for channel, path in explicit if Path(path).is_file())
    return candidates


def fetch_with_browser(request: DownloadRequest) -> tuple[list[dict], int]:
    """在本机 Chrome/Edge 页面上下文中连接 TradingView。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc

    options = {
        "wsUrl": TV_WS_URL,
        "proName": request.pro_name,
        "interval": request.interval,
        "startTs": request.start_ts,
        "endTs": request.end_ts,
        "targetBars": request.target_bars,
        "allHistory": request.all_history,
        "maxBars": request.max_bars,
        "chunkSize": request.chunk_size,
        "initialBars": min(request.chunk_size, request.max_bars),
        "timeoutSeconds": request.timeout_seconds,
        "session": request.session,
        "adjustment": request.adjustment,
    }

    launch_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = None
        for channel, executable in _browser_channels():
            kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if executable:
                kwargs["executable_path"] = executable
            else:
                kwargs["channel"] = channel
            try:
                browser = playwright.chromium.launch(**kwargs)
                break
            except Exception as exc:
                launch_errors.append(f"{channel}: {exc}")
        if browser is None:
            raise RuntimeError(
                "无法启动 Chrome/Edge。请安装其中一个浏览器。\n"
                + "\n".join(launch_errors[-2:])
            )

        try:
            page = browser.new_page()
            page.goto(
                "https://www.tradingview.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            result = page.evaluate(_BROWSER_FETCH_JS, options)
        except Exception as exc:
            raise RuntimeError(f"浏览器抓取 TradingView 失败: {exc}") from exc
        finally:
            browser.close()

    return list(result.get("bars") or []), int(result.get("pages") or 0)


def prepare_dataframe(
    rows: list[dict],
    *,
    start_ts: int | None,
    end_ts: int | None,
    target_bars: int,
    all_history: bool,
    interval_seconds: int,
    keep_forming: bool,
) -> pd.DataFrame:
    columns = ["time", "open", "high", "low", "close", "tick_volume"]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows)
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError(f"TradingView 返回数据缺少列: {missing}")

    df = df[columns].copy()
    for column in columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=columns)
    df["time"] = df["time"].astype("int64")
    df = df.drop_duplicates("time", keep="last").sort_values("time")

    if not keep_forming and not df.empty:
        latest_ts = int(df["time"].iloc[-1])
        if latest_ts + interval_seconds > int(time.time()):
            df = df.iloc[:-1]

    if start_ts is not None:
        df = df[df["time"] >= start_ts]
    if end_ts is not None:
        df = df[df["time"] <= end_ts]
    if not all_history and start_ts is None and len(df) > target_bars:
        df = df.iloc[-target_bars:]

    df = df.reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=columns)

    price_columns = ["open", "high", "low", "close"]
    df[price_columns] = df[price_columns].astype("float32")
    df["tick_volume"] = (
        df["tick_volume"].clip(lower=0).round().astype("int64")
    )

    finite_prices = df[price_columns].map(
        lambda value: math.isfinite(float(value))
    )
    if not bool(finite_prices.all().all()):
        raise ValueError("价格列包含 NaN/Inf")
    valid_ohlc = (
        (df["high"] >= df[["open", "close", "low"]].max(axis=1))
        & (df["low"] <= df[["open", "close", "high"]].min(axis=1))
        & (df[price_columns] > 0).all(axis=1)
    )
    if not bool(valid_ohlc.all()):
        bad = int((~valid_ohlc).sum())
        raise ValueError(f"发现 {bad} 根 OHLC 关系异常的K线")
    return df


def default_output_path(
    pro_name: str,
    timeframe: str,
    output_dir: str | Path,
) -> Path:
    code = pro_name.split(":", 1)[-1]
    safe_code = re.sub(r"[^A-Za-z0-9._-]+", "_", code).strip("_") or "symbol"
    tag = TIMEFRAMES[timeframe][1]
    return Path(output_dir) / f"{safe_code}_{tag}.parquet"


def validate_training_output_path(path: Path, timeframe: str) -> None:
    """Keep custom filenames compatible with AlphaMaster's file parser."""
    tag = TIMEFRAMES[timeframe][1]
    if path.suffix.lower() != ".parquet" or not path.stem.upper().endswith(
        f"_{tag}"
    ):
        raise ValueError(
            f"输出文件名必须以 _{tag}.parquet 结尾，"
            f"例如 159170_{tag}.parquet"
        )


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.parquet")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def format_timestamp(ts: int, tz_name: str) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        ZoneInfo(tz_name)
    ).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载 TradingView K线并保存为 AlphaMaster 训练用 Parquet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s 159170 -t 5m --all\n"
            "  %(prog)s AAPL -e NASDAQ -t 1h --start 2024-01-01 --end 2025-12-31\n"
            "  %(prog)s HKEX:700 -t 1d --bars 3000\n"
        ),
    )
    parser.add_argument("symbol", help="证券代码或 EXCHANGE:CODE")
    parser.add_argument("-e", "--exchange", help="TradingView 交易所，如 NASDAQ/SSE/SZSE")
    parser.add_argument(
        "-t",
        "--timeframe",
        type=normalize_timeframe,
        default="5m",
        help="周期：1m/5m/15m/30m/1h/4h/1d/1w/1M（默认5m）",
    )
    parser.add_argument("--start", help="开始时间，YYYY-MM-DD 或 ISO 8601")
    parser.add_argument("--end", help="结束时间，YYYY-MM-DD 或 ISO 8601")
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="无时区日期的解释和输出时区（默认 Asia/Shanghai）",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=5000,
        help="未给 --start/--all 时保留的最近K线数（默认5000）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="下载 TradingView 当前可访问的全部历史",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=200_000,
        help="分页安全上限（默认200000）",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=5000,
        help="每次向 TradingView 请求的根数（默认5000）",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "browser", "websocket"),
        default="auto",
        help="连接方式；auto先浏览器后Python WebSocket（默认auto）",
    )
    parser.add_argument(
        "--session",
        choices=("regular", "extended"),
        default="regular",
        help="常规或延长交易时段（默认regular）",
    )
    parser.add_argument(
        "--adjustment",
        choices=("splits", "dividends"),
        default="splits",
        help="复权方式（默认splits）",
    )
    parser.add_argument(
        "--keep-forming",
        action="store_true",
        help="保留当前尚未收盘的K线",
    )
    parser.add_argument("-o", "--output", help="输出 Parquet 完整路径")
    parser.add_argument(
        "--output-dir",
        default="data",
        help="未给 --output 时的输出目录（默认data）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="每轮抓取超时秒数（默认120）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.bars <= 0:
        parser.error("--bars 必须大于0")
    if args.max_bars <= 0:
        parser.error("--max-bars 必须大于0")
    if not 1 <= args.chunk_size <= 5000:
        parser.error("--chunk-size 必须在1到5000之间")
    if args.max_bars < args.bars and not args.start and not args.all:
        parser.error("--max-bars 不能小于 --bars")

    try:
        pro_name = infer_pro_name(args.symbol, args.exchange)
        start_ts = parse_user_datetime(args.start, args.timezone)
        end_ts = parse_user_datetime(
            args.end,
            args.timezone,
            end_of_day=True,
        )
        ZoneInfo(args.timezone)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))

    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        parser.error("--start 不能晚于 --end")

    interval, _, interval_seconds = TIMEFRAMES[args.timeframe]
    request = DownloadRequest(
        pro_name=pro_name,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        target_bars=args.bars,
        all_history=bool(args.all),
        max_bars=args.max_bars,
        chunk_size=args.chunk_size,
        timeout_seconds=max(10, args.timeout),
        session=args.session,
        adjustment=args.adjustment,
    )

    output = (
        Path(args.output)
        if args.output
        else default_output_path(pro_name, args.timeframe, args.output_dir)
    )
    try:
        validate_training_output_path(output, args.timeframe)
    except ValueError as exc:
        parser.error(str(exc))

    print("=" * 68)
    print("TradingView K线下载")
    print(f"品种       : {pro_name}")
    print(f"周期       : {args.timeframe} ({interval})")
    print(f"开始       : {args.start or '按根数/全部历史决定'}")
    print(f"结束       : {args.end or '最新'}")
    print(f"连接方式   : {args.transport}")
    print(f"输出       : {output.resolve()}")
    print("=" * 68)

    rows: list[dict] | None = None
    pages = 0
    errors: list[str] = []
    transports = (
        ("browser", "websocket")
        if args.transport == "auto"
        else (args.transport,)
    )
    for transport in transports:
        print(f"[连接] 尝试 {transport} ...", flush=True)
        try:
            if transport == "browser":
                rows, pages = fetch_with_browser(request)
            else:
                rows, pages = fetch_with_websocket(request)
            print(f"[连接] {transport} 成功")
            break
        except Exception as exc:
            message = f"{transport}: {exc}"
            errors.append(message)
            print(f"[连接] {message}", file=sys.stderr)

    if rows is None:
        print("下载失败：所有连接方式均不可用", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if len(rows) >= request.max_bars:
        earliest_raw = min(int(row["time"]) for row in rows)
        if start_ts is not None and earliest_raw > start_ts:
            print(
                "下载停止：已达到 --max-bars，但尚未覆盖 --start。"
                "请增大 --max-bars 后重试。",
                file=sys.stderr,
            )
            return 1
        print(
            f"警告：已达到 --max-bars={request.max_bars:,}，"
            "更早的历史数据可能尚未下载；可增大该参数继续。",
            file=sys.stderr,
        )

    try:
        df = prepare_dataframe(
            rows,
            start_ts=start_ts,
            end_ts=end_ts,
            target_bars=args.bars,
            all_history=bool(args.all),
            interval_seconds=interval_seconds,
            keep_forming=bool(args.keep_forming),
        )
    except ValueError as exc:
        print(f"数据校验失败: {exc}", file=sys.stderr)
        return 1

    if df.empty:
        print("指定条件下没有K线，未生成文件。", file=sys.stderr)
        return 1

    save_parquet(df, output)

    first_ts = int(df["time"].iloc[0])
    last_ts = int(df["time"].iloc[-1])
    print("-" * 68)
    print(f"完成       : {output.resolve()}")
    print(f"K线数量    : {len(df):,}")
    print(f"分页次数   : {pages}")
    print(f"时间范围   : {format_timestamp(first_ts, args.timezone)}")
    print(f"          → {format_timestamp(last_ts, args.timezone)}")
    print(f"文件大小   : {output.resolve().stat().st_size / 1024:.1f} KiB")

    try:
        from config import Config

        if len(df) >= Config.MIN_BARS:
            print(f"训练检查   : 通过（至少 {Config.MIN_BARS:,} 根）")
        else:
            print(
                f"训练检查   : 数据不足，当前 {len(df):,} 根，"
                f"项目至少要求 {Config.MIN_BARS:,} 根"
            )
    except Exception:
        pass

    print("\n训练命令：")
    print(f'  python train_file.py --data-file "{output.resolve()}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
