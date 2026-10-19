---
name: Meraki Kentik Metrics
description: Configure, test, and troubleshoot a Meraki Dashboard API to Kentik NMS metrics pipeline using a deterministic collector and Influx line protocol.
---
# Meraki-to-Kentik metrics workflow

Use this skill when the user wants to collect Cisco Meraki Dashboard API telemetry and make it available in Kentik NMS.

## Operating model

- Keep continuous polling and delivery in the bundled deterministic collector. Do not use an LLM as the transport loop.
- Use mock mode first. Never request or expose API keys in chat.
- Use the collector's environment variables or a local `.env` file with restrictive permissions for credentials; deploy it as a container or service. The distributable archive intentionally excludes `.env`; copy `.env.example` to `.env` on first install and preserve the existing `.env` before upgrades.
- Treat Meraki API data as operational metrics; do not represent it as native flow records.

## Included dashboard measurements

The bundled collector emits these starter measurements:

- `meraki_device_status`: online state and numeric status code.
- `meraki_uplink_quality`: loss percentage and latency in milliseconds.
- `meraki_uplink_status`: uplink state and numeric status code.
- `meraki_wireless_connection`: association, authentication, DHCP, DNS, and successful connection counts.
- `meraki_wireless_client`: client name, IPv4/IPv6, SSID, MAC, connected state, `last_connected` readable UTC date/time as a rounded dimension, backward-compatible ISO-8601 `connected_at` field, signal strength/RSSI, SNR, PHY data rate in Kbps and bps, throughput in Kbps, and sent/received usage in raw KB plus MB display metrics.
- `meraki_wireless_access_point`: AP name, management IP, AP MAC, BSSID, SSID, radio/band, channel, channel width, current endpoint count, AP bandwidth usage in total/sent/received Kbps, and total/Wi-Fi/non-Wi-Fi channel utilization percentages.

The client measurement uses client identity attributes as dimensions for SolarWinds-style per-client views. Use the `last_connected` dimension for a human-readable Connected column. Because each new value creates a new series, it is rounded down to `LAST_CONNECTED_ROUND_SECONDS` (default 3600; use 86400 for daily or 0 for exact seconds); the point timestamp remains the numeric `lastSeen` epoch and `connected_at` remains a field. Review cardinality before scaling beyond a small proof of concept; set `MAX_WIRELESS_CLIENTS` to cap per-client data-rate calls. The AP measurement uses AP, BSSID, SSID, and radio identity attributes as dimensions; set `MAX_WIRELESS_ACCESS_POINTS` to cap AP history calls.

## Reliability and scale controls

The collector follows Meraki `Link` headers for paginated responses and merges list or `items` pages up to `MERAKI_MAX_PAGES` (default 100). Transient Meraki and Kentik failures are retried up to `HTTP_MAX_RETRIES` (default 3) with exponential backoff from `HTTP_RETRY_BACKOFF_SECONDS` (default 1 second); HTTP 429 responses honor `Retry-After`. Keep the page limit bounded and tune retry settings only after checking API rate-limit behavior.

## Workflow

1. Run `scripts/collector.py --mode mock --dry-run` and verify Influx line output.
2. Run the bundled tests before changing mappings.
3. The wireless client list uses the configurable `MERAKI_CLIENT_TIMESPAN` lookback, defaulting to 86400 seconds to match Meraki's one-day client view. Connection stats use a 300-second timespan. Per-client signal and PHY-rate history use a 3600-second lookback with 300-second resolution because Meraki may reject a 300-second history window as having no compatible resolution.
4. AP/SSID rows use organization BSSID status, AP client-count history, AP usage history, and organization channel-utilization history. Bandwidth metrics are Meraki `totalKbps`, `sentKbps`, and `receivedKbps`; channel utilization is reported separately as percentages.
5. For a real organization, set `MERAKI_API_KEY` and `MERAKI_ORG_ID` in the runtime secret store.
6. Set `KENTIK_INGEST_URL`, `KENTIK_EMAIL`, and `KENTIK_API_TOKEN` in the runtime secret store.
7. Start with a 5-minute interval and a small endpoint set. Respect Meraki organization API rate limits, pagination, retry-after responses, and backoff.
8. Verify metric freshness and device dimensions in Kentik before expanding coverage.
9. Add mappings only for numeric or boolean metrics that have stable names and bounded cardinality. Keep organization, network, serial, model, site, and interface as dimensions; keep changing values as metrics.
10. On errors, classify them as authentication, rate limiting, endpoint/schema, transformation, or Kentik ingest failure. Fix the class before increasing polling frequency.

