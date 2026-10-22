# Sysmon — GNOME Shell indicator

A compressed btop in the GNOME top bar. The panel shows three compact percentages:

```
                    CPU 23%  MEM 41%  GPU 12%
```

Click it for the breakdown: per-core meters, memory and swap, disk and network
throughput, temperatures and fans, the dGPU, and the six processes eating the most
CPU, memory, or GPU (the header toggles). The GPU tab lists graphics and compute
processes with their PID, GPU utilisation, and VRAM usage. Unsupported readings
are shown as `--`, rather than zero.

Hover over a process row (or focus it with the keyboard) to see its full working
directory, then click or press Enter to open that folder in the default file
manager. This works in the CPU, memory, and GPU lists. The directory is the
process's current working directory, which may differ from its source repository
or executable location. Exited processes, inaccessible directories, and deleted
directories are shown as unavailable. Directory information is sampled only for
the listed processes, alongside their names.

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
says so, and the panel shows `GPU 0%`. Unavailable readings show `GPU --%`.
Active GPUs are also queried with `nvidia-smi pmon` for graphics and compute
processes; unsupported process metrics remain unavailable.

## Requirements

- GNOME Shell 42 (tested on Ubuntu 22.04, X11).
  43 and 44 will most likely work — add the version to `shell-version` in
  `metadata.json` and try it. GNOME 45+ needs an ESM port and is not supported yet.
- Python 3.8+ (standard library only)
- Linux. Everything comes from `/proc` and `/sys`; no root, no extra packages.

Optional, used when present: `nvidia-smi` (dGPU), `btop` (the "btop を開く" item),
`coretemp`/`k10temp` (CPU temperature), `thinkpad_acpi` (fan RPM).

## インストール

### 1. ダウンロードしてインストールする

「端末」アプリを開き、次の3行を上から順に実行します。

```bash
git clone https://github.com/Raptor-zip/sysmon-indicator.git
cd sysmon-indicator
./install.sh
```

`Installed:` と表示されたら、ファイルの配置とデータ収集サービスの起動は完了です。
インジケーターをトップバーに表示するには、続けて次の2つの操作を行います。

### 2. GNOME Shellを再読み込みする

- **X11の場合:** `Alt+F2`を押し、`r`と入力して`Enter`を押します。一瞬画面が
  点滅することがあります。
- **Waylandの場合:** システムメニューから一度ログアウトし、再度ログインします。
  ログイン後に「端末」アプリをもう一度開いてください。

X11とWaylandのどちらを使っているか分からない場合は、端末で次を実行します。

```bash
echo "$XDG_SESSION_TYPE"
```

`x11`と表示されたらX11、`wayland`と表示されたらWaylandです。

### 3. 拡張機能を有効にする

GNOME Shellの再読み込み後、端末で次の1行を実行します。

```bash
gnome-extensions enable sysmon@raptor-zip.github.io
```

インストーラーに表示される`2.`や`2)`は手順番号なので、コマンドには含めません。
成功しても端末には通常何も表示されません。トップバーに`CPU`、`MEM`、`GPU`が
表示されれば完了です。

「拡張機能が存在しない」というエラーが出た場合は、手順2を行ってからもう一度
実行してください。有効になっているか確認するには、次を実行します。

```bash
gnome-extensions list --enabled | grep -F sysmon@raptor-zip.github.io
```

アンインストールする場合は、ダウンロードした`sysmon-indicator`ディレクトリに
移動して次を実行します。

```bash
./install.sh uninstall
```

## What you get

- **Panel**: `CPU <pct> MEM <pct> GPU <pct>`
  - Yellow at 70%, bold red at 90%, each number coloured on its own
  - Dimmed when the cache stops updating — your cue that the collector died
- **Menu**
  - CPU: overall meter, load average, average clock, package temperature, fan RPM,
    and a per-core meter strip
  - Memory: used / total with cache and available; swap only when the machine has any
  - GPU: utilisation, VRAM, temperature, power — or `サスペンド中`
  - Disk read/write and network down/up, as rates over the last interval
  - Mounted filesystems: usage and free space
  - Top 6 processes by CPU, memory, or GPU (GPU utilisation / VRAM). Interpreted processes get the script name
    appended (`python3 foo.py`, `node cli.js`) so a screen of `node` rows still tells
    you something
  - Uptime, process and thread counts, last sample time
  - `btop を開く` when btop is installed, for when the compressed version isn't enough

## Tuning

Everything worth changing is a constant at the top of a file.

| What | Where |
|---|---|
| Sample interval | `--interval` in `systemd/sysmon.service` |
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
