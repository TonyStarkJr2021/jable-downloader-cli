from __future__ import annotations

import codecs
import errno
import os
import signal
import shutil
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


class TaskNotRunningError(RuntimeError):
    pass


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def safe_log_line(value: str) -> str:
    value = ANSI_ESCAPE_PATTERN.sub("", value).replace("\x00", "")
    if ".m3u8" not in value.lower():
        return value
    return re.sub(r"https?://\S+", "[M3U8 URL 已隐藏]", value)


class PreservedLogBuffer:
    """Keep task startup context and newest output within a bounded response."""

    def __init__(self, max_lines: int, initial: list[str] | None = None) -> None:
        self.max_lines = max(3, int(max_lines))
        self.head_limit = max(2, self.max_lines // 3)
        self.tail_limit = max(1, self.max_lines - self.head_limit - 1)
        self._head: list[str] = []
        self._tail: deque[str] = deque(maxlen=self.tail_limit)
        self._omitted = 0
        for line in initial or []:
            self.append(line)

    def append(self, line: str) -> None:
        if len(self._head) < self.head_limit:
            self._head.append(line)
            return
        if len(self._tail) == self.tail_limit:
            self._omitted += 1
        self._tail.append(line)

    def __iter__(self):
        yield from self._head
        if self._omitted:
            yield f"…… 中间日志过多，已省略 {self._omitted} 行；开头和最新日志均已保留 ……"
        yield from self._tail


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
        work_dir: str | Path | None = None,
        download_dir: str | Path | None = None,
        media_dirs: list[str | Path] | None = None,
    ) -> None:
        self.command = command
        self.max_log_lines = max_log_lines
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested_for: subprocess.Popen[str] | None = None
        self.work_dir = Path(work_dir) if work_dir else None
        self.download_dir = Path(download_dir) if download_dir else None
        self.media_dirs = tuple(dict.fromkeys(Path(item) for item in media_dirs or []))
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
            "logs": PreservedLogBuffer(max_log_lines),
            "magnets": [],
            "magnet_lookup_error": None,
        }

    def _reset_state(self) -> None:
        self._state = {
            "state": "idle",
            "code": None,
            "source": None,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "progress": None,
            "logs": PreservedLogBuffer(self.max_log_lines),
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
            if self._state["state"] in {"running", "searching", "cancelling"}:
                raise TaskBusyError("已有下载任务正在运行")
            self._cancel_requested_for = None
            self._state = {
                "state": "running",
                "code": code,
                "source": request.source,
                "started_at": time.time(),
                "finished_at": None,
                "return_code": None,
                "progress": None,
                "logs": PreservedLogBuffer(
                    self.max_log_lines,
                    [f"准备下载 {code}", f"来源：{source_labels[request.source]}"],
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

    def cancel(self) -> str:
        with self._lock:
            process = self._process
            if (
                self._state["state"] not in {"running", "searching"}
                or process is None
                or process.poll() is not None
            ):
                raise TaskNotRunningError("当前没有可取消的任务")
            self._cancel_requested_for = process
            self._state["state"] = "cancelling"
            self._state["progress"] = None
            self._state["logs"].append("正在取消任务并停止下载进程…")
            code = str(self._state.get("code") or "")

        try:
            self._signal_process(process, force=False)
        except OSError:
            with self._lock:
                if self._process is process:
                    self._cancel_requested_for = None
                    self._state["state"] = "running"
                    self._state["logs"].append("取消失败，下载任务仍在运行")
            raise

        threading.Thread(
            target=self._force_stop_after_grace,
            args=(process,),
            daemon=True,
        ).start()
        return code

    @staticmethod
    def _signal_process(process: subprocess.Popen[Any], force: bool) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
            return
        if force:
            process.kill()
        else:
            process.terminate()

    def _force_stop_after_grace(self, process: subprocess.Popen[Any]) -> None:
        threading.Event().wait(5)
        with self._lock:
            still_cancelled = self._cancel_requested_for is process
        if still_cancelled and process.poll() is None:
            try:
                self._signal_process(process, force=True)
            except OSError:
                pass

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    def _cleanup_cancelled_files(self, code: str) -> None:
        for root in (self.work_dir, self.download_dir):
            if root is None or not root.is_dir():
                continue
            for path in root.iterdir():
                if (
                    path.name == code
                    or path.name.startswith(f"{code}__")
                    or path.name.startswith(f"{code}.")
                ):
                    self._remove_path(path)

        for root in self.media_dirs:
            if not root.is_dir():
                continue
            exact_directory = root / code
            self._remove_path(exact_directory)
            for path in root.iterdir():
                if path.is_dir():
                    continue
                if path.name.startswith(f"{code}."):
                    self._remove_path(path)

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
            cancelled = self._cancel_requested_for is process
            if self._process is process:
                self._process = None
            if cancelled:
                self._cancel_requested_for = None
            self._state["progress"] = None
            self._state["return_code"] = return_code
            self._state["finished_at"] = time.time()
            if cancelled:
                code = str(self._state.get("code") or "")
                self._state["logs"].append("下载进程已停止，正在清理任务文件…")
            should_lookup = (
                not cancelled
                and return_code == 3
                and self._state.get("source") == "auto"
                and self.javbus_enabled
            )
            self._state["state"] = "cancelled" if cancelled else (
                "completed" if return_code == 0 else "searching" if should_lookup else "failed"
            )
            self._state["logs"].append(
                "任务已取消"
                if cancelled
                else "任务完成"
                if return_code == 0
                else "直链来源均未找到，正在查询 JavBus 磁力…"
                if should_lookup
                else f"任务失败（退出码 {return_code}）"
            )
        if cancelled:
            self._cleanup_cancelled_files(code)
            with self._lock:
                self._reset_state()
            return
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
