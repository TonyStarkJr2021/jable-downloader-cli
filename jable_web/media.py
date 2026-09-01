from __future__ import annotations

import json
import mimetypes
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote


MEDIA_EXTENSIONS = {".mp4", ".mkv", ".ts", ".m4v", ".mov"}


def resolve_media(media_dir: Path, filename: str) -> Path:
    if not filename or "\\" in filename:
        raise FileNotFoundError(filename)
    root = media_dir.resolve(strict=True)
    candidate = (root / filename).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError(filename) from exc
    if not candidate.is_file():
        raise FileNotFoundError(filename)
    if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
        raise FileNotFoundError(filename)
    return candidate


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported range")
    start_text, end_text = value[6:].split("-", 1)
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix")
        start = max(0, size - suffix)
        return start, size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("invalid range")
    return start, min(end, size - 1)


def iter_file(path: Path, start: int, length: int, chunk_size: int = 1024 * 1024):
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def content_disposition(filename: str) -> str:
    fallback = "".join(
        char
        if char.isascii() and char not in '"\\' and 32 <= ord(char) < 127
        else "_"
        for char in filename
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def probe_media(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "duration": 0.0,
        "video_codec": "未知",
        "audio_codec": "未知",
        "width": 0,
        "height": 0,
    }
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        info = json.loads(probe.stdout)
        result["duration"] = float(info.get("format", {}).get("duration", 0) or 0)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                result["video_codec"] = str(stream.get("codec_name", "未知")).upper()
                result["width"] = int(stream.get("width", 0) or 0)
                result["height"] = int(stream.get("height", 0) or 0)
            elif stream.get("codec_type") == "audio":
                result["audio_codec"] = str(stream.get("codec_name", "未知")).upper()
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return result


def list_media(media_dir: Path) -> list[dict[str, Any]]:
    if not media_dir.is_dir():
        return []
    entries = []
    for path in media_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        stat = path.stat()
        relative_name = path.relative_to(media_dir).as_posix()
        category = relative_name.split("/", 1)[0] if "/" in relative_name else "未分类"
        entries.append(
            {
                "name": relative_name,
                "code": path.stem.upper(),
                "category": category,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return sorted(entries, key=lambda item: item["modified_at"], reverse=True)
