#!/usr/bin/env python3
"""Safely migrate flat JAV/FC2 media into one directory per title."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FC2_PATTERN = re.compile(r"^FC2(?:[-_ ]?PPV)?[-_ ]?(\d+)", re.IGNORECASE)
JAV_PATTERN = re.compile(r"^([A-Z]{2,10})[-_ ]?(\d{2,8})", re.IGNORECASE)


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path


def code_from_name(name: str) -> str | None:
    """Extract a normalized title code from a video or sidecar filename."""
    fc2_match = FC2_PATTERN.match(name)
    if fc2_match:
        return f"FC2-PPV-{fc2_match.group(1)}"
    jav_match = JAV_PATTERN.match(name)
    if jav_match:
        return f"{jav_match.group(1).upper()}-{jav_match.group(2)}"
    return None


def media_roots(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    root = Path(str(config["media_dir"]))
    jav = Path(str(config.get("jav_media_dir", root / "JAV")))
    fc2 = Path(str(config.get("fc2_media_dir", root / "FC2")))
    return root, jav, fc2


def plan_moves(config: dict[str, Any]) -> tuple[list[Move], list[Path]]:
    """Plan flat-file moves without changing the filesystem."""
    root, jav_root, fc2_root = media_roots(config)
    sources = (root, jav_root, fc2_root)
    moves: list[Move] = []
    skipped: list[Path] = []
    seen_sources: set[Path] = set()

    for source_root in sources:
        if not source_root.is_dir():
            continue
        for source in source_root.iterdir():
            if not source.is_file() or source in seen_sources:
                continue
            seen_sources.add(source)
            code = code_from_name(source.name)
            if not code:
                skipped.append(source)
                continue
            category = "FC2" if code.startswith("FC2-PPV-") else "JAV"
            destination_root = fc2_root if category == "FC2" else jav_root
            moves.append(Move(source, destination_root / code / source.name))

    moves.sort(key=lambda item: str(item.source).casefold())
    skipped.sort(key=lambda item: str(item).casefold())
    return moves, skipped


def validate_moves(moves: list[Move]) -> None:
    targets: set[Path] = set()
    for move in moves:
        if move.target in targets:
            raise RuntimeError(f"多个源文件对应同一目标，停止：{move.target}")
        targets.add(move.target)
        if move.target.exists():
            raise RuntimeError(f"目标已经存在，停止：{move.target}")


def apply_moves(moves: list[Move]) -> None:
    """Apply a fully prevalidated migration without overwriting files."""
    validate_moves(moves)
    for move in moves:
        move.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.target))


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if "media_dir" not in config:
        raise RuntimeError(f"配置缺少 media_dir：{path}")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将平铺媒体安全整理为 JAV/FC2/番号/文件结构"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/jable-downloader/config.json"),
        help="下载器配置文件路径",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行迁移；省略时只预览",
    )
    args = parser.parse_args()

    try:
        moves, skipped = plan_moves(load_config(args.config))
        validate_moves(moves)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1

    if not moves:
        print("✅ 没有需要迁移的平铺媒体。")
    else:
        print(f"发现 {len(moves)} 个待迁移文件：")
        for move in moves:
            print(f"  {move.source}  ->  {move.target}")

    if skipped:
        print(f"跳过 {len(skipped)} 个无法识别番号的文件：")
        for path in skipped:
            print(f"  {path}")

    if not args.apply:
        print("\n当前仅预览；确认无误后加 --apply 执行。")
        return 0

    try:
        apply_moves(moves)
    except (OSError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1
    print(f"✅ 已迁移 {len(moves)} 个文件；未覆盖或删除任何目标文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
