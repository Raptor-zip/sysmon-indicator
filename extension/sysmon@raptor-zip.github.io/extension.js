/* Sysmon — GNOME Shell 42 用のシステムモニターインジケーター
 *
 * ~/.cache/sysmon/system.json（常駐する sysmon-collect が更新）を読んで、
 * トップバーに CPU / メモリ / GPU の使用率を出す。クリックで btop を圧縮したような
 * 内訳（コア別・メモリ・I/O・温度・上位プロセス）を開く。
 *
 * このファイルは /proc を一切読まない。重い採取は全部コレクタ側に置いて、
 * シェルのメインループではキャッシュを 1 つ読むだけにしてある。
 */
'use strict';

const { Clutter, Gio, GLib, GObject, Pango, St } = imports.gi;

const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;

const ExtensionUtils = imports.misc.extensionUtils;
const Me = ExtensionUtils.getCurrentExtension();

const CACHE_PATH = GLib.build_filenamev([
    GLib.get_user_cache_dir(), 'sysmon', 'system.json',
]);

const SERVICE = 'sysmon.service';

// キャッシュの更新が止まっていないかを見張る間隔
const TICK_SECONDS = 5;
// 採取間隔の何倍まで待って「古い」と判断するか
const STALE_FACTOR = 6;
const STALE_FLOOR_SECONDS = 15;

const WARN_PCT = 70;
const CRIT_PCT = 90;

const UNITS = ['B', 'K', 'M', 'G', 'T', 'P'];

function decodeBytes(bytes) {
    if (typeof TextDecoder !== 'undefined')
        return new TextDecoder('utf-8').decode(bytes);
    return imports.byteArray.toString(bytes);
}

function readCache() {
    try {
        const [ok, contents] = GLib.file_get_contents(CACHE_PATH);
        if (!ok)
            return null;
        return JSON.parse(decodeBytes(contents));
    } catch (e) {
        return null;
    }
}

function isNum(value) {
    return typeof value === 'number' && isFinite(value);
}

function formatPct(pct, digits = 0) {
    if (!isNum(pct))
        return '--%';
    return `${pct.toFixed(digits)}%`;
}

function formatBytes(bytes) {
    if (!isNum(bytes))
        return '--';
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < UNITS.length - 1) {
        value /= 1024;
        unit++;
    }
    const digits = (unit === 0 || value >= 100) ? 0 : 1;
    return `${value.toFixed(digits)}${UNITS[unit]}`;
}

function formatRate(bps) {
    if (!isNum(bps))
        return '--';
    return `${formatBytes(bps)}/s`;
}

function formatDuration(seconds) {
    if (!isNum(seconds))
        return '--';
    const total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days > 0)
        return `${days}日 ${hours}時間`;
    if (hours > 0)
        return `${hours}時間 ${minutes}分`;
    return `${minutes}分`;
}

