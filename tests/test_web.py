import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from jable_web.app import create_app
from jable_web.auth import LoginLimiter, hash_password, verify_password
from jable_web.media import parse_range, resolve_media
from jable_web.setup_config import build_config, write_atomic
from jable_web.tasks import safe_log_line


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

    def test_signed_m3u8_is_redacted_from_web_log(self):
        line = "https://cdn.example/video.m3u8?token=secret"
        self.assertNotIn("secret", safe_log_line(line))


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
        self.client = TestClient(
            create_app(self.web_config, self.cli_config, self.tasks)
        )

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def login(self):
        page = self.client.get("/login")
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

    def test_settings_page_contains_firewall_warning(self):
        self.login()
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("防火墙和云服务器安全组", page.text)
        self.assertIn("保存并重启 Web", page.text)


class FrontendRegressionTests(unittest.TestCase):
    def test_requested_copy_and_log_scrolling_are_present(self):
        root = Path(__file__).parents[1] / "jable_web"
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("<h2>已完成</h2>", dashboard)
        self.assertIn("点击对应项目右侧的“查看”", dashboard)
        self.assertIn("log.scrollTop = log.scrollHeight", script)
        self.assertIn("clamp(26px, 3vw, 42px)", css)


if __name__ == "__main__":
    unittest.main()
