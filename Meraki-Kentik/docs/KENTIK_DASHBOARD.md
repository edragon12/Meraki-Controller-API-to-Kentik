# Building the Kentik "Meraki Controller Dashboard"

The collector writes these measurements to Kentik. Create one table panel per measurement.

| Panel | Measurement | Dimensions to add | Metrics to add |
|---|---|---|---|
| Active Clients | `meraki_wireless_client` | `client_name`, `ip_address`, `mac`, `ssid`, `last_connected` | `data_rate_kbps`, `throughput_kbps`, `transmitted_mb`, `received_mb` |
| All Access Points and SSIDs | `meraki_wireless_access_point` | `ap_ip`, `ap_mac`, `radio_type`, `ssid` | `current_endpoint_count`, `channel_utilization_percent` |
| Meraki Wireless Connection | `meraki_wireless_connection` | `network_id`, `organization_id` | `assoc_failures`, `auth_failures`, `dhcp_failures`, `dns_failures`, `success` |
| Meraki Device Status | `meraki_device_status` | `device_name`, `device_serial`, `model`, `network_id`, `organization_id` | `online`, `status_code` |

## Steps
1. Run the collector so data exists: `python3 collector.py --mode real`.
2. In Kentik, create or open a dashboard and add a panel for the Kentik NMS metrics source.
3. Select the measurement from the table above.
4. In the **Dimensions** pane, add the listed dimensions. Dimensions are the text columns.
5. Add the listed metrics. Metrics are the numeric columns.
6. Choose a table visualization, set a time range of at least the last hour, and save.

## Notes
- `last_connected` is a dimension, not a metric, so it appears as a readable text column. It is rounded by `LAST_CONNECTED_ROUND_SECONDS` (default 3600).
- The numeric `connected` metric is true/false and is not a date.
- Menu names can differ between Kentik portal versions; use the pane names above as a guide.
