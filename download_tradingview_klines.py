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

    # 首次打开浏览器登录；以后普通命令会自动复用登录态
    python download_tradingview_klines.py 520840 -t 5m --all --login

    # 显示 Playwright 浏览器和 TradingView 图表操作过程
    python download_tradingview_klines.py 520840 -t 5m --all --show-browser

输出列固定为：
    time, open, high, low, close, tick_volume

说明：
    - 默认优先使用专用 Chrome/Edge 配置中的 TradingView 登录态；首次可加
      --login 完成登录。没有登录态或认证失败时自动回退匿名连接。
    - --all 在普通图表达到历史上限后，会为登录账户自动切换 K线回放通道，
      从最早可用日期分批补齐并按时间戳去重。
    - 也可通过 TRADINGVIEW_AUTH_TOKEN 环境变量或 web_settings.json 中的
      tradingview_auth_token 配置登录令牌。
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
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd


TV_WS_URL = (
    "wss://data.tradingview.com/socket.io/websocket"
    "?from=www.tradingview.com%2Fchart%2F"
)
UNAUTHORIZED_USER_TOKEN = "unauthorized_user_token"
TRADINGVIEW_AUTH_TOKEN_ENV = "TRADINGVIEW_AUTH_TOKEN"

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
  const replaySession = randomId("rs_");

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
    let phase = "normal";
    let resolvedInfo = {};
    let normalEarliest = null;
    let replayDepthTurnaround = null;
    let replayPoint = null;
    let replayAttempted = false;
    let replayUsed = false;
    let replayBarsAdded = 0;
    let replayEarliest = null;
    let replayReason = "未请求";

    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket.close(); } catch (_) {}
      const output = [...bars.values()].sort((a, b) => a.time - b.time);
      resolve({
        bars: output,
        pages,
        replay: {
          attempted: replayAttempted,
          used: replayUsed,
          barsAdded: replayBarsAdded,
          earliest: replayEarliest,
          reason: replayReason,
        },
      });
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

    const addRows = (rows) => {
      for (const item of rows || []) {
        const values = item.v || [];
        if (values.length < 6) continue;
        const ts = Number(values[0]);
        if (!Number.isFinite(ts)) continue;
        const timestamp = Math.trunc(ts);
        const isNew = !bars.has(timestamp);
        if (isNew && bars.size >= opts.maxBars) continue;
        bars.set(timestamp, {
          time: timestamp,
          open: Number(values[1]),
          high: Number(values[2]),
          low: Number(values[3]),
          close: Number(values[4]),
          tick_volume: Number(values[5] || 0),
        });
        if (phase !== "normal" && isNew) {
          replayBarsAdded += 1;
        }
      }
    };

    const send = (method, params) => socket.send(wrap(method, params));

    const normalSymbol = {
      symbol: opts.proName,
      adjustment: opts.adjustment,
      session: opts.session,
    };

    const replaySymbolSpec = () => {
      const legs = Array.isArray(resolvedInfo.legs) ? resolvedInfo.legs : [];
      const replaySymbol =
        legs.find((value) => typeof value === "string" && value.includes(":")) ||
        opts.replaySymbolFallback;
      const spec = {
        adjustment: opts.adjustment,
        session: opts.session,
        symbol: replaySymbol,
      };
      const currency = resolvedInfo.currency_id || resolvedInfo.currency_code;
      if (currency) spec["currency-id"] = currency;
      return spec;
    };

    const replayWrappedSymbol = () => {
      const symbol = {...normalSymbol};
      const currency = resolvedInfo.currency_id || resolvedInfo.currency_code;
      if (currency) symbol["currency-id"] = currency;
      return {replay: replaySession, symbol};
    };

    const sendReplayStep = () => {
      if (settled || phase !== "replay") return;
      if (bars.size >= opts.maxBars) {
        replayReason = "达到 max-bars";
        finish();
        return;
      }
      if (
        normalEarliest !== null &&
        replayPoint !== null &&
        replayPoint >= normalEarliest
      ) {
        replayReason = "已与普通图表历史衔接";
        finish();
        return;
      }
      const remaining = Math.max(1, opts.maxBars - bars.size);
      const steps = Math.min(opts.replayChunkSize, remaining);
      pages += 1;
      armTimeout();
      send("replay_step", [
        replaySession,
        randomId("rt_"),
        steps,
      ]);
    };

    const startReplay = () => {
      if (
        replayAttempted ||
        !opts.enableReplay ||
        opts.authToken === opts.unauthorizedToken ||
        !bars.size ||
        bars.size >= opts.maxBars
      ) {
        replayReason =
          opts.authToken === opts.unauthorizedToken
            ? "匿名连接无回放权限"
            : "无需或无法启用回放";
        finish();
        return;
      }
      replayAttempted = true;
      phase = "replay-depth";
      normalEarliest = Math.min(...bars.keys());
      replayDepthTurnaround = randomId("rt_");
      armTimeout();
      send("replay_create_session", [replaySession]);
      send("replay_get_depth", [
        replaySession,
        replayDepthTurnaround,
        "=" + JSON.stringify(replaySymbolSpec()),
        opts.interval,
      ]);
    };

    const socket = new WebSocket(opts.wsUrl);
    const armTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(
        () => {
          if (phase !== "normal" && bars.size) {
            replayReason = `回放等待超时（${opts.timeoutSeconds}s）`;
            finish();
          } else {
            fail(`TradingView WebSocket 超时（${opts.timeoutSeconds}s）`);
          }
        },
        opts.timeoutSeconds * 1000
      );
    };
    armTimeout();

    socket.onopen = () => {
      const symbolSpec = "=" + JSON.stringify(normalSymbol);
      const messages = [
        ["set_auth_token", [opts.authToken]],
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
        send(method, params);
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

        if (
          message.m === "symbol_error" ||
          message.m === "critical_error" ||
          message.m === "protocol_error"
        ) {
          if (phase !== "normal" && bars.size) {
            replayReason = `回放协议失败：${message.m}`;
            finish();
            return;
          }
          fail(`TradingView 认证、协议或品种解析失败：${opts.proName}`);
          return;
        }

        if (message.m === "symbol_resolved") {
          const rawInfo = message.p?.[2];
          const info = Array.isArray(rawInfo) ? rawInfo[0] : rawInfo;
          if (info && typeof info === "object") {
            resolvedInfo = info;
          }
        }

        if (message.m === "timescale_update") {
          const rows = message.p?.[1]?.s1?.s || [];
          addRows(rows);
        }

        if (message.m === "du") {
          addRows(message.p?.[1]?.s1?.s || []);
        }

        if (
          message.m === "replay_depth" &&
          message.p?.[1] === replayDepthTurnaround
        ) {
          const depth = Number(message.p?.[2]);
          if (
            !Number.isFinite(depth) ||
            normalEarliest === null ||
            depth >= normalEarliest
          ) {
            replayReason = "回放没有更早历史";
            finish();
            return;
          }
          replayUsed = true;
          replayEarliest = Math.trunc(depth);
          replayPoint = replayEarliest;
          phase = "replay-loading";
          const resetPoint = Math.max(
            replayEarliest,
            opts.startTs === null ? replayEarliest : opts.startTs
          );
          send("replay_reset", [
            replaySession,
            randomId("rt_"),
            resetPoint,
          ]);
          send("replay_add_series", [
            replaySession,
            randomId("rt_"),
            "=" + JSON.stringify(replaySymbolSpec()),
            opts.interval,
          ]);
          send("resolve_symbol", [
            chartSession,
            "replay_symbol_1",
            "=" + JSON.stringify(replayWrappedSymbol()),
          ]);
          send("modify_series", [
            chartSession,
            "s1",
            "replay_s1",
            "replay_symbol_1",
            opts.interval,
            "",
          ]);
          armTimeout();
        }

        if (message.m === "replay_point") {
          const point = Number(message.p?.[1]);
          if (Number.isFinite(point)) {
            replayPoint = Math.trunc(point);
          }
          sendReplayStep();
        }

        if (message.m === "replay_end_of_data") {
          replayReason = "回放已到数据末尾";
          finish();
          return;
        }

        if (message.m === "no_data") {
          if (phase === "normal" && bars.size && needMore()) {
            startReplay();
            continue;
          }
          replayReason =
            phase === "normal" ? "普通图表无数据" : "回放无更多数据";
          finish();
          return;
        }

        if (message.m === "series_completed") {
          if (phase === "replay-loading") {
            phase = "replay";
            sendReplayStep();
            continue;
          }
          if (phase !== "normal") {
            continue;
          }
          pages += 1;
          const noGrowth = bars.size === lastCompletedCount;
          lastCompletedCount = bars.size;
          if (noGrowth) {
            startReplay();
          } else if (!needMore()) {
            replayReason = "普通图表已满足范围";
            finish();
          } else {
            const remaining = Math.max(1, opts.maxBars - bars.size);
            armTimeout();
            send(
              "request_more_data",
              [chartSession, "s1", Math.min(opts.chunkSize, remaining)]
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


def load_configured_auth_token(
    settings_path: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """读取 TradingView 登录令牌，但不把令牌内容写入日志。

    优先使用环境变量，其次读取项目 web_settings.json 中的
    tradingview_auth_token。返回值第二项仅用于显示凭证来源。
    """
    env_token = str(os.getenv(TRADINGVIEW_AUTH_TOKEN_ENV, "")).strip()
    if env_token and env_token != UNAUTHORIZED_USER_TOKEN:
        return env_token, f"环境变量 {TRADINGVIEW_AUTH_TOKEN_ENV}"

    path = (
        Path(settings_path)
        if settings_path is not None
        else Path(__file__).resolve().parent / "web_settings.json"
    )
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        file_token = str(settings.get("tradingview_auth_token", "")).strip()
    except (OSError, ValueError, TypeError):
        file_token = ""
    if file_token and file_token != UNAUTHORIZED_USER_TOKEN:
        return file_token, "web_settings.json"
    return None, None


def default_browser_profile_dir() -> Path:
    """返回保存 TradingView 登录态的专用浏览器目录。"""
    configured = str(os.getenv("TRADINGVIEW_PROFILE_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = str(os.getenv("LOCALAPPDATA", "")).strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".alphamaster"
    return base / "AlphaMaster" / "TradingViewBrowser"


def _extract_auth_token(frame: str) -> str | None:
    """从 TradingView WebSocket 发送帧中提取登录令牌。"""
    for payload in _decode_frames(str(frame)):
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if message.get("m") != "set_auth_token":
            continue
        params = message.get("p") or []
        token = str(params[0]).strip() if params else ""
        if token and token != UNAUTHORIZED_USER_TOKEN:
            return token
    return None


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


def replay_symbol_fallback(pro_name: str) -> str:
    """生成 TradingView 回放深度查询使用的默认 DLY 品种名。"""
    exchange, code = pro_name.split(":", 1)
    if exchange.endswith("_DLY"):
        return pro_name
    return f"{exchange}_DLY:{code}"


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


def fetch_with_websocket(
    request: DownloadRequest,
    auth_token: str | None = None,
) -> tuple[list[dict], int]:
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
        ("set_auth_token", [auth_token or UNAUTHORIZED_USER_TOKEN]),
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
                if method in {
                    "symbol_error",
                    "critical_error",
                    "protocol_error",
                }:
                    raise RuntimeError(
                        "TradingView 认证、协议或品种解析失败: "
                        f"{request.pro_name}"
                    )
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


def _browser_context_is_authenticated(context: Any) -> bool:
    """判断 Playwright 浏览器上下文是否已完成 TradingView 登录。"""
    try:
        cookies = context.cookies("https://www.tradingview.com")
    except Exception:
        cookies = []
    if any(
        cookie.get("name") in {"sessionid", "sessionid_sign"}
        and str(cookie.get("value") or "").strip()
        for cookie in cookies
    ):
        return True

    for candidate in reversed(context.pages):
        try:
            if candidate.evaluate("Boolean(window.is_authenticated)"):
                return True
        except Exception:
            continue
    return False


def fetch_with_browser(
    request: DownloadRequest,
    *,
    configured_auth_token: str | None = None,
    profile_dir: str | Path | None = None,
    interactive_login: bool = False,
    show_browser: bool = False,
    login_timeout_seconds: int = 300,
) -> tuple[list[dict], int, str]:
    """优先使用登录账户，并在不可用时回退到匿名浏览器连接。"""
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
        "replayChunkSize": request.chunk_size,
        "replaySymbolFallback": replay_symbol_fallback(request.pro_name),
        "enableReplay": request.all_history or request.start_ts is not None,
        "unauthorizedToken": UNAUTHORIZED_USER_TOKEN,
        "timeoutSeconds": request.timeout_seconds,
        "session": request.session,
        "adjustment": request.adjustment,
    }

    user_data_dir = (
        Path(profile_dir).expanduser()
        if profile_dir is not None
        else default_browser_profile_dir()
    ).resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    launch_errors: list[str] = []
    with sync_playwright() as playwright:
        context = None
        for channel, executable in _browser_channels():
            kwargs: dict[str, Any] = {
                "headless": not (interactive_login or show_browser),
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if executable:
                kwargs["executable_path"] = executable
            else:
                kwargs["channel"] = channel
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    **kwargs,
                )
                break
            except Exception as exc:
                launch_errors.append(f"{channel}: {exc}")
        if context is None:
            raise RuntimeError(
                "无法启动 Chrome/Edge。请安装其中一个浏览器。\n"
                + "\n".join(launch_errors[-2:])
            )
        if interactive_login or show_browser:
            print(
                f"[浏览器] Playwright 已打开 {request.pro_name} 图表页面。",
                flush=True,
            )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            captured_tokens: list[str] = []

            def capture_websocket(websocket: Any) -> None:
                def capture_frame(frame: str) -> None:
                    token = _extract_auth_token(frame)
                    if token:
                        captured_tokens.append(token)

                websocket.on("framesent", capture_frame)

            page.on("websocket", capture_websocket)
            chart_url = (
                "https://www.tradingview.com/chart/?symbol="
                + quote(request.pro_name, safe="")
            )
            page.goto(
                chart_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(3_000)

            if interactive_login and not captured_tokens:
                print(
                    "[认证] 正在打开 TradingView 登录页，"
                    "请在浏览器中完成登录 ...",
                    flush=True,
                )
                try:
                    page.goto(
                        "https://www.tradingview.com/accounts/signin/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except Exception as exc:
                    print(
                        f"[认证] 登录页打开失败，将使用匿名连接：{exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    deadline = (
                        time.monotonic() + max(30, login_timeout_seconds)
                    )
                    while (
                        time.monotonic() < deadline
                        and not _browser_context_is_authenticated(context)
                    ):
                        page.wait_for_timeout(1_000)

                    if _browser_context_is_authenticated(context):
                        print(
                            "[认证] 已检测到登录成功，正在返回图表读取权限 ...",
                            flush=True,
                        )
                        captured_tokens.clear()
                        page.goto(
                            chart_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        page.wait_for_timeout(5_000)
                        if not captured_tokens:
                            print(
                                "[认证] 登录态未返回有效令牌，"
                                "将使用匿名连接。",
                                file=sys.stderr,
                                flush=True,
                            )
                    else:
                        print(
                            f"[认证] 等待登录超过 "
                            f"{max(30, login_timeout_seconds)} 秒，"
                            "将使用匿名连接。",
                            file=sys.stderr,
                            flush=True,
                        )

            attempts: list[tuple[str, str]] = []
            if configured_auth_token:
                attempts.append(("登录账户（配置令牌）", configured_auth_token))
            if captured_tokens:
                attempts.append(("登录账户（浏览器登录态）", captured_tokens[-1]))
            attempts.append(("匿名连接", UNAUTHORIZED_USER_TOKEN))

            deduplicated: list[tuple[str, str]] = []
            seen_tokens: set[str] = set()
            for label, token in attempts:
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                deduplicated.append((label, token))

            errors: list[str] = []
            for index, (auth_label, token) in enumerate(deduplicated):
                options["authToken"] = token
                try:
                    result = page.evaluate(_BROWSER_FETCH_JS, options)
                    replay = result.get("replay") or {}
                    if replay.get("used"):
                        replay_bars = int(replay.get("barsAdded") or 0)
                        replay_earliest = int(replay.get("earliest") or 0)
                        earliest_text = (
                            format_timestamp(replay_earliest, "UTC")
                            if replay_earliest
                            else "未知"
                        )
                        print(
                            f"[回放] 已启用 K线回放历史通道，"
                            f"新增 {replay_bars:,} 根，"
                            f"最早可用 {earliest_text}。",
                            flush=True,
                        )
                    elif replay.get("attempted"):
                        print(
                            f"[回放] 未补充更早数据："
                            f"{replay.get('reason') or '未知原因'}。",
                            file=sys.stderr,
                            flush=True,
                        )
                    return (
                        list(result.get("bars") or []),
                        int(result.get("pages") or 0),
                        auth_label,
                    )
                except Exception as exc:
                    errors.append(f"{auth_label}: {exc}")
                    if index + 1 < len(deduplicated):
                        print(
                            f"[认证] {auth_label}不可用，尝试"
                            f"{deduplicated[index + 1][0]} ...",
                            file=sys.stderr,
                            flush=True,
                        )
            raise RuntimeError("\n".join(errors))
        except Exception as exc:
            raise RuntimeError(f"浏览器抓取 TradingView 失败: {exc}") from exc
        finally:
            context.close()


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
        "--login",
        action="store_true",
        help="打开浏览器登录 TradingView，并保存登录态供以后自动复用",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="显示 Playwright 浏览器及 TradingView 页面操作过程",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=300,
        help="等待浏览器登录完成的秒数（默认300）",
    )
    parser.add_argument(
        "--profile-dir",
        help=(
            "TradingView 专用浏览器配置目录；默认使用 "
            "%%LOCALAPPDATA%%/AlphaMaster/TradingViewBrowser"
        ),
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
    if not 30 <= args.login_timeout <= 1800:
        parser.error("--login-timeout 必须在30到1800秒之间")
    if (args.login or args.show_browser) and args.transport == "websocket":
        parser.error("--login/--show-browser 需要 browser 或 auto 连接方式")

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
    if args.transport in {"auto", "browser"}:
        browser_mode = "可视化" if args.show_browser or args.login else "无头模式"
        print(f"Playwright : {browser_mode}")
    print("认证       : 登录账户优先，匿名连接回退")
    print(f"输出       : {output.resolve()}")
    print("=" * 68)

    configured_auth_token, auth_source = load_configured_auth_token()
    if auth_source:
        print(f"[认证] 已从{auth_source}读取登录令牌。")

    rows: list[dict] | None = None
    pages = 0
    auth_label = ""
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
                rows, pages, auth_label = fetch_with_browser(
                    request,
                    configured_auth_token=configured_auth_token,
                    profile_dir=args.profile_dir,
                    interactive_login=bool(args.login),
                    show_browser=bool(args.show_browser),
                    login_timeout_seconds=args.login_timeout,
                )
            else:
                websocket_attempts: list[tuple[str, str | None]] = []
                if configured_auth_token:
                    websocket_attempts.append(
                        ("登录账户（配置令牌）", configured_auth_token)
                    )
                websocket_attempts.append(("匿名连接", None))
                websocket_errors: list[str] = []
                for index, (candidate_label, token) in enumerate(
                    websocket_attempts
                ):
                    try:
                        rows, pages = fetch_with_websocket(request, token)
                        auth_label = candidate_label
                        break
                    except Exception as exc:
                        websocket_errors.append(f"{candidate_label}: {exc}")
                        if index + 1 < len(websocket_attempts):
                            print(
                                f"[认证] {candidate_label}不可用，"
                                "尝试匿名连接 ...",
                                file=sys.stderr,
                                flush=True,
                            )
                if rows is None:
                    raise RuntimeError("\n".join(websocket_errors))
            print(f"[连接] {transport} 成功（{auth_label}）")
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

    if args.all and interval_seconds < 24 * 60 * 60:
        print(
            "[范围] --all 已尝试普通图表及登录账户 K线回放通道；"
            "最终历史深度以 TradingView 返回的最早可用日期为准。",
            flush=True,
        )

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
