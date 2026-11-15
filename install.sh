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

# Add the extension to GNOME's enabled list. A freshly copied extension is not
# known to the running Shell, so `gnome-extensions enable` fails until the
# Shell reloads; writing the setting directly makes it load on that reload.
enable_on_next_load() {
    if gnome-extensions enable "$UUID" 2>/dev/null; then
        return 0
    fi
    command -v gsettings >/dev/null || return 0
    local current
    current=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null) || return 0
    python3 - "$current" "$UUID" <<'PY' || true
import ast, subprocess, sys
current, uuid = sys.argv[1], sys.argv[2]
enabled = ast.literal_eval(current.replace('@as ', '', 1))
if uuid not in enabled:
    enabled.append(uuid)
    subprocess.run(['gsettings', 'set', 'org.gnome.shell',
                    'enabled-extensions', str(enabled)], check=True)
PY
}

install_all() {
    # Check the GNOME version before touching any installed file
    local shell_major
    shell_major=$(gnome-shell --version | sed -nE 's/.* ([0-9]+)\..*/\1/p')
    case "$shell_major" in
        42|45|46) ;;
        *) echo "Unsupported GNOME Shell: ${shell_major:-unknown} (supported: 42, 45, 46)" >&2; return 1 ;;
    esac

    # Collector -> ~/.local/bin so nothing depends on where this repo lives
    mkdir -p "$BIN_DIR"
    install -m 755 "$SRC/bin/sysmon-collect.py" "$BIN_DIR/sysmon-collect"

    # GNOME Shell extension (copied, not symlinked: some setups refuse symlinks).
    # GNOME 45+ needs an ES module, so the entry point is generated per version.
    python3 "$SRC/bin/build-extension.py" "$SRC/extension/$UUID" "$EXT_DIR" "$shell_major"

    # systemd user service (long-running: CPU usage needs deltas between samples)
    mkdir -p "$UNIT_DIR"
    install -m 644 "$SRC/systemd/sysmon.service" "$UNIT_DIR/"
    systemctl --user daemon-reload
    systemctl --user enable sysmon.service
    # restart, not start: a re-install must replace an already running collector
    systemctl --user restart sysmon.service

    enable_on_next_load

    cat <<EOF

Installed:
  extension : $EXT_DIR
  collector : $BIN_DIR/sysmon-collect
  service   : $UNIT_DIR/sysmon.service (samples every 2s)

インストールが完了しました。
トップバーに表示するには、GNOME Shellを再読み込みしてください。

  X11:     Alt+F2を押し、rと入力してEnterを押します。
  Wayland: 一度ログアウトし、再ログインします。

トップバーにCPU、MEM、GPUが表示されたら完了です。
表示されない場合は、再読み込み後に次を実行してください。

  gnome-extensions enable $UUID
EOF
}

case "${1:-install}" in
    uninstall) uninstall ;;
    install) install_all ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
