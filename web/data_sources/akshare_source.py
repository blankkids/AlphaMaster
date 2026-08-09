"""AKShare 数据源（A股 / 期货主力 / 指数，东财 + 新浪）。

接入 web/data_sources 的 DataSource 抽象，补齐国内市场行情。
不同品种走不同 akshare 接口，返回列名中英文混杂，本模块统一归一为 Bar：
  - 期货（字母开头，如 IF0/rb0）：futures_zh_minute_sina（分钟）/ futures_main_sina（日线）
  - 指数（sh/sz 前缀，如 sh000001）：index_zh_a_hist（日线）
  - A股（6 位数字，如 600519）：stock_zh_a_hist（日线）/ stock_zh_a_hist_min_em（分钟）

时间口径：akshare 返回北京时间的「收盘时刻」（分钟 bar 标结束时刻；日线标交易日），
转为 UTC 秒后与通达信一致——drop_forming 用 `ts > now` 判断未收盘 bar。

注意：部分接口走 eastmoney，受网络/代理影响较大；本模块对所有外部调用做异常归一
为 DataSourceUnavailable，前端据此给出提示，不抛裸异常。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from web.data_sources.base import Bar, DataSource, DataSourceError, DataSourceUnavailable

_CST = timezone(timedelta(hours=8))  # akshare 返回北京时间

# 项目规范周期 -> 分钟数
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
_SUPPORTED_TIMEFRAMES = ["5m", "15m", "30m", "60m", "1d"]

_PRESETS = [
    "600519", "000001", "300750",  # A 股
    "IF0", "rb0",                  # 期货主力
    "sh000001", "sz399001",        # 指数
]


def _classify(symbol: str) -> str:
    """品种分类：futures / index / stock。

    - 首字符为字母：sh/sz 前缀 → index；其余（IF0、rb0 等）→ futures
    - 纯数字：A 股股票（含 ETF/转债），归 stock
    """
    c = symbol.strip()
    if c[:1].isalpha():
        return "index" if c.lower().startswith(("sh", "sz")) else "futures"
    return "stock"


def _pick_col(df, candidates: tuple[str, ...]):
    """在 df 列中按候选名查找（大小写不敏感，兼容中文）。"""
    raw = {str(c): c for c in df.columns}
    lower = {str(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand in raw:
            return raw[cand]
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


_TIME_COLS = ("datetime", "date", "时间", "日期")
_OPEN_COLS = ("open", "开盘")
_HIGH_COLS = ("high", "最高")
_LOW_COLS = ("low", "最低")
_CLOSE_COLS = ("close", "收盘")
_VOL_COLS = ("volume", "vol", "成交量")


def _parse_close_ts(val, daily: bool) -> int:
    """akshare 时间值 -> 收盘时刻 UTC 秒。

    daily=True：val 为 'YYYY-MM-DD'，取当日 15:00 CST 为收盘时刻。
    daily=False：val 为 'YYYY-MM-DD HH:MM:SS'（北京时间，视为 bar 收盘时刻）。
    解析失败返回 0（调用方据此丢弃）。
    """
    s = str(val).strip()
    if not s:
        return 0
    if daily:
        s = s[:10] + " 15:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=_CST).timestamp())
        except ValueError:
            continue
    return 0


def _normalize_df(df, daily: bool) -> list[Bar]:
    """akshare DataFrame -> 升序 Bar 列表（兼容中英文列名）。"""
    if df is None or len(df) == 0:
        return []

    tcol = _pick_col(df, _TIME_COLS)
    if tcol is None:
        raise DataSourceError(f"akshare 返回缺时间列: {list(df.columns)}")

    picks: dict = {}
    for std, cands in (
        ("open", _OPEN_COLS), ("high", _HIGH_COLS), ("low", _LOW_COLS),
        ("close", _CLOSE_COLS), ("volume", _VOL_COLS),
    ):
        col = _pick_col(df, cands)
        if col is None:
            raise DataSourceError(f"akshare 返回缺 {std} 列: {list(df.columns)}")
        picks[std] = df[col]

    times = df[tcol]
    bars_by_ts: dict[int, Bar] = {}
    for i in range(len(df)):
        ts = _parse_close_ts(times.iloc[i], daily)
        if ts <= 0:
            continue
        bars_by_ts[ts] = Bar(
            ts=ts,
            open=float(picks["open"].iloc[i]),
            high=float(picks["high"].iloc[i]),
            low=float(picks["low"].iloc[i]),
            close=float(picks["close"].iloc[i]),
            volume=max(float(picks["volume"].iloc[i]), 0.0),
        )
    return sorted(bars_by_ts.values(), key=lambda b: b.ts)


def _estimate_start_date(n: int, daily: bool, timeframe: str) -> str:
    """粗略反推 start_date，保证覆盖 n 根（含冗余）。"""
    now = datetime.now(_CST)
    if daily:
        days = max(int(n / 250 * 365) + 30, 60)
    else:
        # 每交易日约 240 根分钟 bar；按交易日数 ×7 估算自然日（含周末/节假日冗余）
        trading_days = max(int(n / 240) + 5, 10)
        days = trading_days * 7
    return (now - timedelta(days=days)).strftime("%Y%m%d")


class AkshareSource(DataSource):
    kind = "akshare"
    label = "AKShare(A股/期货/指数)"

    def available(self) -> tuple[bool, str]:
        try:
            import akshare  # noqa: F401
        except ImportError:
            return (False, "未安装 akshare: pip install akshare")
        return (True, "东财/新浪 · A股 / 期货 / 指数")

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def preset_symbols(self) -> list[str]:
        return list(_PRESETS)

    def fetch_bars(
        self, symbol: str, timeframe: str, n: int, drop_forming: bool = True
    ) -> list[Bar]:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise DataSourceUnavailable(f"AKShare 不支持周期 {timeframe}")
        if n <= 0:
            return []

        import akshare as ak
        cls = _classify(symbol)
        daily = timeframe == "1d"
        try:
            df = self._fetch(ak, cls, symbol, timeframe, n)
        except (DataSourceError, DataSourceUnavailable):
            raise
        except Exception as exc:  # akshare 网络代理/接口变更等
            raise DataSourceUnavailable(f"akshare 拉取失败 {symbol}: {exc}") from exc

        bars = _normalize_df(df, daily)
        if drop_forming and bars:
            now = time.time()
            while bars and int(bars[-1].ts) > now:
                bars.pop()
        return bars[-n:]

    def _fetch(self, ak, cls: str, symbol: str, timeframe: str, n: int):
        """按品种类型 + 周期选择 akshare 接口。集中在此便于测试 mock。"""
        daily = timeframe == "1d"
        start = _estimate_start_date(n, daily, timeframe)
        end = datetime.now(_CST).strftime("%Y%m%d")
        minutes = _TF_MINUTES.get(timeframe, 5)

        if cls == "futures":
            if daily:
                return ak.futures_main_sina(symbol=symbol, end_date=end)
            return ak.futures_zh_minute_sina(symbol=symbol, period=str(minutes))

        if cls == "index":
            pure = symbol[2:] if symbol.lower().startswith(("sh", "sz")) else symbol
            if daily:
                return ak.index_zh_a_hist(symbol=pure, period="daily",
                                          start_date=start, end_date=end)
            # 指数分钟：尝试东财分钟接口（部分指数支持）
            return ak.index_zh_a_hist_min_em(symbol=symbol, period=str(minutes),
                                             start_date=start, end_date=end)

        # stock
        if daily:
            return ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                      start_date=start, end_date=end, adjust="qfq")
        return ak.stock_zh_a_hist_min_em(symbol=symbol, period=str(minutes),
                                         start_date=start, end_date=end, adjust="qfq")
