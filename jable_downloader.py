#!/usr/bin/env python3
"""Jable search, M3U8 capture and download workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_CONFIG_FILES = (
    Path("/etc/jable-downloader/config.json"),
    Path("/opt/jable-downloader/config.json"),  # compatible with the verified legacy install
)
MEDIA_EXTENSIONS = (".mp4", ".mkv", ".ts", ".m4v", ".mov")
SUMMARY_ICONS = {
    "code": "🎬",
    "video": "📺",
    "audio": "🔊",
    # Avoid the stopwatch sequence (U+23F1 U+FE0F): some terminals render it
    # as one cell while the other summary icons occupy two, shifting the label.
    "duration": "🕒",
    "size": "💾",
    "elapsed": "⚡",
}


class AppError(RuntimeError):
    """A user-facing application error with a stable exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def normalize_code(value: str) -> str:
    """Normalize IPX850, IPX 850 and IPX-850 to IPX-850."""
    match = re.fullmatch(r"([A-Za-z]+)[\s_-]*(\d+)", value.strip())
    if not match:
        raise ValueError("无法识别番号格式，请输入类似 IPX-850、IPX850 或 IPX 850")
    return f"{match.group(1).upper()}-{match.group(2)}"


def config_path() -> Path:
    configured = os.environ.get("JABLE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    for candidate in DEFAULT_CONFIG_FILES:
        if candidate.is_file():
            return candidate
    return DEFAULT_CONFIG_FILES[0]


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise AppError(f"找不到配置文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise AppError(f"配置文件不是有效 JSON：{path}（第 {exc.lineno} 行）") from exc

    required = (
        "work_dir",
        "download_dir",
        "media_dir",
        "browser_profile",
        "chromium",
        "n_m3u8dl_re",
        "site",
        "m3u8_preferred_domain",
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise AppError("配置缺少字段：" + ", ".join(missing))
    return config


def find_detail_url(html: str, code: str, base: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    code_upper = code.upper()
    code_space = code_upper.replace("-", " ")
    candidates: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        text = " ".join(anchor.stripped_strings)
        combined = f"{text} {href}".upper()
        if code_upper not in combined and code_space not in combined:
            continue
        url = urljoin(base, href)
        if "/videos/" in url:
            candidates.append(url)

    candidates = list(dict.fromkeys(candidates))
    expected = code.lower()
    for url in candidates:
        if url.rstrip("/").split("/")[-1].lower() == expected:
            return url
    return candidates[0] if candidates else None


def existing_media(code: str, config: dict[str, Any]) -> Path | None:
    for base_key in ("download_dir", "media_dir"):
        base = Path(config[base_key])
        if not base.is_dir():
            continue
        for candidate in base.iterdir():
            if (
                candidate.is_file()
                and candidate.suffix.lower() in MEDIA_EXTENSIONS
                and candidate.stem.upper() == code
            ):
                return candidate
    return None


def find_finished_file(code: str, download_dir: Path) -> Path | None:
    matches = [
        path
        for path in download_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and path.stem.upper() == code
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def ensure_directories(config: dict[str, Any]) -> None:
    for key in ("work_dir", "download_dir", "media_dir", "browser_profile"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)


def raise_open_file_limit() -> None:
    """Mirror the stable launcher's ulimit when invoked directly."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(65535, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, OSError, ValueError):
        pass


def capture_m3u8(code: str, config: dict[str, Any]) -> str:
    base = str(config["site"]).rstrip("/")
    preferred_domain = str(config["m3u8_preferred_domain"]).lower()
    search_url = f"{base}/search/{quote(code)}/"
    preferred: list[str] = []
    fallback: list[str] = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(config["browser_profile"]),
            headless=False,
            executable_path=str(config["chromium"]),
            viewport={"width": 1365, "height": 768},
            locale=str(config.get("locale", "zh-CN")),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            timeout_ms = int(config.get("page_timeout_ms", 30000))
            search_wait_ms = int(config.get("search_wait_ms", 5000))
            capture_timeout_ms = int(config.get("capture_timeout_ms", 20000))

            print("🔎 正在搜索作品...")
            response = page.goto(
                search_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            page.wait_for_timeout(search_wait_ms)
            if response is None:
                raise AppError("搜索页没有返回 HTTP 响应", 2)
            print(f"   HTTP：{response.status}")
            if response.status != 200:
                raise AppError(f"搜索页面访问失败（HTTP {response.status}）", 2)

            detail_url = find_detail_url(page.content(), code, base)
            if not detail_url:
                raise AppError(f"没找到 {code} 的作品详情页", 3)
            print(f"✅ 详情页：{detail_url}\n")

            def record(url: str) -> None:
                lower = url.lower()
                if ".m3u8" not in lower:
                    return
                bucket = preferred if preferred_domain in lower else fallback
                if url not in bucket:
                    bucket.append(url)
                    if bucket is preferred:
                        print("🎯 捕获主视频 M3U8")
                        print(url, "\n")

            page.on("request", lambda request: record(request.url))
            page.on("response", lambda response: record(response.url))
            print("📡 正在加载播放器...")
            response = page.goto(
                detail_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            if response:
                print(f"   HTTP：{response.status}")

            deadline = time.monotonic() + capture_timeout_ms / 1000
            while not preferred and time.monotonic() < deadline:
                page.wait_for_timeout(500)
        except PlaywrightTimeoutError as exc:
            raise AppError("浏览器等待页面超时，请稍后重试", 4) from exc
        finally:
            context.close()

    if preferred:
        return preferred[0]
    if config.get("allow_m3u8_fallback", False) and fallback:
        print("⚠️ 未捕获首选域名，按配置使用备用 M3U8")
        return fallback[0]
    raise AppError(f"没有捕获到 {preferred_domain} 的主视频 M3U8", 4)


def run_downloader(code: str, m3u8: str, config: dict[str, Any]) -> Path:
    download_dir = Path(config["download_dir"])
    work_dir = Path(config["work_dir"])
    command = [
        str(config["n_m3u8dl_re"]),
        m3u8,
        "--save-dir",
        str(download_dir),
        "--save-name",
        code,
        "--auto-select",
        "--use-ffmpeg-concat-demuxer",
        "--del-after-done",
        "true",
    ]
    extra_args = config.get("n_m3u8dl_extra_args", [])
    if not isinstance(extra_args, list) or not all(
        isinstance(item, str) for item in extra_args
    ):
        raise AppError("n_m3u8dl_extra_args 必须是字符串数组")
    command.extend(extra_args)

    print("\n📥 开始调用 N_m3u8DL-RE...\n")
    result = subprocess.run(command, cwd=work_dir, check=False)
    if result.returncode != 0:
        raise AppError(
            f"下载失败，退出码：{result.returncode}；临时分片保留在 {work_dir} 便于恢复",
            5,
        )
    finished = find_finished_file(code, download_dir)
    if not finished:
        raise AppError("下载器显示完成，但没有找到最终媒体文件", 6)
    return finished


def move_to_media(finished: Path, code: str, config: dict[str, Any]) -> Path:
    target = Path(config["media_dir"]) / finished.name
    if target.exists():
        raise AppError(f"目标文件已经存在，未覆盖：{target}", 7)
    print("\n📦 正在移动到正式媒体库...")
    moved = Path(shutil.move(str(finished), str(target)))

    # N_m3u8DL-RE normally removes this after success. Remove only this exact
    # code directory if an empty/leftover work directory remains.
    leftover = Path(config["work_dir"]) / code
    if leftover.is_symlink():
        leftover.unlink()
    elif leftover.is_dir():
        shutil.rmtree(leftover)
    return moved


def media_summary(target: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "duration": 0.0,
        "size": target.stat().st_size,
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
                "format=duration,size",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        info = json.loads(probe.stdout)
        fmt = info.get("format", {})
        result["duration"] = float(fmt.get("duration", 0) or 0)
        result["size"] = int(fmt.get("size", result["size"]) or result["size"])
        for stream in info.get("streams", []):
            codec_type = stream.get("codec_type")
            if codec_type == "video":
                result["video_codec"] = str(
                    stream.get("codec_name", "未知")
                ).upper()
                result["width"] = int(stream.get("width", 0) or 0)
                result["height"] = int(stream.get("height", 0) or 0)
            elif codec_type == "audio":
                result["audio_codec"] = str(
                    stream.get("codec_name", "未知")
                ).upper()
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return result


def duration_text(seconds_value: float) -> str:
    seconds = int(round(seconds_value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def print_summary_field(icon_key: str, label: str, value: str) -> None:
    """Print one consistently formatted field in the completion summary."""
    print(f"{SUMMARY_ICONS[icon_key]} {label}：{value}")


def print_success(code: str, target: Path, started_at: float) -> None:
    info = media_summary(target)
    resolution = ""
    if info["width"] and info["height"]:
        resolution = f" / {info['width']}×{info['height']}"
    print("\n========================================")
    print("✅ 下载完成\n")
    print_summary_field("code", "番号", code)
    print_summary_field("video", "视频", f"{info['video_codec']}{resolution}")
    print_summary_field("audio", "音频", str(info["audio_codec"]))
    print_summary_field("duration", "时长", duration_text(info["duration"]))
    print_summary_field("size", "大小", f"{info['size'] / (1024**3):.2f} GB")
    print_summary_field(
        "elapsed", "总耗时", f"{int(round(time.time() - started_at))} 秒"
    )
    print()
    print("📁 成品：")
    print(target)
    print("========================================")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    started_at = time.time()
    raise_open_file_limit()
    try:
        raw_code = " ".join(argv) if argv else input("请输入番号：").strip()
        code = normalize_code(raw_code)
        config = load_config()
        ensure_directories(config)

        print("\n======================================")
        print(" Jable Downloader")
        print("======================================")
        print(f"🎬 番号：{code}\n")

        existing = existing_media(code, config)
        if existing:
            print("⚠️ 已存在同番号成品：")
            print(existing)
            print("\n为避免重复下载，本次退出。")
            return 0

        m3u8 = capture_m3u8(code, config)
        print("\n✅ M3U8 解析成功")
        finished = run_downloader(code, m3u8, config)
        target = move_to_media(finished, code, config)
        print_success(code, target, started_at)
        return 0
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except AppError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n⚠️ 已取消；未完成的分片会保留在 work 目录。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
