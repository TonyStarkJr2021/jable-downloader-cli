from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any

from jable_web.auth import hash_password


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def validate_host(value: str) -> str:
    if value == "0.0.0.0":
        return value
    ipaddress.ip_address(value)
    return value


def port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe_host = "::" if host == "0.0.0.0" and family == socket.AF_INET6 else host
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((probe_host, port))
        return True
    except OSError:
        return False


def choose_port(host: str) -> int:
    candidates = list(range(20000, 60001))
    for _ in range(256):
        port = secrets.choice(candidates)
        if port_available(host, port):
            return port
    raise RuntimeError("无法找到可用的 Web 端口")


def load_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_atomic(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".web.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_config(
    path: Path,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    existing = load_existing(path)
    selected_host = validate_host(host or str(existing.get("host", "0.0.0.0")))
    selected_username = username or str(existing.get("username", ""))
    if not selected_username:
        selected_username = f"jable_{secrets.token_hex(3)}"
    if not USERNAME_RE.fullmatch(selected_username):
        raise ValueError("用户名必须为 3–64 位字母、数字、点、下划线或短横线")

    selected_port = int(port or existing.get("port", 0) or 0)
    port_changed = port is not None and selected_port != int(existing.get("port", 0) or 0)
    if selected_port:
        if not 1024 <= selected_port <= 65535:
            raise ValueError("Web 端口必须在 1024–65535 之间")
        if (not existing or port_changed or host is not None) and not port_available(
            selected_host, selected_port
        ):
            raise ValueError(f"端口 {selected_port} 已被占用")
    else:
        selected_port = choose_port(selected_host)

    shown_password: str | None = None
    password_hash = str(existing.get("password_hash", ""))
    if password is not None:
        if any(character in password for character in "\r\n"):
            raise ValueError("密码不能包含换行符")
        password_hash = hash_password(password)
        shown_password = password
    elif not password_hash:
        shown_password = secrets.token_urlsafe(18)
        password_hash = hash_password(shown_password)

    config = {
        "host": selected_host,
        "port": selected_port,
        "username": selected_username,
        "password_hash": password_hash,
        "secure_cookie": bool(existing.get("secure_cookie", False)),
        "session_timeout_seconds": int(existing.get("session_timeout_seconds", 43200)),
        "login_max_attempts": int(existing.get("login_max_attempts", 5)),
        "login_lockout_seconds": int(existing.get("login_lockout_seconds", 900)),
        "command": str(existing.get("command", "/usr/local/bin/n")),
    }
    return config, shown_password


def main() -> None:
    parser = argparse.ArgumentParser(description="生成或保留 Jable Web 配置")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--username")
    parser.add_argument("--password")
    args = parser.parse_args()
    config, shown_password = build_config(
        args.output, args.host, args.port, args.username, args.password
    )
    write_atomic(args.output, config)
    print(f"JABLE_WEB_HOST={config['host']}")
    print(f"JABLE_WEB_PORT={config['port']}")
    print(f"JABLE_WEB_USER={config['username']}")
    if shown_password is None:
        print("JABLE_WEB_PASSWORD=__PRESERVED__")
    else:
        print(f"JABLE_WEB_PASSWORD={shown_password}")


if __name__ == "__main__":
    main()
