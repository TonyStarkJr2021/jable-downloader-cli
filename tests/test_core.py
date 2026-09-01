import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


# Unit tests exercise the pure/local behavior without starting a browser.
bs4 = types.ModuleType("bs4")
bs4.BeautifulSoup = object
sys.modules.setdefault("bs4", bs4)
playwright = types.ModuleType("playwright")
playwright_sync = types.ModuleType("playwright.sync_api")
playwright_sync.TimeoutError = TimeoutError
playwright_sync.sync_playwright = lambda: None
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", playwright_sync)

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

    def test_rejects_unsafe_or_ambiguous_input(self):
        for value in ("", "850", "IPX", "IPX-850;rm", "IPX/850"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.normalize_code(value)


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

    def test_load_config_reports_missing_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps({"work_dir": "/tmp/work"}), encoding="utf-8")
            with self.assertRaises(MODULE.AppError):
                MODULE.load_config(path)

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


if __name__ == "__main__":
    unittest.main()
