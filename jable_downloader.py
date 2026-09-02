#!/usr/bin/env python3
"""Automatic Jable/MissAV/SupJav stream discovery and download workflow."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from hls_proxy import HLSRelay

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover - installer supplies this dependency
    browser_requests = None


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
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PACKER_PATTERN = re.compile(
    r"eval\(\s*function\(p,a,c,k,e,d\).*?\}\(\s*"
    r"(?P<payload>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")\s*,\s*"
    r"(?P<radix>\d+)\s*,\s*(?P<count>\d+)\s*,\s*"
    r"(?P<keys>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"\.split\(\s*['\"]\|['\"]\s*\)",
    re.DOTALL,
)
HLS_URL_PATTERN = re.compile(
    r"https?://[^\s'\"<>\\;]+(?:\.m3u8|/master\.txt)(?:\?[^\s'\"<>\\;]*)?",
    re.IGNORECASE,
)
# Kept for callers that imported the pre-v2.6.1 constant directly.
M3U8_URL_PATTERN = HLS_URL_PATTERN


class AppError(RuntimeError):
    """A user-facing application error with a stable exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class DownloadInput:
    code: str
    source: str
    detail_url: str | None = None


@dataclass(frozen=True)
class CapturedStream:
    url: str
    source: str
    referer: str
    user_agent: str
    cookies: dict[str, str]
    width: int = 0
    height: int = 0
    bandwidth: int = 0
    duration: float = 0.0
    server: str = ""
    verified: bool = False


def normalize_code(value: str) -> str:
    """Normalize ordinary and FC2 identifiers to one safe filename form."""
    compact = re.sub(r"[\s_-]+", "", value.strip().upper())
    fc2_match = re.fullmatch(r"FC2(?:PPV)?(\d+)", compact)
    if fc2_match:
        normalized = f"FC2-PPV-{fc2_match.group(1)}"
    else:
        # Some legitimate labels start with digits (for example 300MIUM and
        # 1PONDO). Requiring the prefix to end in a letter keeps the split before
        # the numeric id unambiguous while still rejecting numeric-only input.
        match = re.fullmatch(r"([A-Z0-9]*[A-Z])(\d+)", compact)
        if not match:
            raise ValueError(
                "无法识别输入，请使用 IPX-850、300MIUM-1483、FC2-PPV-1234567 或受支持的详情页链接"
            )
        normalized = f"{match.group(1).upper()}-{match.group(2)}"
    if len(normalized) > 32:
        raise ValueError("番号过长，请检查输入")
    return normalized


def provider_from_hostname(hostname: str) -> str | None:
    host = hostname.lower().rstrip(".")
    if host == "jable.tv" or host.endswith(".jable.tv"):
        return "jable"
    if host.startswith("missav.") or ".missav." in host:
        return "missav"
    if host == "supjav.com" or host.endswith(".supjav.com"):
        return "supjav"
    return None


def parse_download_input(value: str) -> DownloadInput:
    """Recognize a code or a safe Jable/MissAV/SupJav detail URL."""
    raw = value.strip()
    if not raw or len(raw) > 2048:
        raise ValueError("输入为空或过长")
    if re.match(r"^https?://", raw, flags=re.IGNORECASE):
        parsed = urlparse(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("详情页链接端口无效") from exc
        source = provider_from_hostname(parsed.hostname or "")
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or source is None
            or parsed.username
            or parsed.password
            or port not in {None, 80, 443}
        ):
            raise ValueError("只支持 Jable、MissAV 或 SupJav 的 HTTP/HTTPS 详情页链接")
        code = None
        for segment in reversed([part for part in parsed.path.split("/") if part]):
            try:
                code = normalize_code(unquote(segment))
                break
            except ValueError:
                continue
        if not code:
            raise ValueError("无法从详情页链接识别番号")
        clean_url = urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, "")
        )
        return DownloadInput(code=code, source=source, detail_url=clean_url)

    code = normalize_code(raw)
    source = "fc2" if code.startswith("FC2-PPV-") else "auto"
    return DownloadInput(code=code, source=source)


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
    # Runtime defaults keep v1/v2 installations compatible without replacing
    # user-owned paths or the already verified Jable settings.
    config.setdefault("jable_site", config["site"])
    config.setdefault(
        "jable_m3u8_preferred_domains", [config["m3u8_preferred_domain"]]
    )
    config.setdefault("missav_site", "https://missav.ai")
    config.setdefault("missav_language", "en")
    config.setdefault("missav_m3u8_preferred_domains", ["surrit.com"])
    config.setdefault("missav_allow_m3u8_fallback", True)
    config.setdefault("missav_hls_relay", True)
    config.setdefault("supjav_site", "https://supjav.com")
    config.setdefault("supjav_language", "")
    config.setdefault("supjav_m3u8_preferred_domains", [])
    config.setdefault("supjav_allow_m3u8_fallback", True)
    config.setdefault("supjav_hls_relay", True)
    config.setdefault("supjav_min_duration_seconds", 600)
    config.setdefault("provider_probe_workers", 3)
    config.setdefault("javbus_fallback_enabled", True)
    config.setdefault("javbus_site", "https://www.javbus.com")
    config.setdefault("javbus_timeout_seconds", 15)
    media_root = Path(str(config["media_dir"]))
    config.setdefault("jav_media_dir", str(media_root / "JAV"))
    config.setdefault("fc2_media_dir", str(media_root / "FC2"))
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


