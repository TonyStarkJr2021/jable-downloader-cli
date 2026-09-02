import json
import tempfile
import unittest
from pathlib import Path

import supjav_adblock as MODULE


RULES_PATH = Path(__file__).parents[1] / "rules" / "supjav-adblock.json"


class SupJavAdblockTests(unittest.TestCase):
    def setUp(self):
        self.rules = MODULE.load_supjav_adblock_rules(RULES_PATH)

    def decision(
        self,
        url,
        resource_type="script",
        *,
        navigation=False,
        main=False,
        popup=False,
    ):
        return MODULE.should_block_supjav_request(
            url,
            resource_type,
            is_navigation=navigation,
            is_main_page_navigation=main,
            is_popup_navigation=popup,
            rules=self.rules,
        )

    def test_release_rules_are_valid_and_project_maintained(self):
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(self.rules.schema_version, 1)
        self.assertTrue(self.rules.revision)
        self.assertIn("supjav.com", self.rules.allowed_page_hosts)
        self.assertNotIn("subscription_url", data)
        self.assertNotIn("http://", RULES_PATH.read_text(encoding="utf-8"))

    def test_popup_and_external_main_navigation_are_blocked_without_domain_rule(self):
        self.assertEqual(
            self.decision(
                "https://random-new-ad-domain.example/landing",
                "document",
                navigation=True,
                popup=True,
            ),
            (True, "popup-navigation"),
        )
        self.assertEqual(
            self.decision(
                "https://another-random-domain.example/",
                "document",
                navigation=True,
                main=True,
            ),
            (True, "external-main-navigation"),
        )

    def test_supjav_main_navigation_and_external_player_frame_remain_allowed(self):
        self.assertEqual(
            self.decision(
                "https://www.supjav.com/123.html",
                "document",
                navigation=True,
                main=True,
            ),
            (False, ""),
        )
        self.assertEqual(
            self.decision(
                "https://player-cdn.example/embed/123",
                "document",
                navigation=True,
            ),
            (False, ""),
        )

    def test_confirmed_hosts_and_url_patterns_are_blocked(self):
        self.assertEqual(
            self.decision("https://cdn.exoclick.com/tag.js"),
            (True, "blocked-host"),
        )
        self.assertEqual(
            self.decision("https://assets.example/player/popunder.js"),
            (True, "blocked-url"),
        )

    def test_media_and_player_data_override_network_rules(self):
        for url, resource_type in (
            ("https://cdn.exoclick.com/master.m3u8", "other"),
            ("https://cdn.exoclick.com/segment.ts", "other"),
            ("https://cdn.exoclick.com/opaque-key", "fetch"),
            ("https://cdn.exoclick.com/video", "media"),
        ):
            with self.subTest(url=url, resource_type=resource_type):
                self.assertEqual(
                    self.decision(url, resource_type),
                    (False, ""),
                )

    def test_invalid_or_oversized_rules_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "rules.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "revision": "test",
                        "allowed_page_hosts": ["https://not-a-host.example"],
                        "blocked_hosts": [],
                        "blocked_url_contains": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "无效域名"):
                MODULE.load_supjav_adblock_rules(path)

    def test_init_script_suppresses_window_open_without_cancelling_clicks(self):
        self.assertIn('Object.defineProperty(window, "open"', MODULE.SUPJAV_ADBLOCK_INIT_SCRIPT)
        self.assertNotIn("preventDefault", MODULE.SUPJAV_ADBLOCK_INIT_SCRIPT)


if __name__ == "__main__":
    unittest.main()
