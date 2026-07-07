#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smooth an Experiment 1 plot without changing deadline/error semantics.

This reads the original 2xx-only latency time series and all-error QPS series,
applies a centered rolling average within each policy/load phase, and writes a
new plot next to the original files.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys


LATENCY_FIELDS = ("p50_us", "p90_us", "p99_us", "p99_9_us")
SMOOTH_FIELDS = LATENCY_FIELDS + ("avg_us", "error_qps")
CPU_PLOT_SCALE = 100.0
TS_FIELDS = [
    "algo", "servers", "load_frac", "load_pct", "target_qps", "workers",
    "bin_seconds", "bin_index", "timestamp", "elapsed_s", "p50_us",
    "p90_us", "p99_us", "p99_9_us", "avg_us", "total_responses",
    "ok_responses", "error_responses", "response_qps", "error_qps",
    "status_dist",
]


def parse_ts(value):
    return dt.datetime.fromisoformat(value)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


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


def phase_key(row):
    return row.get("algo"), "%.9g" % float(row.get("load_frac", 0.0))


def smooth_rows(rows, window):
    if window <= 1:
        return [dict(r) for r in rows]
    out = [dict(r) for r in rows]
    half = window // 2
    by_phase = {}
    for idx, row in enumerate(rows):
        by_phase.setdefault(phase_key(row), []).append(idx)

    for indices in by_phase.values():
        indices.sort(key=lambda i: int(float(rows[i].get("bin_index") or 0)))
        for metric in SMOOTH_FIELDS:
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


def fmt_us_tick(x, _pos):
    if x >= 1000000:
        return ("%gM" % (x / 1000000.0)).replace(".0", "")
    if x >= 1000:
        return ("%gk" % (x / 1000.0)).replace(".0", "")
    return "%g" % x


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


def plot(rows, phases, cpu_rows, out_png, smooth_bins):
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

    ax1.plot(xs, series("p50_us"), color="#ff3333", lw=1.7,
             label="Query Latency 50pct, 2xx only")
    ax1.plot(xs, series("p90_us"), color="#35c94a", lw=1.7,
             label="Query Latency 90pct, 2xx only")
    ax1.plot(xs, series("p99_us"), color="#2f9bff", lw=1.7,
             label="Query Latency 99pct, 2xx only")
    ax1.plot(xs, series("p99_9_us"), color="#d642ff", lw=1.7,
             label="Query Latency 99.9pct, 2xx only")
    ax1.set_yscale("log")
    latency_vals = [float(r[m]) for r in rows for m in LATENCY_FIELDS
                    if r.get(m) not in (None, "", "None")]
    ymax = max(latency_vals) * 1.3 if latency_vals else 500000.0
    ymin = max(1000.0, min(latency_vals) * 0.7) if latency_vals else 10000.0
    ticks = [1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000,
             500000, 1000000, 2000000, 5000000]
    ax1.set_ylim(ymin, max(ymax, 200000.0))
    ax1.set_yticks([t for t in ticks if ymin <= t <= max(ymax, 200000.0) * 1.05])
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

    servers = phases[0].get("servers", "?") if phases else "?"
    fig.suptitle("Experiment 1 Smoothed | %s servers | 2xx latency | %d-bin"
                 % (servers, smooth_bins), y=0.985, fontsize=14)
    fig.tight_layout(rect=[0.03, 0.04, 0.99, 0.93])
    fig.savefig(out_png, dpi=300)
    fig.savefig(os.path.splitext(out_png)[0] + ".pdf")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", help="experiment1 run directory")
    ap.add_argument("--smooth-bins", type=int, default=5,
                    help="centered rolling window; 5 bins = 25s for 5s bins")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    ts_path = os.path.join(run_dir, "timeseries.csv")
    phases_path = os.path.join(run_dir, "phases.csv")
    if not os.path.exists(ts_path) or not os.path.exists(phases_path):
        print("missing timeseries.csv or phases.csv under %s" % run_dir,
              file=sys.stderr)
        return 2

    rows = read_csv(ts_path)
    phases = read_csv(phases_path)
    phases.sort(key=lambda r: int(r["phase"]))
    smoothed = smooth_rows(rows, args.smooth_bins)

    out_csv = os.path.join(run_dir, "timeseries_smoothed.csv")
    out_png = os.path.join(run_dir, "experiment1_smoothed.png")
    write_csv(out_csv, smoothed, TS_FIELDS)

    cpu_path = os.path.join(run_dir, "host_cpu_timeseries.csv")
    cpu_rows = read_csv(cpu_path) if os.path.exists(cpu_path) else []
    plot(smoothed, phases, cpu_rows, out_png, args.smooth_bins)

    meta = {
        "source_run_dir": run_dir,
        "smooth_bins": args.smooth_bins,
        "smoothed_fields": list(SMOOTH_FIELDS),
        "latency_mapping": "unchanged from experiment1: 2xx only",
        "errors": "unchanged from experiment1: all non-2xx error_qps",
        "plot": os.path.basename(out_png),
        "timeseries": os.path.basename(out_csv),
    }
    with open(os.path.join(run_dir, "smooth_postprocess_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print("wrote:", out_png)
    print("wrote:", os.path.splitext(out_png)[0] + ".pdf")
    print("wrote:", out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
