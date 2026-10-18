#!/usr/bin/env bash
# Mystic Center installer.
#   ./install.sh            user-level: app files, CLI symlink, .desktop, icon
#   ./install.sh --system   (root) systemd unit + ec_sys write_support + udev
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/mystic-center"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

if [[ "${1:-}" == "--system" ]]; then
    echo "==> Installing system parts (root)"
    # pkexec/sudo tunnel HOME=$HOME(root); the app lives in the invoking user's dir
    UID_TARGET="${PKEXEC_UID:-${SUDO_UID:-$(id -u)}}"
    USER_HOME="$(getent passwd "$UID_TARGET" | cut -d: -f6)"
    APP_DIR="${USER_HOME}/.local/share/mystic-center"
    echo "    app dir: ${APP_DIR}"
    # systemd service (points at $APP_DIR)
    sed "s|/home/krishna/.local/share/mystic-center|${APP_DIR}|g" \
        "$SRC/systemd/mystic-center.service" > /etc/systemd/system/mystic-center.service
    # EC debugfs write support (idempotent with mcontrolcenter's copy)
    mkdir -p /usr/lib/modprobe.d /usr/lib/modules-load.d
    cp "$SRC/data/modprobe.d/mystic-center.conf" /usr/lib/modprobe.d/mystic-center.conf
    if [[ ! -f /usr/lib/modules-load.d/mcontrolcenter.conf ]]; then
        echo "ec_sys" > /usr/lib/modules-load.d/mystic-center.conf
    fi
    # udev rule for the RGB keyboard (same as legacy project)
    mkdir -p /etc/udev/rules.d
    cp "$SRC/data/udev/61-mystic-control.rules" /etc/udev/rules.d/
    systemctl daemon-reload || true
    systemctl enable --now mystic-center.service || true
    udevadm control --reload || true
    udevadm trigger -c add -s hidraw || true
    echo "==> System parts installed; service enabled."
    systemctl is-active mystic-center.service || true
    exit 0
fi

echo "==> Checking runtime dependencies"
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
    || { echo "GTK4/libadwaita via PyGObject required"; exit 1; }

echo "==> Installing app files to ${APP_DIR}"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "${APP_DIR:?}/mystic_center" "${APP_DIR}/"?* 2>/dev/null || true
cp -r "$SRC/mystic_center" "$SRC/data" "$SRC/tests" "$SRC/docs" "$APP_DIR/"
cp "$SRC/launcher.py" "$SRC/install.sh" "$SRC/README.md" "$APP_DIR/" 2>/dev/null || true
find "$APP_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "${BIN_DIR}/mystic-center" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${APP_DIR}\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 "${APP_DIR}/launcher.py" "\$@"
EOF
chmod +x "${BIN_DIR}/mystic-center"

cp "$SRC/data/mystic-center.desktop" "$DESKTOP_DIR/"
cp "$SRC/data/mystic-center-icon.svg" "$ICON_DIR/" 2>/dev/null || true
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "==> Installed."
echo "    CLI:  mystic-center status"
echo "    GUI:  mystic-center        (or the 'Mystic Center' icon in GNOME)"
echo "    Daemon (root parts): ./install.sh --system"
