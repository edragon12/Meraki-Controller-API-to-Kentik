#!/usr/bin/env python3
"""Meraki Dashboard API -> Kentik NMS Influx line protocol collector."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


class CollectorError(RuntimeError):
    pass


@dataclass
class Config:
    meraki_base_url: str = "https://api.meraki.com/api/v1"
    meraki_api_key: str = ""
    org_id: str = ""
    kentik_ingest_url: str = ""
    kentik_email: str = ""
    kentik_api_token: str = ""
    timeout_seconds: int = 20
    client_timespan_seconds: int = 86400
    max_wireless_clients: int = 100
    max_wireless_access_points: int = 100
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    max_pages: int = 100
    last_connected_round_seconds: int = 3600


def load_dotenv_file(path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs without overriding existing environment variables."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as dotenv:
        for raw_line in dotenv:
            line_text = raw_line.strip()
            if not line_text or line_text.startswith("#"):
                continue
            if line_text.startswith("export "):
                line_text = line_text[7:].lstrip()
            if "=" not in line_text:
                continue
            key, value = line_text.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_config(env: dict[str, str] | None = None) -> Config:
    if env is None:
        load_dotenv_file()
        env = os.environ
    return Config(
        meraki_base_url=env.get("MERAKI_BASE_URL", "https://api.meraki.com/api/v1").rstrip("/"),
        meraki_api_key=env.get("MERAKI_API_KEY", ""),
        org_id=env.get("MERAKI_ORG_ID", ""),
        kentik_ingest_url=env.get("KENTIK_INGEST_URL", ""),
        kentik_email=env.get("KENTIK_EMAIL", ""),
        kentik_api_token=env.get("KENTIK_API_TOKEN", ""),
        timeout_seconds=int(env.get("HTTP_TIMEOUT_SECONDS", "20")),
        client_timespan_seconds=int(env.get("MERAKI_CLIENT_TIMESPAN", "86400")),
        max_wireless_clients=int(env.get("MAX_WIRELESS_CLIENTS", "100")),
        max_wireless_access_points=int(env.get("MAX_WIRELESS_ACCESS_POINTS", "100")),
        max_retries=max(0, int(env.get("HTTP_MAX_RETRIES", "3"))),
        retry_backoff_seconds=max(0.0, float(env.get("HTTP_RETRY_BACKOFF_SECONDS", "1.0"))),
        max_pages=max(1, int(env.get("MERAKI_MAX_PAGES", "100"))),
        last_connected_round_seconds=max(0, int(env.get("LAST_CONNECTED_ROUND_SECONDS", "3600"))),
    )


def escape_tag(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def escape_field_key(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value}i"
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"Unsupported field value: {value!r}")


def line(measurement: str, tags: dict[str, Any], fields: dict[str, Any], timestamp_ns: int | None = None) -> str:
    if not fields:
        raise ValueError("At least one field is required")
    tag_text = ",".join(f"{escape_tag(k)}={escape_tag(v)}" for k, v in sorted(tags.items()))
    field_text = ",".join(f"{escape_field_key(k)}={field_value(v)}" for k, v in sorted(fields.items()))
    prefix = escape_tag(measurement) + ("," + tag_text if tag_text else "")
    return f"{prefix} {field_text}" + (f" {timestamp_ns}" if timestamp_ns is not None else "")


def iso_to_ns(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_last_seen(value: Any) -> float | None:
    """Return Unix seconds from Meraki lastSeen (epoch number, numeric string, or ISO-8601)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    if seconds > 1e12:  # milliseconds
        seconds /= 1000.0
    return seconds if seconds > 0 else None


def format_last_connected(epoch_seconds: int | float, round_seconds: int = 0) -> str:
    """Format Meraki lastSeen as a readable UTC date/time.

    round_seconds floors the value (for example 3600 = hour) so the dimension
    changes less often; values of 60 or more omit seconds from the display.
    """
    epoch = float(epoch_seconds)
    if round_seconds > 0:
        epoch = epoch - (epoch % round_seconds)
    connected = datetime.fromtimestamp(epoch, tz=timezone.utc)
    hour = connected.hour % 12 or 12
    clock = f"{hour}:{connected:%M}" + (f":{connected:%S}" if round_seconds < 60 else "")
    return f"{connected:%m/%d/%Y} {clock} {connected:%p} UTC"


RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
NEXT_LINK_PATTERN = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?", re.IGNORECASE)


def retry_after_seconds(value: str | None) -> float:
    """Return a non-negative Retry-After delay for seconds or an HTTP date."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def retry_delay(attempt: int, backoff_seconds: float, headers: Any = None) -> float:
    """Use the server's Retry-After value when present, otherwise exponential backoff."""
    retry_after = headers.get("Retry-After") if headers is not None else None
    return max(retry_after_seconds(retry_after), backoff_seconds * (2 ** attempt))


def http_json_page(
    url: str,
    headers: dict[str, str],
    timeout: int,
    query: dict[str, Any] | None = None,
    *,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Any, Any]:
    """GET one JSON page with bounded retries and return payload plus response headers."""
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query)
    opener = opener or urlopen
    sleeper = sleeper or time.sleep
    request = Request(url, headers=headers, method="GET")

    for attempt in range(max_retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8")), response.headers
        except HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < max_retries:
                sleeper(retry_delay(attempt, backoff_seconds, exc.headers))
                continue
            body = exc.read().decode("utf-8", errors="replace")
            raise CollectorError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
        except URLError as exc:
            if attempt < max_retries:
                sleeper(backoff_seconds * (2 ** attempt))
                continue
            raise CollectorError(f"Unable to reach {url}: {exc.reason}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise CollectorError(f"Invalid JSON response from {url}: {exc}") from exc

    raise CollectorError(f"Request retries exhausted for {url}")


def http_json(url: str, headers: dict[str, str], timeout: int, query: dict[str, Any] | None = None) -> Any:
    """Backward-compatible single-page JSON helper."""
    payload, _ = http_json_page(url, headers, timeout, query)
    return payload


def next_link(headers: Any) -> str | None:
    """Extract the RFC 5988/8288 next URL from a Meraki Link response header."""
    link_header = headers.get("Link", "") if headers is not None else ""
    for match in NEXT_LINK_PATTERN.finditer(link_header):
        return match.group(1)
    return None


def merge_pages(current: Any, page: Any) -> Any:
    """Merge list pages or item-list dictionaries while preserving response shape."""
    if isinstance(current, list) and isinstance(page, list):
        current.extend(page)
        return current
    if isinstance(current, dict) and isinstance(page, dict):
        if isinstance(current.get("items"), list) and isinstance(page.get("items"), list):
            current["items"].extend(page["items"])
            return current
    raise CollectorError("Meraki pagination returned incompatible response shapes")


def fetch_meraki(
    config: Config,
    path: str,
    query: dict[str, Any] | None = None,
    getter: Callable[..., Any] | None = None,
    *,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Any:
    if not config.meraki_api_key or not config.org_id:
        raise CollectorError("MERAKI_API_KEY and MERAKI_ORG_ID are required in real mode; create .env from .env.example")
    headers = {"X-Cisco-Meraki-API-Key": config.meraki_api_key, "Accept": "application/json"}
    base_url = f"{config.meraki_base_url}{path}"

    # Preserve a simple injectable getter for unit tests and callers that already
    # provide decoded payloads. Normal collection uses the paginated request path.
    if getter is not None:
        return getter(base_url, headers, config.timeout_seconds, query)

    page_url = base_url
    page_query = query
    combined = None
    for page_number in range(config.max_pages):
        payload, response_headers = http_json_page(
            page_url,
            headers,
            config.timeout_seconds,
            page_query,
            opener=opener,
            sleeper=sleeper,
            max_retries=config.max_retries,
            backoff_seconds=config.retry_backoff_seconds,
        )
        combined = payload if combined is None else merge_pages(combined, payload)
        link = next_link(response_headers)
        if not link:
            return combined
        page_url = urljoin(page_url, link)
        page_query = None

    raise CollectorError(f"Meraki pagination exceeded MERAKI_MAX_PAGES={config.max_pages} for {base_url}")


def mock_payloads() -> dict[str, Any]:
    return {
        "devices": [
            {"serial": "Q2XX-TEST-001", "name": "mx-test-01", "model": "MX95", "networkId": "N-test-01", "status": "online"},
            {"serial": "Q2XX-TEST-002", "name": "mr-test-01", "model": "MR46", "networkId": "N-test-01", "status": "online", "productType": "wireless", "mac": "aa:bb:cc:dd:ee:02", "lanIp": "10.0.0.2"},
        ],
        "statuses": [
            {"serial": "Q2XX-TEST-001", "status": "online", "lanIp": "10.0.0.1"},
            {"serial": "Q2XX-TEST-002", "status": "online", "lanIp": "10.0.0.2"},
        ],
        "uplink_quality": [{
            "networkId": "N-test-01", "serial": "Q2XX-TEST-001", "uplink": "wan1", "ip": "198.51.100.1",
            "timeSeries": [{"ts": "2026-10-14T12:00:00Z", "lossPercent": 0.5, "latencyMs": 23.4}],
        }],
        "uplink_statuses": [{
            "networkId": "N-test-01", "serial": "Q2XX-TEST-001", "model": "MX95",
            "uplinks": [{"interface": "wan1", "status": "active"}],
        }],
        "wireless_connection": {"N-test-01": {"assoc": 1, "auth": 2, "dhcp": 0, "dns": 1, "success": 42}},
        "wireless_clients": {"N-test-01": [{
            "id": "k-test-client-01", "description": "Lab Laptop", "mac": "aa:bb:cc:dd:ee:01",
            "ip": "192.0.2.10", "ip6Local": "2001:db8::10", "ssid": "Corp-WiFi",
            "lastSeen": 1791979200, "status": "Online", "recentDeviceConnection": "Wireless",
            "recentDeviceSerial": "Q2XX-TEST-002", "usage": {"sent": 123456, "recv": 654321},
        }]},
        "wireless_ap_statuses": {"items": [{
            "serial": "Q2XX-TEST-002", "name": "mr-test-01",
            "network": {"id": "N-test-01", "name": "Test Network"},
            "basicServiceSets": [{
                "bssid": "aa:bb:cc:dd:ee:03",
                "ssid": {"name": "Corp-WiFi", "number": 0, "enabled": True, "advertised": True},
                "radio": {"band": "5", "channel": 36, "channelWidth": 80, "power": 18, "isBroadcasting": True, "index": "1"},
            }],
        }]},
        "wireless_client_counts": {"Q2XX-TEST-002": [{
            "startTs": "2026-10-14T12:00:00Z", "endTs": "2026-10-14T12:05:00Z", "clientCount": 7,
        }]},
        "wireless_usage": {"Q2XX-TEST-002|5": [{
            "startTs": "2026-10-14T12:00:00Z", "endTs": "2026-10-14T12:05:00Z",
            "totalKbps": 2450, "sentKbps": 650, "receivedKbps": 1800,
        }]},
        "wireless_channel_utilization": [{
            "serial": "Q2XX-TEST-002", "mac": "aa:bb:cc:dd:ee:02", "network": {"id": "N-test-01"},
            "byBand": [{"band": "5", "total": {"percentage": 42.5}, "wifi": {"percentage": 35.0}, "nonWifi": {"percentage": 7.5}}],
        }],
        "wireless_signal_quality": [{
            "snr": 37, "rssi": -58,
            "client": {"id": "k-test-client-01", "mac": "aa:bb:cc:dd:ee:01"},
            "network": {"id": "N-test-01", "name": "Test Network"},
        }],
        "wireless_data_rates": {"N-test-01|aa:bb:cc:dd:ee:01": [{
            "startTs": "2026-10-14T12:00:00Z", "endTs": "2026-10-14T12:05:00Z",
            "averageKbps": 12000, "downloadKbps": 10000, "uploadKbps": 2000,
        }]},
    }


def _device_tags(device: dict[str, Any], org_id: str, serial: str | None = None) -> dict[str, Any]:
    serial = serial or device.get("serial", "unknown")
    return {
        "organization_id": org_id or "mock-org",
        "device_serial": serial,
        "device_name": device.get("name", "unknown"),
        "model": device.get("model", "unknown"),
        "network_id": device.get("networkId", "unknown"),
    }


def build_metrics(payloads: dict[str, Any], org_id: str, timestamp_ns: int | None = None, last_connected_round_seconds: int = 3600) -> list[str]:
    devices = {d.get("serial", ""): d for d in payloads.get("devices", [])}
    result: list[str] = []

    for status in payloads.get("statuses", []):
        device = devices.get(status.get("serial", ""), {})
        state = str(status.get("status", "unknown")).lower()
        result.append(line("meraki_device_status", _device_tags(device, org_id, status.get("serial")), {
            "online": state == "online",
            "status_code": {"offline": 0, "online": 1, "alerting": 2, "dormant": -1}.get(state, -2),
        }, timestamp_ns))

    for item in payloads.get("uplink_quality", []):
        device = devices.get(item.get("serial", ""), {})
        for sample in item.get("timeSeries", [])[-1:]:
            tags = _device_tags(device, org_id, item.get("serial"))
            tags.update({"uplink": item.get("uplink", "unknown"), "uplink_ip": item.get("ip", "unknown")})
            result.append(line("meraki_uplink_quality", tags, {
                "loss_percent": float(sample.get("lossPercent", 0)),
                "latency_ms": float(sample.get("latencyMs", 0)),
            }, iso_to_ns(sample.get("ts"))))

    for item in payloads.get("uplink_statuses", []):
        device = devices.get(item.get("serial", ""), {})
        for uplink in item.get("uplinks", []):
            status = str(uplink.get("status", "unknown")).lower()
            tags = _device_tags(device, org_id, item.get("serial"))
            tags["uplink"] = uplink.get("interface", "unknown")
            result.append(line("meraki_uplink_status", tags, {
                "is_up": status in {"active", "ready"},
                "status_code": {"failed": 0, "connecting": 1, "ready": 2, "active": 3, "not connected": -1}.get(status, -2),
            }, timestamp_ns))

    for network_id, stats in payloads.get("wireless_connection", {}).items():
        tags = {"organization_id": org_id or "mock-org", "network_id": network_id}
        result.append(line("meraki_wireless_connection", tags, {
            "assoc_failures": int(stats.get("assoc", 0)),
            "auth_failures": int(stats.get("auth", 0)),
            "dhcp_failures": int(stats.get("dhcp", 0)),
            "dns_failures": int(stats.get("dns", 0)),
            "success": int(stats.get("success", 0)),
        }, timestamp_ns))

    signal_by_mac = {}
    signal_by_key = {}
    signal_payload = payloads.get("wireless_signal_quality", {})
    if isinstance(signal_payload, dict):
        for key, samples in signal_payload.items():
            if isinstance(samples, list) and samples:
                signal_by_key[key] = samples[-1]
    else:
        for item in signal_payload:
            client = item.get("client", {}) if isinstance(item, dict) else {}
            mac = str(client.get("mac", "")).lower()
            if mac:
                signal_by_mac[mac] = item

    # Emit one bounded row for each AP/BSSID/SSID/radio combination.  The
    # organization status endpoint supplies identity and live radio settings;
    # the history payloads supply the latest endpoint and usage values.
    ap_status_payload = payloads.get("wireless_ap_statuses", [])
    ap_items = ap_status_payload.get("items", []) if isinstance(ap_status_payload, dict) else ap_status_payload
    ap_devices = {d.get("serial", ""): d for d in payloads.get("devices", [])}
    ap_lan_ips = {s.get("serial", ""): s.get("lanIp") for s in payloads.get("statuses", []) if s.get("lanIp")}
    channel_utilization = {}
    channel_payload = payloads.get("wireless_channel_utilization", [])
    channel_samples = channel_payload.get("items", []) if isinstance(channel_payload, dict) else channel_payload
    for sample in channel_samples if isinstance(channel_samples, list) else []:
        serial = sample.get("serial", "")
        for band_sample in sample.get("byBand", []) or []:
            channel_utilization[f"{serial}|{band_sample.get('band', 'unknown')}"] = band_sample

    def latest_history(value: Any) -> dict[str, Any]:
        if isinstance(value, list) and value:
            return value[-1] if isinstance(value[-1], dict) else {}
        if isinstance(value, dict):
            return value
        return {}

    for ap in ap_items[:100]:
        serial = ap.get("serial", "unknown")
        device = ap_devices.get(serial, {})
        network = ap.get("network", {}) or {}
        network_id = network.get("id") or device.get("networkId", "unknown")
        ap_name = ap.get("name") or device.get("name", "unknown")
        ap_ip = device.get("lanIp") or ap_lan_ips.get(serial, "unknown")
        ap_mac = device.get("mac") or ap.get("mac") or "unknown"
        count_sample = latest_history(payloads.get("wireless_client_counts", {}).get(serial, []))
        endpoint_count = int(count_sample.get("clientCount", 0) or 0)
        bsses = ap.get("basicServiceSets", []) or []
        for bss in bsses:
            ssid_info = bss.get("ssid", {}) or {}
            radio = bss.get("radio", {}) or {}
            # Also accept the flatter shape returned by /devices/{serial}/wireless/status.
            ssid = ssid_info.get("name") or bss.get("ssidName") or "unknown"
            band = radio.get("band") or bss.get("band") or "unknown"
            channel = radio.get("channel")
            if isinstance(channel, dict):
                channel = channel.get("value")
            if channel is None:
                channel = bss.get("channel", 0)
            width = radio.get("channelWidth") or bss.get("channelWidth") or 0
            usage = latest_history(payloads.get("wireless_usage", {}).get(f"{serial}|{band}", []))
            utilization = channel_utilization.get(f"{serial}|{band}", {})
            total_util = utilization.get("total", {}) or {}
            wifi_util = utilization.get("wifi", {}) or {}
            non_wifi_util = utilization.get("nonWifi", {}) or {}
            tags = {
                "organization_id": org_id or "mock-org", "network_id": network_id,
                "device_serial": serial, "device_name": ap_name, "ap_ip": ap_ip,
                "ap_mac": ap_mac, "bssid": bss.get("bssid", "unknown"),
                "ssid": ssid, "radio_type": band,
            }
            fields = {
                "channel": int(channel or 0), "channel_width_mhz": int(width or 0),
                "current_endpoint_count": endpoint_count,
                "bandwidth_utilization_kbps": float(usage.get("totalKbps", 0) or 0),
                "bandwidth_sent_kbps": float(usage.get("sentKbps", 0) or 0),
                "bandwidth_received_kbps": float(usage.get("receivedKbps", 0) or 0),
                "channel_utilization_percent": float(total_util.get("percentage", 0) or 0),
                "wifi_channel_utilization_percent": float(wifi_util.get("percentage", 0) or 0),
                "non_wifi_channel_utilization_percent": float(non_wifi_util.get("percentage", 0) or 0),
            }
            sample_ts = usage.get("endTs") or count_sample.get("endTs")
            result.append(line("meraki_wireless_access_point", tags, fields, iso_to_ns(sample_ts) or timestamp_ns))

    for network_id, clients in payloads.get("wireless_clients", {}).items():
        for client in clients if isinstance(clients, list) else []:
            connection_type = str(client.get("recentDeviceConnection", "")).lower()
            if connection_type and connection_type != "wireless" and not client.get("ssid"):
                continue
            mac = str(client.get("mac", "unknown"))
            usage = client.get("usage", {}) or {}
            name = client.get("description") or client.get("dhcpHostname") or client.get("user") or mac
            tags = {
                "organization_id": org_id or "mock-org",
                "network_id": network_id,
                "client_name": name,
                "ip_address": client.get("ip") or "unknown",
                "ip_address_v6": client.get("ip6") or client.get("ip6Local") or "unknown",
                "ssid": client.get("ssid") or "unknown",
                "mac": mac,
            }
            sent_kb = float(usage.get("sent", 0) or 0)
            received_kb = float(usage.get("recv", 0) or 0)
            fields = {
                "connected": str(client.get("status", "")).lower() == "online",
                "transmitted": sent_kb,
                "received": received_kb,
                "transmitted_mb": sent_kb / 1024.0,
                "received_mb": received_kb / 1024.0,
            }
            last_seen = parse_last_seen(client.get("lastSeen"))
            client_timestamp = timestamp_ns
            # A dimension (tag) so Kentik tables show it as readable text.
            tags["last_connected"] = "unknown"
            if last_seen is not None:
                client_timestamp = int(last_seen * 1_000_000_000)
                connected = datetime.fromtimestamp(last_seen, tz=timezone.utc)
                fields["connected_at"] = connected.isoformat().replace("+00:00", "Z")
                tags["last_connected"] = format_last_connected(last_seen, last_connected_round_seconds)
            signal = signal_by_key.get(f"{network_id}|{mac.lower()}") or signal_by_mac.get(mac.lower(), {})
            if signal.get("rssi") is not None:
                fields["signal_strength_dbm"] = float(signal["rssi"])
            if signal.get("snr") is not None:
                fields["snr_db"] = float(signal["snr"])
            rates = payloads.get("wireless_data_rates", {}).get(f"{network_id}|{mac.lower()}", [])
            if isinstance(rates, list) and rates:
                sample = rates[-1]
                if sample.get("averageKbps") is not None:
                    fields["data_rate_kbps"] = float(sample["averageKbps"])
                    fields["data_rate_bps"] = float(sample["averageKbps"]) * 1000.0
                if sample.get("downloadKbps") is not None or sample.get("uploadKbps") is not None:
                    fields["throughput_kbps"] = float(sample.get("downloadKbps", 0) or 0) + float(sample.get("uploadKbps", 0) or 0)
            result.append(line("meraki_wireless_client", tags, fields, client_timestamp))

    return result


def send_to_kentik(config: Config, body: str, post: Callable[..., Any] | None = None, sleeper: Callable[[float], None] | None = None) -> None:
    if not config.kentik_ingest_url:
        raise CollectorError("KENTIK_INGEST_URL is required when not using --dry-run")
    post = post or urlopen
    sleeper = sleeper or time.sleep
    request = Request(config.kentik_ingest_url, data=body.encode("utf-8"), headers={
        "Content-Type": "application/influx",
        "X-CH-Auth-Email": config.kentik_email,
        "X-CH-Auth-API-Token": config.kentik_api_token,
    }, method="POST")

    for attempt in range(config.max_retries + 1):
        try:
            with post(request, timeout=config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status >= 300:
                    if status in RETRYABLE_HTTP_STATUS and attempt < config.max_retries:
                        sleeper(retry_delay(attempt, config.retry_backoff_seconds, getattr(response, "headers", None)))
                        continue
                    raise CollectorError(f"Kentik ingest returned HTTP {status}")
                return
        except HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUS and attempt < config.max_retries:
                sleeper(retry_delay(attempt, config.retry_backoff_seconds, exc.headers))
                continue
            raise CollectorError(f"Kentik ingest returned HTTP {exc.code}") from exc
        except URLError as exc:
            if attempt < config.max_retries:
                sleeper(config.retry_backoff_seconds * (2 ** attempt))
                continue
            raise CollectorError(f"Unable to reach Kentik ingest endpoint: {exc.reason}") from exc

    raise CollectorError("Kentik ingest retries exhausted")


def optional_fetch(config: Config, path: str, query: dict[str, Any] | None = None) -> Any:
    try:
        return fetch_meraki(config, path, query)
    except CollectorError as exc:
        print(f"warning: optional endpoint skipped: {exc}", file=sys.stderr)
        return []


def collect_once(config: Config, mode: str, dry_run: bool = False) -> list[str]:
    if mode == "mock":
        payloads = mock_payloads()
    else:
        devices = fetch_meraki(config, f"/organizations/{config.org_id}/devices")
        payloads = {
            "devices": devices,
            "statuses": fetch_meraki(config, f"/organizations/{config.org_id}/devices/statuses"),
            "uplink_quality": optional_fetch(config, f"/organizations/{config.org_id}/devices/uplinksLossAndLatency", {"timespan": 300}),
            "uplink_statuses": optional_fetch(config, f"/organizations/{config.org_id}/uplinks/statuses"),
            "wireless_connection": {},
            "wireless_clients": {},
            "wireless_signal_quality": {},
            "wireless_data_rates": {},
            "wireless_ap_statuses": [],
            "wireless_client_counts": {},
            "wireless_usage": {},
            "wireless_channel_utilization": [],
        }
        ap_statuses = optional_fetch(
            config, f"/organizations/{config.org_id}/wireless/ssids/statuses/byDevice"
        )
        payloads["wireless_ap_statuses"] = ap_statuses
        channel_utilization = optional_fetch(
            config, f"/organizations/{config.org_id}/wireless/devices/channelUtilization/history/byDevice/byInterval",
            {"timespan": 3600, "resolution": 300},
        )
        if isinstance(channel_utilization, (list, dict)):
            payloads["wireless_channel_utilization"] = channel_utilization

        wireless_networks = {
            d.get("networkId") for d in devices
            if d.get("networkId") and (d.get("productType") == "wireless" or str(d.get("model", "")).startswith("MR"))
        }
        ap_items = ap_statuses.get("items", []) if isinstance(ap_statuses, dict) else (ap_statuses if isinstance(ap_statuses, list) else [])
        for ap in ap_items[:config.max_wireless_access_points]:
            serial = ap.get("serial")
            network_id = (ap.get("network", {}) or {}).get("id")
            if not serial or not network_id:
                continue
            counts = optional_fetch(
                config, f"/networks/{network_id}/wireless/clientCountHistory",
                {"timespan": 3600, "resolution": 300, "deviceSerial": serial},
            )
            if isinstance(counts, list):
                payloads["wireless_client_counts"][serial] = counts
            bands = {
                (bss.get("radio", {}) or {}).get("band") or bss.get("band")
                for bss in (ap.get("basicServiceSets", []) or [])
            }
            for band in sorted(b for b in bands if b):
                usage = optional_fetch(
                    config, f"/networks/{network_id}/wireless/usageHistory",
                    {"timespan": 3600, "resolution": 300, "deviceSerial": serial, "band": band},
                )
                if isinstance(usage, list):
                    payloads["wireless_usage"][f"{serial}|{band}"] = usage

        for network_id in wireless_networks:
            stats = optional_fetch(config, f"/networks/{network_id}/wireless/connectionStats", {"timespan": 300})
            if isinstance(stats, dict):
                payloads["wireless_connection"][network_id] = stats

            clients = optional_fetch(config, f"/networks/{network_id}/clients", {"timespan": config.client_timespan_seconds})
            if isinstance(clients, list):
                wireless_clients = [
                    client for client in clients
                    if str(client.get("recentDeviceConnection", "")).lower() == "wireless" or client.get("ssid")
                ]
                payloads["wireless_clients"][network_id] = wireless_clients
                for client in wireless_clients[:config.max_wireless_clients]:
                    mac = str(client.get("mac", "")).lower()
                    client_id = client.get("id") or client.get("mac")
                    if not mac or not client_id:
                        continue
                    signal = optional_fetch(
                        config,
                        f"/networks/{network_id}/wireless/signalQualityHistory",
                        {"timespan": 3600, "clientId": client_id, "resolution": 300},
                    )
                    if isinstance(signal, list):
                        payloads["wireless_signal_quality"][f"{network_id}|{mac}"] = signal
                    rates = optional_fetch(
                        config,
                        f"/networks/{network_id}/wireless/dataRateHistory",
                        {"timespan": 3600, "clientId": client_id, "resolution": 300},
                    )
                    if isinstance(rates, list):
                        payloads["wireless_data_rates"][f"{network_id}|{mac}"] = rates

    metrics = build_metrics(payloads, config.org_id, last_connected_round_seconds=config.last_connected_round_seconds)
    body = "\n".join(metrics) + "\n"
    if dry_run or mode == "mock":
        print(body, end="")
    else:
        send_to_kentik(config, body)
        print(f"sent {len(metrics)} metric lines to Kentik")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--dry-run", action="store_true", help="print metrics instead of sending them")
    parser.add_argument("--interval", type=int, default=0, help="repeat every N seconds; 0 means once")
    args = parser.parse_args(argv)
    config = load_config()
    try:
        while True:
            collect_once(config, args.mode, args.dry_run)
            if args.interval <= 0:
                break
            time.sleep(args.interval)
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
