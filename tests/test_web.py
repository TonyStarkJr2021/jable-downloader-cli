import json
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from jable_web.app import create_app
from jable_web.auth import LoginLimiter, hash_password, verify_password
from jable_web.media import (
    HiddenMediaStore,
    delete_media_files,
    list_media,
    parse_range,
    resolve_media,
)
from jable_web.setup_config import build_config, write_atomic
from jable_web.tasks import (
    DownloadTaskManager,
    PreservedLogBuffer,
    TerminalLogParser,
    safe_log_line,
)


class DummyTaskManager:
    def __init__(self):
        self.started = []
        self.state = "idle"

    def snapshot(self):
        return {"state": self.state, "code": None, "logs": []}

    def start(self, code):
        self.started.append(code)
        return "IPX-850"


class AuthenticationTests(unittest.TestCase):
    def test_scrypt_password_round_trip(self):
        record = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", record))
        self.assertFalse(verify_password("incorrect password", record))
        self.assertNotIn("correct horse", record)

    def test_limiter_blocks_after_maximum(self):
        limiter = LoginLimiter(max_attempts=2, window_seconds=60)
        self.assertTrue(limiter.allowed("client"))
        limiter.fail("client")
        limiter.fail("client")
        self.assertFalse(limiter.allowed("client"))
        self.assertGreater(limiter.retry_after("client"), 0)


