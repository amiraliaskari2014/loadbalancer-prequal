#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the Experiment 1 scaling error comparison plot.

Expected input layout:

  results/results_experiment1_scaling_.../
    servers_5/run_.../
    servers_10/run_.../
    servers_20/run_.../

Each run directory must contain phases.csv from experiment1.py.
"""

import argparse
import csv
import glob
import json
import os
import re
import sys


FIELDS = [
    "servers", "run_dir", "phase", "algo", "load_frac", "load_pct",
    "target_qps", "workers", "duration_s", "response_qps", "error_qps",
    "error_pct", "total_responses", "ok_responses", "error_responses",
    "status_dist",
]
AGG_FIELDS = [
    "servers", "algo", "duration_s", "total_responses", "ok_responses",
    "error_responses", "mean_error_qps", "overall_error_pct",
]


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_aggregate_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=AGG_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def parse_servers(path, metadata):
    if metadata and metadata.get("servers") is not None:
        return int(metadata["servers"])
    m = re.search(r"servers_(\d+)", path)
    if not m:
        raise ValueError("cannot infer server count from %s" % path)
    return int(m.group(1))


def read_metadata(run_dir):
    path = os.path.join(run_dir, "metadata.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def latest_runs(root):
    candidates = sorted(glob.glob(os.path.join(root, "servers_*", "run_*")))
    latest = {}
    for run_dir in candidates:
        if not os.path.exists(os.path.join(run_dir, "phases.csv")):
            continue
        metadata = read_metadata(run_dir)
        servers = parse_servers(run_dir, metadata)
        prev = latest.get(servers)
        if prev is None or os.path.basename(run_dir) > os.path.basename(prev):
            latest[servers] = run_dir
    return latest


def build_summary(root):
    runs = latest_runs(root)
    if not runs:
        raise RuntimeError("no servers_*/run_*/phases.csv found under %s" % root)

    rows = []
    for servers, run_dir in sorted(runs.items()):
        phases = read_csv(os.path.join(run_dir, "phases.csv"))
        phases.sort(key=lambda r: int(r["phase"]))
        for phase in phases:
            rows.append({
                "servers": servers,
                "run_dir": run_dir,
                "phase": int(phase["phase"]),
                "algo": phase["algo"],
                "load_frac": float(phase["load_frac"]),
                "load_pct": int(round(float(phase["load_frac"]) * 100)),
                "target_qps": float(phase.get("target_qps") or 0.0),
                "workers": int(float(phase.get("workers") or 0)),
                "duration_s": float(phase.get("duration_s") or 0.0),
                "response_qps": float(phase.get("response_qps") or 0.0),
                "error_qps": float(phase.get("error_qps") or 0.0),
                "error_pct": float(phase.get("error_pct") or 0.0),
                "total_responses": int(float(phase.get("total_responses") or 0)),
                "ok_responses": int(float(phase.get("ok_responses") or 0)),
                "error_responses": int(float(phase.get("error_responses") or 0)),
                "status_dist": phase.get("status_dist") or "{}",
            })
    return rows, runs


def aggregate_errors(rows):
    grouped = {}
    for row in rows:
        key = (int(row["servers"]), row["algo"])
        agg = grouped.setdefault(key, {
            "servers": int(row["servers"]),
            "algo": row["algo"],
            "duration_s": 0.0,
            "total_responses": 0,
            "ok_responses": 0,
            "error_responses": 0,
        })
        agg["duration_s"] += float(row.get("duration_s") or 0.0)
        agg["total_responses"] += int(row.get("total_responses") or 0)
        agg["ok_responses"] += int(row.get("ok_responses") or 0)
        agg["error_responses"] += int(row.get("error_responses") or 0)

    out = []
    for _key, agg in sorted(grouped.items()):
        duration = agg["duration_s"]
        total = agg["total_responses"]
        errors = agg["error_responses"]
        agg["mean_error_qps"] = (errors / duration) if duration else 0.0
        agg["overall_error_pct"] = (100.0 * errors / total) if total else 0.0
        out.append(agg)
    return out


def plot(rows, out_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        raise RuntimeError("matplotlib unavailable: %s" % e)

    servers = sorted({int(r["servers"]) for r in rows})
    colors = {5: "#e74c3c", 10: "#2f9bff", 20: "#35c94a"}
    markers = {5: "o", 10: "s", 20: "^"}

    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    algos = [("wrr", "WRR"), ("prequal", "Prequal")]
    for ax, (algo, label) in zip(axes, algos):
        for server_count in servers:
            data = [r for r in rows
                    if int(r["servers"]) == server_count and r["algo"] == algo]
            data.sort(key=lambda r: float(r["load_frac"]))
            if not data:
                continue
            ax.plot([r["load_pct"] for r in data],
                    [r["error_qps"] for r in data],
                    color=colors.get(server_count),
                    marker=markers.get(server_count, "o"),
                    lw=2.0,
                    label="%d servers" % server_count)
        ax.axvline(100, color="#b8b8b8", lw=2, ls="--", alpha=0.9)
        ax.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
        ax.set_ylabel("Error QPS")
        ax.set_ylim(bottom=0)
        ax.set_title(label, fontsize=13, fontweight="bold")
        ax.legend(loc="upper left", ncol=len(servers), frameon=False)

    axes[-1].set_xlabel("Load (% of allocation)")
    fig.suptitle("Experiment 1 Scaling Errors | 5 vs 10 vs 20 servers",
                 fontsize=15, y=0.98)
    fig.tight_layout(rect=[0.04, 0.04, 0.99, 0.94])
    fig.savefig(out_png, dpi=300)
    fig.savefig(os.path.splitext(out_png)[0] + ".pdf")
    plt.close(fig)


def scaling_styles():
    styles = {
        "wrr": {"label": "WRR", "color": "#e74c3c", "marker": "o"},
        "prequal": {"label": "Prequal", "color": "#2f9bff", "marker": "s"},
    }
    return styles


def draw_error_by_servers(ax, aggregate_rows, scale="linear"):
    styles = scaling_styles()

    for algo in ("wrr", "prequal"):
        data = [r for r in aggregate_rows if r["algo"] == algo]
        data.sort(key=lambda r: int(r["servers"]))
        if not data:
            continue
        style = styles[algo]
        ax.plot([int(r["servers"]) for r in data],
                [float(r["mean_error_qps"]) for r in data],
                color=style["color"], marker=style["marker"], lw=2.2,
                markersize=7, label=style["label"])
        for r in data:
            ax.annotate("%d" % int(r["error_responses"]),
                        (int(r["servers"]), float(r["mean_error_qps"])),
                        xytext=(0, 8), textcoords="offset points",
                        ha="center", fontsize=8, color=style["color"])

    server_ticks = sorted({int(r["servers"]) for r in aggregate_rows})
    ax.set_xticks(server_ticks)
    ax.set_xlabel("Number of servers")
    ax.set_ylabel("Mean Error QPS")
    ax.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    if scale == "symlog":
        positives = [float(r["mean_error_qps"]) for r in aggregate_rows
                     if float(r["mean_error_qps"]) > 0]
        linthresh = min(positives) / 2.0 if positives else 0.001
        ax.set_yscale("symlog", linthresh=linthresh, linscale=0.8)
        ax.set_title("Log-style y-axis", fontsize=12, fontweight="bold")
    else:
        ax.set_ylim(bottom=0)
        ax.set_title("Linear y-axis", fontsize=12, fontweight="bold")
    ax.legend(loc="upper left", frameon=False)
    ax.text(0.99, 0.02, "point labels = total non-2xx responses",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8,
            color="#555555")


def plot_error_by_servers(aggregate_rows, out_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        raise RuntimeError("matplotlib unavailable: %s" % e)

    fig, ax = plt.subplots(1, 1, figsize=(8, 5))
    draw_error_by_servers(ax, aggregate_rows, "linear")
    fig.suptitle("Experiment 1 Scaling Error Rate", fontsize=14,
                 fontweight="bold", y=0.98)
    fig.tight_layout()
    fig.savefig(out_png, dpi=300)
    fig.savefig(os.path.splitext(out_png)[0] + ".pdf")
    plt.close(fig)


def plot_error_by_servers_linear_log(aggregate_rows, out_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        raise RuntimeError("matplotlib unavailable: %s" % e)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.4), sharex=True)
    draw_error_by_servers(axes[0], aggregate_rows, "linear")
    draw_error_by_servers(axes[1], aggregate_rows, "symlog")
    fig.suptitle("Experiment 1 Scaling Error Rate", fontsize=16,
                 fontweight="bold", y=0.98)
    fig.tight_layout(rect=[0.02, 0.02, 0.99, 0.93])
    fig.savefig(out_png, dpi=300)
    fig.savefig(os.path.splitext(out_png)[0] + ".pdf")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="local experiment1 scaling result directory")
    ap.add_argument("--out", default="experiment1_scaling_errors.png")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    rows, runs = build_summary(root)

    csv_path = os.path.join(root, "experiment1_scaling_error_summary.csv")
    write_csv(csv_path, rows)
    aggregate_rows = aggregate_errors(rows)
    aggregate_csv_path = os.path.join(root, "experiment1_scaling_error_by_servers.csv")
    write_aggregate_csv(aggregate_csv_path, aggregate_rows)

    out_png = args.out
    if not os.path.isabs(out_png):
        out_png = os.path.join(root, out_png)
    plot(rows, out_png)
    by_servers_png = os.path.join(root, "experiment1_scaling_error_by_servers.png")
    plot_error_by_servers(aggregate_rows, by_servers_png)
    by_servers_linear_log_png = os.path.join(
        root, "experiment1_scaling_error_by_servers_linear_log.png")
    plot_error_by_servers_linear_log(aggregate_rows, by_servers_linear_log_png)

    manifest = {
        "root": root,
        "runs": {str(k): v for k, v in sorted(runs.items())},
        "summary_csv": os.path.basename(csv_path),
        "aggregate_csv": os.path.basename(aggregate_csv_path),
        "plot_png": os.path.basename(out_png),
        "plot_pdf": os.path.basename(os.path.splitext(out_png)[0] + ".pdf"),
        "by_servers_plot_png": os.path.basename(by_servers_png),
        "by_servers_plot_pdf": os.path.basename(os.path.splitext(by_servers_png)[0] + ".pdf"),
        "by_servers_linear_log_plot_png": os.path.basename(by_servers_linear_log_png),
        "by_servers_linear_log_plot_pdf": os.path.basename(os.path.splitext(by_servers_linear_log_png)[0] + ".pdf"),
    }
    with open(os.path.join(root, "experiment1_scaling_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print("runs:", ", ".join("%s=%s" % (k, v) for k, v in sorted(runs.items())))
    print("wrote:", csv_path)
    print("wrote:", aggregate_csv_path)
    print("wrote:", out_png)
    print("wrote:", by_servers_png)
    print("wrote:", by_servers_linear_log_png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
