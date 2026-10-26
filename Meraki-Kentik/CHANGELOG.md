# Changelog

## Unreleased
- Add `last_connected`, a readable UTC date/time dimension on `meraki_wireless_client`, rounded by `LAST_CONNECTED_ROUND_SECONDS` (default 3600).
- Accept `lastSeen` as an epoch number, numeric string, millisecond epoch, or ISO-8601 date; emit `last_connected=unknown` when unavailable.
- Keep `connected_at` (ISO-8601) as a field and `lastSeen` as the point timestamp.
- Follow Meraki `Link` header pagination, bounded by `MERAKI_MAX_PAGES`.
- Retry transient Meraki and Kentik failures with exponential backoff; honor `Retry-After` on HTTP 429.
- Add a "How it works" diagram, a Linux download link, a troubleshooting section, and Kentik dashboard and Docker documentation.
