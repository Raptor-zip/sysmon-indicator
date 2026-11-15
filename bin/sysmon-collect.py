#!/usr/bin/env python3
"""システムのメトリクスを一定間隔でサンプリングして JSON キャッシュへ書き出す。

GNOME Shell 拡張はこのキャッシュを読むだけにする。/proc の全走査（プロセス数ぶんの
read）や nvidia-smi の起動をシェルのメインループから追い出すのが目的。

CPU 使用率のような「前回との差分」が要る値があるので、タイマーで都度起動するのでは
なく常駐して差分を持つ。標準ライブラリのみで動く。
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'sysmon')
CACHE_PATH = os.path.join(CACHE_DIR, 'system.json')

CLK_TCK = os.sysconf('SC_CLK_TCK')
PAGE_SIZE = os.sysconf('SC_PAGE_SIZE')
SECTOR_BYTES = 512

DEFAULT_INTERVAL = 2.0
HISTORY_LEN = 30          # パネルのスパークライン用に残す CPU 使用率の履歴
TOP_N = 6                 # メニューに出すプロセス数

# comm だけでは中身が分からない実行系。cmdline から実際のスクリプト名を拾う
INTERPRETERS = {
    'python', 'python2', 'python3', 'node', 'deno', 'bun', 'ruby', 'perl',
    'java', 'bash', 'sh', 'zsh', 'fish', 'dotnet', 'mono', 'electron',
}
# この直後の引数はスクリプト本文そのもの（`python3 -c "..."`）なので名前にならない
INLINE_CODE_FLAGS = {'-c', '-e', '--eval', '--command'}
LABEL_MAX = 40

# I/O 集計から外す仮想ブロックデバイス
SKIP_BLOCK_PREFIX = ('loop', 'ram', 'zram', 'sr', 'md', 'dm-')
# 帯域集計から外す仮想インターフェース
SKIP_IFACE_PREFIX = ('lo', 'docker', 'br-', 'veth', 'virbr', 'tap', 'vnet',
                     'tun', 'wg', 'kube', 'cni', 'flannel')

REAL_FSTYPES = {
    'ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'zfs', 'jfs', 'reiserfs',
    'vfat', 'exfat', 'ntfs', 'ntfs3', 'bcachefs',
}


def read_text(path):
    # procfs は latin-1 で必ずデコードできる。UTF-8 の検証を挟まないぶん、
    # プロセス数ぶん叩くホットパスではこちらの方が目に見えて速い
    try:
        with open(path, 'rb') as fp:
            return fp.read().decode('latin-1')
    except OSError:
        return None


def fix_utf8(text):
    """latin-1 で読んだ文字列に非 ASCII があれば UTF-8 として解釈し直す。

    プロセス名やパスに日本語が入っていても化けないようにするための後始末。
    """
    if text.isascii():
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except UnicodeError:
        return text


def read_int(path):
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def read_proc_cwd(pid):
    """上位プロセスだけの作業ディレクトリ。終了・権限不足・削除済みは未取得。"""
    try:
        link = '/proc/%d/cwd' % pid
        path = os.readlink(link)
        return path if os.path.isabs(path) and os.stat(link).st_nlink > 0 else None
    except OSError:
        return None


class CpuSampler:
    """/proc/stat の累積カウンタから全体・コア別の使用率を出す。"""

    def __init__(self):
        self._prev = {}
        self.history = []

    @staticmethod
    def _snapshot():
        snap = {}
        raw = read_text('/proc/stat') or ''
        for line in raw.splitlines():
            if not line.startswith('cpu'):
                break
            parts = line.split()
            try:
                vals = [int(v) for v in parts[1:]]
            except ValueError:
                continue
            if len(vals) < 5:
                continue
            # iowait は「待っているだけで走ってはいない」ので idle 側に入れる
            # （top の %Cpu 行が us+sy+ni を busy として扱うのと同じ数え方）
            idle = vals[3] + vals[4]
            snap[parts[0]] = (sum(vals), idle)
        return snap

    def sample(self):
        cur = self._snapshot()
        prev, self._prev = self._prev, cur

        def pct(key):
            if key not in cur or key not in prev:
                return 0.0
            d_total = cur[key][0] - prev[key][0]
            d_idle = cur[key][1] - prev[key][1]
            if d_total <= 0:
                return 0.0
            return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))

        cores = sorted((k for k in cur if k != 'cpu'),
                       key=lambda k: int(k[3:] or 0))
        overall = pct('cpu')

        self.history.append(round(overall, 1))
        del self.history[:-HISTORY_LEN]

        return {
            'pct': round(overall, 1),
            'per_core': [round(pct(k), 1) for k in cores],
            'history': list(self.history),
            'count': len(cores),
        }


class ProcSampler:
    """/proc/<pid>/stat を全走査して CPU / メモリ上位のプロセスを出す。"""

    def __init__(self):
        self._prev = {}

    @staticmethod
    def _clean_label(text):
        """1 行に収まる短い名前だけを通す。

        `python3 -c '<スクリプト本文>'` のような引数をそのまま名前にすると、
        改行だらけの巨大なラベルがメニューに流れ込む。
        """
        text = text.split('\n', 1)[0].strip()
        if not text or len(text) > LABEL_MAX:
            return None
        if any(ord(ch) < 32 for ch in text):
            return None
        return text

    @classmethod
    def _refine_name(cls, pid, comm):
        base = comm.rstrip('0123456789.')
        if comm not in INTERPRETERS and base not in INTERPRETERS:
            return comm
        raw = read_text('/proc/%d/cmdline' % pid)
        if not raw:
            return comm
        args = [a for a in raw.split('\0') if a]
        skip_next = False
        for arg in args[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg.startswith('-'):
                skip_next = arg in INLINE_CODE_FLAGS
                continue
            name = cls._clean_label(os.path.basename(arg.split('\n', 1)[0]))
            if name:
                return '%s %s' % (comm, fix_utf8(name))
            break
        return comm

    def sample(self, dt):
        cur = {}
        rows = []
        total_threads = 0

        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            raw = read_text('/proc/%s/stat' % entry)
            if not raw:
                continue
            close = raw.rfind(')')
            open_ = raw.find('(')
            if close < 0 or open_ < 0:
                continue
            comm = raw[open_ + 1:close]
            fields = raw[close + 2:].split()
            if len(fields) < 22:
                continue
            try:
                state = fields[0]
                ticks = int(fields[11]) + int(fields[12])
                threads = int(fields[17])
                starttime = int(fields[19])
                rss = int(fields[21]) * PAGE_SIZE
            except (ValueError, IndexError):
                continue

            cur[pid] = (starttime, ticks)
            total_threads += threads

            prev = self._prev.get(pid)
            if prev and prev[0] == starttime and dt > 0:
                cpu = (ticks - prev[1]) / CLK_TCK / dt * 100.0
            else:
                cpu = 0.0  # 初回サンプル、または pid の使い回し

            rows.append({
                'pid': pid,
                'name': comm,
                'cpu': max(0.0, cpu),
                'rss': rss,
                'state': state,
            })

        self._prev = cur

        by_cpu = sorted(rows, key=lambda r: r['cpu'], reverse=True)[:TOP_N]
        by_mem = sorted(rows, key=lambda r: r['rss'], reverse=True)[:TOP_N]

        # 名前の補正（cmdline を読む）は上位だけ。全プロセスに掛けると倍近く重くなる
        for row in by_cpu + by_mem:
            if 'label' not in row:
                row['name'] = fix_utf8(row['name'])
                row['label'] = self._refine_name(row['pid'], row['name'])
                row['cwd'] = read_proc_cwd(row['pid'])
            row['cpu'] = round(row['cpu'], 1)

        return {
            'total': len(rows),
            'threads': total_threads,
            'running': sum(1 for r in rows if r['state'] == 'R'),
            'by_cpu': by_cpu,
            'by_mem': by_mem,
        }


class RateSampler:
    """ディスクとネットワークの累積バイト数を bytes/sec に均す。"""

    def __init__(self):
        self._prev = {}

    def _rate(self, key, value, dt):
        prev = self._prev.get(key)
        self._prev[key] = value
        if prev is None or dt <= 0 or value < prev:
            return 0.0
        return (value - prev) / dt

    @staticmethod
    def _physical_disks():
        try:
            names = os.listdir('/sys/block')
        except OSError:
            return set()
        return {n for n in names if not n.startswith(SKIP_BLOCK_PREFIX)}

    def disk(self, dt):
        disks = self._physical_disks()
        read_b = write_b = 0
        raw = read_text('/proc/diskstats') or ''
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 10 or parts[2] not in disks:
                continue
            try:
                read_b += int(parts[5]) * SECTOR_BYTES
                write_b += int(parts[9]) * SECTOR_BYTES
            except ValueError:
                continue
        return {
            'read_bps': self._rate('disk_r', read_b, dt),
            'write_bps': self._rate('disk_w', write_b, dt),
        }

    def net(self, dt):
        rx = tx = 0
        raw = read_text('/proc/net/dev') or ''
        for line in raw.splitlines()[2:]:
            name, _, rest = line.partition(':')
            name = name.strip()
            if not name or name.startswith(SKIP_IFACE_PREFIX):
                continue
            parts = rest.split()
            if len(parts) < 9:
                continue
            try:
                rx += int(parts[0])
                tx += int(parts[8])
            except ValueError:
                continue
        return {
            'rx_bps': self._rate('net_rx', rx, dt),
            'tx_bps': self._rate('net_tx', tx, dt),
            'iface': default_iface(),
        }


class GpuSampler:
    """NVIDIA dGPU。ランタイムサスペンド中は nvidia-smi を叩かない。

    prime on-demand 運用だと dGPU は普段 D3cold で寝ている。そこへ 2 秒ごとに
    nvidia-smi を投げると監視のためだけに起こし続けることになるので、
    runtime_status が active のときだけ問い合わせる。
    """

    QUERY = ('name,utilization.gpu,memory.used,memory.total,'
             'temperature.gpu,power.draw')

    # pmon は 1 秒間計測してから返すので、毎サンプルだと採取ループが遅れる
    PROC_SECONDS = 5.0

    def __init__(self):
        self._smi = shutil.which('nvidia-smi')
        self._procs_at = None
        self._procs = None
        # PCI ドメインは 0000 とは限らない（DGX Spark の GB10 は 000f:01:00.0）
        devices = sorted(glob.glob('/sys/bus/pci/drivers/nvidia/[0-9a-f]*:*'))
        self._dev = devices[0] if devices else None

    @staticmethod
    def _short_name(name):
        """製品名をパネルの一行に収まる長さに落とす。

        nvidia-smi は "NVIDIA RTX PRO 5000 Blackwell Generation Laptop GPU" の
        ような長い名前を返す。そのまま見出しにするとメニューが横に伸びる。
        """
        for junk in ('NVIDIA ', 'GeForce ', ' Generation', ' Laptop GPU'):
            name = name.replace(junk, '')
        name = name.strip()
        return name[:28] if name else None

    def _processes(self):
        """描画・計算の両プロセスを取得。未対応の値は None のまま残す。"""
        try:
            out = subprocess.run(
                [self._smi, 'pmon', '-i', '0', '-c', '1', '-s', 'um'],
                capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None

        columns = None
        rows = []
        for line in out.stdout.splitlines():
            if line.lstrip().startswith('#'):
                header = line.lstrip()[1:].split()
                if 'pid' in header and 'command' in header:
                    columns = header
                continue
            if not columns or not line.strip():
                continue
            values = line.split(None, len(columns) - 1)
            fields = dict(zip(columns, values))
            try:
                pid = int(fields.get('pid', '-'))
            except ValueError:
                continue
            if pid <= 0:
                continue

            def number(key):
                try:
                    return float(fields.get(key, '-'))
                except ValueError:
                    return None

            # pmon の command は短く切られるので /proc から名前を補完する。
            name = (read_text('/proc/%d/comm' % pid) or
                    fields.get('command', '?')).strip()
            name = fix_utf8(name)
            memory = number('fb')
            rows.append({
                'pid': pid,
                'name': name,
                'label': ProcSampler._refine_name(pid, name),
                'gpu': number('sm'),
                'vram': memory * 1024 * 1024 if memory is not None else None,
            })
        if columns is None:
            return None
        rows.sort(key=lambda r: (r['gpu'] or 0, r['vram'] or 0), reverse=True)
        rows = rows[:TOP_N]
        for row in rows:
            row['cwd'] = read_proc_cwd(row['pid'])
        return rows

    def _cached_processes(self):
        now = time.monotonic()
        if self._procs_at is None or now - self._procs_at >= self.PROC_SECONDS:
            self._procs_at = now
            self._procs = self._processes()
        return self._procs

    def sample(self):
        if not self._dev or not self._smi:
            return None
        status = (read_text(os.path.join(self._dev, 'power', 'runtime_status'))
                  or '').strip()
        if status != 'active':
            return {'present': True, 'state': status or 'unknown'}
        try:
            out = subprocess.run(
                [self._smi, '--query-gpu=' + self.QUERY,
                 '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return {'present': True, 'state': 'error'}
        if out.returncode != 0 or not out.stdout.strip():
            return {'present': True, 'state': 'error'}

        cols = [c.strip() for c in out.stdout.strip().splitlines()[0].split(',')]
        name = self._short_name(cols[0]) if cols else None

        def num(idx):
            try:
                return float(cols[idx])
            except (IndexError, ValueError):
                return None

        used, total = num(2), num(3)
        return {
            'present': True,
            'state': 'active',
            'name': name,
            'util': num(1),
            'mem_used': used * 1024 * 1024 if used is not None else None,
            'mem_total': total * 1024 * 1024 if total is not None else None,
            'mem_pct': round(used / total * 100.0, 1) if used is not None and total else None,
            'temp': num(4),
            'power': num(5),
            'processes': self._cached_processes(),
        }


def default_iface():
    """デフォルトルートの出口インターフェース名（無ければ None）。"""
    raw = read_text('/proc/net/route') or ''
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1 and parts[1] == '00000000':
            return parts[0]
    return None


def read_meminfo():
    info = {}
    raw = read_text('/proc/meminfo') or ''
    for line in raw.splitlines():
        key, _, rest = line.partition(':')
        parts = rest.split()
        if parts:
            try:
                info[key] = int(parts[0]) * 1024
            except ValueError:
                pass

    total = info.get('MemTotal', 0)
    avail = info.get('MemAvailable', info.get('MemFree', 0))
    used = max(0, total - avail)
    cached = info.get('Cached', 0) + info.get('SReclaimable', 0)
    swap_total = info.get('SwapTotal', 0)
    swap_used = max(0, swap_total - info.get('SwapFree', 0))

    return {
        'total': total,
        'used': used,
        'available': avail,
        'cached': cached,
        'buffers': info.get('Buffers', 0),
        'pct': round(used / total * 100.0, 1) if total else 0.0,
        'swap_total': swap_total,
        'swap_used': swap_used,
        'swap_pct': round(swap_used / swap_total * 100.0, 1) if swap_total else 0.0,
    }


def read_cpu_extra():
    """周波数・ロードアベレージ・稼働時間。"""
    freqs = []
    raw = read_text('/proc/cpuinfo') or ''
    for line in raw.splitlines():
        if line.startswith('cpu MHz'):
            try:
                freqs.append(float(line.split(':', 1)[1]))
            except (IndexError, ValueError):
                pass

    load = None
    raw = read_text('/proc/loadavg') or ''
    parts = raw.split()
    if len(parts) >= 3:
        try:
            load = [float(parts[0]), float(parts[1]), float(parts[2])]
        except ValueError:
            load = None

    uptime = None
    raw = read_text('/proc/uptime') or ''
    try:
        uptime = float(raw.split()[0])
    except (IndexError, ValueError):
        pass

    return {
        'freq_mhz': round(sum(freqs) / len(freqs)) if freqs else None,
        'freq_max_mhz': round(max(freqs)) if freqs else None,
        'load': load,
        'uptime': uptime,
    }


class ThermalSampler:
    """CPU 温度・ファン回転数・NVMe 温度。

    どの hwmon を読むかは毎回 glob せず、一度決めたパスを使い回す
    （USB-C 機器の抜き差しで hwmon が増減することがあるので時々だけ引き直す）。
    """

    # CPU パッケージ温度として使えるものを優先順に
    CPU_SENSORS = ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal', 'acpitz')
    RESCAN_SECONDS = 60.0
    # ファン（ACPI EC 経由）と NVMe（admin コマンド）の読み出しは 1 本あたり数 ms
    # 掛かる。CPU 温度は MSR 読みで実質ゼロなので、遅い方だけ間引く
    SLOW_SECONDS = 5.0

    def __init__(self):
        self._at = 0.0
        self._cpu = None
        self._fans = []
        self._nvme = []
        self._slow_at = 0.0
        self._slow = {'fans': [], 'nvme': []}

    def _rescan(self):
        by_name = {}
        for path in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
            name = (read_text(os.path.join(path, 'name')) or '').strip()
            if name and name not in by_name:
                by_name[name] = path

        self._cpu = None
        for name in self.CPU_SENSORS:
            path = by_name.get(name)
            if path and read_int(os.path.join(path, 'temp1_input')) is not None:
                self._cpu = os.path.join(path, 'temp1_input')
                break

        self._fans = []
        self._nvme = []
        for name, path in by_name.items():
            self._fans.extend(sorted(glob.glob(os.path.join(path, 'fan*_input'))))
            if name.startswith('nvme'):
                self._nvme.append(os.path.join(path, 'temp1_input'))

    @staticmethod
    def _milli(path):
        value = read_int(path)
        return round(value / 1000.0, 1) if value is not None else None

    def sample(self, now):
        if now - self._at > self.RESCAN_SECONDS:
            self._rescan()
            self._at = now
            self._slow_at = 0.0

        if now - self._slow_at >= self.SLOW_SECONDS:
            self._slow_at = now
            self._slow = {
                'fans': [rpm for rpm in (read_int(p) for p in self._fans) if rpm],
                'nvme': [t for t in (self._milli(p) for p in self._nvme)
                         if t is not None],
            }

        return {
            'cpu': self._milli(self._cpu) if self._cpu else None,
            'fans': list(self._slow['fans']),
            'nvme': list(self._slow['nvme']),
        }


def read_filesystems():
    """実体のあるファイルシステムの使用量。/ は必ず先頭に置く。"""
    seen = set()
    out = []
    raw = read_text('/proc/mounts') or ''
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        if fstype not in REAL_FSTYPES or device in seen:
            continue
        mount = mount.replace('\\040', ' ')
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        seen.add(device)
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        out.append({
            'mount': mount,
            'total': total,
            'used': used,
            'free': free,
            'pct': round(used / (used + free) * 100.0, 1) if used + free else 0.0,
        })

    out.sort(key=lambda f: (f['mount'] != '/', f['mount']))
    return out[:4]


def write_cache(payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_PATH + '.tmp'
    with open(tmp, 'w') as fp:
        json.dump(payload, fp)
    os.replace(tmp, CACHE_PATH)   # 読み手が半端な JSON を掴まないよう差し替えで置く


class Collector:
    def __init__(self, interval):
        self.interval = interval
        self.cpu = CpuSampler()
        self.procs = ProcSampler()
        self.rates = RateSampler()
        self.gpu = GpuSampler()
        self.thermal = ThermalSampler()
        self._last = None
        self._fs_at = 0.0
        self._fs = []

    def sample(self):
        now = time.time()
        dt = (now - self._last) if self._last else 0.0
        self._last = now

        cpu = self.cpu.sample()
        cpu.update(read_cpu_extra())

        # ファイルシステムの使用量はそんなに動かないので 30 秒に 1 回で十分
        if now - self._fs_at > 30.0:
            self._fs = read_filesystems()
            self._fs_at = now

        payload = {
            'ok': True,
            'sampled_at': now,
            'interval': self.interval,
            'cpu': cpu,
            'mem': read_meminfo(),
            'disk': self.rates.disk(dt),
            'net': self.rates.net(dt),
            'fs': self._fs,
            'thermal': self.thermal.sample(now),
            'procs': self.procs.sample(dt),
            'gpu': self.gpu.sample(),
            'hostname': os.uname().nodename,
        }
        payload['uptime'] = cpu.pop('uptime', None)
        return payload

    def run_once(self):
        # 差分が要る値のために 2 回踏む
        self.sample()
        time.sleep(min(0.5, self.interval))
        write_cache(self.sample())

    def run_forever(self):
        stopping = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopping.append(True))

        write_cache(self.sample())
        next_at = time.monotonic() + self.interval
        while not stopping:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at += self.interval
            if next_at < time.monotonic():
                next_at = time.monotonic() + self.interval  # サスペンド復帰などで遅れた分は捨てる
            try:
                write_cache(self.sample())
            except Exception as exc:      # 1 回の失敗で常駐を落とさない
                print('sysmon-collect: %s' % exc, file=sys.stderr)


def main(argv):
    interval = DEFAULT_INTERVAL
    once = False
    args = argv[1:]
    while args:
        arg = args.pop(0)
        if arg in ('-i', '--interval') and args:
            try:
                interval = max(0.5, float(args.pop(0)))
            except ValueError:
                pass
        elif arg == '--once':
            once = True
        elif arg in ('-h', '--help'):
            print(__doc__)
            print('usage: sysmon-collect [--interval SECONDS] [--once]')
            return 0
        else:
            print('unknown argument: %s' % arg, file=sys.stderr)
            return 2

    collector = Collector(interval)
    if once:
        collector.run_once()
    else:
        collector.run_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
