#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-process an Experiment 1 run into paper-style deadline plots.

This reads the raw hey CSV files, maps non-2xx responses to a synthetic
deadline latency, and replots the experiment without modifying the original
measurement outputs.
"""

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import shutil
import sys


LATENCY_FIELDS = ("p50_us", "p90_us", "p99_us", "p99_9_us")
CPU_PLOT_SCALE = 100.0
TS_FIELDS = [
    "phase", "algo", "servers", "load_frac", "load_pct", "target_qps",
    "workers", "bin_seconds", "bin_index", "timestamp", "elapsed_s",
    "p50_us", "p90_us", "p99_us", "p99_9_us", "avg_us",
    "total_responses", "ok_responses", "error_responses", "response_qps",
    "error_qps", "status_dist",
]
PHASE_FIELDS = [
    "phase", "algo", "servers", "load_frac", "load_pct", "target_qps",
    "workers", "duration_s", "started", "ended", "total_responses",
    "ok_responses", "error_responses", "error_pct", "response_qps",
    "error_qps", "p50_us", "p90_us", "p99_us", "p99_9_us", "avg_us",
    "status_dist",
]
CPU_FIELDS = [
    "phase", "algo", "servers", "load_frac", "load_pct", "target_qps",
    "host", "server_index", "timestamp", "elapsed_s", "host_cpu_pct",
]


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = max(1, int(math.ceil((q / 100.0) * len(sorted_vals))))
    return sorted_vals[min(k - 1, len(sorted_vals) - 1)]


def parse_ts(value):
    return dt.datetime.fromisoformat(value)


def fmt_float(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return "%.12g" % value
    return value


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: fmt_float(row.get(k, "")) for k in fields})


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def cpu_plot_points(cpu_rows):
    cx, cy, cc = [], [], []
    for r in cpu_rows:
        try:
            cx.append(parse_ts(r["timestamp"]))
            cy.append(float(r["host_cpu_pct"]) * CPU_PLOT_SCALE)
            cc.append(int(float(r.get("server_index") or 0)))
        except (ValueError, KeyError):
            continue
    return cx, cy, cc


def finish_cpu_axis(ax, fig, scatter, cx, cy, phases):
    ax.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    ax.set_ylabel("Host CPU\n(core-normalized %)")
    fig.colorbar(scatter, ax=ax, pad=0.004, label="server id")

    top = 100.0
    if cy:
        top = max(top, math.ceil(max(cy) * 1.15 / 25.0) * 25.0)
    ax.set_ylim(0, top)

    if phases and cx:
        end = parse_ts(phases[-1]["ended"])
        last = max(cx)
        if (end - last).total_seconds() > 30:
            ax.axvspan(last, end, facecolor="none", hatch="///",
                       edgecolor="#bdbdbd", linewidth=0.0, alpha=0.45,
                       zorder=3)
            mid = last + (end - last) / 2
            ax.text(mid, top * 0.92, "CPU not sampled", ha="center",
                    va="top", fontsize=9, color="#666666", zorder=4)


def raw_csv_for_phase(run_dir, phase):
    pattern = os.path.join(run_dir, "raw", "phase%02d_*.csv" % int(phase["phase"]))
    matches = sorted(p for p in glob.glob(pattern) if not p.endswith(".stderr"))
    if not matches:
        raise FileNotFoundError("no raw CSV for phase %s (%s)" %
                                (phase.get("phase"), pattern))
    return matches[0]


def build_deadline_phase(run_dir, phase, run_start, bin_seconds, deadline_us):
    started = parse_ts(phase["started"])
    duration = int(float(phase["duration_s"]))
    bins = {}
    total = ok = err = 0
    all_lats = []
    status_dist = {}

    with open(raw_csv_for_phase(run_dir, phase), newline="") as f:
        reader = csv.reader(f)
        for parts in reader:
            try:
                rt_s = float(parts[0])
            except (ValueError, IndexError):
                continue
            try:
                code = int(float(parts[6]))
            except (ValueError, IndexError):
                code = 0
            try:
                offset = float(parts[7])
            except (ValueError, IndexError):
                offset = 0.0

            idx = max(0, int(offset // bin_seconds))
            b = bins.setdefault(idx, {"lats": [], "total": 0, "ok": 0,
                                      "err": 0, "dist": {}})
            total += 1
            b["total"] += 1
            key = str(code) if code else "unknown"
            status_dist[key] = status_dist.get(key, 0) + 1
            b["dist"][key] = b["dist"].get(key, 0) + 1

            if 200 <= code < 300:
                ok += 1
                b["ok"] += 1
                lat_us = rt_s * 1000000.0
            else:
                err += 1
                b["err"] += 1
                lat_us = float(deadline_us)
            b["lats"].append(lat_us)
            all_lats.append(lat_us)

    rows = []
    n_bins = int(math.ceil(duration / float(bin_seconds)))
    for idx in range(n_bins):
        b = bins.get(idx, {"lats": [], "total": 0, "ok": 0, "err": 0,
                           "dist": {}})
        lats = sorted(b["lats"])
        ts = started + dt.timedelta(seconds=min(duration,
                                                (idx + 0.5) * bin_seconds))
        rows.append({
            "phase": phase["phase"],
            "algo": phase["algo"],
            "servers": phase["servers"],
            "load_frac": phase["load_frac"],
            "load_pct": phase["load_pct"],
            "target_qps": phase["target_qps"],
            "workers": phase["workers"],
            "bin_seconds": bin_seconds,
            "bin_index": idx,
            "timestamp": ts.isoformat(timespec="seconds"),
            "elapsed_s": (ts - run_start).total_seconds(),
            "p50_us": percentile(lats, 50),
            "p90_us": percentile(lats, 90),
            "p99_us": percentile(lats, 99),
            "p99_9_us": percentile(lats, 99.9),
            "avg_us": (sum(lats) / len(lats)) if lats else None,
            "total_responses": b["total"],
            "ok_responses": b["ok"],
            "error_responses": b["err"],
            "response_qps": b["total"] / float(bin_seconds),
            "error_qps": b["err"] / float(bin_seconds),
            "status_dist": json.dumps(b["dist"], sort_keys=True),
        })

    all_lats.sort()
    summary = {
        "phase": phase["phase"],
        "algo": phase["algo"],
        "servers": phase["servers"],
        "load_frac": phase["load_frac"],
        "load_pct": phase["load_pct"],
        "target_qps": phase["target_qps"],
        "workers": phase["workers"],
        "duration_s": duration,
        "started": phase["started"],
        "ended": phase["ended"],
        "total_responses": total,
        "ok_responses": ok,
        "error_responses": err,
        "error_pct": (100.0 * err / total) if total else None,
        "response_qps": total / float(duration) if duration else None,
        "error_qps": err / float(duration) if duration else None,
        "p50_us": percentile(all_lats, 50),
        "p90_us": percentile(all_lats, 90),
        "p99_us": percentile(all_lats, 99),
        "p99_9_us": percentile(all_lats, 99.9),
        "avg_us": (sum(all_lats) / len(all_lats)) if all_lats else None,
        "status_dist": json.dumps(status_dist, sort_keys=True),
    }
    return rows, summary


def smooth_rows(rows, window):
    if window <= 1:
        return [dict(r) for r in rows]
    out = [dict(r) for r in rows]
    half = window // 2
    by_phase = {}
    for idx, row in enumerate(rows):
        by_phase.setdefault(row["phase"], []).append(idx)

    for indices in by_phase.values():
        for metric in LATENCY_FIELDS + ("avg_us", "error_qps"):
            vals = []
            for idx in indices:
                raw = rows[idx].get(metric)
                vals.append(float(raw) if raw not in (None, "", "None") else math.nan)
            for local_i, idx in enumerate(indices):
                lo = max(0, local_i - half)
                hi = min(len(vals), local_i + half + 1)
                chunk = [v for v in vals[lo:hi] if not math.isnan(v)]
                out[idx][metric] = (sum(chunk) / len(chunk)) if chunk else None
    return out


def load_cpu_rows(run_dir):
    path = os.path.join(run_dir, "host_cpu_timeseries.csv")
    if not os.path.exists(path):
        return []
    rows = read_csv(path)
    return rows


def plot(rows, phases, cpu_rows, out_path, smooth_window, deadline_us):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        raise RuntimeError("matplotlib unavailable: %s" % e)

    has_cpu = bool(cpu_rows)
    nrows = 3 if has_cpu else 2
    fig, axes = plt.subplots(nrows, 1, figsize=(14, 12 if has_cpu else 9),
                             sharex=True,
                             gridspec_kw={"height_ratios": [1.1, 1.0, 0.9][:nrows]})
    if nrows == 2:
        ax1, ax2 = axes
        ax3 = None
    else:
        ax1, ax2, ax3 = axes

    xs = [parse_ts(r["timestamp"]) for r in rows]

    def series(metric):
        vals = []
        for r in rows:
            v = r.get(metric)
            vals.append(float(v) if v not in (None, "", "None") else math.nan)
        return vals

    def fmt_us_tick(x, _pos):
        if x >= 1000000:
            return ("%gM" % (x / 1000000.0)).replace(".0", "")
        if x >= 1000:
            return ("%gk" % (x / 1000.0)).replace(".0", "")
        return "%g" % x

    axes_list = [ax for ax in (ax1, ax2, ax3) if ax is not None]
    for p in phases:
        start = parse_ts(p["started"])
        end = parse_ts(p["ended"])
        for ax in axes_list:
            if p["algo"] == "wrr":
                ax.axvspan(start, end, color="#eeeeee", alpha=0.9, lw=0)
            ax.axvline(start, color="#8a8a8a", lw=0.8, alpha=0.55)

    alloc_phase = next((p for p in phases if float(p["load_frac"]) > 1.0), None)
    if alloc_phase:
        boundary = parse_ts(alloc_phase["started"])
        for ax in axes_list:
            ax.axvline(boundary, color="#b8b8b8", lw=5, ls="--", alpha=0.9)

    suffix = "smoothed %d-bin" % smooth_window if smooth_window > 1 else "raw"
    ax1.plot(xs, series("p50_us"), color="#ff3333", lw=1.7,
             label="Query Latency 50pct (deadline mapped)")
    ax1.plot(xs, series("p90_us"), color="#35c94a", lw=1.7,
             label="Query Latency 90pct (deadline mapped)")
    ax1.plot(xs, series("p99_us"), color="#2f9bff", lw=1.7,
             label="Query Latency 99pct (deadline mapped)")
    ax1.plot(xs, series("p99_9_us"), color="#d642ff", lw=1.7,
             label="Query Latency 99.9pct (deadline mapped)")
    ax1.set_yscale("log")
    latency_vals = [float(r[m]) for r in rows for m in LATENCY_FIELDS
                    if r.get(m) not in (None, "", "None")]
    ymax = max(latency_vals + [deadline_us]) * 1.15 if latency_vals else deadline_us
    ymin = max(1000.0, min(latency_vals) * 0.7) if latency_vals else 10000.0
    ticks = [1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000,
             500000, 1000000, 2000000, 5000000]
    ax1.set_ylim(ymin, ymax)
    ax1.set_yticks([t for t in ticks if ymin <= t <= ymax * 1.05])
    ax1.yaxis.set_major_formatter(FuncFormatter(fmt_us_tick))
    ax1.grid(True, which="major", ls="--", color="#bfbfbf", alpha=0.75)
    ax1.legend(loc="upper center", ncol=2, fontsize=8, frameon=False)
    ax1.set_ylabel("Latency (usec)")
    ax1.text(0.5, -0.24, "(a) Tail Latency", transform=ax1.transAxes,
             ha="center", va="center", fontsize=22, fontweight="bold")

    ax2.plot(xs, series("error_qps"), color="#ff3333", lw=1.8, label="QPS")
    ax2.set_ylim(bottom=0)
    ax2.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    ax2.set_ylabel("Error QPS")
    ax2.legend(loc="upper right", fontsize=10, frameon=False)
    ax2.text(0.5, -0.22, "(b) Errors", transform=ax2.transAxes,
             ha="center", va="center", fontsize=22, fontweight="bold")

    if ax3 is not None:
        cx, cy, cc = cpu_plot_points(cpu_rows)
        scatter = ax3.scatter(cx, cy, c=cc, s=7, cmap="autumn_r", alpha=0.65,
                              edgecolors="none")
        finish_cpu_axis(ax3, fig, scatter, cx, cy, phases)
        ax3.text(0.5, -0.28, "(c) Distribution of Host CPU Utilization",
                 transform=ax3.transAxes, ha="center", va="center",
                 fontsize=22, fontweight="bold")

    if phases:
        ax2.set_xlim(parse_ts(phases[0]["started"]), parse_ts(phases[-1]["ended"]))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

    loads = []
    for p in phases:
        if p["load_frac"] not in loads:
            loads.append(p["load_frac"])
    for load in loads:
        group = [p for p in phases if p["load_frac"] == load]
        start = parse_ts(group[0]["started"])
        end = parse_ts(group[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.13, "%d%%" % int(round(float(load) * 100)),
                 transform=ax1.get_xaxis_transform(), ha="center", va="bottom",
                 fontsize=16, fontweight="bold", clip_on=False)

    below = [p for p in phases if float(p["load_frac"]) < 1.0]
    above = [p for p in phases if float(p["load_frac"]) > 1.0]
    if below:
        start = parse_ts(below[0]["started"])
        end = parse_ts(below[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.31, "Below Alloc <-",
                 transform=ax1.get_xaxis_transform(), ha="center",
                 va="bottom", fontsize=19, fontweight="bold", clip_on=False)
    if above:
        start = parse_ts(above[0]["started"])
        end = parse_ts(above[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.31, "-> Above Alloc",
                 transform=ax1.get_xaxis_transform(), ha="center",
                 va="bottom", fontsize=19, fontweight="bold", clip_on=False)

    fig.suptitle("Experiment 1 Deadline Postprocess | non-2xx = %.1fs | %s"
                 % (deadline_us / 1000000.0, suffix),
                 y=0.985, fontsize=14)
    fig.tight_layout(rect=[0.03, 0.04, 0.99, 0.93])
    fig.savefig(out_path, dpi=300)
    fig.savefig(os.path.splitext(out_path)[0] + ".pdf")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", help="experiment1 run directory")
    ap.add_argument("--deadline-us", type=float, default=5000000.0)
    ap.add_argument("--bin-seconds", type=int, default=0,
                    help="0 = use bin_seconds from timeseries/metadata")
    ap.add_argument("--smooth-bins", type=int, default=3,
                    help="centered rolling window for plotted lines")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    phases_path = os.path.join(run_dir, "phases.csv")
    if not os.path.exists(phases_path):
        print("missing phases.csv under %s" % run_dir, file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(run_dir, "raw")):
        print("missing raw/ under %s" % run_dir, file=sys.stderr)
        return 2

    phases = read_csv(phases_path)
    phases.sort(key=lambda r: int(r["phase"]))
    if not phases:
        print("no phases found in %s" % phases_path, file=sys.stderr)
        return 2
    run_start = parse_ts(phases[0]["started"])

    bin_seconds = args.bin_seconds
    if bin_seconds <= 0:
        ts_path = os.path.join(run_dir, "timeseries.csv")
        if os.path.exists(ts_path):
            sample = read_csv(ts_path)[:1]
            if sample:
                bin_seconds = int(float(sample[0].get("bin_seconds") or 5))
    if bin_seconds <= 0:
        bin_seconds = 5

    all_rows = []
    deadline_phases = []
    for phase in phases:
        rows, summary = build_deadline_phase(run_dir, phase, run_start,
                                             bin_seconds, args.deadline_us)
        all_rows.extend(rows)
        deadline_phases.append(summary)

    write_csv(os.path.join(run_dir, "deadline_timeseries.csv"), all_rows, TS_FIELDS)
    write_csv(os.path.join(run_dir, "deadline_phases.csv"), deadline_phases,
              PHASE_FIELDS)

    smoothed = smooth_rows(all_rows, args.smooth_bins)
    write_csv(os.path.join(run_dir, "deadline_timeseries_smoothed.csv"), smoothed,
              TS_FIELDS)

    cpu_rows = load_cpu_rows(run_dir)
    raw_png = os.path.join(run_dir, "experiment1_deadline_raw.png")
    smooth_png = os.path.join(run_dir, "experiment1_deadline_smoothed.png")
    plot(all_rows, deadline_phases, cpu_rows, raw_png, 1, args.deadline_us)
    plot(smoothed, deadline_phases, cpu_rows, smooth_png, args.smooth_bins,
         args.deadline_us)

    meta = {
        "source_run_dir": run_dir,
        "deadline_us": args.deadline_us,
        "bin_seconds": bin_seconds,
        "smooth_bins": args.smooth_bins,
        "latency_mapping": "2xx real latency; non-2xx mapped to deadline_us",
        "raw_plot": os.path.basename(raw_png),
        "smoothed_plot": os.path.basename(smooth_png),
    }
    with open(os.path.join(run_dir, "deadline_postprocess_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    total = {}
    for p in deadline_phases:
        for k, v in json.loads(p["status_dist"]).items():
            total[k] = total.get(k, 0) + int(v)
    print("wrote:", raw_png)
    print("wrote:", smooth_png)
    print("status totals:", json.dumps(total, sort_keys=True))
    print("deadline mapped errors:", sum(v for k, v in total.items()
                                         if not (k.isdigit() and 200 <= int(k) < 300)))
    if shutil.which("open"):
        print("open:", smooth_png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
