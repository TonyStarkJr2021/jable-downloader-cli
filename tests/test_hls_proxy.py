import unittest
import urllib.error
import urllib.request
from unittest import mock

from hls_proxy import HLSRelay, RelayResponse


class HLSRelayTests(unittest.TestCase):
    def test_playlist_rewrites_variants_segments_keys_and_maps(self):
        relay = HLSRelay("https://missav.ai/en/example", "Browser UA")
        relay.base_url = "http://127.0.0.1:32000"
        source = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXT-X-MAP:URI="init.mp4"\n'
            "720p/playlist.m3u8\n"
            "segment-1.ts?token=test\n"
        )
        rewritten = relay.rewrite_playlist(
            source, "https://surrit.example/path/master.m3u8"
        )
        self.assertNotIn("surrit.example", rewritten)
        self.assertNotIn("key.bin", rewritten)
        self.assertNotIn("segment-1.ts", rewritten)
        self.assertEqual(rewritten.count("http://127.0.0.1:32000/resource/"), 4)
        self.assertIn("#EXT-X-KEY:METHOD=AES-128,URI=", rewritten)

    def test_only_http_upstreams_can_be_registered(self):
        relay = HLSRelay("https://missav.ai/en/example", "Browser UA")
        relay.base_url = "http://127.0.0.1:32000"
        with self.assertRaises(ValueError):
            relay._register("file:///etc/passwd")

    def test_loopback_server_exposes_only_registered_resources(self):
        relay = HLSRelay("https://missav.ai/en/example", "Browser UA")
        response = RelayResponse(b"segment", "video/mp2t")
        with (
            mock.patch.object(relay, "available", return_value=True),
            mock.patch.object(relay, "_fetch", return_value=response),
        ):
            url = relay.start("https://surrit.example/segment.ts")
            try:
                with urllib.request.urlopen(url, timeout=3) as fetched:
                    self.assertEqual(fetched.read(), b"segment")
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"{relay.base_url}/resource/999", timeout=3)
                self.assertEqual(missing.exception.code, 404)
            finally:
                relay.stop()


if __name__ == "__main__":
    unittest.main()