## Safety checks

- Use `--dry-run` for every new endpoint mapping.
- Never print `MERAKI_API_KEY` or `KENTIK_API_TOKEN`.
- Do not send data to Kentik until the generated line protocol has passed local tests.
- Do not claim flow visibility from API metrics alone.

## Installation options

The collector has no third-party Python dependencies; Python 3.9 or newer is sufficient. The archives intentionally exclude credentials.

### Option A: Run the Python script directly

Use this option for testing, troubleshooting, or a host where another process supervisor already manages the collector.

1. Extract the collector package and enter its directory:

   ```bash
   cd ~/Downloads
   tar -xzf meraki-kentik-poc.tar.gz
   cd meraki-kentik-poc
   ```

2. Create the private environment file:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

3. Edit `.env` and set `MERAKI_API_KEY`, `MERAKI_ORG_ID`, `KENTIK_INGEST_URL`, `KENTIK_EMAIL`, and `KENTIK_API_TOKEN`. Keep the API keys out of chat, source control, and logs. `MERAKI_CLIENT_TIMESPAN=86400` is the default 24-hour client lookback.

4. Validate locally without contacting Meraki or Kentik:

   ```bash
   python3 collector.py --mode mock --dry-run
   ```

5. Validate against Meraki without sending to Kentik:

   ```bash
   python3 collector.py --mode real --dry-run
   ```

6. Send one collection batch to Kentik:

   ```bash
   python3 collector.py --mode real
   ```

7. For continuous collection, run the five-minute polling loop under a process supervisor or terminal session:

   ```bash
   python3 collector.py --mode real --interval 300
   ```

   For a persistent server deployment, use the systemd option instead.

### Option B: Install the Linux systemd agent

Use this option for a Linux VM or server that should run the collector continuously after reboot.

1. Extract the systemd package and enter its directory:

   ```bash
   cd ~/Downloads
   tar -xzf meraki-kentik-systemd-agent.tar.gz
   cd meraki-kentik-poc
   ```

2. Install and enable the service. The installer creates the unprivileged `meraki-kentik` user, installs the collector under `/opt/meraki-kentik`, creates the protected environment-file location, and does not start the service until credentials are configured:

   ```bash
   sudo ./install-systemd.sh
   ```

3. Add credentials to the protected environment file:

   ```bash
   sudoedit /etc/meraki-kentik/meraki-kentik.env
   ```

   Set `MERAKI_API_KEY`, `MERAKI_ORG_ID`, `KENTIK_INGEST_URL`, `KENTIK_EMAIL`, and `KENTIK_API_TOKEN`. Keep `MERAKI_CLIENT_TIMESPAN=86400` unless a different client lookback is required.

4. Start the agent and verify its status:

   ```bash
   sudo systemctl start meraki-kentik.service
   sudo systemctl status meraki-kentik.service
   ```

5. Follow collection logs:

   ```bash
   sudo journalctl -u meraki-kentik.service -f
   ```

   A successful batch reports the number of metric lines sent to Kentik. The service polls every five minutes, restarts after failures, runs as the unprivileged `meraki-kentik` user, and starts automatically on future boots.

6. Upgrade the agent without replacing credentials:

   ```bash
   sudo ./install-systemd.sh
   sudo systemctl restart meraki-kentik.service
   ```

   The installer preserves the existing `/etc/meraki-kentik/meraki-kentik.env`.
