"""Migrate legacy symbol-only training artifacts to symbol+timeframe names.

The migration is intentionally conservative:

* dry-run is the default;
* legacy files are copied, never deleted;
* existing destination files are never overwritten;
* a timeframe must be unambiguous before any files are copied.

Examples:

    python scripts/migrate_training_artifacts.py
    python scripts/migrate_training_artifacts.py --apply
    python scripts/migrate_training_artifacts.py --symbol 159170 --timeframe M5 --apply
"""
from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


TIMEFRAME_ALIASES = {
    "M1": "M1",
    "1M": "M1",
    "1MIN": "M1",
    "M5": "M5",
    "5M": "M5",
    "5MIN": "M5",
    "M15": "M15",
    "15M": "M15",
    "15MIN": "M15",
    "M30": "M30",
    "30M": "M30",
    "30MIN": "M30",
    "H1": "H1",
    "1H": "H1",
    "60M": "H1",
    "60MIN": "H1",
    "H4": "H4",
    "4H": "H4",
    "D1": "D1",
    "1D": "D1",
    "W1": "W1",
    "1W": "W1",
    "MN1": "MN1",
    "1MO": "MN1",
    "1MON": "MN1",
}
CANONICAL_TIMEFRAMES = frozenset(TIMEFRAME_ALIASES.values())
CHECKPOINT_RE = re.compile(r"^ckpt_(.+)_step_(\d+)\.pt$", re.IGNORECASE)
LOG_SUFFIX_RE = re.compile(r"_(\d{8})_(\d{6})\.log$", re.IGNORECASE)


@dataclass(frozen=True)
class Migration:
    kind: str
    symbol: str
    timeframe: str
    source: Path
    destination: Path


def normalize_timeframe(value: object) -> str | None:
    raw = str(value or "").strip().upper().replace("-", "").replace("_", "")
    return TIMEFRAME_ALIASES.get(raw)


def has_timeframe_suffix(identity: str) -> bool:
    if "_" not in identity:
        return False
    return normalize_timeframe(identity.rsplit("_", 1)[1]) in CANONICAL_TIMEFRAMES


