import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


# Unit tests exercise the pure/local behavior without starting a browser. Keep
# lightweight fallbacks only for environments where optional packages are absent.
try:
    import bs4  # noqa: F401
except ImportError:
    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules["bs4"] = bs4
try:
    import playwright.sync_api  # noqa: F401
except ImportError:
    playwright = types.ModuleType("playwright")
    playwright_sync = types.ModuleType("playwright.sync_api")
    playwright_sync.TimeoutError = TimeoutError
    playwright_sync.sync_playwright = lambda: None
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = playwright_sync

MODULE_PATH = Path(__file__).parents[1] / "jable_downloader.py"
SPEC = importlib.util.spec_from_file_location("jable_downloader", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NormalizeCodeTests(unittest.TestCase):
    def test_supported_forms(self):
        expected = "IPX-850"
        for value in ("IPX-850", "ipx850", "IPX 850", " ipx_850 "):
            with self.subTest(value=value):
                self.assertEqual(MODULE.normalize_code(value), expected)

    def test_digit_prefixed_labels_are_supported(self):
        for value, expected in (
            ("300MIUM-1483", "300MIUM-1483"),
            ("300mium1483", "300MIUM-1483"),
            ("300MIUM 1483", "300MIUM-1483"),
            ("1pondo-123456", "1PONDO-123456"),
        ):
            with self.subTest(value=value):
                self.assertEqual(MODULE.normalize_code(value), expected)

    def test_fc2_supported_forms(self):
        expected = "FC2-PPV-4968748"
        for value in (
            "FC2-PPV-4968748",
            "fc2ppv4968748",
            "FC2 PPV 4968748",
            "fc2-4968748",
            "fc24968748",
        ):
            with self.subTest(value=value):
                self.assertEqual(MODULE.normalize_code(value), expected)

    def test_rejects_unsafe_or_ambiguous_input(self):
        for value in (
            "",
            "850",
            "IPX",
            "IPX-850;rm",
            "IPX/850",
            f"A{'B' * 32}123",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.normalize_code(value)

    def test_automatic_provider_routing(self):
        ordinary = MODULE.parse_download_input("ipx850")
        self.assertEqual((ordinary.code, ordinary.source), ("IPX-850", "auto"))
        fc2 = MODULE.parse_download_input("fc2ppv4968748")
        self.assertEqual((fc2.code, fc2.source), ("FC2-PPV-4968748", "missav"))

    def test_supported_detail_urls_are_sanitized_and_routed(self):
        jable = MODULE.parse_download_input(
            "https://jable.tv/videos/ipx-850/?quality=best#player"
        )
        self.assertEqual(jable.code, "IPX-850")
        self.assertEqual(jable.source, "jable")
        self.assertNotIn("#player", jable.detail_url)
        missav = MODULE.parse_download_input(
            "https://missav.ai/dm597/en/fc2-ppv-4968748"
        )
        self.assertEqual(missav.code, "FC2-PPV-4968748")
        self.assertEqual(missav.source, "missav")

    def test_unsupported_or_credentialed_urls_are_rejected(self):
        for value in (
            "https://example.com/IPX-850",
            "https://user:pass@missav.ai/en/ipx-850",
            "https://missav.ai:8080/en/ipx-850",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.parse_download_input(value)

    def test_ordinary_code_falls_back_from_jable_to_missav(self):
        request = MODULE.parse_download_input("IPX-850")
        expected = MODULE.CapturedStream(
            "https://surrit.example/playlist.m3u8",
            "missav",
            "https://missav.ai/en/ipx-850",
            "Browser UA",
            {},
        )

        def capture(_request, source, _config):
            if source == "jable":
                raise MODULE.AppError("JABLE 没找到 IPX-850", 3)
            return expected

        with (
            mock.patch.object(MODULE, "capture_from_provider", side_effect=capture) as provider,
            mock.patch("builtins.print"),
        ):
            result = MODULE.capture_stream(request, {})
        self.assertEqual(result, expected)
        self.assertEqual(
            [call.args[1] for call in provider.call_args_list], ["jable", "missav"]
        )

    def test_all_not_found_preserves_distinct_exit_code_for_web_fallback(self):
        request = MODULE.parse_download_input("IPX-850")

        def unavailable(_request, source, _config):
            raise MODULE.AppError(f"{source.upper()} 没找到 IPX-850", 3)

        with (
            mock.patch.object(
                MODULE, "capture_from_provider", side_effect=unavailable
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(MODULE.AppError) as raised:
                MODULE.capture_stream(request, {})
        self.assertEqual(raised.exception.exit_code, 3)


class MissAVStaticCaptureTests(unittest.TestCase):
    PACKED = (
        "eval(function(p,a,c,k,e,d){return p;}"
        "('0=\\'1://2/3.4\\';',5,5,"
        "'source|https|surrit.com|asset/playlist|m3u8'.split('|'),0,{}))"
    )

    def test_safely_unpacks_public_player_stream(self):
        payloads = MODULE.unpack_packer_payloads(self.PACKED)
        self.assertEqual(
            payloads,
            ["source='https://surrit.com/asset/playlist.m3u8';"],
        )
        self.assertEqual(
            MODULE.extract_packed_m3u8_urls(self.PACKED),
            ["https://surrit.com/asset/playlist.m3u8"],
        )

    def test_prefers_master_playlist_on_configured_cdn(self):
        urls = [
            "https://other.example/video/720p.m3u8",
            "https://surrit.com/asset/video/720p.m3u8",
            "https://surrit.com/asset/playlist.m3u8",
        ]
        self.assertEqual(
            MODULE.choose_m3u8_url(urls, ["surrit.com"]),
            "https://surrit.com/asset/playlist.m3u8",
        )

    def test_static_capture_returns_headers_and_cookies_without_browser(self):
        response = types.SimpleNamespace(
            status_code=200,
            url="https://missav.ai/en/fc2-ppv-4968748",
            text=self.PACKED,
        )
        session = mock.Mock()
        session.get.return_value = response
        session.cookies.get_dict.return_value = {"session": "value"}
        requests = types.SimpleNamespace(Session=mock.Mock(return_value=session))
        request = MODULE.parse_download_input(
            "https://missav.ai/en/fc2-ppv-4968748"
        )
        config = {
            "missav_site": "https://missav.ai",
            "missav_language": "en",
            "missav_m3u8_preferred_domains": ["surrit.com"],
            "page_timeout_ms": 30000,
        }
        with (
            mock.patch.object(MODULE, "browser_requests", requests),
            mock.patch("builtins.print"),
        ):
            stream = MODULE.capture_missav_static(request, config)
        self.assertEqual(stream.url, "https://surrit.com/asset/playlist.m3u8")
        self.assertEqual(stream.source, "missav")
        self.assertEqual(stream.cookies, {"session": "value"})
        requests.Session.assert_called_once()


class LocalFileTests(unittest.TestCase):
    def test_existing_media_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            downloads = root_path / "downloads"
            media = root_path / "media"
            downloads.mkdir()
            media.mkdir()
            expected = media / "ipx-850.MP4"
            expected.write_bytes(b"test")
            found = MODULE.existing_media(
                "IPX-850",
                {"download_dir": str(downloads), "media_dir": str(media)},
            )
            self.assertEqual(found, expected)

    def test_existing_media_finds_legacy_root_and_classified_directories(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            downloads = root_path / "downloads"
            media = root_path / "media"
            downloads.mkdir()
            (media / "JAV").mkdir(parents=True)
            (media / "FC2").mkdir()
            legacy = media / "IPX-850.mp4"
            classified = media / "FC2" / "FC2-PPV-4968748.mp4"
            legacy.write_bytes(b"legacy")
            classified.write_bytes(b"classified")
            config = {
                "download_dir": str(downloads),
                "media_dir": str(media),
                "jav_media_dir": str(media / "JAV"),
                "fc2_media_dir": str(media / "FC2"),
            }
            self.assertEqual(MODULE.existing_media("IPX-850", config), legacy)
            self.assertEqual(
                MODULE.existing_media("FC2-PPV-4968748", config), classified
            )

    def test_media_destination_classifies_and_creates_per_title_path(self):
        config = {"media_dir": "/media"}
        self.assertEqual(
            MODULE.media_destination("IPX-850", config), Path("/media/JAV/IPX-850")
        )
        self.assertEqual(
            MODULE.media_destination("FC2-PPV-4968748", config),
            Path("/media/FC2/FC2-PPV-4968748"),
        )

    def test_move_to_media_creates_fc2_destination_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            finished = root_path / "downloads" / "FC2-PPV-4968748.mp4"
            finished.parent.mkdir()
            finished.write_bytes(b"media")
            config = {
                "work_dir": str(root_path / "work"),
                "media_dir": str(root_path / "media"),
            }
            with mock.patch("builtins.print"):
                target = MODULE.move_to_media(
                    finished, "FC2-PPV-4968748", config
                )
            self.assertEqual(
                target,
                root_path
                / "media"
                / "FC2"
                / "FC2-PPV-4968748"
                / "FC2-PPV-4968748.mp4",
            )
            self.assertEqual(target.read_bytes(), b"media")
            self.assertFalse(finished.exists())

    def test_load_config_reports_missing_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps({"work_dir": "/tmp/work"}), encoding="utf-8")
            with self.assertRaises(MODULE.AppError):
                MODULE.load_config(path)

    def test_legacy_config_receives_multisource_runtime_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "work_dir": "/tmp/work",
                        "download_dir": "/tmp/downloads",
                        "media_dir": "/tmp/media",
                        "browser_profile": "/tmp/profile",
                        "chromium": "/usr/bin/chromium",
                        "n_m3u8dl_re": "/usr/local/bin/N_m3u8DL-RE",
                        "site": "https://jable.tv",
                        "m3u8_preferred_domain": "mushroomtrack.com",
                    }
                ),
                encoding="utf-8",
            )
            config = MODULE.load_config(path)
            self.assertEqual(config["jable_site"], "https://jable.tv")
            self.assertEqual(config["missav_site"], "https://missav.ai")
            self.assertTrue(config["missav_hls_relay"])
            self.assertTrue(config["javbus_fallback_enabled"])
            self.assertEqual(config["javbus_site"], "https://www.javbus.com")
            self.assertEqual(Path(config["jav_media_dir"]), Path("/tmp/media") / "JAV")
            self.assertEqual(Path(config["fc2_media_dir"]), Path("/tmp/media") / "FC2")

    def test_duration_rounding(self):
        self.assertEqual(MODULE.duration_text(9180.245), "02:33:00")

    def test_summary_icons_do_not_use_variable_width_variation_selectors(self):
        self.assertNotIn("\ufe0f", "".join(MODULE.SUMMARY_ICONS.values()))
        self.assertEqual(MODULE.SUMMARY_ICONS["duration"], "🕒")

    def test_summary_fields_share_one_output_format(self):
        with mock.patch("builtins.print") as output:
            MODULE.print_summary_field("duration", "时长", "02:11:27")
        output.assert_called_once_with("🕒 时长：02:11:27")

    def test_downloader_keeps_verified_merge_flags_and_raid_cwd(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            work = root_path / "work"
            downloads = root_path / "downloads"
            work.mkdir()
            downloads.mkdir()
            output = downloads / "IPX-850.mp4"
            output.write_bytes(b"media")
            config = {
                "work_dir": str(work),
                "download_dir": str(downloads),
                "n_m3u8dl_re": "/usr/local/bin/N_m3u8DL-RE",
                "n_m3u8dl_extra_args": [],
            }
            completed = types.SimpleNamespace(returncode=0)
            with (
                mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run,
                mock.patch("builtins.print"),
            ):
                found = MODULE.run_downloader(
                    "IPX-850", "https://cdn/test.m3u8", config
                )
            self.assertEqual(found, output)
            command = run.call_args.args[0]
            self.assertIn("--use-ffmpeg-concat-demuxer", command)
            self.assertEqual(command[-2:], ["--del-after-done", "true"])
            self.assertEqual(run.call_args.kwargs["cwd"], work)

    def test_missav_download_uses_loopback_relay_and_stops_it(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            work = root_path / "work"
            downloads = root_path / "downloads"
            work.mkdir()
            downloads.mkdir()
            output = downloads / "FC2-PPV-4968748.mp4"
            output.write_bytes(b"media")
            config = {
                "work_dir": str(work),
                "download_dir": str(downloads),
                "n_m3u8dl_re": "/usr/local/bin/N_m3u8DL-RE",
                "n_m3u8dl_extra_args": [],
                "missav_hls_relay": True,
            }
            stream = MODULE.CapturedStream(
                "https://surrit.example/playlist.m3u8",
                "missav",
                "https://missav.ai/en/fc2-ppv-4968748",
                "Browser UA",
                {"session": "cookie"},
            )
            relay = mock.Mock()
            relay.start.return_value = "http://127.0.0.1:32100/resource/1"
            completed = types.SimpleNamespace(returncode=0)
            with (
                mock.patch.object(MODULE, "HLSRelay", return_value=relay),
                mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run,
                mock.patch("builtins.print"),
            ):
                found = MODULE.run_downloader(
                    "FC2-PPV-4968748", stream, config
                )
            self.assertEqual(found, output)
            self.assertEqual(run.call_args.args[0][1], "http://127.0.0.1:32100/resource/1")
            relay.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
