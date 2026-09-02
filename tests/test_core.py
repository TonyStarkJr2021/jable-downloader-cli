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
        self.assertEqual((fc2.code, fc2.source), ("FC2-PPV-4968748", "fc2"))

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

    def test_proxy_url_validation_masking_and_playwright_conversion(self):
        value = "https://user:p%40ss@proxy.example:8443"
        self.assertEqual(MODULE.normalize_proxy_url(value), value)
        self.assertEqual(
            MODULE.proxy_display_label(value), "https://proxy.example:8443"
        )
        self.assertEqual(
            MODULE.playwright_proxy(value),
            {
                "server": "https://proxy.example:8443",
                "username": "user",
                "password": "p@ss",
            },
        )

    def test_proxy_url_rejects_unsafe_or_incomplete_values(self):
        for value in (
            "ftp://proxy.example:21",
            "http://proxy.example",
            "http://proxy.example:8080/path",
            "http://proxy.example:8080?token=value",
            "http://proxy.example:99999",
            "http://proxy example:8080",
            "socks5://user:secret@proxy.example:1080",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.normalize_proxy_url(value)

    def test_ordinary_code_probes_all_three_providers_and_ranks_quality(self):
        request = MODULE.parse_download_input("IPX-850")
        missav = MODULE.CapturedStream(
            "https://surrit.example/playlist.m3u8",
            "missav",
            "https://missav.ai/en/ipx-850",
            "Browser UA",
            {},
            height=720,
        )
        supjav = MODULE.CapturedStream(
            "https://cdn.example/playlist.m3u8",
            "supjav",
            "https://supjav.com/1.html",
            "Browser UA",
            {},
            height=1080,
        )

        def capture(_request, source, _config):
            if source == "jable":
                raise MODULE.AppError("JABLE 没找到 IPX-850", 3)
            return [missav if source == "missav" else supjav]

        with (
            mock.patch.object(
                MODULE, "capture_candidates_from_provider", side_effect=capture
            ) as provider,
            mock.patch("builtins.print"),
        ):
            result = MODULE.capture_streams(request, {})
        self.assertEqual(result, [supjav, missav])
        self.assertEqual(
            {call.args[1] for call in provider.call_args_list},
            {"jable", "missav", "supjav"},
        )

    def test_all_not_found_preserves_distinct_exit_code_for_web_fallback(self):
        request = MODULE.parse_download_input("IPX-850")

        def unavailable(_request, source, _config):
            raise MODULE.AppError(f"{source.upper()} 没找到 IPX-850", 3)

        with (
            mock.patch.object(
                MODULE, "capture_candidates_from_provider", side_effect=unavailable
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(MODULE.AppError) as raised:
                MODULE.capture_stream(request, {})
        self.assertEqual(raised.exception.exit_code, 3)

    def test_supjav_search_results_require_exact_code(self):
        html = """
        <div class="post"><a href="/100.html" title="IPX-850 full"></a></div>
        <div class="post"><a href="/101.html" title="IPX-8500 other"></a></div>
        """
        self.assertEqual(
            MODULE.find_supjav_detail_urls(html, "IPX-850", "https://supjav.com/"),
            ["https://supjav.com/100.html"],
        )

    def test_supjav_uses_site_specific_fc2_search_spelling(self):
        self.assertEqual(
            MODULE.supjav_search_terms("FC2-PPV-4968930"),
            ["FC2PPV 4968930", "FC2-PPV-4968930"],
        )
        self.assertEqual(MODULE.supjav_search_terms("IPX-850"), ["IPX-850"])

    def test_supjav_prefers_pretty_search_route_and_keeps_query_fallback(self):
        self.assertEqual(
            MODULE.supjav_search_urls(
                "https://supjav.com", "", "FC2-PPV-4968930"
            ),
            [
                "https://supjav.com/search/FC2PPV%204968930/",
                "https://supjav.com/?s=FC2PPV%204968930",
                "https://supjav.com/search/FC2-PPV-4968930/",
                "https://supjav.com/?s=FC2-PPV-4968930",
            ],
        )

    def test_hls_quality_prefers_resolution_then_bandwidth(self):
        playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080
1080/video.m3u8
"""
        self.assertEqual(
            MODULE.hls_playlist_quality(playlist, "https://cdn.example/master.m3u8"),
            (1920, 1080, 4500000, "https://cdn.example/1080/video.m3u8"),
        )

    def test_empty_hls_shell_is_not_usable(self):
        self.assertFalse(MODULE.hls_playlist_usable("#EXTM3U\n#EXT-X-VERSION:6\n"))
        self.assertTrue(MODULE.hls_playlist_usable("#EXTM3U\n#EXTINF:4.0,\na.ts\n"))

    def test_supjav_disguised_master_playlist_is_recognized(self):
        disguised = "https://cdn.example/path/.urlset/master.txt?token=value"
        self.assertTrue(MODULE.is_hls_candidate_url(disguised))
        self.assertEqual(MODULE.extract_m3u8_urls(f'file: "{disguised}"'), [disguised])
        self.assertFalse(MODULE.is_hls_candidate_url("https://cdn.example/notes.txt"))

    def test_quality_probe_accepts_disguised_supjav_master(self):
        master_url = "https://cdn.example/path/.urlset/master.txt"
        variant_url = "https://cdn.example/path/1080/video.m3u8"
        master = types.SimpleNamespace(
            status_code=200,
            url=master_url,
            text=(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2684340,RESOLUTION=1920x1080\n"
                "1080/video.m3u8\n"
            ),
        )
        variant = types.SimpleNamespace(
            status_code=200,
            url=variant_url,
            text="#EXTM3U\n#EXTINF:10.0,\nsegment.ts\n",
        )
        requests = types.SimpleNamespace(get=mock.Mock(side_effect=[master, variant]))
        stream = MODULE.CapturedStream(master_url, "supjav", "https://player.example/", "UA", {})
        with mock.patch.object(MODULE, "browser_requests", requests):
            inspected = MODULE.inspect_stream_quality(
                stream, {"stream_probe_timeout_seconds": 12}
            )
        self.assertEqual(
            (inspected.width, inspected.height, inspected.bandwidth, inspected.verified),
            (1920, 1080, 2684340, True),
        )

    def test_supjav_browser_capture_skips_short_ad_and_ranks_complete_streams(self):
        short = MODULE.CapturedStream(
            "https://cdn.example/ad.m3u8", "supjav", "", "UA", {}
        )
        complete_720 = MODULE.CapturedStream(
            "https://cdn.example/720.m3u8", "supjav", "", "UA", {}
        )
        complete_1080 = MODULE.CapturedStream(
            "https://cdn.example/1080.m3u8", "supjav", "", "UA", {}
        )
        inspected = {
            short.url: MODULE.replace(
                short, verified=True, duration=6, height=1080, bandwidth=5000000
            ),
            complete_720.url: MODULE.replace(
                complete_720,
                verified=True,
                duration=7200,
                height=720,
                bandwidth=1800000,
            ),
            complete_1080.url: MODULE.replace(
                complete_1080,
                verified=True,
                duration=7200,
                height=1080,
                bandwidth=4200000,
            ),
        }
        with (
            mock.patch.object(
                MODULE,
                "inspect_stream_quality",
                side_effect=lambda stream, config: inspected[stream.url],
            ),
            mock.patch("builtins.print"),
        ):
            selected = MODULE.choose_complete_supjav_browser_stream(
                [short, complete_720, complete_1080],
                {"supjav_min_duration_seconds": 600},
            )
        self.assertEqual(selected.url, complete_1080.url)

    def test_supjav_browser_capture_reports_when_only_short_ads_exist(self):
        short = MODULE.CapturedStream(
            "https://cdn.example/ad.m3u8", "supjav", "", "UA", {}
        )
        with (
            mock.patch.object(
                MODULE,
                "inspect_stream_quality",
                return_value=MODULE.replace(
                    short, verified=True, duration=6, height=720
                ),
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaisesRegex(MODULE.AppError, "6 秒"):
                MODULE.choose_complete_supjav_browser_stream(
                    [short], {"supjav_min_duration_seconds": 600}
                )

    def test_supjav_server_rotation_keeps_each_player_loaded_for_retries(self):
        self.assertEqual(
            [MODULE.supjav_server_index(i, 3, 10) for i in range(10)],
            [0, 0, 0, 0, 1, 1, 1, 1, 2, 2],
        )
        self.assertEqual(
            [MODULE.supjav_server_index(i, 1, 10) for i in range(10)],
            [0] * 10,
        )

    def test_player_activation_reaches_cross_origin_iframe(self):
        page = mock.Mock()
        main_frame = object()
        child_frame = mock.Mock()
        page.main_frame = main_frame
        page.frames = [main_frame, child_frame]

        absent = mock.Mock()
        absent.count.return_value = 0
        main_locator = mock.Mock()
        main_locator.first = absent
        page.locator.return_value = main_locator

        target = mock.Mock()
        target.count.return_value = 1
        target.is_visible.return_value = True

        def child_locator(selector):
            locator = mock.Mock()
            locator.first = target if selector == ".vjs-big-play-button" else absent
            return locator

        child_frame.locator.side_effect = child_locator

        self.assertTrue(MODULE.activate_player(page))
        target.click.assert_called_once_with(force=True, timeout=1500)

    def test_quality_probe_forwards_only_stream_scoped_proxy(self):
        response = types.SimpleNamespace(
            status_code=200,
            url="https://cdn.example/video.m3u8",
            text="#EXTM3U\n#EXTINF:10.0,\nsegment.ts\n",
        )
        requests = types.SimpleNamespace(get=mock.Mock(return_value=response))
        stream = MODULE.CapturedStream(
            "https://cdn.example/video.m3u8",
            "supjav",
            "https://supjav.com/1.html",
            "UA",
            {},
            proxy_url="http://proxy.example:8080",
        )
        with mock.patch.object(MODULE, "browser_requests", requests):
            MODULE.inspect_stream_quality(stream, {})
        self.assertEqual(
            requests.get.call_args.kwargs["proxy"], "http://proxy.example:8080"
        )

    def test_same_resolution_prefers_higher_missav_bitrate(self):
        missav = MODULE.CapturedStream(
            "https://missav.example/master.m3u8",
            "missav",
            "",
            "",
            {},
            width=1920,
            height=1080,
            bandwidth=4179000,
        )
        supjav = MODULE.CapturedStream(
            "https://supjav.example/.urlset/master.txt",
            "supjav",
            "",
            "",
            {},
            width=1920,
            height=1080,
            bandwidth=2684340,
        )
        self.assertGreater(
            MODULE.stream_quality_key(missav), MODULE.stream_quality_key(supjav)
        )


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


class SupJavProxyCaptureTests(unittest.TestCase):
    def test_static_search_session_uses_only_supjav_proxy(self):
        response = types.SimpleNamespace(
            status_code=200,
            url="https://supjav.com/search/IPX-850/",
            text="<html></html>",
        )
        session = mock.Mock()
        session.get.return_value = response
        requests = types.SimpleNamespace(Session=mock.Mock(return_value=session))
        config = {
            "supjav_site": "https://supjav.com",
            "supjav_language": "",
            "supjav_proxy_url": "http://user:secret@proxy.example:8080",
            "page_timeout_ms": 30000,
        }
        with (
            mock.patch.object(MODULE, "browser_requests", requests),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(MODULE.AppError):
                MODULE.capture_supjav_static(
                    MODULE.parse_download_input("IPX-850"), config
                )
        self.assertEqual(
            requests.Session.call_args.kwargs["proxy"],
            "http://user:secret@proxy.example:8080",
        )

    def test_browser_proxy_launch_error_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            launch = mock.Mock(
                side_effect=RuntimeError(
                    "failed http://user:secret@proxy.example:8080"
                )
            )
            playwright = types.SimpleNamespace(
                chromium=types.SimpleNamespace(launch_persistent_context=launch)
            )
            manager = mock.MagicMock()
            manager.__enter__.return_value = playwright
            config = {
                "supjav_site": "https://supjav.com",
                "supjav_language": "",
                "supjav_proxy_url": "http://user:secret@proxy.example:8080",
                "browser_profile": root,
                "browser_headless": True,
                "chromium": "/usr/bin/chromium",
            }
            with mock.patch.object(MODULE, "sync_playwright", return_value=manager):
                with self.assertRaises(MODULE.AppError) as raised:
                    MODULE.capture_from_provider(
                        MODULE.parse_download_input("IPX-850"), "supjav", config
                    )
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(str(raised.exception), "SUPJAV Chromium 无法连接专用代理")


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
            self.assertEqual(config["supjav_site"], "https://supjav.com")
            self.assertTrue(config["supjav_hls_relay"])
            self.assertEqual(config["supjav_proxy_url"], "")
            self.assertFalse(config["supjav_proxy_download"])
            self.assertTrue(config["supjav_adblock_enabled"])
            self.assertEqual(config["supjav_play_attempts"], 10)
            self.assertEqual(config["provider_probe_workers"], 3)
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
                mock.patch.object(
                    MODULE, "HLSRelay", return_value=relay
                ) as relay_class,
                mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run,
                mock.patch("builtins.print"),
            ):
                found = MODULE.run_downloader(
                    "FC2-PPV-4968748", stream, config
                )
            self.assertEqual(found, output)
            self.assertEqual(run.call_args.args[0][1], "http://127.0.0.1:32100/resource/1")
            self.assertEqual(
                relay_class.call_args.kwargs["proxy_url"], stream.proxy_url
            )
            relay.stop.assert_called_once()

    def test_ranked_download_falls_back_without_reusing_partial_name(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            work = root_path / "work"
            downloads = root_path / "downloads"
            work.mkdir()
            downloads.mkdir()
            config = {
                "work_dir": str(work),
                "download_dir": str(downloads),
                "n_m3u8dl_re": "/usr/local/bin/N_m3u8DL-RE",
                "n_m3u8dl_extra_args": [],
                "supjav_hls_relay": False,
                "missav_hls_relay": False,
            }
            streams = [
                MODULE.CapturedStream("https://one/test.m3u8", "supjav", "", "", {}),
                MODULE.CapturedStream("https://two/test.m3u8", "missav", "", "", {}),
            ]

            def execute(command, **_kwargs):
                save_name = command[command.index("--save-name") + 1]
                if "__supjav_" in save_name:
                    return types.SimpleNamespace(returncode=1)
                (downloads / f"{save_name}.mp4").write_bytes(b"media")
                return types.SimpleNamespace(returncode=0)

            with (
                mock.patch.object(MODULE.subprocess, "run", side_effect=execute) as run,
                mock.patch("builtins.print"),
            ):
                result = MODULE.download_from_candidates("IPX-850", streams, config)
            self.assertEqual(result.name, "IPX-850.mp4")
            names = [
                call.args[0][call.args[0].index("--save-name") + 1]
                for call in run.call_args_list
            ]
            self.assertEqual(names, ["IPX-850__supjav_1", "IPX-850__missav_2"])


if __name__ == "__main__":
    unittest.main()