function formatClock(epochSeconds) {
    if (!epochSeconds)
        return '--:--:--';
    const d = new Date(epochSeconds * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function severityClass(pct) {
    if (!isNum(pct))
        return '';
    if (pct >= CRIT_PCT)
        return 'sysmon-crit';
    if (pct >= WARN_PCT)
        return 'sysmon-warn';
    return '';
}

/* 使用率 1 件ぶんの行：見出し + パーセント + バー + 補足 */
const MeterRow = GObject.registerClass(
class MeterRow extends PopupMenu.PopupBaseMenuItem {
    _init(title) {
        super._init({ reactive: false, can_focus: false });

        const box = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'sysmon-row',
        });

        const header = new St.BoxLayout({ x_expand: true });
        this._title = new St.Label({
            text: title,
            x_expand: true,
            style_class: 'sysmon-row-title',
        });
        // GPU 名のような長い見出しでメニューが横に伸びないよう頭打ちにする
        this._title.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._value = new St.Label({ style_class: 'sysmon-row-value' });
        header.add_child(this._title);
        header.add_child(this._value);

        this._track = new St.Widget({
            style_class: 'sysmon-bar-track',
            layout_manager: new Clutter.BinLayout(),
            x_expand: true,
        });
        // 塗りは BoxLayout 越しに入れて必ず左端から伸ばす
        // （BinLayout に直接入れると幅を指定した子が中央に寄る）
        const fillBox = new St.BoxLayout({
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });
        this._fill = new St.Widget({
            style_class: 'sysmon-bar-fill',
            x_expand: false,
            y_expand: true,
        });
        fillBox.add_child(this._fill);
        this._track.add_child(fillBox);

        this._sub = new St.Label({ style_class: 'sysmon-row-sub' });
        this._sub.clutter_text.ellipsize = Pango.EllipsizeMode.END;

        box.add_child(header);
        box.add_child(this._track);
        box.add_child(this._sub);
        this.add_child(box);
    }

    setTitle(text) {
        this._title.text = text;
    }

    update(pct, valueText, subtitle) {
        const cls = severityClass(pct);

        this._value.text = valueText !== undefined && valueText !== null
            ? valueText : formatPct(pct);
        this._value.style_class = `sysmon-row-value ${cls}`.trim();

        const ratio = isNum(pct) ? Math.max(0, Math.min(100, pct)) : 0;
        // トラック幅は stylesheet.css で 300px 固定なので割合をそのまま px に換算
        this._fill.style = `width: ${Math.max(ratio * 3, ratio > 0 ? 3 : 0)}px;`;
        this._fill.style_class = `sysmon-bar-fill ${cls}`.trim();

        this._sub.text = subtitle || '';
        this._sub.visible = !!subtitle;
    }
});

/* コア別使用率の縦棒グリッド（btop のコアメーター相当） */
const CoreGrid = GObject.registerClass(
class CoreGrid extends PopupMenu.PopupBaseMenuItem {
    _init() {
        super._init({ reactive: false, can_focus: false });

        this._row = new St.BoxLayout({
            x_expand: true,
            style_class: 'sysmon-cores',
        });
        this.add_child(this._row);
        this._bars = [];
    }

    _resize(count) {
        while (this._bars.length > count)
            this._bars.pop().track.destroy();

        while (this._bars.length < count) {
            const track = new St.Widget({
                style_class: 'sysmon-core-track',
                layout_manager: new Clutter.BinLayout(),
                x_expand: true,
                x_align: Clutter.ActorAlign.FILL,
            });
            // 下端から伸ばすために、上に伸縮するだけの空き箱を積む
            const column = new St.BoxLayout({
                vertical: true,
                x_expand: true,
                y_expand: true,
                y_align: Clutter.ActorAlign.FILL,
                x_align: Clutter.ActorAlign.FILL,
            });
            column.add_child(new St.Widget({ y_expand: true }));
            const fill = new St.Widget({
                style_class: 'sysmon-core-fill',
                y_expand: false,
            });
            column.add_child(fill);
            track.add_child(column);
            this._row.add_child(track);
            this._bars.push({ track, fill });
        }
    }

    update(perCore) {
        const values = perCore || [];
        // 全トラックの自然幅を同じに保ち、親の余白を均等に分ける。
        // メニューの実際の幅と表示倍率に追従し、固定 px 幅は使わない。
        this._resize(values.length);
        // トラックの高さは stylesheet.css の 26px に合わせる
        values.forEach((pct, i) => {
            const ratio = isNum(pct) ? Math.max(0, Math.min(100, pct)) : 0;
            const bar = this._bars[i];
            bar.fill.style = `height: ${Math.max(2, Math.round(ratio * 0.26))}px;`;
            bar.fill.style_class = `sysmon-core-fill ${severityClass(pct)}`.trim();
        });
        this.visible = values.length > 0;
    }
});