def normalized_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def code_in_text(value: str, code: str) -> bool:
    prefix, number = code.upper().rsplit("-", 1)
    prefix_pattern = r"[\s_-]*".join(re.escape(part) for part in prefix.split("-"))
    pattern = rf"(?<![A-Z0-9]){prefix_pattern}[\s_-]*{re.escape(number)}(?![0-9])"
    return re.search(pattern, value.upper()) is not None


def find_missav_detail_url(html: str, code: str, base: str) -> str | None:
    """Pick the exact MissAV detail result while ignoring categories/search links."""
    soup = BeautifulSoup(html, "html.parser")
    wanted = normalized_text(code)
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        url = urljoin(base, href)
        parsed = urlparse(url)
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        if not segments:
            continue
        if normalized_text(segments[-1]) != wanted:
            continue
        if any(part.lower() in {"search", "actresses", "genres", "makers"} for part in segments):
            continue
        candidates.append(url)
    return next(iter(dict.fromkeys(candidates)), None)


def find_supjav_detail_urls(html: str, code: str, base: str) -> list[str]:
    """Return exact SupJav search results, including alternate encodes."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for post in soup.select("div.post"):
        anchor = post.select_one('a[href*=".html"]')
        if anchor is None:
            continue
        href = str(anchor.get("href", ""))
        title = str(anchor.get("title", "")) or " ".join(anchor.stripped_strings)
        if not code_in_text(f"{title} {href}", code):
            continue
        url = urljoin(base, href)
        if provider_from_hostname(urlparse(url).hostname or "") == "supjav":
            candidates.append(url)
    return list(dict.fromkeys(candidates))


def supjav_search_terms(code: str) -> list[str]:
    """Use the FC2 spelling accepted by SupJav while retaining the canonical code."""
    if code.startswith("FC2-PPV-"):
        return [f"FC2PPV {code.rsplit('-', 1)[1]}", code]
    return [code]


def supjav_search_urls(base: str, language: str, code: str) -> list[str]:
    """Return public SupJav search routes, preferring the WAF-friendlier path form."""
    search_root = f"{base.rstrip('/')}/{language.strip('/')}/" if language else f"{base.rstrip('/')}/"
    urls: list[str] = []
    for search_term in supjav_search_terms(code):
        encoded = quote(search_term, safe="")
        urls.extend(
            (
                urljoin(search_root, f"search/{encoded}/"),
                f"{search_root}?s={encoded}",
            )
        )
    return list(dict.fromkeys(urls))


def media_directories(config: dict[str, Any]) -> tuple[Path, ...]:
    """Return the media root and classified destinations without duplicates."""
    root = Path(str(config["media_dir"]))
    configured = (
        root,
        Path(str(config.get("jav_media_dir", root / "JAV"))),
        Path(str(config.get("fc2_media_dir", root / "FC2"))),
    )
    return tuple(dict.fromkeys(configured))


def media_destination(code: str, config: dict[str, Any]) -> Path:
    """Choose the per-title Jellyfin directory from the normalized identifier."""
    root = Path(str(config["media_dir"]))
    key = "fc2_media_dir" if code.startswith("FC2-PPV-") else "jav_media_dir"
    default_name = "FC2" if key == "fc2_media_dir" else "JAV"
    return Path(str(config.get(key, root / default_name))) / code


def existing_media(code: str, config: dict[str, Any]) -> Path | None:
    locations = (Path(str(config["download_dir"])), *media_directories(config))
    search_roots: list[Path] = []
    for location in locations:
        try:
            resolved = location.resolve()
        except OSError:
            resolved = location.absolute()
        if any(resolved == root or root in resolved.parents for root in search_roots):
            continue
        search_roots.append(resolved)

    seen: set[Path] = set()
    for base in search_roots:
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            try:
                identity = candidate.resolve()
            except OSError:
                continue
            if identity in seen:
                continue
            seen.add(identity)
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
        and path.stem.casefold() == code.casefold()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def ensure_directories(config: dict[str, Any]) -> None:
    directories = (
        Path(str(config["work_dir"])),
        Path(str(config["download_dir"])),
        Path(str(config["browser_profile"])),
        *media_directories(config),
    )
    for directory in dict.fromkeys(directories):
        directory.mkdir(parents=True, exist_ok=True)


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


def preferred_domains(config: dict[str, Any], source: str) -> list[str]:
    value = config.get(f"{source}_m3u8_preferred_domains", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AppError(f"{source}_m3u8_preferred_domains 必须是字符串数组")
    return [item.lower() for item in value if item]


def decode_js_string(literal: str) -> str:
    """Decode one quoted JavaScript string without evaluating JavaScript."""
    if len(literal) < 2 or literal[0] not in {"'", '"'} or literal[-1] != literal[0]:
        raise ValueError("无效的 JavaScript 字符串")
    content = literal[1:-1]
    output: list[str] = []
    index = 0
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        "'": "'",
        '"': '"',
    }
    while index < len(content):
        character = content[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        index += 1
        if index >= len(content):
            raise ValueError("JavaScript 字符串包含不完整转义")
        escaped = content[index]
        if escaped in {"\n", "\r"}:
            if escaped == "\r" and index + 1 < len(content) and content[index + 1] == "\n":
                index += 1
            index += 1
            continue
        if escaped == "x" and index + 2 < len(content):
            output.append(chr(int(content[index + 1 : index + 3], 16)))
            index += 3
            continue
        if escaped == "u" and index + 4 < len(content):
            output.append(chr(int(content[index + 1 : index + 5], 16)))
            index += 5
            continue
        output.append(escapes.get(escaped, escaped))
        index += 1
    return "".join(output)


def packer_key(value: int, radix: int) -> str:
    """Return the token spelling used by Dean Edwards-style P.A.C.K.E.R."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if not 2 <= radix <= len(alphabet):
        raise ValueError("不支持的脚本压缩进制")
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, radix)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def unpack_packer_payloads(script_text: str) -> list[str]:
    """Safely unpack string-substitution payloads used on public player pages."""
    unpacked: list[str] = []
    for match in PACKER_PATTERN.finditer(script_text):
        try:
            payload = decode_js_string(match.group("payload"))
            keys = decode_js_string(match.group("keys")).split("|")
            radix = int(match.group("radix"))
            count = int(match.group("count"))
            if count < 0 or count > 10000:
                continue
            for value in range(count - 1, -1, -1):
                token = packer_key(value, radix)
                replacement = keys[value] if value < len(keys) and keys[value] else token
                payload = re.sub(
                    rf"\b{re.escape(token)}\b",
                    lambda _match, text=replacement: text,
                    payload,
                )
            unpacked.append(payload)
        except (ValueError, OverflowError):
            continue
    return unpacked


