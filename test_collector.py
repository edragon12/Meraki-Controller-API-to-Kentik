import io
import os
import tempfile
import unittest
from urllib.error import HTTPError

from collector import (
    CollectorError,
    build_metrics,
    escape_tag,
    field_value,
    fetch_meraki,
    format_last_connected,
    http_json_page,
    line,
    load_config,
    load_dotenv_file,
    mock_payloads,
    parse_last_seen,
    send_to_kentik,
)


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self.payload = payload
        self.headers = headers or {}
        self.status = status

    def read(self):
        import json
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class CollectorTests(unittest.TestCase):
    def test_mock_metrics_include_dashboard_measurements(self):
        metrics = build_metrics(mock_payloads(), "org-123", 1700000000000000000)
        self.assertEqual(len(metrics), 7)
        self.assertTrue(any(x.startswith("meraki_device_status,") for x in metrics))
        self.assertTrue(any(x.startswith("meraki_uplink_quality,") for x in metrics))
        self.assertTrue(any(x.startswith("meraki_uplink_status,") for x in metrics))
        self.assertTrue(any(x.startswith("meraki_wireless_connection,") for x in metrics))
        self.assertTrue(any(x.startswith("meraki_wireless_client,") for x in metrics))
        self.assertTrue(any(x.startswith("meraki_wireless_access_point,") for x in metrics))
        ap = next(x for x in metrics if x.startswith("meraki_wireless_access_point,"))
        self.assertIn("ap_ip=10.0.0.2", ap)
        self.assertIn("ap_mac=aa:bb:cc:dd:ee:02", ap)
        self.assertIn("bssid=aa:bb:cc:dd:ee:03", ap)
        self.assertIn("ssid=Corp-WiFi", ap)
        self.assertIn("radio_type=5", ap)
        self.assertIn("channel=36i", ap)
        self.assertIn("current_endpoint_count=7i", ap)
        self.assertIn("bandwidth_utilization_kbps=2450.0", ap)
        self.assertIn("channel_utilization_percent=42.5", ap)
        wireless = next(x for x in metrics if x.startswith("meraki_wireless_client,"))
        self.assertIn("client_name=Lab\\ Laptop", wireless)
        self.assertIn("connected=true", wireless)
        self.assertIn("data_rate_kbps=12000.0", wireless)
        self.assertIn("data_rate_bps=12000000.0", wireless)
        self.assertIn("throughput_kbps=12000.0", wireless)
        self.assertIn("signal_strength_dbm=-58.0", wireless)
        self.assertIn("connected_at=\"2026-10-14T12:00:00Z\"", wireless)
        self.assertIn("last_connected=10/14/2026\\ 12:00\\ PM\\ UTC", wireless)
        self.assertNotIn("last_connected=\"", wireless)
        self.assertIn("transmitted_mb=120.5625", wireless)
        self.assertIn("loss_percent=0.5", "\n".join(metrics))
        self.assertIn("latency_ms=23.4", "\n".join(metrics))

    def test_last_connected_matches_dashboard_style_utc_format(self):
        self.assertEqual(format_last_connected(1791979200), "10/14/2026 12:00:00 PM UTC")
        self.assertEqual(format_last_connected(1791992312), "10/14/2026 3:38:32 PM UTC")
        self.assertEqual(format_last_connected(1791992312, 3600), "10/14/2026 3:00 PM UTC")
        self.assertEqual(format_last_connected(1791992312, 86400), "10/14/2026 12:00 AM UTC")

    def test_parse_last_seen_accepts_epoch_string_and_iso(self):
        self.assertEqual(parse_last_seen(1791979200), 1791979200.0)
        self.assertEqual(parse_last_seen("1791979200"), 1791979200.0)
        self.assertEqual(parse_last_seen(1791979200000), 1791979200.0)
        self.assertEqual(parse_last_seen("2026-10-14T12:00:00Z"), 1791979200.0)
        self.assertIsNone(parse_last_seen(None))
        self.assertIsNone(parse_last_seen("not a date"))

    def test_missing_last_seen_is_unknown(self):
        payloads = mock_payloads()
        for clients in payloads["wireless_clients"].values():
            for client in clients:
                client.pop("lastSeen", None)
        metrics = build_metrics(payloads, "org-123", 1700000000000000000)
        wireless = next(x for x in metrics if x.startswith("meraki_wireless_client,"))
        self.assertIn("last_connected=unknown", wireless)

    def test_escaping(self):
        self.assertEqual(escape_tag("a b,c=d"), "a\\ b\\,c\\=d")
        self.assertEqual(field_value('a"b'), '"a\\"b"')

    def test_integer_and_float_fields(self):
        result = line("m", {"host": "x"}, {"count": 2, "ratio": 1.5})
        self.assertIn("count=2i", result)
        self.assertIn("ratio=1.5", result)

    def test_client_timespan_defaults_to_one_day_and_is_configurable(self):
        default_config = load_config({})
        self.assertEqual(default_config.client_timespan_seconds, 86400)
        custom_config = load_config({"MERAKI_CLIENT_TIMESPAN": "3600"})
        self.assertEqual(custom_config.client_timespan_seconds, 3600)

    def test_meraki_fetch_follows_link_header_pagination(self):
        config = load_config({
            "MERAKI_API_KEY": "key",
            "MERAKI_ORG_ID": "org",
            "HTTP_MAX_RETRIES": "0",
        })
        pages = [
            FakeResponse([{"id": "one"}], {"Link": '<https://api.meraki.com/api/v1/next?page=2>; rel="next"'}),
            FakeResponse([{"id": "two"}]),
        ]
        requested_urls = []

        def opener(request, timeout):
            requested_urls.append(request.full_url)
            return pages.pop(0)

        result = fetch_meraki(config, "/organizations/org/devices", opener=opener)
        self.assertEqual(result, [{"id": "one"}, {"id": "two"}])
        self.assertEqual(len(requested_urls), 2)
        self.assertIn("page=2", requested_urls[1])

    def test_http_json_retries_rate_limit_and_honors_retry_after(self):
        attempts = []
        sleeps = []
        failure = HTTPError(
            "https://api.meraki.com/test",
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            io.BytesIO(b"rate limited"),
        )

        def opener(request, timeout):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise failure
            return FakeResponse({"ok": True})

        payload, _ = http_json_page(
            "https://api.meraki.com/test",
            {},
            5,
            opener=opener,
            sleeper=sleeps.append,
            max_retries=2,
            backoff_seconds=0.0,
        )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.0])

    def test_kentik_ingest_retries_transient_status(self):
        config = load_config({
            "KENTIK_INGEST_URL": "https://kentik.example/write",
            "KENTIK_EMAIL": "user@example.com",
            "KENTIK_API_TOKEN": "token",
            "HTTP_MAX_RETRIES": "1",
            "HTTP_RETRY_BACKOFF_SECONDS": "0",
        })
        responses = [
            FakeResponse({}, {"Retry-After": "0"}, status=503),
            FakeResponse({}, status=204),
        ]
        sleeps = []

        def post(request, timeout):
            return responses.pop(0)

        send_to_kentik(config, "measurement value=1i\n", post=post, sleeper=sleeps.append)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps, [0.0])

    def test_meraki_fetch_stops_at_configured_page_limit(self):
        config = load_config({
            "MERAKI_API_KEY": "key",
            "MERAKI_ORG_ID": "org",
            "MERAKI_MAX_PAGES": "2",
            "HTTP_MAX_RETRIES": "0",
        })
        pages = [
            FakeResponse([{"id": "one"}], {"Link": '<https://api.meraki.com/page=2>; rel="next"'}),
            FakeResponse([{"id": "two"}], {"Link": '<https://api.meraki.com/page=3>; rel="next"'}),
        ]

        def opener(request, timeout):
            return pages.pop(0)

        with self.assertRaises(CollectorError):
            fetch_meraki(config, "/organizations/org/devices", opener=opener)

    def test_dotenv_loader_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = os.path.join(directory, ".env")
            with open(dotenv, "w", encoding="utf-8") as handle:
                handle.write("TEST_MERAKI_KEY=example\n")
            old = os.environ.pop("TEST_MERAKI_KEY", None)
            try:
                load_dotenv_file(dotenv)
                self.assertEqual(os.environ["TEST_MERAKI_KEY"], "example")
            finally:
                os.environ.pop("TEST_MERAKI_KEY", None)
                if old is not None:
                    os.environ["TEST_MERAKI_KEY"] = old


if __name__ == "__main__":
    unittest.main()
