# Sysmon — GNOME Shell indicator

A compressed btop in the GNOME top bar. The panel keeps it to two numbers and a
sparkline of the last 8 CPU samples:

```
                    ▁▂▅█▃▂▁▁ CPU 23%  MEM 41%
```

Click it for the breakdown: per-core meters, memory and swap, disk and network
throughput, temperatures and fans, the dGPU, and the six processes eating the most
CPU (or memory — the header toggles).

```
 CPU（24 スレッド）                                          45.4%
 ██████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
 負荷 13.03 9.93 9.98  ·  2.90GHz  ·  69°C  ·  5686/6382rpm
 ▄▂▇▃▅▁▂▆▃▄▂▅▇▁▃▂▄▆▂▃▅▁▄▂          ← コア別

 メモリ                                             22.2G / 125G
 ███████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
 キャッシュ 15.1G  ·  空き 103G

 GPU（RTX PRO 5000 Blackwell）                              0%
 ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
 VRAM 15.0M / 23.9G（0%）  ·  57°C  ·  8W

 ディスク 読み   書き        wlp132s0f0 ↓   ↑
 0B/s          4.5M/s      1.4K/s        1.5K/s

 上位プロセス                                    [ CPU ] [メモリ]
 chrome                            482016    297%     2.1G
 AWSIM.x86_64                      466741    155%     1.2G
 python3 gnss_dr_localizer_node.py 468855     37%    50.2M
```

## How it works

| Path | Role |
|---|---|
| `bin/sysmon-collect.py` | Samples `/proc` and `/sys` every 2s and writes the cache |
| `systemd/sysmon.service` | Keeps the collector running |
| `extension/sysmon@raptor-zip.github.io/` | Reads the cache and draws the indicator |
| `~/.cache/sysmon/system.json` | The cache that connects the two |

The extension never touches `/proc` itself. CPU usage is a delta between two samples,
and getting it means reading `/proc/<pid>/stat` for every process — several hundred
file reads. Doing that on the GNOME Shell main loop would stutter the compositor, so
it lives in a separate long-running process instead. (Long-running rather than a
systemd timer, precisely because the deltas need the previous sample.)

One sample costs 10–20 ms on a busy 24-thread machine with ~750 processes, so the
collector burns 1–2% of a single core (well under 0.1% of the machine) and sits at
7 MB RSS. It runs at `Nice=10` with idle I/O priority so the monitor stays out of the
way of the thing it is monitoring; raise `--interval` if even that is too much.

The expensive sensors are throttled separately: fan RPM goes through ACPI and NVMe
temperature through an admin command, several ms each, so those are refreshed every
5 s instead of every sample. Filesystem usage is refreshed every 30 s.

### The dGPU is left asleep

On a laptop with `prime-select on-demand`, the NVIDIA card is normally runtime
suspended (D3cold). Polling `nvidia-smi` every 2 seconds would keep it awake purely
for the sake of watching it. So the collector reads
`/sys/bus/pci/drivers/nvidia/*/power/runtime_status` first and only queries
`nvidia-smi` when the card is already `active`. While it is asleep the GPU row just
says so.

## Requirements

- GNOME Shell 42 (tested on Ubuntu 22.04, X11).
  43 and 44 will most likely work — add the version to `shell-version` in
  `metadata.json` and try it. GNOME 45+ needs an ESM port and is not supported yet.
- Python 3.8+ (standard library only)
- Linux. Everything comes from `/proc` and `/sys`; no root, no extra packages.

Optional, used when present: `nvidia-smi` (dGPU), `btop` (the "btop を開く" item),
`coretemp`/`k10temp` (CPU temperature), `thinkpad_acpi` (fan RPM).

## Install

```bash
git clone https://github.com/Raptor-zip/sysmon-indicator.git
cd sysmon-indicator
./install.sh
```

Then activate it:

1. Restart GNOME Shell — `Alt+F2` → `r` → `Enter` (X11 only; on Wayland, log out and back in)
2. `gnome-extensions enable sysmon@raptor-zip.github.io`

Uninstall with `./install.sh uninstall`.

## What you get

- **Panel**: `<sparkline> CPU <pct> MEM <pct>`
  - Yellow at 70%, bold red at 90%, each number coloured on its own
  - Dimmed when the cache stops updating — your cue that the collector died
- **Menu**
  - CPU: overall meter, load average, average clock, package temperature, fan RPM,
    and a per-core meter strip
  - Memory: used / total with cache and available; swap only when the machine has any
  - GPU: utilisation, VRAM, temperature, power — or `サスペンド中`
  - Disk read/write and network down/up, as rates over the last interval
  - Mounted filesystems: usage and free space
  - Top 6 processes by CPU or by memory. Interpreted processes get the script name
    appended (`python3 foo.py`, `node cli.js`) so a screen of `node` rows still tells
    you something
  - Uptime, process and thread counts, last sample time
  - `btop を開く` when btop is installed, for when the compressed version isn't enough

## Tuning

Everything worth changing is a constant at the top of a file.

| What | Where |
|---|---|
| Sample interval | `--interval` in `systemd/sysmon.service` |
| Sparkline width (0 hides it) | `SPARK_WIDTH` in `extension.js` |
| Yellow / red thresholds | `WARN_PCT`, `CRIT_PCT` in `extension.js` |
| Number of processes listed | `TOP_N` in `sysmon-collect.py` (and the loop count in `_buildMenu`) |
| Panel position | the `addToStatusArea(..., 0, 'right')` call at the bottom of `extension.js` — `'center'` puts it next to the clock |

After editing, re-run `./install.sh` and restart GNOME Shell.

## Troubleshooting

**Panel shows `CPU --%` / everything is dimmed.** The collector is not running:

```bash
systemctl --user status sysmon.service
journalctl --user -u sysmon.service -n 50
```

The menu also grows a `サービスを起動` button whenever the cache is missing or stale.

**Fans or temperatures are missing.** Nothing exposed them under `/sys/class/hwmon`.
`sensors` from `lm-sensors` shows what the kernel actually has; on ThinkPads fan RPM
needs the `thinkpad_acpi` module.

**Per-core strip is missing cores.** It is drawn from `/proc/stat`, so it shows
logical CPUs (SMT threads), which is why a 24-thread machine shows 24 bars.

## License

MIT