def safe_stream_url(url: str) -> bool:
    """Reject malformed, credentialed and obvious local-network stream URLs."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_hls_candidate_url(url: str) -> bool:
    """Recognize normal and intentionally disguised HLS playlist URLs."""
    if not safe_stream_url(url):
        return False
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith("/master.txt")


def extract_packed_m3u8_urls(html: str) -> list[str]:
    """Extract HLS URLs while retaining the historical public helper name."""
    urls: list[str] = []
    for payload in unpack_packer_payloads(html):
        for match in HLS_URL_PATTERN.finditer(payload):
            url = match.group(0)
            if is_hls_candidate_url(url) and url not in urls:
                urls.append(url)
    return urls


def choose_m3u8_url(urls: list[str], domains: list[str]) -> str | None:
    def score(url: str) -> tuple[int, int]:
        host = (urlparse(url).hostname or "").lower()
        preferred = any(host == domain or host.endswith(f".{domain}") for domain in domains)
        path = urlparse(url).path.lower()
        master = path.endswith("/playlist.m3u8") or "master" in path
        return (100 if preferred else 0) + (30 if master else 0), -urls.index(url)

    return max(urls, key=score) if urls else None


def extract_m3u8_urls(text: str) -> list[str]:
    """Extract safe HLS URLs, including SupJav's master.txt playlists."""
    values = [text.replace("\\/", "/"), *unpack_packer_payloads(text)]
    urls: list[str] = []
    for value in values:
        for match in HLS_URL_PATTERN.finditer(value):
            url = match.group(0).replace("&amp;", "&")
            if is_hls_candidate_url(url) and url not in urls:
                urls.append(url)
    return urls


