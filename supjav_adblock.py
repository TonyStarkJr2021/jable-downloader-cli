"""Project-maintained, SupJav-only browser request protection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "rules" / "supjav-adblock.json"
HOST_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
PROTECTED_RESOURCE_TYPES = frozenset({"media", "xhr", "fetch", "websocket"})
PROTECTED_MEDIA_SUFFIXES = (
    ".m3u8",
    ".m3u",
    ".ts",
    ".m4s",
    ".mp4",
    ".webm",
    ".aac",
    ".m4a",
    ".key",
)

# Installed as a browser init script. It suppresses the new browsing context,
# not the click itself, so a player handler can continue and reveal its stream.
SUPJAV_ADBLOCK_INIT_SCRIPT = r"""
(() => {
  const blockedOpen = () => null;
  try {
    Object.defineProperty(window, "open", {
      configurable: false,
      writable: false,
      value: blockedOpen,
    });
  } catch (_) {
    window.open = blockedOpen;
  }
  document.addEventListener("click", (event) => {
    const anchor = event.target && event.target.closest
      ? event.target.closest("a[target='_blank']")
      : null;
    if (anchor) anchor.removeAttribute("target");
  }, true);
})();
"""


@dataclass(frozen=True)
class SupJavAdblockRules:
    schema_version: int
    revision: str
    allowed_page_hosts: tuple[str, ...]
    blocked_hosts: tuple[str, ...]
    blocked_url_contains: tuple[str, ...]


def _validated_hosts(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 512:
        raise ValueError(f"{field} 必须是最多 512 项的数组")
    result: list[str] = []
    for item in value:
        host = str(item).strip().lower().rstrip(".")
        if not HOST_PATTERN.fullmatch(host):
            raise ValueError(f"{field} 包含无效域名")
        if host not in result:
            result.append(host)
    return tuple(result)


def _validated_patterns(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 512:
        raise ValueError("blocked_url_contains 必须是最多 512 项的数组")
    result: list[str] = []
    for item in value:
        pattern = str(item).strip().lower()
        if not pattern or len(pattern) > 200 or any(char.isspace() for char in pattern):
            raise ValueError("blocked_url_contains 包含无效规则")
        if pattern not in result:
            result.append(pattern)
    return tuple(result)


def load_supjav_adblock_rules(
    path: Path | None = None,
) -> SupJavAdblockRules:
    """Load the release-bundled rules without consulting the network."""
    rules_path = path or DEFAULT_RULES_PATH
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到规则文件：{rules_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"规则文件不是有效 JSON（第 {exc.lineno} 行）") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("不支持的 SupJav 广告规则格式")
    revision = str(data.get("revision", "")).strip()
    if not revision or len(revision) > 64:
        raise ValueError("规则 revision 无效")
    return SupJavAdblockRules(
        schema_version=1,
        revision=revision,
        allowed_page_hosts=_validated_hosts(
            data.get("allowed_page_hosts", []), "allowed_page_hosts"
        ),
        blocked_hosts=_validated_hosts(data.get("blocked_hosts", []), "blocked_hosts"),
        blocked_url_contains=_validated_patterns(
            data.get("blocked_url_contains", [])
        ),
    )


def host_matches(hostname: str, rule_host: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == rule_host or host.endswith("." + rule_host)


def is_protected_media_request(url: str, resource_type: str) -> bool:
    if resource_type.lower() in PROTECTED_RESOURCE_TYPES:
        return True
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(PROTECTED_MEDIA_SUFFIXES) or path.endswith("/master.txt")


def should_block_supjav_request(
    url: str,
    resource_type: str,
    *,
    is_navigation: bool,
    is_main_page_navigation: bool,
    is_popup_navigation: bool,
    rules: SupJavAdblockRules,
) -> tuple[bool, str]:
    """Return a safe blocking decision and a non-sensitive reason."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False, ""
    hostname = parsed.hostname.lower().rstrip(".")

    if is_navigation and is_popup_navigation:
        return True, "popup-navigation"
    if is_navigation and is_main_page_navigation and not any(
        host_matches(hostname, allowed) for allowed in rules.allowed_page_hosts
    ):
        return True, "external-main-navigation"

    # Media and player data are protected even if a CDN happens to share an
    # advertising hostname. Final duration probing remains the source of truth.
    if is_protected_media_request(url, resource_type):
        return False, ""
    if any(host_matches(hostname, blocked) for blocked in rules.blocked_hosts):
        return True, "blocked-host"
    lowered_url = url.lower()
    if any(pattern in lowered_url for pattern in rules.blocked_url_contains):
        return True, "blocked-url"
    return False, ""
