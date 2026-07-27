from __future__ import annotations

from datetime import datetime, timedelta

from web.data_sources.factory import SOURCE_KINDS
from web.data_sources.tongdaxin_source import (
    TongdaxinSource,
    _is_index,
    _parse_market,
)


def _rows(count: int) -> list[dict]:
    start = datetime(2020, 1, 1, 9, 30)
    return [
        {
            "datetime": (start + timedelta(minutes=5 * index)).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "open": float(index + 1),
            "high": float(index + 2),
            "low": float(index),
            "close": float(index + 1.5),
            "vol": float(index + 10),
        }
        for index in range(count)
    ]


class _FakeTdxApi:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.security_calls: list[tuple[int, int]] = []
        self.index_calls: list[tuple[int, int]] = []

    def _page(self, start: int, count: int) -> list[dict]:
        end = max(0, len(self.rows) - start)
        begin = max(0, end - count)
        return self.rows[begin:end]

    def get_security_bars(
        self, _cat: int, _market: int, _code: str, start: int, count: int
    ) -> list[dict]:
        self.security_calls.append((start, count))
        return self._page(start, count)

    def get_index_bars(
        self, _cat: int, _market: int, _code: str, start: int, count: int
    ) -> list[dict]:
        self.index_calls.append((start, count))
        return self._page(start, count)

    def disconnect(self) -> None:
        return None


def test_tongdaxin_is_enabled_for_realtime_analysis() -> None:
    assert ("tongdaxin", "通达信") in SOURCE_KINDS


def test_shenzhen_etf_market_mapping() -> None:
    market, code = _parse_market("159170")

    assert (market, code) == (0, "159170")
    assert _is_index(market, code) is False


def test_security_bars_are_fetched_across_multiple_pages() -> None:
    api = _FakeTdxApi(_rows(2_000))
    source = TongdaxinSource()
    source._api = api

    bars = source.fetch_bars("600519", "5m", 1_500, drop_forming=False)

    assert api.security_calls == [(0, 800), (800, 702)]
    assert len(bars) == 1_500
    assert all(left.ts < right.ts for left, right in zip(bars, bars[1:]))


def test_index_pagination_uses_index_api() -> None:
    api = _FakeTdxApi(_rows(1_000))
    source = TongdaxinSource()
    source._api = api

    bars = source.fetch_bars("sh000001", "1d", 900, drop_forming=False)

    assert api.index_calls == [(0, 800), (800, 102)]
    assert api.security_calls == []
    assert len(bars) == 900


def test_overlapping_pages_are_deduplicated_by_timestamp() -> None:
    api = _FakeTdxApi(_rows(900))
    source = TongdaxinSource()
    source._api = api

    def overlapping_page(
        _cat: int, _market: int, _code: str, start: int, count: int
    ) -> list[dict]:
        overlapping_start = max(0, start - 1) if start else 0
        return api._page(overlapping_start, count)

    api.get_security_bars = overlapping_page

    bars = source.fetch_bars("600519", "5m", 899, drop_forming=False)

    assert len(bars) == 899
    assert len({bar.ts for bar in bars}) == 899


def test_failed_page_reconnects_and_retries_same_offset() -> None:
    api = _FakeTdxApi(_rows(1_700))
    source = TongdaxinSource()
    source._api = api
    failed_once = False
    original = api.get_security_bars

    def flaky_page(
        cat: int, market: int, code: str, start: int, count: int
    ) -> list[dict]:
        nonlocal failed_once
        api.security_calls.append((start, count))
        if start == 800 and not failed_once:
            failed_once = True
            raise ConnectionError("temporary failure")
        return api._page(start, count)

    api.get_security_bars = flaky_page
    source.disconnect = lambda: setattr(source, "_api", None)
    source.connect = lambda: setattr(source, "_api", api)

    bars = source.fetch_bars("600519", "5m", 1_500, drop_forming=False)

    assert len(bars) == 1_500
    assert api.security_calls.count((800, 702)) == 2
    api.get_security_bars = original


def test_empty_page_reconnects_and_retries_same_offset() -> None:
    api = _FakeTdxApi(_rows(100))
    source = TongdaxinSource()
    source._api = api
    returned_empty = False

    def stale_connection_page(
        _cat: int, _market: int, _code: str, start: int, count: int
    ) -> list[dict] | None:
        nonlocal returned_empty
        api.security_calls.append((start, count))
        if not returned_empty:
            returned_empty = True
            return None
        return api._page(start, count)

    api.get_security_bars = stale_connection_page
    source.disconnect = lambda: setattr(source, "_api", None)
    source.connect = lambda: setattr(source, "_api", api)

    bars = source.fetch_bars("159170", "5m", 20, drop_forming=False)

    assert len(bars) == 20
    assert api.security_calls.count((0, 22)) == 2
