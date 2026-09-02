"""Loopback-only HLS relay for streams that require browser-like requests."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover - reported cleanly when the feature is used
    browser_requests = None


@dataclass(frozen=True)
class RelayResponse:
    body: bytes
    content_type: str
    status: int = 200
    content_range: str | None = None


class HLSRelay:
    """Expose approved upstream HLS resources through a random loopback port."""

    def __init__(
        self,
        referer: str,
        user_agent: str,
        cookies: dict[str, str] | None = None,
        host: str = "127.0.0.1",
        strip_fake_ts_header: bool = False,
    ) -> None:
        self.referer = referer
        self.user_agent = user_agent
        self.cookies = dict(cookies or {})
        self.host = host
        self.strip_fake_ts_header = strip_fake_ts_header
        self._lock = threading.Lock()
        self._next_id = 0
        self._ids_by_url: dict[str, int] = {}
        self._urls_by_id: dict[int, str] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    @staticmethod
    def available() -> bool:
        return browser_requests is not None

    def _register(self, upstream_url: str) -> str:
        parsed = urlparse(upstream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HLS 资源地址无效")
        with self._lock:
            resource_id = self._ids_by_url.get(upstream_url)
            if resource_id is None:
                self._next_id += 1
                resource_id = self._next_id
                self._ids_by_url[upstream_url] = resource_id
                self._urls_by_id[resource_id] = upstream_url
        return f"{self.base_url}/resource/{resource_id}"

    def rewrite_playlist(self, playlist: str, playlist_url: str) -> str:
        """Rewrite media, variant, key and map URIs to the loopback relay."""

        def rewrite_attribute(match: re.Match[str]) -> str:
            absolute = urljoin(playlist_url, match.group(1))
            return f'URI="{self._register(absolute)}"'

        rewritten: list[str] = []
        for raw_line in playlist.splitlines():
            line = raw_line.strip()
            if not line:
                rewritten.append("")
            elif line.startswith("#"):
                rewritten.append(re.sub(r'URI="([^"]+)"', rewrite_attribute, line))
            else:
                rewritten.append(self._register(urljoin(playlist_url, line)))
        suffix = "\n" if playlist.endswith("\n") else ""
        return "\n".join(rewritten) + suffix

    def _fetch(self, upstream_url: str, byte_range: str | None = None) -> RelayResponse:
        if browser_requests is None:
            raise RuntimeError("curl-cffi 未安装，无法使用 HLS 转发")
        parsed = urlparse(self.referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else self.referer
        headers = {
            "User-Agent": self.user_agent,
            "Referer": self.referer,
            "Origin": origin,
        }
        if byte_range:
            headers["Range"] = byte_range
        response: Any = browser_requests.get(
            upstream_url,
            impersonate="chrome",
            headers=headers,
            cookies=self.cookies,
            timeout=60,
            allow_redirects=True,
        )
        if response.status_code not in {200, 206}:
            raise RuntimeError(f"上游返回 HTTP {response.status_code}")
        body = bytes(response.content or b"")
        content_type = str(
            response.headers.get("Content-Type", "application/octet-stream")
        )
        text = body.decode("utf-8", errors="ignore")
        if "#EXTM3U" in text:
            body = self.rewrite_playlist(text, upstream_url).encode("utf-8")
            content_type = "application/vnd.apple.mpegurl"
        elif self.strip_fake_ts_header and body and body[0] != 0x47:
            # Some SupJav hosts prepend image-like junk to MPEG-TS segments.
            # Accept an offset only when five 188-byte sync positions agree.
            limit = min(max(0, len(body) - 188 * 4 - 1), 8000)
            offset = None
            for candidate in range(limit + 1):
                if all(
                    candidate + 188 * index < len(body)
                    and body[candidate + 188 * index] == 0x47
                    for index in range(5)
                ):
                    offset = candidate
                    break
            if offset is not None:
                body = body[offset:]
        return RelayResponse(
            body=body,
            content_type=content_type,
            status=int(response.status_code),
            content_range=response.headers.get("Content-Range"),
        )

    def start(self, upstream_url: str) -> str:
        if self._server is not None:
            return self._register(upstream_url)
        if not self.available():
            raise RuntimeError("curl-cffi 未安装，无法使用 HLS 转发")
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = self.path.split("?", 1)[0]
                match = re.fullmatch(r"/resource/(\d+)", path)
                if not match:
                    self.send_error(404)
                    return
                with relay._lock:
                    upstream = relay._urls_by_id.get(int(match.group(1)))
                if upstream is None:
                    self.send_error(404)
                    return
                try:
                    result = relay._fetch(upstream, self.headers.get("Range"))
                except Exception as exc:
                    self.send_error(502, explain=str(exc))
                    return
                self.send_response(result.status)
                self.send_header("Content-Type", result.content_type)
                self.send_header("Content-Length", str(len(result.body)))
                self.send_header("Accept-Ranges", "bytes")
                if result.content_range:
                    self.send_header("Content-Range", result.content_range)
                self.end_headers()
                self.wfile.write(result.body)

        self._server = ThreadingHTTPServer((self.host, 0), Handler)
        self._server.daemon_threads = True
        port = int(self._server.server_address[1])
        self.base_url = f"http://{self.host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._register(upstream_url)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None
