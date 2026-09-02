from __future__ import annotations

import codecs
import errno
import os
import subprocess
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from jable_downloader import parse_download_input
from jable_web.javbus import JavBusLookupError, lookup_javbus_magnets

try:
    import pty
except ImportError:  # pragma: no cover - Windows development fallback
    pty = None


class TaskBusyError(RuntimeError):
    pass


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def safe_log_line(value: str) -> str:
    value = ANSI_ESCAPE_PATTERN.sub("", value).replace("\x00", "")
    if ".m3u8" not in value.lower():
        return value
    return re.sub(r"https?://\S+", "[M3U8 URL 已隐藏]", value)


class TerminalLogParser:
    """Turn terminal newlines and in-place carriage returns into log records."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def feed(self, chunk: bytes, final: bool = False) -> list[tuple[str, bool]]:
        self._buffer += self._decoder.decode(chunk, final=final)
        records: list[tuple[str, bool]] = []
        start = 0
        index = 0
        while index < len(self._buffer):
            character = self._buffer[index]
            if character not in {"\r", "\n"}:
                index += 1
                continue
            value = self._buffer[start:index]
            transient = character == "\r"
            if character == "\r" and index + 1 < len(self._buffer) and self._buffer[index + 1] == "\n":
                transient = False
                index += 1
            records.append((value, transient))
            index += 1
            start = index
        self._buffer = self._buffer[start:]
        if final and self._buffer:
            records.append((self._buffer, False))
            self._buffer = ""
        return records


class DownloadTaskManager:
    def __init__(
        self,
        command: str = "/usr/local/bin/n",
        max_log_lines: int = 600,
        magnet_lookup: Callable[[str], list[dict[str, Any]]] | None = None,
        javbus_enabled: bool = True,
        javbus_site: str = "https://www.javbus.com",
        javbus_timeout_seconds: int = 15,
    ) -> None:
        self.command = command
        self.max_log_lines = max_log_lines
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self.javbus_enabled = javbus_enabled
        self._magnet_lookup = magnet_lookup or (
            lambda code: lookup_javbus_magnets(
                code, site=javbus_site, timeout_seconds=javbus_timeout_seconds
            )
        )
        self._state: dict[str, Any] = {
            "state": "idle",
            "code": None,
            "source": None,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "progress": None,
            "logs": deque(maxlen=max_log_lines),
            "magnets": [],
            "magnet_lookup_error": None,
        }

    def start(self, raw_code: str) -> str:
        request = parse_download_input(raw_code)
        code = request.code
        argument = request.detail_url or code
        source_labels = {
            "auto": "自动：并行解析 Jable / MissAV / SupJav",
            "fc2": "自动：并行解析 Jable / MissAV / SupJav",
            "jable": "Jable",
            "missav": "MissAV",
            "supjav": "SupJav",
        }
        with self._lock:
            if self._state["state"] in {"running", "searching"}:
                raise TaskBusyError("已有下载任务正在运行")
            self._state = {
                "state": "running",
                "code": code,
                "source": request.source,
                "started_at": time.time(),
                "finished_at": None,
                "return_code": None,
                "progress": None,
                "logs": deque(
                    [f"准备下载 {code}", f"来源：{source_labels[request.source]}"],
                    maxlen=self.max_log_lines,
                ),
                "magnets": [],
                "magnet_lookup_error": None,
            }
            master_fd: int | None = None
            slave_fd: int | None = None
            try:
                environment = os.environ.copy()
                environment["PYTHONUNBUFFERED"] = "1"
                use_pty = os.name == "posix" and pty is not None
                if use_pty:
                    master_fd, slave_fd = pty.openpty()
                process = subprocess.Popen(
                    [self.command, argument],
                    stdin=subprocess.DEVNULL,
                    stdout=slave_fd if use_pty else subprocess.PIPE,
                    stderr=slave_fd if use_pty else subprocess.STDOUT,
                    text=False,
                    bufsize=0,
                    start_new_session=True,
                    env=environment,
                )
                self._process = process
            except OSError:
                if master_fd is not None:
                    os.close(master_fd)
                self._state["state"] = "failed"
                self._state["finished_at"] = time.time()
                self._state["logs"].append("无法启动下载命令")
                raise
            finally:
                if slave_fd is not None:
                    os.close(slave_fd)
            thread = threading.Thread(
                target=self._collect, args=(process, master_fd), daemon=True
            )
            thread.start()
        return code

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["logs"] = list(self._state["logs"])
            return state

    def _record(self, value: str, transient: bool) -> None:
        cleaned = safe_log_line(value).strip()
        if not cleaned:
            return
        with self._lock:
            if transient:
                self._state["progress"] = cleaned
            else:
                self._state["progress"] = None
                self._state["logs"].append(cleaned)

    def _collect(self, process: subprocess.Popen[bytes], master_fd: int | None) -> None:
        parser = TerminalLogParser()
        try:
            while True:
                try:
                    if master_fd is not None:
                        chunk = os.read(master_fd, 4096)
                    elif process.stdout is not None:
                        chunk = process.stdout.read(4096)
                    else:
                        chunk = b""
                except OSError as exc:
                    if master_fd is not None and exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                for value, transient in parser.feed(chunk):
                    self._record(value, transient)
            for value, transient in parser.feed(b"", final=True):
                self._record(value, transient)
        finally:
            if master_fd is not None:
                os.close(master_fd)
        return_code = process.wait()
        should_lookup = False
        with self._lock:
            self._state["progress"] = None
            self._state["return_code"] = return_code
            self._state["finished_at"] = time.time()
            should_lookup = (
                return_code == 3
                and self._state.get("source") == "auto"
                and self.javbus_enabled
            )
            self._state["state"] = (
                "completed" if return_code == 0 else "searching" if should_lookup else "failed"
            )
            self._state["logs"].append(
                "任务完成"
                if return_code == 0
                else "直链来源均未找到，正在查询 JavBus 磁力…"
                if should_lookup
                else f"任务失败（退出码 {return_code}）"
            )
        if not should_lookup:
            return

        try:
            magnets = self._magnet_lookup(str(self._state["code"]))
        except JavBusLookupError as exc:
            with self._lock:
                self._state["state"] = "failed"
                self._state["magnet_lookup_error"] = str(exc)
                self._state["logs"].append(str(exc))
            return
        except Exception:
            with self._lock:
                self._state["state"] = "failed"
                self._state["magnet_lookup_error"] = "JavBus 查询暂时不可用"
                self._state["logs"].append("JavBus 查询暂时不可用")
            return

        with self._lock:
            self._state["magnets"] = magnets
            if magnets:
                self._state["state"] = "alternatives"
                self._state["logs"].append(
                    f"JavBus 找到 {len(magnets)} 条磁力，已在页面推荐最合适的资源"
                )
            else:
                self._state["state"] = "failed"
                self._state["logs"].append("JavBus 也没有找到可用磁力")


def command_exists(command: str) -> bool:
    path = Path(command)
    return path.is_file() and path.stat().st_mode & 0o111 != 0
