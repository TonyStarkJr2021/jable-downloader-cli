from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jable_downloader import (
    DEFAULT_BROWSER_UA,
    duration_text,
    normalize_proxy_url,
    proxy_display_label,
)
from jable_web import __version__
from jable_web.auth import (
    LoginLimiter,
    Session,
    SessionStore,
    hash_password,
    verify_password,
)
from jable_web.media import (
    HiddenMediaStore,
    content_disposition,
    delete_media_files,
    iter_file,
    list_media,
    media_type,
    parse_range,
    probe_media,
    resolve_media,
)
from jable_web.tasks import DownloadTaskManager, TaskBusyError
from jable_web.setup_config import USERNAME_RE, port_available, write_atomic

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover - installer supplies this dependency
    browser_requests = None


COOKIE_NAME = "jable_session"
LOGIN_CSRF_COOKIE = "jable_login_csrf"
PACKAGE_DIR = Path(__file__).resolve().parent


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def create_app(
    web_config_path: Path | None = None,
    cli_config_path: Path | None = None,
    task_manager: DownloadTaskManager | None = None,
    restart_service: Callable[[], None] | None = None,
    hidden_media_path: Path | None = None,
) -> FastAPI:
    web_config_path = web_config_path or Path(
        os.environ.get("JABLE_WEB_CONFIG", "/etc/jable-downloader/web.json")
    )
    cli_config_path = cli_config_path or Path(
        os.environ.get("JABLE_CONFIG_FILE", "/etc/jable-downloader/config.json")
    )
    web_config = load_json(web_config_path)
    cli_config = load_json(cli_config_path)
    username = str(web_config["username"])
    password_hash = str(web_config["password_hash"])
    secure_cookie = bool(web_config.get("secure_cookie", False))
    session_timeout = int(web_config.get("session_timeout_seconds", 43200))
    max_attempts = int(web_config.get("login_max_attempts", 5))
    lockout_seconds = int(web_config.get("login_lockout_seconds", 900))
    command = str(web_config.get("command", "/usr/local/bin/n"))
    media_dir = Path(str(cli_config["media_dir"]))
    hidden_media_path = hidden_media_path or Path(
        os.environ.get(
            "JABLE_HIDDEN_MEDIA_FILE", "/var/lib/jable-downloader/hidden-media.json"
        )
    )

    app = FastAPI(
        title="Jable + MissAV + SupJav Downloader Web",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.state.sessions = SessionStore(session_timeout)
    app.state.limiter = LoginLimiter(max_attempts, lockout_seconds)
    app.state.task_manager = task_manager or DownloadTaskManager(
        command,
        javbus_enabled=bool(cli_config.get("javbus_fallback_enabled", True)),
        javbus_site=str(cli_config.get("javbus_site", "https://www.javbus.com")),
        javbus_timeout_seconds=int(cli_config.get("javbus_timeout_seconds", 15)),
    )
    app.state.media_dir = media_dir
    app.state.hidden_media = HiddenMediaStore(hidden_media_path)
    app.state.username = username
    app.state.password_hash = password_hash
    app.state.secure_cookie = secure_cookie
    app.state.web_config_path = web_config_path
    app.state.cli_config_path = cli_config_path
    app.state.web_host = str(web_config.get("host", "0.0.0.0"))
    app.state.web_port = int(web_config.get("port", 0))
    app.state.config_lock = threading.Lock()
    app.state.restart_service = restart_service or default_restart_service

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def current_session(request: Request) -> Session | None:
        return app.state.sessions.get(request.cookies.get(COOKIE_NAME))

    def require_session(request: Request) -> Session:
        session = current_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session

    def require_csrf(request: Request, session: Session) -> None:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
            raise HTTPException(status_code=403, detail="请求验证失败，请刷新页面")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if current_session(request):
            return RedirectResponse("/", status_code=303)
        nonce = secrets.token_urlsafe(24)
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"login_csrf": nonce, "app_version": __version__},
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            nonce,
            max_age=600,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path="/login",
        )
        return response

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        key = client_key(request)
        supplied_csrf = str(form.get("csrf_token", ""))
        cookie_csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not cookie_csrf or not hmac.compare_digest(supplied_csrf, cookie_csrf):
            raise HTTPException(status_code=403, detail="登录页面已失效，请刷新后重试")
        if not app.state.limiter.allowed(key):
            retry = app.state.limiter.retry_after(key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                status_code=429,
                context={
                    "login_csrf": supplied_csrf,
                    "error": f"尝试次数过多，请在 {retry} 秒后重试",
                    "app_version": __version__,
                },
            )
        supplied_user = str(form.get("username", ""))[:128]
        supplied_password = str(form.get("password", ""))
        valid_password = verify_password(supplied_password, app.state.password_hash)
        valid = hmac.compare_digest(supplied_user, app.state.username) and valid_password
        if not valid:
            app.state.limiter.fail(key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                status_code=401,
                context={
                    "login_csrf": supplied_csrf,
                    "error": "用户名或密码错误",
                    "app_version": __version__,
                },
            )
        app.state.limiter.clear(key)
        token, _session = app.state.sessions.create(app.state.username)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=session_timeout,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = require_session(request)
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))
        if not hmac.compare_digest(supplied, session.csrf_token):
            raise HTTPException(status_code=403, detail="请求验证失败")
        app.state.sessions.delete(request.cookies.get(COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "username": session.username,
                "csrf_token": session.csrf_token,
                "app_version": __version__,
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        current_cli_config = load_json(app.state.cli_config_path)
        current_proxy = str(current_cli_config.get("supjav_proxy_url", ""))
        try:
            proxy_label = proxy_display_label(current_proxy)
        except ValueError:
            proxy_label = "配置格式无效"
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "username": app.state.username,
                "port": app.state.web_port,
                "supjav_proxy_configured": bool(current_proxy),
                "supjav_proxy_label": proxy_label,
                "supjav_proxy_download": bool(
                    current_cli_config.get("supjav_proxy_download", False)
                ),
                "csrf_token": session.csrf_token,
                "app_version": __version__,
            },
        )

    @app.post("/api/settings/account")
    async def update_account(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求格式错误")
        current_password = str(payload.get("current_password", ""))
        new_username = str(payload.get("username", "")).strip()
        new_password = str(payload.get("new_password", ""))
        confirm_password = str(payload.get("confirm_password", ""))
        if not verify_password(current_password, app.state.password_hash):
            raise HTTPException(status_code=403, detail="当前密码错误")
        if not USERNAME_RE.fullmatch(new_username):
            raise HTTPException(
                status_code=400,
                detail="用户名必须为 3–64 位字母、数字、点、下划线或短横线",
            )
        if new_password != confirm_password:
            raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
        try:
            new_hash = hash_password(new_password) if new_password else app.state.password_hash
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            with app.state.config_lock:
                config = load_json(app.state.web_config_path)
                config["username"] = new_username
                config["password_hash"] = new_hash
                write_atomic(app.state.web_config_path, config)
                app.state.username = new_username
                app.state.password_hash = new_hash
        except OSError as exc:
            raise HTTPException(status_code=500, detail="保存账号设置失败") from exc
        app.state.sessions.keep_only(request.cookies.get(COOKIE_NAME), new_username)
        return {"message": "账号设置已保存", "username": new_username}

    @app.post("/api/settings/port")
    async def update_port(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求格式错误")
        current_password = str(payload.get("current_password", ""))
        if not verify_password(current_password, app.state.password_hash):
            raise HTTPException(status_code=403, detail="当前密码错误")
        try:
            new_port = int(payload.get("port"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="请输入有效端口") from exc
        if not 1024 <= new_port <= 65535:
            raise HTTPException(status_code=400, detail="端口必须在 1024–65535 之间")
        if app.state.task_manager.snapshot().get("state") in {"running", "searching"}:
            raise HTTPException(status_code=409, detail="下载任务运行中，暂时不能更换端口")
        if new_port == app.state.web_port:
            return {"message": "端口没有变化", "restart": False}
        if not port_available(app.state.web_host, new_port):
            raise HTTPException(status_code=409, detail=f"端口 {new_port} 已被占用")
        try:
            with app.state.config_lock:
                config = load_json(app.state.web_config_path)
                config["port"] = new_port
                write_atomic(app.state.web_config_path, config)
                app.state.web_port = new_port
        except OSError as exc:
            raise HTTPException(status_code=500, detail="保存端口设置失败") from exc
        schedule_restart(app.state.restart_service)
        display_host = request.url.hostname or "服务器IP"
        new_url = f"{request.url.scheme}://{display_host}:{new_port}/settings"
        return {
            "message": "端口已保存，Web 服务即将重启",
            "restart": True,
            "new_url": new_url,
            "port": new_port,
        }

    def requested_supjav_proxy(payload: dict[str, Any]) -> str:
        entered = str(payload.get("proxy_url", "")).strip()
        if entered:
            try:
                return normalize_proxy_url(entered)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        current = load_json(app.state.cli_config_path)
        try:
            return normalize_proxy_url(str(current.get("supjav_proxy_url", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"现有代理配置无效：{exc}") from exc

    @app.post("/api/settings/supjav-proxy/test")
    async def test_supjav_proxy(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求格式错误")
        if not verify_password(
            str(payload.get("current_password", "")), app.state.password_hash
        ):
            raise HTTPException(status_code=403, detail="当前密码错误")
        proxy_url = requested_supjav_proxy(payload)
        if not proxy_url:
            raise HTTPException(status_code=400, detail="请先输入代理地址")
        if browser_requests is None:
            raise HTTPException(status_code=500, detail="服务器缺少代理测试组件")
        try:
            response = browser_requests.get(
                "https://supjav.com/search/TEST-000/",
                impersonate="chrome",
                headers={"User-Agent": DEFAULT_BROWSER_UA},
                timeout=15,
                allow_redirects=True,
                proxy=proxy_url,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="代理连接失败") from exc
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"代理已连接，但 SupJav 返回 HTTP {response.status_code}",
            )
        return {
            "message": "代理连接成功，SupJav 搜索页可访问",
            "proxy_label": proxy_display_label(proxy_url),
        }

    @app.post("/api/settings/supjav-proxy")
    async def update_supjav_proxy(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求格式错误")
        if not verify_password(
            str(payload.get("current_password", "")), app.state.password_hash
        ):
            raise HTTPException(status_code=403, detail="当前密码错误")
        clear = bool(payload.get("clear", False))
        proxy_url = "" if clear else requested_supjav_proxy(payload)
        if not clear and not proxy_url:
            raise HTTPException(status_code=400, detail="请先输入代理地址")
        proxy_download = bool(payload.get("proxy_download", False)) if proxy_url else False
        try:
            with app.state.config_lock:
                config = load_json(app.state.cli_config_path)
                config["supjav_proxy_url"] = proxy_url
                config["supjav_proxy_download"] = proxy_download
                write_atomic(app.state.cli_config_path, config)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="保存 SupJav 代理失败") from exc
        return {
            "message": "SupJav 代理已清除" if clear else "SupJav 代理已保存，下个任务生效",
            "configured": bool(proxy_url),
            "proxy_label": proxy_display_label(proxy_url),
            "proxy_download": proxy_download,
        }

    @app.get("/api/status")
    async def status(request: Request):
        require_session(request)
        return app.state.task_manager.snapshot()

    @app.post("/api/tasks")
    async def start_task(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        try:
            payload = await request.json()
            code = app.state.task_manager.start(str(payload.get("code", "")))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TaskBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="下载命令启动失败") from exc
        return JSONResponse({"code": code, "state": "running"}, status_code=202)

    @app.get("/api/media")
    async def media_list(request: Request):
        require_session(request)
        all_items = list_media(app.state.media_dir)
        hidden = app.state.hidden_media.hidden()
        return {
            "items": [item for item in all_items if item["name"] not in hidden],
            "total_count": len(all_items),
        }

    @app.post("/api/media/actions")
    async def media_actions(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        try:
            payload = await request.json()
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="请求格式错误") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求格式错误")
        action = payload.get("action")
        raw_names = payload.get("items")
        if action not in {"hide", "delete"}:
            raise HTTPException(status_code=400, detail="不支持的删除方式")
        if not isinstance(raw_names, list) or not 1 <= len(raw_names) <= 200:
            raise HTTPException(status_code=400, detail="请选择 1–200 个已完成项目")
        if not all(isinstance(name, str) and name for name in raw_names):
            raise HTTPException(status_code=400, detail="已完成项目列表无效")
        names = list(dict.fromkeys(raw_names))
        if len(names) != len(raw_names):
            raise HTTPException(status_code=400, detail="已完成项目列表包含重复项")
        try:
            for name in names:
                resolve_media(app.state.media_dir, name)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="部分文件已不存在，请刷新列表") from exc

        if action == "hide":
            try:
                app.state.hidden_media.add(names)
            except OSError as exc:
                raise HTTPException(status_code=500, detail="保存已完成列表失败") from exc
            return {"message": f"已从列表移除 {len(names)} 项，服务器文件保持不变"}

        try:
            delete_media_files(app.state.media_dir, names)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="删除服务器文件失败") from exc
        try:
            app.state.hidden_media.discard(names)
        except OSError:
            pass
        return {"message": f"已删除 {len(names)} 项及对应服务器文件"}

    @app.get("/api/media/{filename:path}")
    async def media_detail(filename: str, request: Request):
        require_session(request)
        try:
            path = resolve_media(app.state.media_dir, filename)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        info = probe_media(path)
        info.update(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "duration_text": duration_text(float(info["duration"])),
            }
        )
        return info

    @app.get("/download/{filename:path}")
    async def download(filename: str, request: Request):
        require_session(request)
        try:
            path = resolve_media(app.state.media_dir, filename)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        size = path.stat().st_size
        try:
            requested = parse_range(request.headers.get("range"), size)
        except (ValueError, TypeError):
            return Response416(size)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": content_disposition(path.name),
        }
        if requested is None:
            headers["Content-Length"] = str(size)
            return StreamingResponse(
                iter_file(path, 0, size), media_type=media_type(path), headers=headers
            )
        start, end = requested
        length = end - start + 1
        headers.update(
            {
                "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }
        )
        return StreamingResponse(
            iter_file(path, start, length),
            status_code=206,
            media_type=media_type(path),
            headers=headers,
        )

    return app


def Response416(size: int) -> JSONResponse:
    return JSONResponse(
        {"detail": "请求的文件范围无效"},
        status_code=416,
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def default_restart_service() -> None:
    subprocess.run(
        ["/bin/systemctl", "--no-block", "restart", "jable-downloader-web.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def schedule_restart(callback: Callable[[], None]) -> None:
    def delayed_restart() -> None:
        time.sleep(1.0)
        callback()

    threading.Thread(target=delayed_restart, daemon=True).start()
