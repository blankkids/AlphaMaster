"""Stable identities and filenames for per-dataset training artifacts.

The market symbol alone is not a unique training identity: the same symbol can
be trained on M1, M5, H1, ... data independently.  New artifacts therefore use
``{symbol}_{timeframe}`` while callers that omit a timeframe retain the legacy
symbol-only names.
"""
from __future__ import annotations

import re


def normalize_timeframe(timeframe: str | None) -> str | None:
    value = str(timeframe or "").strip().upper()
    return value or None


def artifact_tag(symbol: str, timeframe: str | None = None) -> str:
    symbol_value = str(symbol or "").strip()
    if not symbol_value:
        raise ValueError("symbol 不能为空")
    timeframe_value = normalize_timeframe(timeframe)
    return (
        f"{symbol_value}_{timeframe_value}"
        if timeframe_value
        else symbol_value
    )


def safe_artifact_tag(symbol: str, timeframe: str | None = None) -> str:
    """Return a filesystem/download-safe form of :func:`artifact_tag`."""
    return re.sub(r"[^0-9A-Za-z_-]+", "_", artifact_tag(symbol, timeframe))


def strategy_filename(symbol: str, timeframe: str | None = None) -> str:
    return f"best_{artifact_tag(symbol, timeframe)}.json"


def history_filename(symbol: str, timeframe: str | None = None) -> str:
    return f"training_history_{artifact_tag(symbol, timeframe)}.json"


def training_time_filename(symbol: str, timeframe: str | None = None) -> str:
    return f"training_time_{safe_artifact_tag(symbol, timeframe)}.json"


def checkpoint_filename(
    symbol: str,
    timeframe: str | None,
    step: int,
) -> str:
    return f"ckpt_{artifact_tag(symbol, timeframe)}_step_{int(step):04d}.pt"


def checkpoint_pattern(symbol: str, timeframe: str | None = None) -> str:
    return f"ckpt_{artifact_tag(symbol, timeframe)}_step_*.pt"
