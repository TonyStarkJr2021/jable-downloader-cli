import io
import unittest

from jable_web.javbus import parse_magnets_html
from jable_web.tasks import DownloadTaskManager


MAGNET_A = "A" * 40
MAGNET_B = "B" * 40
MAGNET_C = "C" * 40


class JavBusParserTests(unittest.TestCase):
    def test_recommendation_prefers_quality_then_newest_share_date(self):
        html = f"""
        <table>
          <tr>
            <td><a href="magnet:?xt=urn:btih:{MAGNET_A}&dn=SQTE-713">SQTE-713</a><a>高清</a></td>
            <td><a>3.84GB</a></td><td><a>2026-08-28</a></td>
          </tr>
          <tr>
            <td><a href="magnet:?xt=urn:btih:{MAGNET_B}&dn=SQTE-713">SQTE-713</a><a>高清</a></td>
            <td><a>3.87GB</a></td><td><a>2026-08-29</a></td>
          </tr>
          <tr>
            <td><a href="magnet:?xt=urn:btih:{MAGNET_C}&dn=SQTE-713-C">SQTE-713-C</a><a>高清</a><a>字幕</a></td>
            <td><a>2.20GB</a></td><td><a>2026-08-27</a></td>
          </tr>
        </table>
        """
        results = parse_magnets_html(html, "SQTE-713")
        self.assertEqual(
            [item["info_hash"] for item in results],
            [MAGNET_C, MAGNET_B, MAGNET_A],
        )
        self.assertTrue(results[0]["has_subtitle"])
        self.assertEqual(results[1]["share_date"], "2026-08-29")
        self.assertEqual(results[1]["size_bytes"], int(3.87 * 1024**3))

    def test_unrelated_or_invalid_magnets_are_ignored(self):
        html = f"""
        <table><tr>
          <td><a href="magnet:?xt=urn:btih:{MAGNET_A}">OTHER-100</a></td>
          <td>1GB</td><td>2026-08-29</td>
        </tr></table>
        """
        self.assertEqual(parse_magnets_html(html, "SQTE-713"), [])


class FakeProcess:
    def __init__(self, output: str, return_code: int):
        self.stdout = io.BytesIO(output.encode("utf-8"))
        self.return_code = return_code

    def wait(self):
        return self.return_code


class JavBusFallbackTaskTests(unittest.TestCase):
    def test_not_found_exit_code_exposes_ranked_alternatives(self):
        recommended = {
            "title": "SQTE-713",
            "magnet": f"magnet:?xt=urn:btih:{MAGNET_B}",
            "info_hash": MAGNET_B,
            "size": "3.87GB",
            "size_bytes": int(3.87 * 1024**3),
            "share_date": "2026-08-29",
            "is_hd": True,
            "has_subtitle": False,
        }
        manager = DownloadTaskManager(magnet_lookup=lambda code: [recommended])
        manager._state.update(
            {"state": "running", "code": "SQTE-713", "source": "auto"}
        )
        manager._collect(FakeProcess("both providers not found", 3), None)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["state"], "alternatives")
        self.assertEqual(snapshot["magnets"][0]["share_date"], "2026-08-29")

    def test_download_error_does_not_query_javbus(self):
        calls = []
        manager = DownloadTaskManager(
            magnet_lookup=lambda code: calls.append(code) or []
        )
        manager._state.update(
            {"state": "running", "code": "SQTE-713", "source": "auto"}
        )
        manager._collect(FakeProcess("disk error", 6), None)
        self.assertEqual(manager.snapshot()["state"], "failed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
