from __future__ import annotations

import subprocess
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from jable_downloader import normalize_code


class TaskBusyError(RuntimeError):
    pass


def safe_log_line(value: str) -> str:
    if ".m3u8" not in value.lower():
        return value
    return re.sub(r"https?://\S+", "[M3U8 URL 已隐藏]", value)


class DownloadTaskManager:
    def __init__(self, command: str = "/usr/local/bin/n", max_log_lines: int = 600) -> None:
        self.command = command
        self.max_log_lines = max_log_lines
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "code": None,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "logs": deque(maxlen=max_log_lines),
        }

    def start(self, raw_code: str) -> str:
        code = normalize_code(raw_code)
        with self._lock:
            if self._state["state"] == "running":
                raise TaskBusyError("已有下载任务正在运行")
            self._state = {
                "state": "running",
                "code": code,
                "started_at": time.time(),
                "finished_at": None,
                "return_code": None,
                "logs": deque([f"准备下载 {code}"], maxlen=self.max_log_lines),
            }
            try:
                process = subprocess.Popen(
                    [self.command, code],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
                self._process = process
            except OSError:
                self._state["state"] = "failed"
                self._state["finished_at"] = time.time()
                self._state["logs"].append("无法启动下载命令")
                raise
            thread = threading.Thread(target=self._collect, args=(process,), daemon=True)
            thread.start()
        return code

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["logs"] = list(self._state["logs"])
            return state

    def _collect(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                cleaned = safe_log_line(line.rstrip("\r\n"))
                if cleaned:
                    with self._lock:
                        self._state["logs"].append(cleaned)
        return_code = process.wait()
        with self._lock:
            self._state["return_code"] = return_code
            self._state["finished_at"] = time.time()
            self._state["state"] = "completed" if return_code == 0 else "failed"
            self._state["logs"].append(
                "任务完成" if return_code == 0 else f"任务失败（退出码 {return_code}）"
            )


def command_exists(command: str) -> bool:
    path = Path(command)
    return path.is_file() and path.stat().st_mode & 0o111 != 0
