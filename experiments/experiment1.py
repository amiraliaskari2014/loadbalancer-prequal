#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Experiment 1: fixed WRR vs Prequal load-ramp experiment.

Notes:
  * fixed low worker count by default (30) to avoid hey's per-worker burst issue
  * latency percentiles are computed from successful 2xx responses only
  * errors remain a separate all-response panel
  * optional real host CPU sampling from bhost*/proc/stat, instead of synthetic
    lb_server_cpu_load_pct

Runs on the CloudLab lb node after prepare.py has started containers.
"""

import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time

from common import (
    DEFAULT_LOADS,
    TS_FIELDS,
    PHASE_FIELDS,
    capture_lb_template,
    filter_healthy,
    fmt,
    get_full_lb_servers,
    reconfigure_lb,
    run_hey_csv,
    sh,
    write_csv,
    write_status,
)

DEFAULT_STEP_SECONDS = 240
DEFAULT_BIN_SECONDS = 5
DEFAULT_WORKERS = 30
CPU_PLOT_SCALE = 100.0

log = logging.getLogger("experiment1")


def setup_logging(outdir):
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fmtter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                               "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmtter)
    fh = logging.FileHandler(os.path.join(outdir, "experiment1.log"))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmtter)
    log.addHandler(ch)
    log.addHandler(fh)


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = max(1, int(math.ceil((q / 100.0) * len(sorted_vals))))
    return sorted_vals[min(k - 1, len(sorted_vals) - 1)]


def parse_phase_csv_2xx_latency(raw_path, phase, bin_seconds):
    """Parse hey CSV.

    Error counts include every non-2xx response. Latency percentile inputs include
    only 2xx responses, matching the intended paper-style separation of latency
    and errors.
    """
    bins = {}
    ok_lats = []
    total = ok = err = 0
    status_dist = {}

    with open(raw_path, newline="") as f:
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
            b = bins.setdefault(idx, {"ok_lats": [], "total": 0, "ok": 0,
                                      "err": 0, "dist": {}})
            total += 1
            b["total"] += 1
            key = str(code) if code else "unknown"
            status_dist[key] = status_dist.get(key, 0) + 1
            b["dist"][key] = b["dist"].get(key, 0) + 1

            if 200 <= code < 300:
                lat_us = rt_s * 1000000.0
                ok += 1
                b["ok"] += 1
                ok_lats.append(lat_us)
                b["ok_lats"].append(lat_us)
            else:
                err += 1
                b["err"] += 1

    rows = []
    n_bins = int(math.ceil(phase["duration_s"] / float(bin_seconds)))
    for idx in range(n_bins):
        b = bins.get(idx, {"ok_lats": [], "total": 0, "ok": 0,
                           "err": 0, "dist": {}})
        lats = sorted(b["ok_lats"])
        ts_epoch = phase["start_epoch"] + min(phase["duration_s"], (idx + 0.5) * bin_seconds)
        rows.append({
            "algo": phase["algo"],
            "servers": phase["servers"],
            "load_frac": phase["load_frac"],
            "load_pct": int(round(phase["load_frac"] * 100)),
            "target_qps": phase["target_qps"],
            "workers": phase["workers"],
            "bin_seconds": bin_seconds,
            "bin_index": idx,
            "timestamp": dt.datetime.fromtimestamp(ts_epoch).isoformat(timespec="seconds"),
            "elapsed_s": round(ts_epoch - phase["run_start_epoch"], 3),
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

    ok_lats.sort()
    summary = dict(phase)
    summary.update({
        "total_responses": total,
        "ok_responses": ok,
        "error_responses": err,
        "error_pct": (100.0 * err / total) if total else None,
        "response_qps": total / float(phase["duration_s"]) if phase["duration_s"] else None,
        "error_qps": err / float(phase["duration_s"]) if phase["duration_s"] else None,
        "p50_us": percentile(ok_lats, 50),
        "p90_us": percentile(ok_lats, 90),
        "p99_us": percentile(ok_lats, 99),
        "p99_9_us": percentile(ok_lats, 99.9),
        "avg_us": (sum(ok_lats) / len(ok_lats)) if ok_lats else None,
        "status_dist": json.dumps(status_dist, sort_keys=True),
    })
    return rows, summary


def cpu_snapshot(host):
    rc, out = sh("ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 %s "
                 "\"awk '/^cpu /{print}' /proc/stat\"" % host, timeout=4)
    if rc != 0:
        return host, None
    parts = out.strip().split()
    if len(parts) < 8 or parts[0] != "cpu":
        return host, None
    vals = [int(v) for v in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return host, (total, idle)


def server_index(host):
    m = re.search(r"(\d+)", str(host))
    return int(m.group(1)) if m else 0


class HostCPUSampler:
    def __init__(self, hosts, interval, run_start_epoch):
        self.hosts = hosts
        self.interval = interval
        self.run_start_epoch = run_start_epoch
        self.rows = []
        self.previous = {}
        self.phase = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=max(2, self.interval + 2))

    def set_phase(self, phase):
        with self.lock:
            self.phase = dict(phase)

    def clear_phase(self):
        with self.lock:
            self.phase = None

    def get_rows(self):
        with self.lock:
            return list(self.rows)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self._sample_once()
            except Exception as e:
                log.warning("host CPU sampler error; continuing: %s", e)
            self.stop_event.wait(self.interval)

    def _sample_once(self):
        ts_epoch = time.time()
        with self.lock:
            phase = dict(self.phase) if self.phase else None
        if not phase:
            return

        samples = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(self.hosts), 10)) as ex:
            futs = [ex.submit(cpu_snapshot, h) for h in self.hosts]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    samples.append(fut.result())
                except Exception as e:
                    log.debug("host CPU sample failed: %s", e)

        new_rows = []
        for host, snap in samples:
            if snap is None:
                continue
            prev = self.previous.get(host)
            self.previous[host] = snap
            if prev is None:
                continue
            total_delta = snap[0] - prev[0]
            idle_delta = snap[1] - prev[1]
            if total_delta <= 0:
                continue
            cpu_pct = 100.0 * (1.0 - idle_delta / float(total_delta))
            new_rows.append({
                "phase": phase["phase"],
                "algo": phase["algo"],
                "servers": phase["servers"],
                "load_frac": phase["load_frac"],
                "load_pct": phase["load_pct"],
                "target_qps": phase["target_qps"],
                "host": host,
                "server_index": server_index(host),
                "timestamp": dt.datetime.fromtimestamp(ts_epoch).isoformat(timespec="seconds"),
                "elapsed_s": round(ts_epoch - self.run_start_epoch, 3),
                "host_cpu_pct": cpu_pct,
            })
        if new_rows:
            with self.lock:
                self.rows.extend(new_rows)


def write_cpu_csv(path, rows):
    fields = ["phase", "algo", "servers", "load_frac", "load_pct", "target_qps",
              "host", "server_index", "timestamp", "elapsed_s", "host_cpu_pct"]
    write_csv(path, rows, fields)


def parse_ts(value):
    return dt.datetime.fromisoformat(value)


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


def plot_experiment(rows, phases, cpu_rows, outdir, args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        log.warning("matplotlib unavailable (%s); skipping plot.", e)
        return

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
            vals.append(float(v) if v not in (None, "", "None") else float("nan"))
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

    ax1.plot(xs, series("p50_us"), color="#ff3333", lw=1.7, label="Query Latency 50pct, 2xx only (usec)")
    ax1.plot(xs, series("p90_us"), color="#35c94a", lw=1.7, label="Query Latency 90pct, 2xx only (usec)")
    ax1.plot(xs, series("p99_us"), color="#2f9bff", lw=1.7, label="Query Latency 99pct, 2xx only (usec)")
    ax1.plot(xs, series("p99_9_us"), color="#d642ff", lw=1.7, label="Query Latency 99.9pct, 2xx only (usec)")
    ax1.set_yscale("log")
    latency_vals = [float(r[m]) for r in rows for m in ("p50_us", "p90_us", "p99_us", "p99_9_us")
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
        ax1.text(mid, 1.31, "Below Alloc <-", transform=ax1.get_xaxis_transform(),
                 ha="center", va="bottom", fontsize=19, fontweight="bold",
                 clip_on=False)
    if above:
        start = parse_ts(above[0]["started"])
        end = parse_ts(above[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.31, "-> Above Alloc", transform=ax1.get_xaxis_transform(),
                 ha="center", va="bottom", fontsize=19, fontweight="bold",
                 clip_on=False)

    fig.suptitle("Experiment 1 | %d servers | workers=%d | gray = WRR, white = Prequal"
                 % (args.servers, args.workers),
                 y=0.985, fontsize=14)
    fig.tight_layout(rect=[0.03, 0.04, 0.99, 0.93])
    png = os.path.join(outdir, "experiment1_latency_errors_cpu.png")
    pdf = os.path.join(outdir, "experiment1_latency_errors_cpu.pdf")
    fig.savefig(png, dpi=300)
    fig.savefig(pdf)
    plt.close(fig)
    log.info("plot -> %s", png)


def write_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", type=int, default=20)
    ap.add_argument("--per-server-qps", type=int, default=25)
    ap.add_argument("--loads", type=float, nargs="+", default=DEFAULT_LOADS)
    ap.add_argument("--step-seconds", type=int, default=DEFAULT_STEP_SECONDS)
    ap.add_argument("--bin-seconds", type=int, default=DEFAULT_BIN_SECONDS)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--warmup", type=int, default=15)
    ap.add_argument("--order", choices=["wrr-prequal", "prequal-wrr"],
                    default="wrr-prequal")
    ap.add_argument("--prequal-url", default="http://localhost:8080")
    ap.add_argument("--wrr-url", default="http://localhost:8081")
    ap.add_argument("--lb-servers", default="",
                    help="full LB_SERVERS list; useful when rerunning after a previous subset reconfiguration")
    ap.add_argument("--host-cpu-interval", type=int, default=5)
    ap.add_argument("--no-host-cpu", action="store_true")
    ap.add_argument("--outdir", default="experiment1_results")
    args = ap.parse_args()

    if args.bin_seconds <= 0 or args.step_seconds <= 0 or args.workers <= 0:
        print("--bin-seconds, --step-seconds, and --workers must be positive",
              file=sys.stderr)
        sys.exit(2)
    if args.bin_seconds > args.step_seconds:
        print("--bin-seconds cannot exceed --step-seconds", file=sys.stderr)
        sys.exit(2)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = os.path.join(os.path.expanduser(args.outdir), "run_%s" % ts)
    raw_dir = os.path.join(run_root, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    setup_logging(run_root)
    status_path = os.path.join(run_root, "STATUS.json")

    order = args.order.split("-")
    total_seconds = len(args.loads) * len(order) * args.step_seconds
    meta = {
        "mode": "experiment1_load_ramp",
        "topology": "cloudlab_profile: one physical backend node per backend server",
        "servers": args.servers,
        "per_server_qps": args.per_server_qps,
        "loads": args.loads,
        "step_seconds": args.step_seconds,
        "bin_seconds": args.bin_seconds,
        "workers": args.workers,
        "order": order,
        "latency_percentiles": "2xx responses only",
        "errors": "all non-2xx responses",
        "host_cpu": not args.no_host_cpu,
        "hostname": os.uname().nodename,
        "started": ts,
    }
    write_json(os.path.join(run_root, "metadata.json"), meta)

    log.info("=" * 70)
    log.info("EXPERIMENT 1 | servers=%d | per_server_qps=%d | workers=%d",
             args.servers, args.per_server_qps, args.workers)
    log.info("loads=%s | step=%ds | bin=%ds | total~%.1f min | output=%s",
             args.loads, args.step_seconds, args.bin_seconds,
             total_seconds / 60.0, run_root)
    log.info("=" * 70)

    rc, out = sh("sudo docker ps --format '{{.Names}}'")
    for name in ("lb-prequal", "lb-weightedrr"):
        if name not in out:
            log.error("%s container not found. Run prepare.py first.", name)
            sys.exit(1)
        capture_lb_template(name)

    full_raw = args.lb_servers or os.environ.get("LB_SERVERS_FULL", "") or get_full_lb_servers()
    healthy, dead = filter_healthy(full_raw)
    if dead:
        log.warning("Skipping %d unreachable backend(s): %s", len(dead),
                    ", ".join(e.split("=")[0] for e in dead))
    if len(healthy) < args.servers:
        log.error("Need %d healthy backends, found %d.", args.servers, len(healthy))
        sys.exit(1)
    full = ",".join(healthy)
    log.info("Configuring both LBs to first %d healthy backends...", args.servers)
    reconfigure_lb("lb-prequal", full, args.servers)
    reconfigure_lb("lb-weightedrr", full, args.servers)
    time.sleep(4)

    urls = {"prequal": args.prequal_url, "wrr": args.wrr_url}
    for algo in order:
        rc, test_out = sh("hey -z 3s -q 5 -c 5 %s/" % urls[algo], timeout=20)
        if "Requests/sec" not in test_out:
            log.error("hey/self-test failed for %s. Output:\n%s", algo, test_out[:500])
            sys.exit(1)

    if args.warmup > 0:
        log.info("Warming up both policies (%ds each)...", args.warmup)
        for algo in order:
            sh("hey -z %ds -q 1 -c %d %s/" %
               (args.warmup, max(args.servers, 5), urls[algo]),
               timeout=args.warmup + 30)

    run_start_epoch = time.time()
    hosts = ["bhost%d" % i for i in range(1, args.servers + 1)]
    sampler = None
    if not args.no_host_cpu:
        log.info("Starting host CPU sampler for %s every %ds...", hosts,
                 args.host_cpu_interval)
        sampler = HostCPUSampler(hosts, args.host_cpu_interval, run_start_epoch)
        sampler.start()

    all_rows = []
    phases = []
    phase_no = 0
    try:
        for load in args.loads:
            target = max(1, int(round(args.servers * args.per_server_qps * load)))
            workers = args.workers
            for algo in order:
                phase_no += 1
                raw_path = os.path.join(raw_dir, "phase%02d_%s_load%03d.csv" %
                                        (phase_no, algo, int(round(load * 100))))
                log.info("[%d/%d] %s load=%d%% target=%d qps workers=%d duration=%ds",
                         phase_no, len(args.loads) * len(order), algo,
                         int(round(load * 100)), target, workers, args.step_seconds)
                write_status(status_path, {
                    "state": "RUNNING",
                    "phase": phase_no,
                    "total_phases": len(args.loads) * len(order),
                    "algo": algo,
                    "load_frac": load,
                    "target_qps": target,
                    "workers": workers,
                    "updated": dt.datetime.now().isoformat(timespec="seconds"),
                })

                started_epoch = time.time()
                phase_for_sampler = {
                    "phase": phase_no,
                    "algo": algo,
                    "servers": args.servers,
                    "load_frac": load,
                    "load_pct": int(round(load * 100)),
                    "target_qps": target,
                }
                if sampler:
                    sampler.set_phase(phase_for_sampler)
                rc, stderr, command = run_hey_csv(urls[algo], target,
                                                  args.step_seconds, workers,
                                                  raw_path)
                if sampler:
                    sampler.clear_phase()
                ended_epoch = time.time()
                if rc != 0:
                    log.warning("hey returned rc=%d for %s load=%s. stderr tail:\n%s",
                                rc, algo, load, "\n".join(stderr.splitlines()[-5:]))

                phase = dict(phase_for_sampler)
                phase.update({
                    "workers": workers,
                    "duration_s": args.step_seconds,
                    "start_epoch": started_epoch,
                    "end_epoch": ended_epoch,
                    "run_start_epoch": run_start_epoch,
                    "started": dt.datetime.fromtimestamp(started_epoch).isoformat(timespec="seconds"),
                    "ended": dt.datetime.fromtimestamp(ended_epoch).isoformat(timespec="seconds"),
                    "command": command,
                    "returncode": rc,
                })
                rows, summary = parse_phase_csv_2xx_latency(raw_path, phase,
                                                            args.bin_seconds)
                all_rows.extend(rows)
                phases.append(summary)
                write_csv(os.path.join(run_root, "timeseries.csv"), all_rows, TS_FIELDS)
                write_csv(os.path.join(run_root, "phases.csv"), phases, PHASE_FIELDS)
                if sampler:
                    write_cpu_csv(os.path.join(run_root, "host_cpu_timeseries.csv"),
                                  sampler.get_rows())
                log.info("    %s p50=%s p90=%s p99=%s p99.9=%s usec (2xx only) | err=%s%% | err_qps=%s",
                         algo, fmt(summary.get("p50_us"), 0), fmt(summary.get("p90_us"), 0),
                         fmt(summary.get("p99_us"), 0), fmt(summary.get("p99_9_us"), 0),
                         fmt(summary.get("error_pct"), 2), fmt(summary.get("error_qps"), 2))
    finally:
        if sampler:
            sampler.stop()

    cpu_rows = sampler.get_rows() if sampler else []
    if cpu_rows:
        write_cpu_csv(os.path.join(run_root, "host_cpu_timeseries.csv"), cpu_rows)
    plot_experiment(all_rows, phases, cpu_rows, run_root, args)
    meta["finished"] = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    write_json(os.path.join(run_root, "metadata.json"), meta)
    write_status(status_path, {
        "state": "DONE",
        "phases": len(phases),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    log.info("=" * 70)
    log.info("EXPERIMENT 1 DONE. Everything is under: %s", run_root)
    log.info("=" * 70)


if __name__ == "__main__":
    main()
