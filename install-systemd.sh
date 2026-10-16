#!/bin/sh
set -eu

PREFIX=/opt/meraki-kentik
CONFIG_DIR=/etc/meraki-kentik
SERVICE_USER=meraki-kentik
UNIT_NAME=meraki-kentik.service
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root, for example: sudo ./install-systemd.sh" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "systemd/systemctl is required" >&2; exit 1; }

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$PREFIX" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$PREFIX"
install -d -o root -g root -m 0750 "$CONFIG_DIR"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$SCRIPT_DIR/collector.py" "$PREFIX/collector.py"
install -o root -g root -m 0644 "$SCRIPT_DIR/README.md" "$PREFIX/README.md"

if [ ! -f "$CONFIG_DIR/meraki-kentik.env" ]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$SCRIPT_DIR/.env.example" "$CONFIG_DIR/meraki-kentik.env"
    echo "Created $CONFIG_DIR/meraki-kentik.env; add Meraki and Kentik credentials before starting collection."
else
    echo "Preserving existing $CONFIG_DIR/meraki-kentik.env"
fi

install -o root -g root -m 0644 "$SCRIPT_DIR/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"

echo "Installed and enabled $UNIT_NAME (not started until credentials are configured)"
echo "Edit credentials: sudoedit $CONFIG_DIR/meraki-kentik.env"
echo "Start service:   systemctl start $UNIT_NAME"
echo "View status:     systemctl status $UNIT_NAME"
echo "View logs:       journalctl -u $UNIT_NAME -f"