def safe_tag(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", value)


def parse_parquet_identity(path: Path) -> tuple[str, str] | None:
    if path.suffix.lower() != ".parquet" or "_" not in path.stem:
        return None
    symbol, raw_timeframe = path.stem.rsplit("_", 1)
    timeframe = normalize_timeframe(raw_timeframe)
    if not symbol or timeframe is None:
        return None
    return symbol, timeframe


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_legacy_symbols(root: Path) -> set[str]:
    symbols: set[str] = set()

    strategies_dir = root / "strategies"
    if strategies_dir.exists():
        for path in strategies_dir.glob("best_*.json"):
            identity = path.stem.removeprefix("best_")
            if identity and not has_timeframe_suffix(identity):
                symbols.add(identity)

    for path in root.glob("training_history_*.json"):
        identity = path.stem.removeprefix("training_history_")
        if identity and not has_timeframe_suffix(identity):
            symbols.add(identity)

    checkpoint_dir = root / "checkpoints"
    if checkpoint_dir.exists():
        for path in checkpoint_dir.glob("ckpt_*_step_*.pt"):
            match = CHECKPOINT_RE.match(path.name)
            if not match:
                continue
            identity = match.group(1)
            if identity and not has_timeframe_suffix(identity):
                symbols.add(identity)

    return symbols


def parquet_timeframes(root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    data_dir = root / "data"
    if not data_dir.exists():
        return found
    for path in data_dir.rglob("*.parquet"):
        identity = parse_parquet_identity(path)
        if identity is None:
            continue
        symbol, timeframe = identity
        found.setdefault(symbol, set()).add(timeframe)
    return found


def infer_timeframe(
    root: Path,
    symbol: str,
    data_timeframes: dict[str, set[str]],
    override: str | None,
) -> tuple[str | None, str]:
    if override:
        return override, "命令行指定"

    strategy_path = root / "strategies" / f"best_{symbol}.json"
    strategy = load_json(strategy_path) if strategy_path.exists() else {}

    strategy_timeframe = normalize_timeframe(strategy.get("timeframe"))
    if strategy_timeframe:
        return strategy_timeframe, "策略 JSON"

    data_file = str(strategy.get("data_file") or "").strip()
    if data_file:
        identity = parse_parquet_identity(Path(data_file))
        if identity and identity[0] == symbol:
            return identity[1], "策略 data_file"

    candidates = sorted(data_timeframes.get(symbol, set()))
    if len(candidates) == 1:
        return candidates[0], "本地 Parquet"
    if len(candidates) > 1:
        return None, f"发现多个数据周期: {', '.join(candidates)}"
    return None, "没有可用于推断周期的策略元数据或 Parquet"


def build_migrations(root: Path, symbol: str, timeframe: str) -> list[Migration]:
    migrations: list[Migration] = []
    tagged = f"{symbol}_{timeframe}"

    strategy_source = root / "strategies" / f"best_{symbol}.json"
    if strategy_source.exists():
        migrations.append(
            Migration(
                "strategy",
                symbol,
                timeframe,
                strategy_source,
                root / "strategies" / f"best_{tagged}.json",
            )
        )

    history_source = root / f"training_history_{symbol}.json"
    if history_source.exists():
        migrations.append(
            Migration(
                "history",
                symbol,
                timeframe,
                history_source,
                root / f"training_history_{tagged}.json",
            )
        )

    checkpoint_dir = root / "checkpoints"
    if checkpoint_dir.exists():
        for source in checkpoint_dir.glob("ckpt_*_step_*.pt"):
            match = CHECKPOINT_RE.match(source.name)
            if not match or match.group(1) != symbol:
                continue
            step = match.group(2)
            migrations.append(
                Migration(
                    "checkpoint",
                    symbol,
                    timeframe,
                    source,
                    checkpoint_dir / f"ckpt_{tagged}_step_{step}.pt",
                )
            )

    old_safe = safe_tag(symbol)
    new_safe = safe_tag(tagged)
    training_time_source = root / f"training_time_{old_safe}.json"
    if training_time_source.exists():
        migrations.append(
            Migration(
                "training_time",
                symbol,
                timeframe,
                training_time_source,
                root / f"training_time_{new_safe}.json",
            )
        )

    logs_dir = root / "logs"
    if logs_dir.exists():
        prefix = f"train_{old_safe}_"
        for source in logs_dir.glob(f"{prefix}*.log"):
            suffix = source.name.removeprefix(f"train_{old_safe}")
            if not LOG_SUFFIX_RE.fullmatch(suffix):
                continue
            migrations.append(
                Migration(
                    "log",
                    symbol,
                    timeframe,
                    source,
                    logs_dir / f"train_{new_safe}{suffix}",
                )
            )

    return sorted(migrations, key=lambda row: (row.symbol, row.kind, row.source.name))


def strategy_copy_data(migration: Migration) -> dict:
    data = load_json(migration.source)
    if not data:
        raise ValueError(f"策略 JSON 无法读取: {migration.source}")
    existing = normalize_timeframe(data.get("timeframe"))
    if existing and existing != migration.timeframe:
        raise ValueError(
            f"策略周期 {existing} 与迁移周期 {migration.timeframe} 不一致: "
            f"{migration.source}"
        )
    data["timeframe"] = migration.timeframe
    return data


def write_strategy_copy(migration: Migration) -> None:
    data = strategy_copy_data(migration)
    migration.destination.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def apply_migration(migration: Migration) -> str:
    destination = migration.destination
    if destination.exists():
        if migration.kind == "strategy":
            if load_json(destination) == strategy_copy_data(migration):
                return "已存在且内容相同"
        elif filecmp.cmp(migration.source, destination, shallow=False):
            return "已存在且内容相同"
        return "目标已存在，未覆盖"

    destination.parent.mkdir(parents=True, exist_ok=True)
    if migration.kind == "strategy":
        write_strategy_copy(migration)
    else:
        shutil.copy2(migration.source, destination)
    return "已复制"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将旧版 symbol-only 训练产物复制为 symbol+timeframe 新命名"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="项目根目录，默认自动识别",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        help="只迁移指定品种，可重复传入；默认扫描全部旧产物",
    )
    parser.add_argument(
        "--timeframe",
        help="显式指定周期，例如 M5；用于无法自动推断或只迁移单一周期时",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际复制；不传时只显示迁移计划",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    override = normalize_timeframe(args.timeframe)
    if args.timeframe and override is None:
        print(f"[错误] 不支持的周期: {args.timeframe}", file=sys.stderr)
        return 2

    discovered = discover_legacy_symbols(root)
    requested = {str(value).strip() for value in (args.symbol or []) if str(value).strip()}
    if requested:
        symbols = sorted(requested)
        missing = requested - discovered
        for symbol in sorted(missing):
            print(f"[提示] {symbol}: 未发现旧命名训练产物")
    else:
        symbols = sorted(discovered)

    if not symbols:
        print("没有发现需要迁移的旧命名训练产物。")
        return 0

    data_timeframes = parquet_timeframes(root)
    plan: list[Migration] = []
    unresolved: list[tuple[str, str]] = []

    for symbol in symbols:
        timeframe, source = infer_timeframe(root, symbol, data_timeframes, override)
        if timeframe is None:
            unresolved.append((symbol, source))
            continue
        rows = build_migrations(root, symbol, timeframe)
        print(f"[识别] {symbol} -> {timeframe}（{source}），产物 {len(rows)} 个")
        plan.extend(rows)

    if unresolved:
        for symbol, reason in unresolved:
            print(
                f"[跳过] {symbol}: {reason}；请使用 "
                f"--symbol {symbol} --timeframe <周期>",
                file=sys.stderr,
            )
        if args.apply:
            print("[中止] 存在无法确定周期的产物，未执行任何复制。", file=sys.stderr)
            return 2

    if not plan:
        print("没有生成可执行的迁移计划。")
        return 0 if not unresolved else 2

    print("\n迁移计划：")
    for row in plan:
        source = row.source.relative_to(root)
        destination = row.destination.relative_to(root)
        print(f"  [{row.kind}] {source} -> {destination}")

    if not args.apply:
        print("\n当前为预览模式；确认无误后加 --apply 执行。")
        return 0 if not unresolved else 2

    print("\n执行结果：")
    copied = 0
    conflicts = 0
    for row in plan:
        try:
            result = apply_migration(row)
        except (OSError, ValueError) as exc:
            result = f"失败: {exc}"
            conflicts += 1
        else:
            if result == "已复制":
                copied += 1
            elif result == "目标已存在，未覆盖":
                conflicts += 1
        print(f"  {row.destination.relative_to(root)}: {result}")

    print(f"\n完成：复制 {copied} 个，冲突/失败 {conflicts} 个；旧文件均保留。")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
