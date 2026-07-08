#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Experiment 2: Prequal probing-rate sweep.

This is a Figure-8-style experiment: hold load constant, vary LB_PROBE_RATE,
and plot p99/p99.9 latency plus RIF quantiles over time.

Notes about this repo's LB implementation:
  * LB_PROBE_RATE is accepted as a float, but the current async probe launcher
    truncates it to int(rate) probes per query.
  * The LB also has a fixed background prober, and probe removal is hard-coded.
    So this is a useful repo-level sweep, not an exact paper reproduction.
"""

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import re
import statistics
import sys
import threading
import time

from common import (
    LB_TEMPLATES,
    capture_lb_template,
    container_env,
    container_image,
    fetch,
    filter_healthy,
    fmt,
    get_full_lb_servers,
    run_hey_csv,
    sh,
    write_csv,
    write_status,
)


DEFAULT_PROBE_RATES = [
    4.0,
    2.0 * math.sqrt(2.0),
    2.0,
    math.sqrt(2.0),
    1.0,
    math.sqrt(0.5),
    0.5,
]
DEFAULT_STEP_SECONDS = 240
DEFAULT_BIN_SECONDS = 5
DEFAULT_WORKERS = 30
DEFAULT_LOAD = 1.5

log = logging.getLogger("experiment2")

TS_FIELDS = [
    "phase", "algo", "servers", "load_frac", "load_pct", "probe_rate",
    "probe_label", "effective_async_probes", "target_qps", "workers",
    "bin_seconds", "bin_index", "timestamp", "elapsed_s", "p50_us",
    "p90_us", "p99_us", "p99_9_us", "avg_us", "total_responses",
    "ok_responses", "error_responses", "response_qps", "error_qps",
    "status_dist",
]
PHASE_FIELDS = [
    "phase", "algo", "servers", "load_frac", "load_pct", "probe_rate",
    "probe_label", "effective_async_probes", "target_qps", "workers",
    "duration_s", "started", "ended", "command", "returncode",
    "total_responses", "ok_responses", "error_responses", "error_pct",
    "response_qps", "error_qps", "p50_us", "p90_us", "p99_us",
    "p99_9_us", "avg_us", "status_dist",
]
RIF_FIELDS = [
    "phase", "servers", "load_frac", "load_pct", "probe_rate",
    "probe_label", "effective_async_probes", "target_qps", "timestamp",
    "elapsed_s", "rif_p50", "rif_p90", "rif_p99", "rif_limit",
    "sample_count", "rif_values",
]


def setup_logging(outdir):
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fmtter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                               "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmtter)
    fh = logging.FileHandler(os.path.join(outdir, "experiment2.log"))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmtter)
    log.addHandler(ch)
    log.addHandler(fh)


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = max(1, int(math.ceil((q / 100.0) * len(sorted_vals))))
    return sorted_vals[min(k - 1, len(sorted_vals) - 1)]


def rate_label(rate):
    pairs = [
        (4.0, "4", "4"),
        (2.0 * math.sqrt(2.0), "2sqrt2", r"$2\sqrt{2}$"),
        (2.0, "2", "2"),
        (math.sqrt(2.0), "sqrt2", r"$\sqrt{2}$"),
        (1.0, "1", "1"),
        (math.sqrt(0.5), "sqrt1/2", r"$\sqrt{\frac{1}{2}}$"),
        (0.5, "1/2", r"$\frac{1}{2}$"),
    ]
    for val, label, tex in pairs:
        if abs(rate - val) < 1e-6:
            return label, tex
    return ("%.4g" % rate), ("%.4g" % rate)


def parse_phase_csv_2xx_latency(raw_path, phase, bin_seconds):
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
        ts_epoch = phase["start_epoch"] + min(phase["duration_s"],
                                              (idx + 0.5) * bin_seconds)
        row = dict(phase)
        row.update({
            "bin_seconds": bin_seconds,
            "bin_index": idx,
            "timestamp": dt.datetime.fromtimestamp(ts_epoch).isoformat(timespec="milliseconds"),
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
        rows.append(row)

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


def parse_rif_values(metrics_text):
    vals = []
    for line in metrics_text.splitlines():
        if not line.startswith("lb_server_rif{"):
            continue
        if 'algorithm="prequal"' not in line:
            continue
        try:
            vals.append(float(line.rsplit(" ", 1)[-1]))
        except ValueError:
            pass
    return vals


class RIFSampler:
    def __init__(self, url, interval, run_start_epoch, qrif):
        self.url = url
        self.interval = interval
        self.run_start_epoch = run_start_epoch
        self.qrif = qrif
        self.phase = None
        self.rows = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=max(2.0, self.interval + 2.0))

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
            self._sample_once()
            self.stop_event.wait(self.interval)

    def _sample_once(self):
        with self.lock:
            phase = dict(self.phase) if self.phase else None
        if not phase:
            return
        vals = parse_rif_values(fetch(self.url))
        if not vals:
            return
        vals.sort()
        ts_epoch = time.time()
        row = {
            "phase": phase["phase"],
            "servers": phase["servers"],
            "load_frac": phase["load_frac"],
            "load_pct": phase["load_pct"],
            "probe_rate": phase["probe_rate"],
            "probe_label": phase["probe_label"],
            "effective_async_probes": phase["effective_async_probes"],
            "target_qps": phase["target_qps"],
            "timestamp": dt.datetime.fromtimestamp(ts_epoch).isoformat(timespec="seconds"),
            "elapsed_s": round(ts_epoch - self.run_start_epoch, 3),
            "rif_p50": percentile(vals, 50),
            "rif_p90": percentile(vals, 90),
            "rif_p99": percentile(vals, 99),
            "rif_limit": percentile(vals, self.qrif * 100.0),
            "sample_count": len(vals),
            "rif_values": json.dumps(vals),
        }
        with self.lock:
            self.rows.append(row)


def reconfigure_prequal(full_servers, n, probe_rate, qrif):
    name = "lb-prequal"
    subset = ",".join([s for s in full_servers.split(",") if s.strip()][:n])
    env, image = LB_TEMPLATES[name]
    clean = []
    for e in env:
        if e.startswith("LB_PROBE_RATE=") or e.startswith("LB_QRIF="):
            continue
        clean.append(e)
    clean.append("LB_PROBE_RATE=%.12g" % probe_rate)
    clean.append("LB_QRIF=%.12g" % qrif)
    env_args = " ".join("-e '%s'" % e for e in clean) + " -e 'LB_SERVERS=%s'" % subset
    sh("sudo docker rm -f %s 2>/dev/null || true" % name)
    rc, out = sh("sudo docker run -d --restart unless-stopped --name %s --network=host %s %s"
                 % (name, env_args, image), timeout=60)
    if rc != 0:
        raise RuntimeError("failed to recreate %s: %s" % (name, out.strip()))


def smooth_by_phase(rows, fields, window, method="mean"):
    if window <= 1:
        return [dict(r) for r in rows]
    out = [dict(r) for r in rows]
    half = window // 2
    by_phase = {}
    for idx, row in enumerate(rows):
        by_phase.setdefault(row["phase"], []).append(idx)
    for indices in by_phase.values():
        for field in fields:
            vals = []
            for idx in indices:
                raw = rows[idx].get(field)
                vals.append(float(raw) if raw not in (None, "", "None") else math.nan)
            for local_i, idx in enumerate(indices):
                lo = max(0, local_i - half)
                hi = min(len(vals), local_i + half + 1)
                chunk = [v for v in vals[lo:hi] if not math.isnan(v)]
                if not chunk:
                    out[idx][field] = None
                elif method == "median":
                    out[idx][field] = statistics.median(chunk)
                else:
                    out[idx][field] = sum(chunk) / len(chunk)
    return out


def parse_ts(s):
    return dt.datetime.fromisoformat(s)


def trim_phase_edges(rows, phases, edge_seconds):
    if edge_seconds <= 0:
        return [dict(r) for r in rows]

    edge = dt.timedelta(seconds=edge_seconds)
    bounds = {}
    for phase in phases:
        start = parse_ts(phase["started"])
        end = parse_ts(phase["ended"])
        if start + edge < end - edge:
            bounds[str(phase["phase"])] = (start + edge, end - edge)
        else:
            bounds[str(phase["phase"])] = (start, end)

    trimmed = []
    for row in rows:
        phase_bounds = bounds.get(str(row.get("phase")))
        if not phase_bounds:
            trimmed.append(dict(row))
            continue
        ts = parse_ts(row["timestamp"])
        if phase_bounds[0] <= ts <= phase_bounds[1]:
            trimmed.append(dict(row))
    return trimmed


def plot_experiment(rows, phases, rif_rows, outdir, args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        log.warning("matplotlib unavailable (%s); skipping plot.", e)
        return

    plot_rows = smooth_by_phase(rows, ("p99_us", "p99_9_us"),
                                args.smooth_bins)
    rif_for_plot = trim_phase_edges(rif_rows, phases,
                                    args.rif_edge_trim_seconds)
    plot_rif = smooth_by_phase(rif_for_plot, ("rif_p99", "rif_p90", "rif_p50",
                                              "rif_limit"),
                               args.rif_smooth_samples)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [1.0, 1.0]})

    axes = [ax1, ax2]
    for p in phases:
        start = parse_ts(p["started"])
        end = parse_ts(p["ended"])
        for ax in axes:
            if int(p["phase"]) % 2 == 1:
                ax.axvspan(start, end, color="#eeeeee", alpha=0.9, lw=0)
            ax.axvline(start, color="#8a8a8a", lw=0.8, alpha=0.45)

    xs = [parse_ts(r["timestamp"]) for r in plot_rows]

    def yseries(data, metric):
        vals = []
        for r in data:
            v = r.get(metric)
            vals.append(float(v) if v not in (None, "", "None") else math.nan)
        return vals

    def fmt_us_tick(x, _pos):
        if x >= 1000000:
            return ("%gM" % (x / 1000000.0)).replace(".0", "")
        if x >= 1000:
            return ("%gk" % (x / 1000.0)).replace(".0", "")
        return "%g" % x

    ax1.plot(xs, yseries(plot_rows, "p99_us"), color="#ff3333", lw=1.8,
             label="Query Latency 99pct (usec)")
    ax1.plot(xs, yseries(plot_rows, "p99_9_us"), color="#35c94a", lw=1.8,
             label="Query Latency 99.9pct (usec)")
    ax1.yaxis.set_major_formatter(FuncFormatter(fmt_us_tick))
    ax1.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    ax1.legend(loc="upper center", ncol=2, fontsize=9, frameon=False)
    ax1.set_ylabel("Latency (usec)")
    ax1.text(0.5, -0.22, "(a) Tail Latency at 99p and 99.9p",
             transform=ax1.transAxes, ha="center", va="center",
             fontsize=18, fontweight="bold")

    rx = [parse_ts(r["timestamp"]) for r in plot_rif]
    ax2.plot(rx, yseries(plot_rif, "rif_p99"), color="#ff3333", lw=1.8,
             label="99 pct")
    ax2.plot(rx, yseries(plot_rif, "rif_p90"), color="#35c94a", lw=1.8,
             label="90 pct")
    ax2.plot(rx, yseries(plot_rif, "rif_p50"), color="#2f9bff", lw=1.8,
             label="50 pct")
    ax2.plot(rx, yseries(plot_rif, "rif_limit"), color="#d642ff", lw=1.8,
             label="rif_limit")
    ax2.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    ax2.legend(loc="upper right", ncol=4, fontsize=8, frameon=False)
    ax2.set_ylabel("RIF")
    ax2.text(0.5, -0.24, "(b) RIF Quantiles", transform=ax2.transAxes,
             ha="center", va="center", fontsize=18, fontweight="bold")

    if phases:
        ax2.set_xlim(parse_ts(phases[0]["started"]), parse_ts(phases[-1]["ended"]))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=8))

    ax1.text(0.5, 1.30, "Probe Rate", transform=ax1.transAxes, ha="center",
             va="bottom", fontsize=17, fontweight="bold", clip_on=False)
    for p in phases:
        start = parse_ts(p["started"])
        end = parse_ts(p["ended"])
        mid = start + (end - start) / 2
        label = rate_label(float(p["probe_rate"]))[1]
        ax1.text(mid, 1.15, label, transform=ax1.get_xaxis_transform(),
                 ha="center", va="bottom", fontsize=15, clip_on=False)

    fig.suptitle("Experiment 2 | Probe Rate Sweep | load=%.2fx | %d servers"
                 % (args.load, args.servers), y=0.985, fontsize=13)
    fig.tight_layout(rect=[0.04, 0.05, 0.99, 0.91])
    png = os.path.join(outdir, "experiment2_probe_rate.png")
    pdf = os.path.join(outdir, "experiment2_probe_rate.pdf")
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
    ap.add_argument("--load", type=float, default=DEFAULT_LOAD)
    ap.add_argument("--probe-rates", type=float, nargs="+",
                    default=DEFAULT_PROBE_RATES)
    ap.add_argument("--step-seconds", type=int, default=DEFAULT_STEP_SECONDS)
    ap.add_argument("--bin-seconds", type=int, default=DEFAULT_BIN_SECONDS)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--warmup", type=int, default=15)
    ap.add_argument("--qrif", type=float, default=0.84)
    ap.add_argument("--rif-sample-interval", type=float, default=0.5)
    ap.add_argument("--smooth-bins", type=int, default=9)
    ap.add_argument("--rif-smooth-samples", type=int, default=60)
    ap.add_argument("--rif-edge-trim-seconds", type=float, default=12.0)
    ap.add_argument("--prequal-url", default="http://localhost:8080")
    ap.add_argument("--outdir", default="experiment2_results")
    ap.add_argument("--no-restore", action="store_true")
    args = ap.parse_args()

    if args.step_seconds <= 0 or args.bin_seconds <= 0 or args.workers <= 0:
        print("step, bin, and workers must be positive", file=sys.stderr)
        sys.exit(2)
    if args.rif_sample_interval <= 0:
        print("--rif-sample-interval must be positive", file=sys.stderr)
        sys.exit(2)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = os.path.join(os.path.expanduser(args.outdir), "run_%s" % ts)
    raw_dir = os.path.join(run_root, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    setup_logging(run_root)
    status_path = os.path.join(run_root, "STATUS.json")

    meta = {
        "mode": "experiment2_probe_rate",
        "servers": args.servers,
        "per_server_qps": args.per_server_qps,
        "load": args.load,
        "probe_rates": args.probe_rates,
        "step_seconds": args.step_seconds,
        "bin_seconds": args.bin_seconds,
        "workers": args.workers,
        "warmup": args.warmup,
        "qrif": args.qrif,
        "rif_sample_interval": args.rif_sample_interval,
        "rif_edge_trim_seconds": args.rif_edge_trim_seconds,
        "latency_percentiles": "2xx responses only",
        "rif_source": "lb_server_rif gauge sampled from lb-prequal /metrics",
        "limitation": "current LB truncates LB_PROBE_RATE to int(rate) for async per-query probes",
        "started": ts,
    }
    write_json(os.path.join(run_root, "metadata.json"), meta)

    log.info("=" * 70)
    log.info("EXPERIMENT 2 | probe-rate sweep | servers=%d | load=%.2fx | target=%d qps",
             args.servers, args.load,
             int(round(args.servers * args.per_server_qps * args.load)))
    log.info("rates=%s | step=%ds | warmup=%ds | output=%s",
             ["%.4g" % r for r in args.probe_rates], args.step_seconds,
             args.warmup, run_root)
    log.warning("Repo limitation: LB_PROBE_RATE is currently truncated to int(rate); "
                "fractional rates are not paper-exact.")
    log.info("=" * 70)

    rc, out = sh("sudo docker ps --format '{{.Names}}'")
    if "lb-prequal" not in out:
        log.error("lb-prequal container not found. Run prepare.py first.")
        sys.exit(1)

    capture_lb_template("lb-prequal")
    orig_probe_rate = None
    for e in container_env("lb-prequal"):
        if e.startswith("LB_PROBE_RATE="):
            try:
                orig_probe_rate = float(e.split("=", 1)[1])
            except ValueError:
                pass

    full_raw = get_full_lb_servers()
    healthy, dead = filter_healthy(full_raw)
    if dead:
        log.warning("Skipping %d unreachable backend(s): %s", len(dead),
                    ", ".join(e.split("=")[0] for e in dead))
    if len(healthy) < args.servers:
        log.error("Need %d healthy backends, found %d.", args.servers, len(healthy))
        sys.exit(1)
    full = ",".join(healthy)

    target = max(1, int(round(args.servers * args.per_server_qps * args.load)))
    run_start_epoch = time.time()
    sampler = RIFSampler(args.prequal_url.rstrip("/") + "/metrics",
                         args.rif_sample_interval, run_start_epoch, args.qrif)
    sampler.start()

    all_rows = []
    phases = []
    phase_no = 0
    try:
        for rate in args.probe_rates:
            phase_no += 1
            label, _tex = rate_label(rate)
            effective = int(rate)
            log.info("[%d/%d] probe_rate=%s effective_async_probes=%d",
                     phase_no, len(args.probe_rates), label, effective)
            reconfigure_prequal(full, args.servers, rate, args.qrif)
            time.sleep(4)

            rc, test_out = sh("hey -z 3s -q 5 -c 5 %s/" %
                              args.prequal_url.rstrip("/"), timeout=20)
            if "Requests/sec" not in test_out:
                log.error("hey/self-test failed. Output:\n%s", test_out[:500])
                sys.exit(1)

            if args.warmup > 0:
                q = target / float(args.workers)
                log.info("    warming up %ds at target=%d qps ...",
                         args.warmup, target)
                sh("hey -z %ds -q %.4f -c %d %s/" %
                   (args.warmup, q, args.workers, args.prequal_url.rstrip("/")),
                   timeout=args.warmup + 60)

            raw_path = os.path.join(raw_dir, "phase%02d_probe_%s.csv" %
                                    (phase_no, label.replace("/", "_")))
            log.info("    measuring target=%d qps workers=%d duration=%ds",
                     target, args.workers, args.step_seconds)
            write_status(status_path, {
                "state": "RUNNING",
                "phase": phase_no,
                "total_phases": len(args.probe_rates),
                "probe_rate": rate,
                "probe_label": label,
                "effective_async_probes": effective,
                "target_qps": target,
                "updated": dt.datetime.now().isoformat(timespec="seconds"),
            })

            started_epoch = time.time()
            phase_base = {
                "phase": phase_no,
                "algo": "prequal",
                "servers": args.servers,
                "load_frac": args.load,
                "load_pct": int(round(args.load * 100)),
                "probe_rate": rate,
                "probe_label": label,
                "effective_async_probes": effective,
                "target_qps": target,
                "workers": args.workers,
            }
            sampler.set_phase(phase_base)
            rc, stderr, command = run_hey_csv(args.prequal_url.rstrip("/"),
                                              target, args.step_seconds,
                                              args.workers, raw_path)
            sampler.clear_phase()
            ended_epoch = time.time()
            if rc != 0:
                log.warning("hey returned rc=%d for probe_rate=%s. stderr tail:\n%s",
                            rc, label, "\n".join(stderr.splitlines()[-5:]))

            phase = dict(phase_base)
            phase.update({
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
            write_csv(os.path.join(run_root, "rif_timeseries.csv"),
                      sampler.get_rows(), RIF_FIELDS)
            log.info("    p99=%s p99.9=%s usec (2xx only) | err=%s%% | err_qps=%s",
                     fmt(summary.get("p99_us"), 0), fmt(summary.get("p99_9_us"), 0),
                     fmt(summary.get("error_pct"), 3), fmt(summary.get("error_qps"), 3))
    finally:
        sampler.stop()
        if not args.no_restore:
            try:
                restore_rate = 2.0 if orig_probe_rate is None else orig_probe_rate
                log.info("Restoring lb-prequal LB_PROBE_RATE=%s", restore_rate)
                reconfigure_prequal(full, args.servers, restore_rate, args.qrif)
            except Exception as e:
                log.warning("Could not restore lb-prequal: %s", e)

    rif_rows = sampler.get_rows()
    write_csv(os.path.join(run_root, "rif_timeseries.csv"), rif_rows, RIF_FIELDS)
    plot_experiment(all_rows, phases, rif_rows, run_root, args)

    meta["finished"] = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    write_json(os.path.join(run_root, "metadata.json"), meta)
    write_status(status_path, {
        "state": "DONE",
        "phases": len(phases),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    log.info("=" * 70)
    log.info("EXPERIMENT 2 DONE. Everything is under: %s", run_root)
    log.info("=" * 70)


if __name__ == "__main__":
    main()