/* 「ラベル : 値」を 1 行に並べるだけの補助行 */
const InfoRow = GObject.registerClass(
class InfoRow extends PopupMenu.PopupBaseMenuItem {
    _init() {
        super._init({ reactive: false, can_focus: false });

        this._box = new St.BoxLayout({
            x_expand: true,
            style_class: 'sysmon-info',
        });
        this.add_child(this._box);
        this._labels = [];
    }

    /* items: [{ label, value }] を横並びで均等に置く */
    update(items) {
        while (this._labels.length > items.length)
            this._labels.pop().box.destroy();

        while (this._labels.length < items.length) {
            const box = new St.BoxLayout({ vertical: true, x_expand: true });
            const key = new St.Label({ style_class: 'sysmon-info-key' });
            const value = new St.Label({ style_class: 'sysmon-info-value' });
            box.add_child(key);
            box.add_child(value);
            this._box.add_child(box);
            this._labels.push({ box, key, value });
        }

        items.forEach((item, i) => {
            this._labels[i].key.text = item.label;
            this._labels[i].value.text = item.value;
        });
        this.visible = items.length > 0;
    }
});

/* 上位プロセス 1 行：名前 / CPU% / RSS */
const ProcRow = GObject.registerClass(
class ProcRow extends PopupMenu.PopupBaseMenuItem {
    _init() {
        super._init({ reactive: false, can_focus: false });

        const box = new St.BoxLayout({ x_expand: true, style_class: 'sysmon-proc' });

        this._name = new St.Label({ x_expand: true, style_class: 'sysmon-proc-name' });
        this._name.clutter_text.ellipsize = Pango.EllipsizeMode.END;

        this._pid = new St.Label({ style_class: 'sysmon-proc-pid' });
        this._cpu = new St.Label({ style_class: 'sysmon-proc-num' });
        this._mem = new St.Label({ style_class: 'sysmon-proc-num' });

        box.add_child(this._name);
        box.add_child(this._pid);
        box.add_child(this._cpu);
        box.add_child(this._mem);
        this.add_child(box);
    }

    update(proc, gpu = false) {
        if (!proc) {
            this.visible = false;
            return;
        }
        this.visible = true;
        this._name.text = proc.label || proc.name || '?';
        this._pid.text = String(proc.pid);
        const pct = gpu ? proc.gpu : proc.cpu;
        this._cpu.text = formatPct(pct, pct < 10 ? 1 : 0);
        // コア 1 個ぶんを超えたら色を付ける（btop と同じで 100% = 1 コア）
        this._cpu.style_class = `sysmon-proc-num ${severityClass(
            isNum(pct) ? Math.min(100, pct) : 0)}`.trim();
        this._mem.text = formatBytes(gpu ? proc.vram : proc.rss);
    }
});

