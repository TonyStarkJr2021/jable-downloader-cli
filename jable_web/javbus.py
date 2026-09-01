from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover - validated by the installer
    browser_requests = None


DEFAULT_SITE = "https://www.javbus.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAGNET_PATTERN = re.compile(
    r"^magnet:\?xt=urn:btih:([A-F0-9]{40}|[A-Z2-7]{32})(?:&|$)", re.IGNORECASE
)
SAFE_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")


class JavBusLookupError(RuntimeError):
    pass


def _trusted_javbus_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        hostname == "javbus.com" or hostname.endswith(".javbus.com")
    )


def _size_bytes(value: str) -> int:
    matched = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B)\s*", value, re.IGNORECASE)
    if not matched:
        return 0
    number = float(matched.group(1))
    powers = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}
    return int(number * (1024 ** powers[matched.group(2).upper()]))


def _date_ordinal(value: str) -> int:
    try:
        return date.fromisoformat(value).toordinal()
    except ValueError:
        return 0


def _quality_rank(item: dict[str, Any]) -> int:
    if item["is_hd"] and item["has_subtitle"]:
        return 0
    if item["is_hd"]:
        return 1
    if item["has_subtitle"]:
        return 2
    return 3


def rank_magnets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer HD+subtitles, then HD, with newest shares first per group."""
    return sorted(
        items,
        key=lambda item: (
            _quality_rank(item),
            -_date_ordinal(str(item["share_date"])),
            -int(item["size_bytes"]),
            str(item["title"]).casefold(),
        ),
    )


def parse_magnets_html(html: str, code: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    code_pattern = re.escape(code).replace(r"\-", r"[-_ ]?")
    results: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue
        first_anchor = cells[0].find("a", href=True)
        if first_anchor is None:
            continue
        link = str(first_anchor.get("href", "")).strip()
        if len(link) > 8192:
            continue
        magnet_match = MAGNET_PATTERN.match(link)
        if not magnet_match:
            continue
        info_hash = magnet_match.group(1).upper()
        if info_hash in seen_hashes:
            continue

        badge_texts = [
            badge.get_text(" ", strip=True) for badge in cells[0].find_all("a")[1:]
        ]
        all_text = first_anchor.get_text(" ", strip=True)
        if not re.match(rf"^{code_pattern}(?=$|[^A-Z0-9])", all_text, re.IGNORECASE):
            continue
        is_hd = any("高清" in badge or badge.upper() == "HD" for badge in badge_texts)
        has_subtitle = any("字幕" in badge for badge in badge_texts)
        title = re.sub(r"(?:高清|中文字幕|字幕)\s*", "", all_text).strip() or code
        size = cells[1].get_text(" ", strip=True)
        share_date = cells[2].get_text(" ", strip=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", share_date):
            share_date = ""

        seen_hashes.add(info_hash)
        results.append(
            {
                "title": title,
                "magnet": link,
                "info_hash": info_hash,
                "size": size,
                "size_bytes": _size_bytes(size),
                "share_date": share_date,
                "is_hd": is_hd,
                "has_subtitle": has_subtitle,
            }
        )
    return rank_magnets(results)


def lookup_javbus_magnets(
    code: str,
    site: str = DEFAULT_SITE,
    timeout_seconds: int = 15,
) -> list[dict[str, Any]]:
    normalized_code = code.upper()
    if not SAFE_CODE_PATTERN.fullmatch(normalized_code):
        raise JavBusLookupError("JavBus 查询番号格式不安全")
    site = site.rstrip("/")
    if not _trusted_javbus_url(site):
        raise JavBusLookupError("JavBus 地址必须使用官方 HTTPS 域名")
    if browser_requests is None:
        raise JavBusLookupError("缺少 curl-cffi，无法查询 JavBus")

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    session = browser_requests.Session(
        impersonate="chrome",
        headers=headers,
        cookies={"existmag": "all"},
    )
    detail_url = f"{site}/{quote(normalized_code)}"
    try:
        detail_response = session.get(
            detail_url, timeout=max(5, timeout_seconds), allow_redirects=True
        )
        if detail_response.status_code == 404:
            return []
        if detail_response.status_code != 200:
            raise JavBusLookupError(
                f"JavBus 详情页访问失败（HTTP {detail_response.status_code}）"
            )
        if not _trusted_javbus_url(str(detail_response.url)):
            raise JavBusLookupError("JavBus 详情页跳转到了不受支持的站点")
        detail_response.encoding = "utf-8"
        gid = re.search(r"var gid = (\d+);", detail_response.text)
        uc = re.search(r"var uc = (\d+);", detail_response.text)
        if not gid or not uc:
            return []

        magnet_response = session.get(
            f"{site}/ajax/uncledatoolsbyajax.php",
            params={"lang": "zh", "gid": gid.group(1), "uc": uc.group(1)},
            headers={"Referer": detail_url},
            timeout=max(5, timeout_seconds),
            allow_redirects=True,
        )
        if magnet_response.status_code != 200:
            raise JavBusLookupError(
                f"JavBus 磁力列表访问失败（HTTP {magnet_response.status_code}）"
            )
        if not _trusted_javbus_url(str(magnet_response.url)):
            raise JavBusLookupError("JavBus 磁力列表跳转到了不受支持的站点")
        magnet_response.encoding = "utf-8"
        return parse_magnets_html(magnet_response.text, normalized_code)[:30]
    except JavBusLookupError:
        raise
    except Exception as exc:
        raise JavBusLookupError("JavBus 查询暂时不可用") from exc
    finally:
        session.close()