def parse_hls_attributes(value: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in re.finditer(r'(?:^|,)([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        attributes[match.group(1)] = match.group(2).strip('"')
    return attributes


def hls_playlist_quality(text: str, playlist_url: str) -> tuple[int, int, int, str]:
    """Return width, height, bandwidth and the best variant URL."""
    lines = [line.strip() for line in text.splitlines()]
    variants: list[tuple[int, int, int, str]] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attributes = parse_hls_attributes(line.split(":", 1)[1])
        width = height = 0
        resolution = attributes.get("RESOLUTION", "")
        match = re.fullmatch(r"(\d+)x(\d+)", resolution, re.IGNORECASE)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        try:
            bandwidth = int(attributes.get("AVERAGE-BANDWIDTH") or attributes.get("BANDWIDTH") or 0)
        except ValueError:
            bandwidth = 0
        for candidate in lines[index + 1 :]:
            if not candidate or candidate.startswith("#"):
                continue
            variants.append((width, height, bandwidth, urljoin(playlist_url, candidate)))
            break
    return max(variants, key=lambda item: (item[1], item[0], item[2])) if variants else (0, 0, 0, playlist_url)


def hls_duration(text: str) -> float:
    total = 0.0
    for match in re.finditer(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", text, re.MULTILINE):
        try:
            total += float(match.group(1))
        except ValueError:
            continue
    return total


def hls_playlist_usable(text: str) -> bool:
    """Reject empty/stale playlist shells that cannot yield media."""
    return "#EXTM3U" in text and (
        "#EXTINF:" in text or "#EXT-X-STREAM-INF:" in text
    )


def stream_request_headers(stream: CapturedStream) -> dict[str, str]:
    headers = {"User-Agent": stream.user_agent or DEFAULT_BROWSER_UA}
    if stream.referer:
        headers["Referer"] = stream.referer
        parsed = urlparse(stream.referer)
        if parsed.scheme and parsed.netloc:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return headers


def inspect_stream_quality(stream: CapturedStream, config: dict[str, Any]) -> CapturedStream:
    """Probe public HLS metadata without downloading media segments."""
    if browser_requests is None or not is_hls_candidate_url(stream.url):
        return stream
    timeout = max(5, int(config.get("stream_probe_timeout_seconds", 12)))
    try:
        response = browser_requests.get(
            stream.url,
            impersonate="chrome",
            headers=stream_request_headers(stream),
            cookies=stream.cookies,
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code != 200 or not hls_playlist_usable(response.text):
            return stream
        width, height, bandwidth, variant_url = hls_playlist_quality(
            response.text, str(response.url)
        )
        duration = hls_duration(response.text)
        verified = "#EXTINF:" in response.text
        if variant_url != str(response.url):
            variant = browser_requests.get(
                variant_url,
                impersonate="chrome",
                headers=stream_request_headers(stream),
                cookies=stream.cookies,
                timeout=timeout,
                allow_redirects=True,
            )
            if variant.status_code == 200 and hls_playlist_usable(variant.text):
                duration = hls_duration(variant.text)
                verified = "#EXTINF:" in variant.text
        return replace(
            stream,
            width=width,
            height=height,
            bandwidth=bandwidth,
            duration=duration,
            verified=verified,
        )
    except Exception:
        # A failed metadata probe must never discard an otherwise captured URL.
        return stream


def stream_quality_key(stream: CapturedStream) -> tuple[int, int, int, float, int]:
    source_preference = {"jable": 3, "missav": 2, "supjav": 1}
    return (
        stream.height,
        stream.width,
        stream.bandwidth,
        stream.duration,
        source_preference.get(stream.source, 0),
    )


def capture_missav_static(
    request: DownloadInput, config: dict[str, Any]
) -> CapturedStream:
    """Resolve a public MissAV player using an impersonated HTTP session."""
    if browser_requests is None:
        raise AppError("缺少 curl-cffi，无法启用 MissAV 静态解析", 4)
    base = str(config["missav_site"]).rstrip("/")
    language = str(config.get("missav_language", "en")).strip("/") or "en"
    detail_url = request.detail_url if request.source == "missav" else None
    headers = {"User-Agent": DEFAULT_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    timeout = max(5, int(config.get("page_timeout_ms", 30000)) // 1000)
    session = browser_requests.Session(impersonate="chrome", headers=headers)

    def fetch(url: str, referer: str | None = None) -> Any:
        response = session.get(
            url,
            headers={"Referer": referer} if referer else None,
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code != 200:
            raise AppError(f"MISSAV 页面访问失败（HTTP {response.status_code}）", 4)
        if provider_from_hostname(urlparse(str(response.url)).hostname or "") != "missav":
            raise AppError("MISSAV 页面跳转到了不受支持的站点", 4)
        return response

    if detail_url is None:
        print("🔎 正在通过 MissAV 搜索作品...")
        search_url = f"{base}/{language}/search/{quote(request.code)}"
        search_response = fetch(search_url)
        detail_url = find_missav_detail_url(
            search_response.text, request.code, str(search_response.url)
        )
        if not detail_url or provider_from_hostname(urlparse(detail_url).hostname or "") != "missav":
            raise AppError(f"MISSAV 没找到 {request.code}", 3)

    response = fetch(detail_url, base)
    urls = extract_packed_m3u8_urls(response.text)
    selected = choose_m3u8_url(urls, preferred_domains(config, "missav"))
    if not selected:
        raise AppError("MISSAV 静态页面没有解析到主视频 M3U8", 4)
    cookies = {
        str(name): str(value)
        for name, value in session.cookies.get_dict().items()
    }
    print("✅ 来源：MissAV")
    print(f"✅ 详情页：{detail_url}\n")
    print("🎯 已从公开播放器数据解析主视频 M3U8")
    print("   地址已隐藏，将立即交给下载器\n")
    return CapturedStream(
        url=selected,
        source="missav",
        referer=str(detail_url),
        user_agent=DEFAULT_BROWSER_UA,
        cookies=cookies,
    )


def capture_supjav_static(
    request: DownloadInput, config: dict[str, Any]
) -> list[CapturedStream]:
    """Resolve every usable public HLS server exposed by a SupJav detail page."""
    if browser_requests is None:
        raise AppError("缺少 curl-cffi，无法启用 SupJav 静态解析", 4)
    base = str(config.get("supjav_site", "https://supjav.com")).rstrip("/")
    language = str(config.get("supjav_language", "")).strip("/")
    timeout = max(5, int(config.get("page_timeout_ms", 30000)) // 1000)
    headers = {
        "User-Agent": DEFAULT_BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    session = browser_requests.Session(impersonate="chrome", headers=headers)

    def fetch(url: str, referer: str) -> Any:
        try:
            response = session.get(
                url,
                headers={"Referer": referer},
                timeout=timeout,
                allow_redirects=True,
            )
        except Exception as exc:
            host = urlparse(url).hostname or "未知主机"
            raise AppError(f"SUPJAV 请求失败（{host}）", 4) from exc
        if response.status_code != 200:
            raise AppError(f"SUPJAV 页面访问失败（HTTP {response.status_code}）", 4)
        return response

    if request.source == "supjav" and request.detail_url:
        detail_urls = [request.detail_url]
    else:
        print("🔎 正在通过 SupJav 搜索作品...")
        detail_urls = []
        search_errors: list[AppError] = []
        successful_search = False
        for search_url in supjav_search_urls(base, language, request.code):
            try:
                search_response = fetch(search_url, f"{base}/")
            except AppError as exc:
                search_errors.append(exc)
                continue
            successful_search = True
            if provider_from_hostname(urlparse(str(search_response.url)).hostname or "") != "supjav":
                search_errors.append(AppError("SUPJAV 搜索页跳转到了不受支持的站点", 4))
                continue
            detail_urls = find_supjav_detail_urls(
                search_response.text, request.code, str(search_response.url)
            )
            if detail_urls:
                break
        if not detail_urls:
            if not successful_search and search_errors:
                raise search_errors[-1]
            raise AppError(f"SUPJAV 没找到 {request.code}", 3)

    max_results = max(1, min(5, int(config.get("supjav_max_results", 3))))
    captured: list[CapturedStream] = []
    errors: list[str] = []
    for detail_url in detail_urls[:max_results]:
        try:
            response = fetch(detail_url, f"{base}/")
            if provider_from_hostname(urlparse(str(response.url)).hostname or "") != "supjav":
                raise AppError("SUPJAV 详情页跳转到了不受支持的站点", 4)
            soup = BeautifulSoup(response.text, "html.parser")
            servers = [
                (
                    " ".join(anchor.stripped_strings).upper() or "HLS",
                    str(anchor.get("data-link", "")),
                )
                for anchor in soup.select("a.btn-server[data-link]")
                if anchor.get("data-link")
            ]
            if not servers:
                errors.append("详情页没有播放器服务器")
                continue
            for server_name, link in servers:
                wrapper_url = f"https://lk1.supremejav.com/supjav.php?l={quote(link)}&bg=undefined"
                bodies: list[tuple[str, str]] = []
                try:
                    wrapper = fetch(wrapper_url, detail_url)
                    bodies.append((wrapper.text, str(wrapper.url)))
                    wrapper_soup = BeautifulSoup(wrapper.text, "html.parser")
                    iframe = wrapper_soup.select_one("iframe[src]")
                    if iframe is not None:
                        nested_url = urljoin(str(wrapper.url), str(iframe.get("src", "")))
                        if (urlparse(nested_url).hostname or "").lower() == "lk1.supremejav.com":
                            nested = fetch(nested_url, str(wrapper.url))
                            bodies.insert(0, (nested.text, str(nested.url)))
                except AppError as exc:
                    errors.append(str(exc))

                if not bodies:
                    direct_url = (
                        "https://lk1.supremejav.com/supjav.php?c=" + link[::-1]
                    )
                    try:
                        direct = fetch(direct_url, f"{base}/")
                        bodies.append((direct.text, str(direct.url)))
                    except AppError as exc:
                        errors.append(str(exc))

                urls: list[str] = []
                url_referers: dict[str, str] = {}
                for body, player_url in bodies:
                    for url in extract_m3u8_urls(body):
                        if url not in urls:
                            urls.append(url)
                            url_referers[url] = player_url
                if not urls:
                    direct_url = (
                        "https://lk1.supremejav.com/supjav.php?c=" + link[::-1]
                    )
                    try:
                        direct = fetch(direct_url, f"{base}/")
                        for url in extract_m3u8_urls(direct.text):
                            if url not in urls:
                                urls.append(url)
                                url_referers[url] = str(direct.url)
                    except AppError as exc:
                        errors.append(str(exc))
                selected = choose_m3u8_url(
                    urls, preferred_domains(config, "supjav")
                )
                if not selected:
                    continue
                cookies = {
                    str(name): str(value)
                    for name, value in session.cookies.get_dict().items()
                }
                candidate = inspect_stream_quality(
                    CapturedStream(
                        url=selected,
                        source="supjav",
                        referer=url_referers.get(selected, detail_url),
                        user_agent=DEFAULT_BROWSER_UA,
                        cookies=cookies,
                        server=server_name,
                    ),
                    config,
                )
                if not candidate.verified:
                    errors.append(f"{server_name} 返回了失效或空的 HLS 清单")
                    continue
                minimum = float(config.get("supjav_min_duration_seconds", 600))
                if candidate.duration and candidate.duration < minimum:
                    print(
                        f"⚠️ SupJav {server_name} 仅 {candidate.duration / 60:.1f} 分钟，按预告片跳过"
                    )
                    continue
                if all(item.url != candidate.url for item in captured):
                    captured.append(candidate)
        except AppError as exc:
            errors.append(str(exc))

    if not captured:
        detail = f"：{errors[-1]}" if errors else ""
        raise AppError(f"SUPJAV 找到作品但没有可用的完整 HLS{detail}", 4)
    captured.sort(key=stream_quality_key, reverse=True)
    best = captured[0]
    quality = f"{best.height}p" if best.height else "画质待下载器确认"
    print(f"✅ 来源：SupJav（{len(captured)} 条可用 HLS，最佳 {quality}）")
    return captured


def activate_player(page: Any) -> None:
    """Best-effort player activation; failures are handled by capture timeout."""
    for selector in ("video", ".plyr", "button[aria-label*='Play']"):
        try:
            target = page.locator(selector).first
            if target.is_visible(timeout=1000):
                target.click(force=True, timeout=2000)
                return
        except Exception:
            continue


def capture_from_provider(
    request: DownloadInput, source: str, config: dict[str, Any]
) -> CapturedStream:
    if source == "missav":
        try:
            return capture_missav_static(request, config)
        except AppError as exc:
            print(f"⚠️ {exc}，改用 Chromium 播放器捕获...\n")

    if source == "jable":
        base = str(config["jable_site"]).rstrip("/")
        search_url = f"{base}/search/{quote(request.code)}/"
        allow_fallback = bool(config.get("allow_m3u8_fallback", False))
    elif source == "missav":
        base = str(config["missav_site"]).rstrip("/")
        language = str(config.get("missav_language", "en")).strip("/") or "en"
        search_url = f"{base}/{language}/search/{quote(request.code)}"
        allow_fallback = bool(config.get("missav_allow_m3u8_fallback", True))
    elif source == "supjav":
        base = str(config.get("supjav_site", "https://supjav.com")).rstrip("/")
        language = str(config.get("supjav_language", "")).strip("/")
        search_urls = supjav_search_urls(base, language, request.code)
        search_url = search_urls[0]
        allow_fallback = bool(config.get("supjav_allow_m3u8_fallback", True))
    else:
        raise AppError(f"不支持的直链来源：{source}", 4)

    preferred: list[str] = []
    fallback: list[str] = []
    request_referers: dict[str, str] = {}
    domains = preferred_domains(config, source)
    detail_url = request.detail_url if request.source == source else None
    stream: CapturedStream | None = None
    profile = Path(str(config["browser_profile"]))
    if source != "jable":
        profile = profile / source
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=bool(config.get("browser_headless", False)),
            executable_path=str(config["chromium"]),
            viewport={"width": 1365, "height": 768},
            locale=str(config.get("locale", "zh-CN")),
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            timeout_ms = int(config.get("page_timeout_ms", 30000))
            search_wait_ms = int(config.get("search_wait_ms", 5000))
            capture_timeout_ms = int(config.get("capture_timeout_ms", 20000))

            if detail_url is None:
                labels = {"jable": "Jable", "missav": "MissAV", "supjav": "SupJav"}
                print(f"🔎 正在通过 {labels[source]} 搜索作品...")
                candidate_search_urls = search_urls if source == "supjav" else [search_url]
                last_search_status: int | None = None
                received_success = False
                for candidate_search_url in candidate_search_urls:
                    response = page.goto(
                        candidate_search_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    page.wait_for_timeout(search_wait_ms)
                    if response is None:
                        continue
                    last_search_status = response.status
                    print(f"   {labels[source]} HTTP：{response.status}")
                    if response.status == 200:
                        received_success = True
                    # A browser verification page may complete after the initial
                    # navigation response. Trust only a subsequently rendered,
                    # exact same-provider result; otherwise try the next route.
                    search_html = page.content()
                    if source == "jable":
                        detail_url = find_detail_url(search_html, request.code, base)
                    elif source == "missav":
                        detail_url = find_missav_detail_url(
                            search_html, request.code, page.url
                        )
                    else:
                        detail_urls = find_supjav_detail_urls(
                            search_html, request.code, page.url
                        )
                        detail_url = detail_urls[0] if detail_urls else None
                    if detail_url:
                        break
                if not detail_url:
                    if last_search_status is None:
                        raise AppError("搜索页没有返回 HTTP 响应", 2)
                    if not received_success:
                        raise AppError(
                            f"搜索页面访问失败（HTTP {last_search_status}）", 2
                        )
                    raise AppError(f"{source.upper()} 没找到 {request.code}", 3)
            labels = {"jable": "Jable", "missav": "MissAV", "supjav": "SupJav"}
            print(f"✅ 来源：{labels[source]}")
            print(f"✅ 详情页：{detail_url}\n")

            def record(url: str, referer: str = "") -> None:
                if not is_hls_candidate_url(url):
                    return
                matched = any(domain in url.lower() for domain in domains)
                bucket = preferred if matched else fallback
                if url not in bucket:
                    bucket.append(url)
                    if referer:
                        request_referers[url] = referer
                    if matched:
                        print("🎯 捕获主视频 M3U8")
                        print("   地址已隐藏，将立即交给下载器\n")

            def record_request(browser_request: Any) -> None:
                headers = browser_request.headers
                record(browser_request.url, str(headers.get("referer", "")))

            page.on("request", record_request)
            page.on("response", lambda browser_response: record(browser_response.url))
            context.on(
                "page",
                lambda popup: popup.close() if popup != page else None,
            )
            print("📡 正在加载播放器...")
            response = page.goto(
                detail_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            if response:
                print(f"   {labels[source]} HTTP：{response.status}")
            if source == "missav":
                activate_player(page)
            elif source == "supjav":
                for button in page.locator("a.btn-server[data-link]").all():
                    try:
                        button.click(force=True, timeout=1500)
                        page.wait_for_timeout(600)
                    except Exception:
                        continue

            deadline = time.monotonic() + capture_timeout_ms / 1000
            while not preferred and time.monotonic() < deadline:
                page.wait_for_timeout(500)
                if source in {"missav", "supjav"} and not preferred and not fallback:
                    activate_player(page)

            selected = preferred[0] if preferred else (fallback[0] if allow_fallback and fallback else None)
            if selected:
                cookie_map = {
                    str(item["name"]): str(item["value"])
                    for item in context.cookies()
                    if item.get("name")
                }
                user_agent = str(page.evaluate("() => navigator.userAgent"))
                stream = CapturedStream(
                    url=selected,
                    source=source,
                    referer=request_referers.get(selected, str(detail_url)),
                    user_agent=user_agent,
                    cookies=cookie_map,
                )
        except PlaywrightTimeoutError as exc:
            raise AppError(f"{source.upper()} 浏览器等待页面超时，请稍后重试", 4) from exc
        finally:
            context.close()

    if stream:
        if not preferred:
            print("⚠️ 未捕获首选 CDN，按配置使用备用 M3U8")
        return stream
    expected = "、".join(domains) or "配置的首选 CDN"
    raise AppError(f"{source.upper()} 没有捕获到 {expected} 的主视频 M3U8", 4)


def capture_candidates_from_provider(
    request: DownloadInput, source: str, config: dict[str, Any]
) -> list[CapturedStream]:
    if source == "supjav":
        try:
            return capture_supjav_static(request, config)
        except AppError as exc:
            print(f"⚠️ {exc}，改用 Chromium 播放器捕获 SupJav...\n")
    stream = inspect_stream_quality(capture_from_provider(request, source, config), config)
    if source == "supjav":
        minimum = float(config.get("supjav_min_duration_seconds", 600))
        if stream.duration and stream.duration < minimum:
            raise AppError(
                f"SUPJAV 浏览器捕获到的流仅 {stream.duration:.0f} 秒，疑似预览或广告",
                4,
            )
    return [stream]


def capture_streams(
    request: DownloadInput, config: dict[str, Any]
) -> list[CapturedStream]:
    """Probe all automatic providers concurrently and rank usable streams."""
    if request.source in {"jable", "missav", "supjav"}:
        sources = [request.source]
    else:
        sources = ["jable", "missav", "supjav"]
    workers = max(1, min(len(sources), int(config.get("provider_probe_workers", 3))))
    captured: list[CapturedStream] = []
    failures: list[str] = []
    failure_codes: list[int] = []
    print(f"🔎 并行解析：{'、'.join(source.title() for source in sources)}\n")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="provider") as pool:
        futures = {
            pool.submit(capture_candidates_from_provider, request, source, config): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                captured.extend(future.result())
            except AppError as exc:
                failures.append(f"{source.upper()}：{exc}")
                failure_codes.append(exc.exit_code)
                print(f"⚠️ {source.upper()} 探测未成功：{exc}")
            except Exception as exc:
                failures.append(f"{source.upper()}：解析异常")
                failure_codes.append(4)
                print(f"⚠️ {source.upper()} 探测异常：{type(exc).__name__}")
    unique: dict[tuple[str, str], CapturedStream] = {}
    for stream in captured:
        unique.setdefault((stream.source, stream.url), stream)
    ranked = sorted(unique.values(), key=stream_quality_key, reverse=True)
    if ranked:
        print("\n📊 可用直链已按分辨率、码率和时长排序：")
        for stream in ranked:
            quality = f"{stream.width}x{stream.height}" if stream.height else "清晰度未知"
            rate = f" / {stream.bandwidth // 1000} Kbps" if stream.bandwidth else ""
            server = f"/{stream.server}" if stream.server else ""
            print(f"   {stream.source.upper()}{server}：{quality}{rate}")
        return ranked
    exit_code = 3 if failure_codes and all(code == 3 for code in failure_codes) else 4
    raise AppError("；".join(failures) or "所有直链来源均不可用", exit_code)


def capture_stream(request: DownloadInput, config: dict[str, Any]) -> CapturedStream:
    """Backward-compatible helper returning the best ranked stream."""
    return capture_streams(request, config)[0]


def capture_m3u8(code: str, config: dict[str, Any]) -> str:
    """Backward-compatible helper retained for callers of the v1/v2 core."""
    return capture_stream(parse_download_input(code), config).url


def run_downloader(
    code: str,
    captured: CapturedStream | str,
    config: dict[str, Any],
    save_name: str | None = None,
) -> Path:
    download_dir = Path(config["download_dir"])
    work_dir = Path(config["work_dir"])
    stream = (
        captured
        if isinstance(captured, CapturedStream)
        else CapturedStream(str(captured), "jable", "", "", {})
    )
    relay: HLSRelay | None = None
    download_url = stream.url
    use_relay = (
        stream.source == "missav" and config.get("missav_hls_relay", True)
    ) or (
        stream.source == "supjav" and config.get("supjav_hls_relay", True)
    )
    if use_relay:
        relay = HLSRelay(
            stream.referer,
            stream.user_agent,
            stream.cookies,
            strip_fake_ts_header=stream.source == "supjav",
        )
        try:
            download_url = relay.start(stream.url)
        except (RuntimeError, OSError, ValueError) as exc:
            raise AppError(f"{stream.source.upper()} HLS 转发启动失败：{exc}", 5) from exc
        print(f"\n🛡️ 已启用 {stream.source.upper()} 本机 HLS 转发")
    output_name = save_name or code
    command = [
        str(config["n_m3u8dl_re"]),
        download_url,
        "--save-dir",
        str(download_dir),
        "--save-name",
        output_name,
        "--auto-select",
        "--use-ffmpeg-concat-demuxer",
        "--del-after-done",
        "true",
    ]
    if relay is None and stream.user_agent:
        command.extend(["--header", f"User-Agent: {stream.user_agent}"])
    if relay is None and stream.referer:
        command.extend(["--header", f"Referer: {stream.referer}"])
    extra_args = config.get("n_m3u8dl_extra_args", [])
    if not isinstance(extra_args, list) or not all(
        isinstance(item, str) for item in extra_args
    ):
        raise AppError("n_m3u8dl_extra_args 必须是字符串数组")
    command.extend(extra_args)

    print("\n📥 开始调用 N_m3u8DL-RE...\n")
    try:
        result = subprocess.run(command, cwd=work_dir, check=False)
    finally:
        if relay is not None:
            relay.stop()
    if result.returncode != 0:
        raise AppError(
            f"下载失败，退出码：{result.returncode}；临时分片保留在 {work_dir} 便于恢复",
            5,
        )
    finished = find_finished_file(output_name, download_dir)
    if not finished:
        raise AppError("下载器显示完成，但没有找到最终媒体文件", 6)
    return finished


def download_from_candidates(
    code: str, streams: list[CapturedStream], config: dict[str, Any]
) -> Path:
    """Try ranked streams one by one without mixing partial data between sources."""
    failures: list[str] = []
    for index, stream in enumerate(streams, start=1):
        server = f"/{stream.server}" if stream.server else ""
        print(
            f"\n🚀 尝试第 {index}/{len(streams)} 个来源：{stream.source.upper()}{server}"
        )
        attempt_name = f"{code}__{stream.source}_{index}"
        try:
            finished = run_downloader(
                code, stream, config, save_name=attempt_name
            )
            final_path = finished.with_name(f"{code}{finished.suffix}")
            if final_path != finished:
                if final_path.exists():
                    raise AppError(f"下载目录已有同名成品，未覆盖：{final_path}", 7)
                finished.rename(final_path)
            return final_path
        except AppError as exc:
            failures.append(f"{stream.source.upper()}{server}：{exc}")
            if index < len(streams):
                print(f"⚠️ 当前直链下载失败，自动尝试下一条：{exc}")
    raise AppError("所有已解析直链下载均失败：" + "；".join(failures), 5)


def move_to_media(finished: Path, code: str, config: dict[str, Any]) -> Path:
    destination = media_destination(code, config)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / finished.name
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
        raw_code = " ".join(argv) if argv else input("请输入番号或详情页链接：").strip()
        request = parse_download_input(raw_code)
        code = request.code
        config = load_config()
        ensure_directories(config)

        print("\n======================================")
        print(" Jable + MissAV + SupJav Downloader")
        print("======================================")
        print(f"🎬 番号：{code}\n")
        if request.source == "missav":
            print("🧭 自动识别：MissAV\n")
        elif request.source == "jable":
            print("🧭 自动识别：Jable\n")
        elif request.source == "supjav":
            print("🧭 自动识别：SupJav\n")
        elif request.source == "fc2":
            print("🧭 自动识别：并行检查 Jable、MissAV、SupJav\n")
        else:
            print("🧭 自动识别：并行检查 Jable、MissAV、SupJav\n")

        existing = existing_media(code, config)
        if existing:
            print("⚠️ 已存在同番号成品：")
            print(existing)
            print("\n为避免重复下载，本次退出。")
            return 0

        captured = capture_streams(request, config)
        print("\n✅ M3U8 并行解析完成")
        finished = download_from_candidates(code, captured, config)
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
