import unittest
import urllib.error
import urllib.request
import types
from unittest import mock

import hls_proxy
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

    def test_loopback_error_does_not_expose_upstream_proxy_credentials(self):
        relay = HLSRelay(
            "https://supjav.com/1.html",
            "Browser UA",
            proxy_url="http://user:secret@proxy.example:8080",
        )
        with (
            mock.patch.object(relay, "available", return_value=True),
            mock.patch.object(
                relay,
                "_fetch",
                side_effect=RuntimeError("http://user:secret@proxy.example:8080"),
            ),
        ):
            url = relay.start("https://cdn.example/segment.ts")
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(url, timeout=3)
                body = raised.exception.read().decode("utf-8", errors="ignore")
                self.assertNotIn("secret", body)
                self.assertIn("上游 HLS 请求失败", body)
            finally:
                relay.stop()

    def test_supjav_fake_segment_header_is_removed_only_on_ts_sync(self):
        packets = b"".join(b"\x47" + bytes([index]) * 187 for index in range(5))
        response = types.SimpleNamespace(
            status_code=200,
            content=b"fake-image-header" + packets,
            text="",
            headers={"Content-Type": "image/png"},
        )
        requests = types.SimpleNamespace(get=mock.Mock(return_value=response))
        relay = HLSRelay(
            "https://lk1.supremejav.com/player",
            "Browser UA",
            strip_fake_ts_header=True,
        )
        with mock.patch.object(hls_proxy, "browser_requests", requests):
            fetched = relay._fetch("https://cdn.example/segment.png")
        self.assertEqual(fetched.body, packets)
        self.assertEqual(fetched.body[0], 0x47)

    def test_upstream_request_uses_configured_proxy(self):
        response = types.SimpleNamespace(
            status_code=200,
            content=b"segment",
            text="segment",
            headers={"Content-Type": "video/mp2t"},
        )
        requests = types.SimpleNamespace(get=mock.Mock(return_value=response))
        relay = HLSRelay(
            "https://supjav.com/1.html",
            "UA",
            proxy_url="http://user:secret@proxy.example:8080",
        )
        with mock.patch.object(hls_proxy, "browser_requests", requests):
            relay._fetch("https://cdn.example/segment.ts")
        self.assertEqual(
            requests.get.call_args.kwargs["proxy"],
            "http://user:secret@proxy.example:8080",
        )


if __name__ == "__main__":
    unittest.main()
