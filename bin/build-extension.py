#!/usr/bin/env python3
"""Build the installed extension for the running GNOME Shell.

The repository keeps a single GNOME 42 (legacy imports) source. GNOME 45 moved
extensions to ES modules, so for 45+ only the import header and the entry point
are rewritten; the indicator code in between is copied unchanged.
"""
import json
import shutil
import sys
from pathlib import Path

SUPPORTED = (42, 45, 46)

# 共有部分の始まりと終わり。ここより前は legacy import、後ろは legacy の入口
BODY_START = "const CACHE_PATH"
BODY_END = "\nclass Extension {"

ESM_HEADER = """\
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

"""

ESM_FOOTER = """

export default class SysmonExtension extends Extension {
    enable() {
        this._indicator = new SysmonIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, PANEL_INDEX, PANEL_BOX);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
"""


def build(source: Path, destination: Path, shell_version: int) -> None:
    if shell_version not in SUPPORTED:
        supported = ", ".join(map(str, SUPPORTED))
        raise ValueError(
            f"Unsupported GNOME Shell {shell_version}; supported: {supported}")

    script = (source / "extension.js").read_text()
    metadata = json.loads((source / "metadata.json").read_text())
    if shell_version >= 45:
        start = script.index(BODY_START)
        end = script.index(BODY_END, start)
        script = ESM_HEADER + script[start:end] + ESM_FOOTER
        metadata["shell-version"] = ["45", "46"]

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "extension.js").write_text(script)
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copyfile(source / "stylesheet.css", destination / "stylesheet.css")


if __name__ == "__main__":
    try:
        build(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
    except (ValueError, OSError, IndexError) as error:
        sys.exit(f"build-extension: {error}")