class MediaTests(unittest.TestCase):
    def test_range_parsing(self):
        self.assertEqual(parse_range("bytes=0-3", 10), (0, 3))
        self.assertEqual(parse_range("bytes=4-", 10), (4, 9))
        self.assertEqual(parse_range("bytes=-4", 10), (6, 9))
        with self.assertRaises(ValueError):
            parse_range("bytes=20-30", 10)

    def test_media_resolution_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "media"
            media.mkdir()
            (media / "IPX-850.mp4").write_bytes(b"data")
            self.assertEqual(resolve_media(media, "IPX-850.mp4").name, "IPX-850.mp4")
            with self.assertRaises(FileNotFoundError):
                resolve_media(media, "../IPX-850.mp4")

    def test_classified_media_is_listed_and_resolved_recursively(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "media"
            classified = (
                media
                / "FC2"
                / "FC2-PPV-4968748"
                / "FC2-PPV-4968748.mp4"
            )
            classified.parent.mkdir(parents=True)
            classified.write_bytes(b"data")
            items = list_media(media)
            self.assertEqual(
                items[0]["name"],
                "FC2/FC2-PPV-4968748/FC2-PPV-4968748.mp4",
            )
            self.assertEqual(items[0]["category"], "FC2")
            self.assertEqual(
                resolve_media(
                    media, "FC2/FC2-PPV-4968748/FC2-PPV-4968748.mp4"
                ),
                classified,
            )

    def test_hidden_store_and_file_delete_are_separate_actions(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            media = base / "media"
            video = media / "JAV" / "IPX-850" / "IPX-850.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"data")
            (video.parent / "poster.jpg").write_bytes(b"image")
            store = HiddenMediaStore(base / "hidden.json")
            name = "JAV/IPX-850/IPX-850.mp4"
            store.add([name])
            self.assertIn(name, store.hidden())
            self.assertTrue(video.is_file())
            delete_media_files(media, [name])
            self.assertFalse(video.exists())
            self.assertFalse(video.parent.exists())

    def test_signed_m3u8_is_redacted_from_web_log(self):
        line = "https://cdn.example/video.m3u8?token=secret"
        self.assertNotIn("secret", safe_log_line(line))

    def test_terminal_parser_streams_carriage_return_progress(self):
        parser = TerminalLogParser()
        first = "下载进度 10%\r下载进".encode("utf-8")
        second = "度 20%\r\n已合并\n".encode("utf-8")
        self.assertEqual(parser.feed(first), [("下载进度 10%", True)])
        self.assertEqual(
            parser.feed(second),
            [("下载进度 20%", False), ("已合并", False)],
        )

    def test_log_buffer_preserves_task_start_and_latest_output(self):
        logs = PreservedLogBuffer(6, ["准备下载 IPX-850", "来源：SupJav"])
        for index in range(10):
            logs.append(f"下载日志 {index}")

        visible = list(logs)
        self.assertEqual(visible[:2], ["准备下载 IPX-850", "来源：SupJav"])
        self.assertIn("已省略 7 行", visible[2])
        self.assertEqual(visible[-3:], ["下载日志 7", "下载日志 8", "下载日志 9"])
        self.assertEqual(len(visible), 6)

    def test_terminal_control_codes_are_removed(self):
        self.assertEqual(safe_log_line("\x1b[32mDownloading 25%\x1b[0m"), "Downloading 25%")

    def test_task_manager_normalizes_fc2_and_routes_safe_detail_urls(self):
        process = mock.Mock()
        with (
            mock.patch("jable_web.tasks.subprocess.Popen", return_value=process) as popen,
            mock.patch("jable_web.tasks.threading.Thread.start"),
        ):
            manager = DownloadTaskManager("/usr/local/bin/n")
            code = manager.start("fc2ppv4968748")
        self.assertEqual(code, "FC2-PPV-4968748")
        self.assertEqual(
            popen.call_args.args[0], ["/usr/local/bin/n", "FC2-PPV-4968748"]
        )
        self.assertFalse(popen.call_args.kwargs["text"])
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(manager.snapshot()["source"], "fc2")


class SetupConfigTests(unittest.TestCase):
    def test_generated_config_has_hash_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "web.json"
            config, shown = build_config(
                target,
                host="127.0.0.1",
                username="tester",
                password="long-enough-password",
            )
            self.assertEqual(shown, "long-enough-password")
            self.assertNotIn("long-enough-password", json.dumps(config))
            write_atomic(target, config)
            preserved, shown_again = build_config(target)
            self.assertEqual(preserved["password_hash"], config["password_hash"])
            self.assertIsNone(shown_again)


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.media = root / "media"
        self.media.mkdir()
        (self.media / "IPX-850.mp4").write_bytes(b"0123456789")
        self.web_config = root / "web.json"
        self.cli_config = root / "config.json"
        self.web_config.write_text(
            json.dumps(
                {
                    "username": "admin",
                    "password_hash": hash_password("test-password-123"),
                    "host": "127.0.0.1",
                    "port": 28491,
                    "secure_cookie": False,
                    "session_timeout_seconds": 600,
                    "login_max_attempts": 5,
                    "login_lockout_seconds": 60,
                    "command": "/usr/local/bin/n",
                }
            ),
            encoding="utf-8",
        )
        self.cli_config.write_text(
            json.dumps({"media_dir": str(self.media)}), encoding="utf-8"
        )
        self.tasks = DummyTaskManager()
        self.hidden_media = root / "hidden-media.json"
        self.client = TestClient(
            create_app(
                self.web_config,
                self.cli_config,
                self.tasks,
                hidden_media_path=self.hidden_media,
            )
        )

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def login(self):
        page = self.client.get("/login")
        self.assertIn('/static/app.css?v=2.7.3', page.text)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "test-password-123",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        dashboard = self.client.get("/")
        self.assertIn('/static/app.js?v=2.7.3', dashboard.text)
        return re.search(r'name="csrf-token" content="([^"]+)"', dashboard.text).group(1)

    def test_media_apis_require_login(self):
        self.assertEqual(self.client.get("/api/media").status_code, 401)
        self.assertEqual(self.client.get("/download/IPX-850.mp4").status_code, 401)
        self.assertEqual(
            self.client.get("/settings", follow_redirects=False).status_code, 303
        )

    def test_login_csrf_task_csrf_and_range_download(self):
        csrf = self.login()
        forbidden = self.client.post("/api/tasks", json={"code": "ipx850"})
        self.assertEqual(forbidden.status_code, 403)
        started = self.client.post(
            "/api/tasks",
            json={"code": "ipx850"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(started.status_code, 202)
        response = self.client.get(
            "/download/IPX-850.mp4", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")

    def test_classified_media_download_route_supports_nested_path(self):
        classified = self.media / "JAV" / "IPX-851" / "IPX-851.mp4"
        classified.parent.mkdir(parents=True)
        classified.write_bytes(b"abcdefghij")
        self.login()
        listing = self.client.get("/api/media").json()["items"]
        self.assertIn(
            "JAV/IPX-851/IPX-851.mp4", {item["name"] for item in listing}
        )
        response = self.client.get(
            "/download/JAV/IPX-851/IPX-851.mp4",
            headers={"Range": "bytes=1-3"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"bcd")

    def test_completed_items_can_be_hidden_without_deleting_or_deleted_with_file(self):
        csrf = self.login()
        hidden = self.client.post(
            "/api/media/actions",
            json={"action": "hide", "items": ["IPX-850.mp4"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertTrue((self.media / "IPX-850.mp4").is_file())
        listing = self.client.get("/api/media").json()
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["total_count"], 1)

        deleted = self.client.post(
            "/api/media/actions",
            json={"action": "delete", "items": ["IPX-850.mp4"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse((self.media / "IPX-850.mp4").exists())
        self.assertEqual(self.client.get("/api/media").json()["total_count"], 0)

    def test_completed_item_actions_require_csrf_and_reject_traversal(self):
        csrf = self.login()
        forbidden = self.client.post(
            "/api/media/actions",
            json={"action": "hide", "items": ["IPX-850.mp4"]},
        )
        self.assertEqual(forbidden.status_code, 403)
        malformed = self.client.post(
            "/api/media/actions",
            json=[],
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(malformed.status_code, 400)
        traversal = self.client.post(
            "/api/media/actions",
            json={"action": "delete", "items": ["../IPX-850.mp4"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(traversal.status_code, 404)

    def test_account_settings_require_current_password_and_persist_hash(self):
        csrf = self.login()
        wrong = self.client.post(
            "/api/settings/account",
            json={
                "username": "new_admin",
                "new_password": "new-password-123",
                "confirm_password": "new-password-123",
                "current_password": "wrong-password",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(wrong.status_code, 403)
        changed = self.client.post(
            "/api/settings/account",
            json={
                "username": "new_admin",
                "new_password": "new-password-123",
                "confirm_password": "new-password-123",
                "current_password": "test-password-123",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(changed.status_code, 200)
        saved = json.loads(self.web_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["username"], "new_admin")
        self.assertTrue(verify_password("new-password-123", saved["password_hash"]))
        self.assertNotIn("new-password-123", self.web_config.read_text(encoding="utf-8"))

    def test_port_settings_check_password_conflict_and_schedule_restart(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with mock.patch("jable_web.app.port_available", return_value=False):
            conflict = self.client.post(
                "/api/settings/port",
                json={"port": 34567, "current_password": "test-password-123"},
                headers=headers,
            )
        self.assertEqual(conflict.status_code, 409)
        with (
            mock.patch("jable_web.app.port_available", return_value=True),
            mock.patch("jable_web.app.schedule_restart") as restart,
        ):
            changed = self.client.post(
                "/api/settings/port",
                json={"port": 34567, "current_password": "test-password-123"},
                headers=headers,
            )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["restart"])
        self.assertIn(":34567/settings", changed.json()["new_url"])
        restart.assert_called_once()
        saved = json.loads(self.web_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["port"], 34567)

    def test_port_change_is_blocked_during_download(self):
        csrf = self.login()
        self.tasks.state = "running"
        blocked = self.client.post(
            "/api/settings/port",
            json={"port": 34568, "current_password": "test-password-123"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(blocked.status_code, 409)
        saved = json.loads(self.web_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["port"], 28491)

    def test_supjav_proxy_settings_validate_mask_test_save_and_clear(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        invalid = self.client.post(
            "/api/settings/supjav-proxy",
            json={
                "proxy_url": "ftp://proxy.example:21",
            },
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)
        proxy_url = "http://user:secret@proxy.example:8080"
        saved_response = self.client.post(
            "/api/settings/supjav-proxy",
            json={
                "proxy_url": proxy_url,
                "proxy_download": True,
            },
            headers=headers,
        )
        self.assertEqual(saved_response.status_code, 200)
        self.assertEqual(
            saved_response.json()["proxy_label"], "http://proxy.example:8080"
        )
        saved = json.loads(self.cli_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["supjav_proxy_url"], proxy_url)
        self.assertTrue(saved["supjav_proxy_download"])
        page = self.client.get("/settings")
        self.assertIn("http://proxy.example:8080", page.text)
        self.assertNotIn("user:secret", page.text)
        response = types.SimpleNamespace(status_code=200)
        with mock.patch("jable_web.app.browser_requests.get", return_value=response) as get:
            tested = self.client.post(
                "/api/settings/supjav-proxy/test",
                json={
                    "proxy_url": "",
                },
                headers=headers,
            )
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(get.call_args.kwargs["proxy"], proxy_url)
        cleared = self.client.post(
            "/api/settings/supjav-proxy",
            json={"clear": True},
            headers=headers,
        )
        self.assertEqual(cleared.status_code, 200)
        cleared_config = json.loads(self.cli_config.read_text(encoding="utf-8"))
        self.assertEqual(cleared_config["supjav_proxy_url"], "")
        self.assertFalse(cleared_config["supjav_proxy_download"])

    def test_supjav_proxy_settings_require_login_and_csrf(self):
        unauthenticated = self.client.post(
            "/api/settings/supjav-proxy",
            json={"proxy_url": "http://proxy.example:8080"},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        csrf = self.login()
        missing_csrf = self.client.post(
            "/api/settings/supjav-proxy",
            json={"proxy_url": "http://proxy.example:8080"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        saved = self.client.post(
            "/api/settings/supjav-proxy",
            json={"proxy_url": "http://proxy.example:8080"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(saved.status_code, 200)

    def test_supjav_adblock_settings_validate_save_and_require_csrf(self):
        unauthenticated = self.client.post(
            "/api/settings/supjav-protection",
            json={"enabled": True, "play_attempts": 10},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        csrf = self.login()
        missing_csrf = self.client.post(
            "/api/settings/supjav-protection",
            json={"enabled": True, "play_attempts": 10},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        invalid = self.client.post(
            "/api/settings/supjav-protection",
            json={"enabled": True, "play_attempts": 31},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(invalid.status_code, 400)
        saved_response = self.client.post(
            "/api/settings/supjav-protection",
            json={"enabled": False, "play_attempts": 14},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(saved_response.status_code, 200)
        saved = json.loads(self.cli_config.read_text(encoding="utf-8"))
        self.assertFalse(saved["supjav_adblock_enabled"])
        self.assertEqual(saved["supjav_play_attempts"], 14)
        page = self.client.get("/settings")
        self.assertIn("SupJav 广告防护", page.text)
        self.assertIn('value="14"', page.text)
        self.assertNotIn('id="adblock-enabled" name="adblock_enabled" type="checkbox" checked', page.text)

    def test_settings_page_contains_firewall_warning(self):
        self.login()
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("防火墙和云服务器安全组", page.text)
        self.assertIn("保存并重启 Web", page.text)
        self.assertIn("SupJav 专用代理", page.text)
        self.assertIn("当前未配置代理", page.text)
        self.assertIn("SupJav 广告防护", page.text)
        self.assertIn("不会联网下载第三方规则", page.text)
        self.assertNotIn("proxy-current-password", page.text)


class FrontendRegressionTests(unittest.TestCase):
    def test_requested_copy_and_log_scrolling_are_present(self):
        root = Path(__file__).parents[1] / "jable_web"
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("<h2>已完成</h2>", dashboard)
        self.assertIn("点击对应项目右侧的“查看”", dashboard)
        self.assertIn("并行解析 Jable、MissAV、SupJav", dashboard)
        self.assertIn("按真实分辨率和码率选择最佳直链", dashboard)
        self.assertIn("300MIUM-1483", dashboard)
        self.assertIn("服务器存储", dashboard)
        self.assertIn("JavBus 推荐磁链", dashboard)
        self.assertIn("app.css?v={{ app_version }}", dashboard)
        self.assertIn("app.js?v={{ app_version }}", dashboard)
        self.assertIn("复制磁力链接", script)
        self.assertIn("renderMagnets(task)", script)
        self.assertIn("scrollIntoView", script)
        self.assertIn("showCurrentTask", script)
        self.assertIn("renderIdleTask", script)
        self.assertIn('performMediaAction("hide")', script)
        self.assertIn('performMediaAction("delete")', script)
        self.assertIn("仅从列表移除", dashboard)
        self.assertIn("删除任务及文件", dashboard)
        self.assertIn("部作品", dashboard)
        self.assertIn(".magnet-help", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("log.scrollTop = log.scrollHeight", script)
        self.assertIn("if (task.progress) visibleLogs.push(task.progress)", script)
        self.assertIn("clamp(26px, 3vw, 42px)", css)


if __name__ == "__main__":
    unittest.main()