const SysmonIndicator = GObject.registerClass(
class SysmonIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Sysmon', false);

        this._sortBy = 'cpu';
        this._lastSampledAt = null;

        const panel = new St.BoxLayout({
            style_class: 'sysmon-panel',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._cpuLabel = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'sysmon-panel-label',
        });
        this._memLabel = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'sysmon-panel-label',
        });
        this._gpuLabel = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'sysmon-panel-label',
        });
        panel.add_child(this._cpuLabel);
        panel.add_child(this._memLabel);
        panel.add_child(this._gpuLabel);
        this.add_child(panel);

        this._buildMenu();

        this.menu.connect('open-state-changed', (menu, isOpen) => {
            if (isOpen)
                this._render(true);
        });

        this._startMonitor();
        this._startTicker();
        this._render(true);
    }

    _buildMenu() {
        this._cpuRow = new MeterRow('CPU');
        this.menu.addMenuItem(this._cpuRow);

        this._coreGrid = new CoreGrid();
        this.menu.addMenuItem(this._coreGrid);

        this._memRow = new MeterRow('メモリ');
        this.menu.addMenuItem(this._memRow);

        this._swapRow = new MeterRow('スワップ');
        this.menu.addMenuItem(this._swapRow);

        this._gpuRow = new MeterRow('GPU');
        this.menu.addMenuItem(this._gpuRow);

        this._ioRow = new InfoRow();
        this.menu.addMenuItem(this._ioRow);

        this._fsRow = new InfoRow();
        this.menu.addMenuItem(this._fsRow);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this.menu.addMenuItem(this._buildProcHeader());
        this._procMessage = new PopupMenu.PopupMenuItem('', {
            reactive: false, can_focus: false, style_class: 'sysmon-status',
        });
        this._procMessage.visible = false;
        this.menu.addMenuItem(this._procMessage);
        this._procRows = [];
        for (let i = 0; i < 6; i++) {
            const row = new ProcRow();
            this.menu.addMenuItem(row);
            this._procRows.push(row);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._statusItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
            style_class: 'sysmon-status',
        });
        this.menu.addMenuItem(this._statusItem);

        this._startItem = new PopupMenu.PopupMenuItem('サービスを起動');
        this._startItem.connect('activate', () => this._startService());
        this._startItem.visible = false;
        this.menu.addMenuItem(this._startItem);

        if (GLib.find_program_in_path('btop')) {
            const btop = new PopupMenu.PopupMenuItem('btop を開く');
            btop.connect('activate', () => this._launchBtop());
            this.menu.addMenuItem(btop);
        }
    }

    /* 並び順の切り替えボタンを持つプロセス欄の見出し。
     * PopupMenuItem にすると押した瞬間にメニューが閉じてしまうので、
     * 反応しない行の中に St.Button を置いて自前で受ける。 */
    _buildProcHeader() {
        const item = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        const box = new St.BoxLayout({ x_expand: true, style_class: 'sysmon-proc-head' });

        this._procTitle = new St.Label({
            text: '上位プロセス',
            x_expand: true,
            style_class: 'sysmon-row-title',
        });
        box.add_child(this._procTitle);

        this._sortButtons = [
            { key: 'cpu', button: new St.Button({ label: 'CPU', style_class: 'sysmon-sort' }) },
            { key: 'mem', button: new St.Button({ label: 'メモリ', style_class: 'sysmon-sort' }) },
            { key: 'gpu', button: new St.Button({ label: 'GPU', style_class: 'sysmon-sort' }) },
        ];
        this._sortButtons.forEach(entry => {
            entry.button.connect('clicked', () => {
                this._sortBy = entry.key;
                this._render(true);
            });
            box.add_child(entry.button);
        });

        item.add_child(box);
        return item;
    }

    _syncSortButtons() {
        this._sortButtons.forEach(entry => {
            entry.button.style_class = entry.key === this._sortBy
                ? 'sysmon-sort sysmon-sort-active' : 'sysmon-sort';
        });
    }

    _startMonitor() {
        try {
            const file = Gio.File.new_for_path(CACHE_PATH);
            this._monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._monitorId = this._monitor.connect('changed', () => this._render(false));
        } catch (e) {
            logError(e, 'sysmon: ファイル監視を開始できません');
        }
    }

    /* ファイル監視が取りこぼしても最終的には追いつくよう、定期的にも読み直す。
     * 更新が止まったことに気付く役目も兼ねる。 */
    _startTicker() {
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._render(false);
            return GLib.SOURCE_CONTINUE;
        });
    }

    _startService() {
        try {
            Gio.Subprocess.new(
                ['systemctl', '--user', 'start', SERVICE],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
            );
        } catch (e) {
            logError(e, 'sysmon: サービスを起動できません');
        }
    }

    _launchBtop() {
        try {
            // 端末エミュレータの選択はデスクトップ既定に任せる
            const app = Gio.AppInfo.create_from_commandline(
                'btop', 'btop', Gio.AppInfoCreateFlags.NEEDS_TERMINAL);
            app.launch([], null);
        } catch (e) {
            logError(e, 'sysmon: btop を起動できません');
        }
    }

    _renderPanel(data, stale) {
        const cpu = (data && data.cpu) || {};
        const mem = (data && data.mem) || {};

        const gpu = (data && data.gpu) || {};
        const gpuPct = gpu.state === 'suspended' ? 0 : gpu.util;
        this._cpuLabel.text = `CPU ${formatPct(cpu.pct)}`;
        this._memLabel.text = `MEM ${formatPct(mem.pct)}`;
        this._gpuLabel.text = `GPU ${formatPct(gpuPct)}`;

        const dim = stale ? ' sysmon-stale' : '';
        this._cpuLabel.style_class =
            `sysmon-panel-label ${severityClass(cpu.pct)}${dim}`.trim();
        this._memLabel.style_class =
            `sysmon-panel-label ${severityClass(mem.pct)}${dim}`.trim();
        this._gpuLabel.style_class =
            `sysmon-panel-label ${severityClass(gpuPct)}${dim}`.trim();
    }

    _renderCpu(data) {
        const cpu = data.cpu || {};
        const thermal = data.thermal || {};

        const parts = [];
        if (Array.isArray(cpu.load))
            parts.push(`負荷 ${cpu.load.map(v => v.toFixed(2)).join(' ')}`);
        if (isNum(cpu.freq_mhz))
            parts.push(`${(cpu.freq_mhz / 1000).toFixed(2)}GHz`);
        if (isNum(thermal.cpu))
            parts.push(`${Math.round(thermal.cpu)}°C`);
        // 「ファン」と書くと 1 行に収まらなくなるので rpm だけで分からせる
        if (thermal.fans && thermal.fans.length)
            parts.push(`${thermal.fans.join('/')}rpm`);

        this._cpuRow.setTitle(`CPU（${cpu.count || 0} スレッド）`);
        this._cpuRow.update(cpu.pct, formatPct(cpu.pct, 1), parts.join('  ·  '));
        this._coreGrid.update(cpu.per_core);
    }

    _renderMemory(data) {
        const mem = data.mem || {};
        this._memRow.update(
            mem.pct,
            `${formatBytes(mem.used)} / ${formatBytes(mem.total)}`,
            `キャッシュ ${formatBytes(mem.cached)}  ·  空き ${formatBytes(mem.available)}`
        );

        if (mem.swap_total > 0) {
            this._swapRow.visible = true;
            this._swapRow.update(
                mem.swap_pct,
                `${formatBytes(mem.swap_used)} / ${formatBytes(mem.swap_total)}`,
                ''
            );
        } else {
            this._swapRow.visible = false;
        }
    }

    _renderGpu(data) {
        const gpu = data.gpu;
        if (!gpu || !gpu.present) {
            this._gpuRow.visible = false;
            return;
        }
        this._gpuRow.visible = true;

        if (gpu.state !== 'active') {
            // 眠っている dGPU は起こさない。詳しく見たいときは何かで使えば起きる
            const state = gpu.state === 'suspended' ? 'サスペンド中' : gpu.state;
            this._gpuRow.setTitle('GPU');
            this._gpuRow.update(null, state, '使用中でないため未計測');
            return;
        }

        this._gpuRow.setTitle(gpu.name ? `GPU（${gpu.name}）` : 'GPU');
        const parts = [];
        if (isNum(gpu.mem_used) && isNum(gpu.mem_total)) {
            parts.push(`VRAM ${formatBytes(gpu.mem_used)} / ${formatBytes(gpu.mem_total)}` +
                (isNum(gpu.mem_pct) ? `（${formatPct(gpu.mem_pct)}）` : ''));
        }
        if (isNum(gpu.temp))
            parts.push(`${Math.round(gpu.temp)}°C`);
        if (isNum(gpu.power))
            parts.push(`${gpu.power.toFixed(0)}W`);
        this._gpuRow.update(gpu.util, formatPct(gpu.util), parts.join('  ·  '));
    }

    _renderIo(data) {
        const disk = data.disk || {};
        const net = data.net || {};
        this._ioRow.update([
            { label: 'ディスク 読み', value: formatRate(disk.read_bps) },
            { label: '書き', value: formatRate(disk.write_bps) },
            { label: `${net.iface || 'ネット'} ↓`, value: formatRate(net.rx_bps) },
            { label: '↑', value: formatRate(net.tx_bps) },
        ]);

        const fs = (data.fs || []).slice(0, 3);
        this._fsRow.update(fs.map(entry => ({
            label: entry.mount,
            value: `${formatPct(entry.pct)}（空き ${formatBytes(entry.free)}）`,
        })));
    }

    _renderProcs(data) {
        const procs = data.procs || {};
        const gpu = data.gpu || {};
        const showGpu = this._sortBy === 'gpu';
        const list = (showGpu ? gpu.processes :
            (this._sortBy === 'mem' ? procs.by_mem : procs.by_cpu)) || [];
        this._procTitle.text = showGpu ? 'GPUプロセス' : '上位プロセス';
        let message = '';
        if (showGpu) {
            if (!gpu.present)
                message = '対応するGPUがありません';
            else if (gpu.state === 'suspended')
                message = 'GPUはサスペンド中です';
            else if (gpu.state !== 'active' || !Array.isArray(gpu.processes))
                message = 'GPUプロセス情報を取得できません';
            else if (!list.length)
                message = 'GPUを使用しているプロセスはありません';
            else
                message = 'PID / GPU% / VRAM（--: 取得不可）';
        }
        this._procMessage.label.text = message;
        this._procMessage.visible = !!message;
        this._procRows.forEach((row, i) => row.update(list[i], showGpu));
        this._syncSortButtons();
    }

    _render(force) {
        const data = readCache();

        if (!data) {
            this._renderPanel(null, true);
            this._statusItem.label.text =
                'キャッシュがありません（sysmon.service が動いていない可能性）';
            this._startItem.visible = true;
            return;
        }

        const interval = isNum(data.interval) ? data.interval : 2;
        const age = Date.now() / 1000 - (data.sampled_at || 0);
        const stale = age > Math.max(STALE_FLOOR_SECONDS, interval * STALE_FACTOR);

        this._renderPanel(data, stale);
        this._startItem.visible = stale;

        // メニューが閉じているあいだは中身を組み直さない
        if (!force && !this.menu.isOpen) {
            this._lastSampledAt = data.sampled_at;
            return;
        }
        if (!force && data.sampled_at === this._lastSampledAt)
            return;
        this._lastSampledAt = data.sampled_at;

        this._renderCpu(data);
        this._renderMemory(data);
        this._renderGpu(data);
        this._renderIo(data);
        this._renderProcs(data);

        const procs = data.procs || {};
        const status = [
            `稼働 ${formatDuration(data.uptime)}`,
            `プロセス ${procs.total || 0}・スレッド ${procs.threads || 0}`,
            `更新 ${formatClock(data.sampled_at)}`,
        ];
        if (stale)
            status.push('（更新が止まっています）');
        this._statusItem.label.text = status.join('　');
    }

    destroy() {
        if (this._tickId) {
            GLib.source_remove(this._tickId);
            this._tickId = 0;
        }
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
        }
        super.destroy();
    }
});

class Extension {
    constructor(uuid) {
        this._uuid = uuid;
        this._indicator = null;
    }

    enable() {
        this._indicator = new SysmonIndicator();
        // 右側の枠の左端（システムメニューの左隣）に置く。
        // 時計の右に並べたいなら 'right' を 'center' に変える
        Main.panel.addToStatusArea(this._uuid, this._indicator, 0, 'right');
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}

function init(meta) {
    return new Extension(meta.uuid);
}
