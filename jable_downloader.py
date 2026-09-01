#!/usr/bin/env python3
"""Automatic Jable/MissAV search, stream capture and download workflow."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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
M3U8_URL_PATTERN = re.compile(
    r"https?://[^\s'\"<>\\;]+\.m3u8(?:\?[^\s'\"<>\\;]*)?",
    re.IGNORECASE,
)


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
    return None


def parse_download_input(value: str) -> DownloadInput:
    """Recognize a code or a safe Jable/MissAV detail URL."""
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
            raise ValueError("只支持 Jable 或 MissAV 的 HTTP/HTTPS 详情页链接")
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
    source = "missav" if code.startswith("FC2-PPV-") else "auto"
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
        and path.stem.upper() == code
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


def extract_packed_m3u8_urls(html: str) -> list[str]:
    urls: list[str] = []
    for payload in unpack_packer_payloads(html):
        for match in M3U8_URL_PATTERN.finditer(payload):
            url = match.group(0)
            if safe_stream_url(url) and url not in urls:
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
    else:
        base = str(config["missav_site"]).rstrip("/")
        language = str(config.get("missav_language", "en")).strip("/") or "en"
        search_url = f"{base}/{language}/search/{quote(request.code)}"
        allow_fallback = bool(config.get("missav_allow_m3u8_fallback", True))

    preferred: list[str] = []
    fallback: list[str] = []
    domains = preferred_domains(config, source)
    detail_url = request.detail_url if request.source == source else None
    stream: CapturedStream | None = None

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(config["browser_profile"]),
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
                print(f"🔎 正在通过 {'Jable' if source == 'jable' else 'MissAV'} 搜索作品...")
                response = page.goto(
                    search_url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                page.wait_for_timeout(search_wait_ms)
                if response is None:
                    raise AppError("搜索页没有返回 HTTP 响应", 2)
                print(f"   HTTP：{response.status}")
                if response.status != 200:
                    raise AppError(f"搜索页面访问失败（HTTP {response.status}）", 2)
                if source == "jable":
                    detail_url = find_detail_url(page.content(), request.code, base)
                else:
                    detail_url = find_missav_detail_url(
                        page.content(), request.code, page.url
                    )
                if not detail_url:
                    raise AppError(f"{source.upper()} 没找到 {request.code}", 3)
            print(f"✅ 来源：{'Jable' if source == 'jable' else 'MissAV'}")
            print(f"✅ 详情页：{detail_url}\n")

            def record(url: str) -> None:
                if ".m3u8" not in url.lower():
                    return
                matched = any(domain in url.lower() for domain in domains)
                bucket = preferred if matched else fallback
                if url not in bucket:
                    bucket.append(url)
                    if matched:
                        print("🎯 捕获主视频 M3U8")
                        print("   地址已隐藏，将立即交给下载器\n")

            page.on("request", lambda browser_request: record(browser_request.url))
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
                print(f"   HTTP：{response.status}")
            if source == "missav":
                activate_player(page)

            deadline = time.monotonic() + capture_timeout_ms / 1000
            while not preferred and time.monotonic() < deadline:
                page.wait_for_timeout(500)
                if source == "missav" and not preferred and not fallback:
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
                    referer=str(detail_url),
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


def capture_stream(request: DownloadInput, config: dict[str, Any]) -> CapturedStream:
    if request.source in {"jable", "missav"}:
        sources = [request.source]
    else:
        sources = ["jable", "missav"]
    failures: list[str] = []
    failure_codes: list[int] = []
    for source in sources:
        try:
            return capture_from_provider(request, source, config)
        except AppError as exc:
            failures.append(str(exc))
            failure_codes.append(exc.exit_code)
            if source != sources[-1]:
                print(f"⚠️ {exc}，自动切换到 MissAV...\n")
    exit_code = 3 if failure_codes and all(code == 3 for code in failure_codes) else 4
    raise AppError("；".join(failures), exit_code)


def capture_m3u8(code: str, config: dict[str, Any]) -> str:
    """Backward-compatible helper retained for callers of the v1/v2 core."""
    return capture_stream(parse_download_input(code), config).url


def run_downloader(
    code: str, captured: CapturedStream | str, config: dict[str, Any]
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
    if stream.source == "missav" and config.get("missav_hls_relay", True):
        relay = HLSRelay(stream.referer, stream.user_agent, stream.cookies)
        try:
            download_url = relay.start(stream.url)
        except (RuntimeError, OSError, ValueError) as exc:
            raise AppError(f"MissAV HLS 转发启动失败：{exc}", 5) from exc
        print("\n🛡️ 已启用 MissAV 本机 HLS 转发")
    command = [
        str(config["n_m3u8dl_re"]),
        download_url,
        "--save-dir",
        str(download_dir),
        "--save-name",
        code,
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
    finished = find_finished_file(code, download_dir)
    if not finished:
        raise AppError("下载器显示完成，但没有找到最终媒体文件", 6)
    return finished


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
        print(" Jable + MissAV Downloader")
        print("======================================")
        print(f"🎬 番号：{code}\n")
        if request.source == "missav":
            print("🧭 自动识别：MissAV\n")
        elif request.source == "jable":
            print("🧭 自动识别：Jable\n")
        else:
            print("🧭 自动识别：Jable 优先，未找到时转 MissAV\n")

        existing = existing_media(code, config)
        if existing:
            print("⚠️ 已存在同番号成品：")
            print(existing)
            print("\n为避免重复下载，本次退出。")
            return 0

        captured = capture_stream(request, config)
        print("\n✅ M3U8 解析成功")
        finished = run_downloader(code, captured, config)
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
