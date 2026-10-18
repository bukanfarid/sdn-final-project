#!/usr/bin/env python3
"""
analyze.py — mengolah hasil eksperimen energy-aware SDN
Input : results/raw/<mode>/<skenario>/run<N>/
Output: results/per_run.csv, results/summary.csv, results/saving.csv, figures/*.png
"""
import json
import re
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BASE = Path(__file__).resolve().parent.parent
RAW = BASE / 'results' / 'raw'
RES = BASE / 'results'
FIG = BASE / 'figures'
FIG.mkdir(exist_ok=True)

# ===== Profil daya (W): (P_chassis, P_port) =====
# Profil 'utama' WAJIB diganti dengan nilai dari referensi yang dikutip.
# Profil lain untuk analisis sensitivitas.
PROFILES = {
    'utama':           (100.0, 1.0),   # PLACEHOLDER
    'chassis_dominan': (150.0, 0.5),
    'seimbang':        (50.0, 5.0),
    'port_dominan':    (10.0, 10.0),
}
MAIN = 'utama'
MODES = ['baseline_sp', 'baseline_static', 'energy']
LABEL = {'baseline_sp': 'Shortest path', 'baseline_static': 'Static LB', 'energy': 'Energy-aware'}
SCENARIOS = ['S1', 'S2', 'S3']
PING_INTERVAL = 0.1

PING_RE = re.compile(r'^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+) ms')
SUM_RE = re.compile(r'(\d+) packets transmitted, (\d+) received')
MEM_RE = r'([\d.]+)\s*([KMG]i?B)'
UNIT = {'KiB': 1 / 1024, 'KB': 1 / 1000, 'MiB': 1, 'MB': 1, 'GiB': 1024, 'GB': 1000}


def read_iperf(path):
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
        r = d['end']['sum_received']
        return {'start': d['start']['timestamp']['timesecs'], 'bytes': r['bytes'],
                'seconds': r['seconds'], 'mbps': r['bits_per_second'] / 1e6}
    except (json.JSONDecodeError, KeyError):
        return None


def read_ping(path):
    if not path.exists():
        return None
    rtts, seqs, sent, recv = [], [], None, None
    for line in path.read_text().splitlines():
        m = PING_RE.search(line)
        if m:
            seqs.append(int(m.group(2)))
            rtts.append(float(m.group(3)))
        s = SUM_RE.search(line)
        if s:
            sent, recv = int(s.group(1)), int(s.group(2))
    if not rtts:
        return None
    r = pd.Series(rtts)
    gaps = pd.Series(seqs).diff().fillna(1)
    return {'delay_ms': r.mean(),
            'jitter_ms': r.diff().abs().mean() if len(r) > 1 else 0.0,
            'loss_pct': (1 - recv / sent) * 100 if sent else float('nan'),
            'max_outage_s': (gaps.max() - 1) * PING_INTERVAL}


def read_stats(path):
    if not path.exists():
        return {}
    df = pd.read_csv(path, header=None, names=['t', 'name', 'cpu', 'mem'])
    ryu = df[df.name.str.contains(r'\.Ryu-1\.')]
    if not len(ryu):
        return {}
    mem = ryu.mem.str.split('/').str[0].str.extract(MEM_RE)
    return {'ryu_cpu_pct': ryu.cpu.str.rstrip('%').astype(float).mean(),
            'ryu_mem_mib': (mem[0].astype(float) * mem[1].map(UNIT)).mean()}


def analyze_run(mode, sc, rd):
    c1 = read_iperf(rd / 'iperf_c1.json')
    if c1 is None or not (rd / 'ctrl.csv').exists():
        print('  dilewati (data tidak lengkap):', rd)
        return None
    c2 = read_iperf(rd / 'iperf_c2.json')
    t0, dur = c1['start'], c1['seconds']
    ctrl = pd.read_csv(rd / 'ctrl.csv')
    w = ctrl[(ctrl.t >= t0) & (ctrl.t <= t0 + dur)].copy()
    if len(w) < 2:
        print('  dilewati (log controller kosong di jendela ukur):', rd)
        return None
    w['dt'] = w.t.diff().fillna(w.t.diff().median())
    total_bytes = c1['bytes'] + (c2['bytes'] if c2 else 0)
    row = {'mode': mode, 'scenario': sc, 'run': rd.name,
           'c1_mbps': c1['mbps'],
           'c2_mbps': c2['mbps'] if c2 else float('nan'),
           'agg_mbps': total_bytes * 8 / dur / 1e6,
           'ctrl_msgs': w.ctrl_msgs.iloc[-1] - w.ctrl_msgs.iloc[0],
           'stats_msgs': w.stats_msgs.iloc[-1] - w.stats_msgs.iloc[0],
           'sleep_pct': (w.active_switches < 4).mean() * 100}
    for name, (pc, pp) in PROFILES.items():
        e = ((w.active_switches * pc + w.active_ports * pp) * w.dt).sum()
        row[f'energy_J_{name}'] = e
        row[f'eff_bitJ_{name}'] = total_bytes * 8 / e if e > 0 else float('nan')
    for tag in ('c1', 'c2'):
        p = read_ping(rd / f'ping_{tag}.txt')
        if p:
            row.update({f'{tag}_{k}': v for k, v in p.items()})
    row.update(read_stats(rd / 'docker_stats.csv'))
    return row, w.assign(t_rel=w.t - t0)


