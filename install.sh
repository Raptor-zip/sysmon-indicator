#!/usr/bin/env bash
# Install / uninstall the Sysmon indicator.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="sysmon@raptor-zip.github.io"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"

uninstall() {
    systemctl --user disable --now sysmon.service 2>/dev/null || true
    rm -f "$UNIT_DIR/sysmon.service"
    systemctl --user daemon-reload 2>/dev/null || true

    gnome-extensions disable "$UUID" 2>/dev/null || true
    rm -rf "$EXT_DIR"
    rm -f "$BIN_DIR/sysmon-collect"
    rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/sysmon"

    echo "Uninstalled. Restart GNOME Shell (Alt+F2 -> r on X11) to drop the indicator."
}

install_all() {
    # Collector -> ~/.local/bin so nothing depends on where this repo lives
    mkdir -p "$BIN_DIR"
    install -m 755 "$SRC/bin/sysmon-collect.py" "$BIN_DIR/sysmon-collect"

    # GNOME Shell extension (copied, not symlinked: some setups refuse symlinks)
    mkdir -p "$EXT_DIR"
    install -m 644 \
        "$SRC/extension/$UUID/metadata.json" \
        "$SRC/extension/$UUID/extension.js" \
        "$SRC/extension/$UUID/stylesheet.css" \
        "$EXT_DIR/"

    # systemd user service (long-running: CPU usage needs deltas between samples)
    mkdir -p "$UNIT_DIR"
    install -m 644 "$SRC/systemd/sysmon.service" "$UNIT_DIR/"
    systemctl --user daemon-reload
    systemctl --user enable --now sysmon.service

    cat <<EOF

Installed:
  extension : $EXT_DIR
  collector : $BIN_DIR/sysmon-collect
  service   : $UNIT_DIR/sysmon.service (samples every 2s)

インストールが完了しました。
トップバーに表示するには、続けて次の2つの操作を行ってください。

  1. GNOME Shellを再読み込みします。
     X11:     Alt+F2を押し、rと入力してEnterを押します。
     Wayland: 一度ログアウトし、再ログインして端末を開き直します。

  2. 再読み込み後、端末で次の1行を実行します。
     （「2.」は手順番号なので入力しません）

     gnome-extensions enable $UUID

成功しても通常は何も表示されません。
トップバーにCPU、MEM、GPUが表示されたら完了です。
EOF
}

case "${1:-install}" in
    uninstall) uninstall ;;
    install) install_all ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