def bar(df, col, ylabel, fname):
    if col not in df.columns:
        return
    g = df.groupby(['scenario', 'mode'])[col].agg(['mean', 'std']).fillna(0)
    fig, ax = plt.subplots(figsize=(6, 3.5))
    width = 0.25
    for i, mode in enumerate(MODES):
        vals = [g.loc[(sc, mode), 'mean'] if (sc, mode) in g.index else 0 for sc in SCENARIOS]
        errs = [g.loc[(sc, mode), 'std'] if (sc, mode) in g.index else 0 for sc in SCENARIOS]
        xs = [j + (i - 1) * width for j in range(len(SCENARIOS))]
        ax.bar(xs, vals, width, yerr=errs, capsize=3, label=LABEL[mode])
    ax.set_xticks(range(len(SCENARIOS)))
    ax.set_xticklabels(SCENARIOS)
    ax.set_ylabel(ylabel)
    ax.legend(fontsize=8)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG / fname, dpi=200)
    plt.close(fig)


def main():
    rows, series = [], {}
    for mode in MODES:
        for sc in SCENARIOS:
            for rd in sorted((RAW / mode / sc).glob('run*')):
                res = analyze_run(mode, sc, rd)
                if res:
                    rows.append(res[0])
                    series[(mode, sc, rd.name)] = res[1]
    if not rows:
        print('Tidak ada data. Jalankan run_experiment.sh dulu.')
        return

    df = pd.DataFrame(rows)
    df.to_csv(RES / 'per_run.csv', index=False)
    metrics = [c for c in df.columns if c not in ('mode', 'scenario', 'run')]
    df.groupby(['scenario', 'mode'])[metrics].agg(['mean', 'std', 'count']).to_csv(RES / 'summary.csv')

    # penghematan energi, perubahan efisiensi & throughput vs tiap baseline, per profil daya
    mean = df.groupby(['scenario', 'mode']).mean(numeric_only=True)
    sav = []
    for sc in SCENARIOS:
        for base in ('baseline_sp', 'baseline_static'):
            if (sc, base) not in mean.index or (sc, 'energy') not in mean.index:
                continue
            b, e = mean.loc[(sc, base)], mean.loc[(sc, 'energy')]
            for name in PROFILES:
                sav.append({'scenario': sc, 'vs': base, 'profile': name,
                            'energy_saving_pct': (b[f'energy_J_{name}'] - e[f'energy_J_{name}']) / b[f'energy_J_{name}'] * 100,
                            'efficiency_gain_pct': (e[f'eff_bitJ_{name}'] - b[f'eff_bitJ_{name}']) / b[f'eff_bitJ_{name}'] * 100,
                            'throughput_change_pct': (e['agg_mbps'] - b['agg_mbps']) / b['agg_mbps'] * 100})
    sav = pd.DataFrame(sav)
    sav.to_csv(RES / 'saving.csv', index=False)

    # grafik perbandingan
    bar(df, f'energy_J_{MAIN}', 'Estimated energy (J)', 'energy.png')
    bar(df, f'eff_bitJ_{MAIN}', 'Energy efficiency (bit/J)', 'efficiency.png')
    bar(df, 'agg_mbps', 'Aggregate throughput (Mbps)', 'throughput.png')
    bar(df, 'c1_delay_ms', 'RTT Client1 (ms)', 'delay.png')
    bar(df, 'c1_jitter_ms', 'Jitter Client1 (ms)', 'jitter.png')
    bar(df, 'c1_loss_pct', 'Packet loss Client1 (%)', 'loss.png')
    bar(df, 'c2_max_outage_s', 'Max outage Client2 (s)', 'outage.png')
    bar(df, 'ctrl_msgs', 'Control messages', 'overhead.png')

    # grafik deret waktu: contoh mode energy di S3
    key = next((k for k in sorted(series) if k[0] == 'energy' and k[1] == 'S3'), None)
    if key:
        w = series[key]
        pc, pp = PROFILES[MAIN]
        fig, ax1 = plt.subplots(figsize=(7, 3.5))
        ax1.plot(w.t_rel, w.rate_a_mbps, label='Path A')
        ax1.plot(w.t_rel, w.rate_b_mbps, label='Path B')
        ax1.set_xlabel('Time (s)')
        ax1.set_ylabel('Link rate (Mbps)')
        ax2 = ax1.twinx()
        ax2.step(w.t_rel, w.active_switches * pc + w.active_ports * pp, 'k--', where='post', label='Power')
        ax2.set_ylabel('Estimated power (W)')
        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, fontsize=8, loc='upper right')
        fig.tight_layout()
        fig.savefig(FIG / 'timeseries_energy_S3.png', dpi=200)
        plt.close(fig)

    print(f'{len(df)} run diolah.')
    if len(sav):
        print('\nPenghematan energi (profil utama):')
        print(sav[sav.profile == MAIN].round(2).to_string(index=False))
    print(f'\nTabel: {RES}\nGrafik: {FIG}')


if __name__ == '__main__':
    main()
