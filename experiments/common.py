#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared helpers for the CloudLab experiment scripts.

This module also keeps a standalone stepped-load runner for quick debugging,
but the main user-facing experiments are experiment1.py, experiment2.py, and
the Experiment 1 scaling runner.
"""

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.request

DEFAULT_LOADS = [0.75, 0.83, 0.93, 1.03, 1.14, 1.27, 1.41, 1.57, 1.74]
DEFAULT_STEP_SECONDS = 240
DEFAULT_COOLDOWN_SECONDS = 240  # 8 gaps * 240s = 32 min cooling, like the reference plot
DEFAULT_BIN_SECONDS = 5

ALGO = {
    "prequal": {"container": "lb-prequal", "url": "http://localhost:8080"},
    "wrr": {"container": "lb-weightedrr", "url": "http://localhost:8081"},
}

log = logging.getLogger("common")
LB_TEMPLATES = {}


def setup_logging(outdir):
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(os.path.join(outdir, "common.log"))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(ch)
    log.addHandler(fh)


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def fetch(url):
    try:
        return urllib.request.urlopen(url, timeout=5).read().decode("utf-8", "replace")
    except Exception as e:
        return "FETCH_ERROR %s\n" % e


def container_env(name):
    rc, out = sh("sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' %s" % name)
    return [l for l in out.splitlines() if l.strip()] if rc == 0 else []


def container_image(name):
    rc, out = sh("sudo docker inspect -f '{{.Config.Image}}' %s" % name)
    return out.strip() if rc == 0 else "lb"


def capture_lb_template(name):
    env = [e for e in container_env(name)
           if e.startswith("LB_") and not e.startswith("LB_SERVERS=")]
    image = container_image(name)
    LB_TEMPLATES[name] = (env, image)
    log.debug("template[%s] env=%s image=%s", name, env, image)
    if not env:
        raise RuntimeError("%s container/env not found; run prepare.py first" % name)


def get_full_lb_servers():
    for name in ("lb-prequal", "lb-weightedrr"):
        for e in container_env(name):
            if e.startswith("LB_SERVERS="):
                return e.split("=", 1)[1]
    return ""


def filter_healthy(full_servers):
    healthy, dead = [], []
    for entry in [e.strip() for e in full_servers.split(",") if e.strip()]:
        try:
            _name, addr = entry.split("=", 1)
            ip, port = addr.split(":", 1)
        except ValueError:
            continue
        rc, _ = sh("curl -sS -m3 -o /dev/null http://%s:%s/" % (ip, port), timeout=6)
        (healthy if rc == 0 else dead).append(entry)
    return healthy, dead


def reconfigure_lb(name, full_servers, n):
    subset = ",".join([s for s in full_servers.split(",") if s.strip()][:n])
    env, image = LB_TEMPLATES[name]
    env_args = " ".join("-e '%s'" % e for e in env) + " -e 'LB_SERVERS=%s'" % subset
    sh("sudo docker rm -f %s 2>/dev/null || true" % name)
    rc, out = sh("sudo docker run -d --restart unless-stopped --name %s --network=host %s %s"
                 % (name, env_args, image), timeout=60)
    if rc != 0:
        raise RuntimeError("failed to recreate %s: %s" % (name, out.strip()))


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = max(1, int(math.ceil((q / 100.0) * len(sorted_vals))))
    return sorted_vals[min(k - 1, len(sorted_vals) - 1)]


def fmt(v, digits=1):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%." + str(digits) + "f") % v
    return str(v)


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_status(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def run_hey_csv(url, target_qps, duration, workers, raw_path):
    workers = max(1, int(workers))
    q = target_qps / float(workers)
    cmd = ["hey", "-z", "%ds" % duration, "-q", "%.4f" % q,
           "-c", str(workers), "-o", "csv", url]
    stderr = ""
    rc = 0
    try:
        with open(raw_path, "w") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True,
                               timeout=duration + 180)
        rc = p.returncode
        stderr = p.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = 124
        stderr = "HEY TIMEOUT after %ds\n%s" % (duration + 180, e)
    except FileNotFoundError:
        rc = 127
        stderr = "hey not found\n"
    with open(raw_path + ".stderr", "w") as f:
        f.write(" ".join(cmd) + "\n\n" + stderr)
    return rc, stderr, " ".join(cmd)


def parse_phase_csv(raw_path, phase, bin_seconds):
    bins = {}
    all_lats = []
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

            lat_us = rt_s * 1000000.0
            idx = max(0, int(offset // bin_seconds))
            b = bins.setdefault(idx, {"lats": [], "total": 0, "ok": 0, "err": 0, "dist": {}})
            b["lats"].append(lat_us)
            b["total"] += 1
            total += 1
            all_lats.append(lat_us)
            key = str(code) if code else "unknown"
            b["dist"][key] = b["dist"].get(key, 0) + 1
            status_dist[key] = status_dist.get(key, 0) + 1
            if 200 <= code < 300:
                b["ok"] += 1
                ok += 1
            else:
                b["err"] += 1
                err += 1

    rows = []
    n_bins = int(math.ceil(phase["duration_s"] / float(bin_seconds)))
    for idx in range(n_bins):
        b = bins.get(idx, {"lats": [], "total": 0, "ok": 0, "err": 0, "dist": {}})
        lats = sorted(b["lats"])
        ts_epoch = phase["start_epoch"] + min(phase["duration_s"], (idx + 0.5) * bin_seconds)
        row = {
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
        }
        rows.append(row)

    all_lats.sort()
    summary = dict(phase)
    summary.update({
        "total_responses": total,
        "ok_responses": ok,
        "error_responses": err,
        "error_pct": (100.0 * err / total) if total else None,
        "response_qps": total / float(phase["duration_s"]) if phase["duration_s"] else None,
        "error_qps": err / float(phase["duration_s"]) if phase["duration_s"] else None,
        "p50_us": percentile(all_lats, 50),
        "p90_us": percentile(all_lats, 90),
        "p99_us": percentile(all_lats, 99),
        "p99_9_us": percentile(all_lats, 99.9),
        "avg_us": (sum(all_lats) / len(all_lats)) if all_lats else None,
        "status_dist": json.dumps(status_dist, sort_keys=True),
    })
    return rows, summary


def cooldown_rows(cooldown, bin_seconds):
    rows = []
    for ts_epoch in (cooldown["start_epoch"], cooldown["end_epoch"]):
        rows.append({
            "algo": cooldown["algo"],
            "servers": cooldown["servers"],
            "load_frac": "",
            "load_pct": "",
            "target_qps": 0,
            "workers": 0,
            "bin_seconds": bin_seconds,
            "bin_index": "",
            "timestamp": dt.datetime.fromtimestamp(ts_epoch).isoformat(timespec="seconds"),
            "elapsed_s": round(ts_epoch - cooldown["run_start_epoch"], 3),
            "p50_us": None,
            "p90_us": None,
            "p99_us": None,
            "p99_9_us": None,
            "avg_us": None,
            "total_responses": 0,
            "ok_responses": 0,
            "error_responses": 0,
            "response_qps": 0.0,
            "error_qps": 0.0,
            "status_dist": "{}",
        })
    return rows


def plot_experiment(rows, phases, cooldowns, outdir, args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        log.warning("matplotlib unavailable (%s); skipping plot.", e)
        return

    def parse_ts(s):
        return dt.datetime.fromisoformat(s)

    xs = [parse_ts(r["timestamp"]) for r in rows]

    def ys(metric):
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

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 9), sharex=True,
        gridspec_kw={"height_ratios": [1.15, 1.0]},
    )

    for i, p in enumerate(phases):
        start = dt.datetime.fromisoformat(p["started"])
        end = dt.datetime.fromisoformat(p["ended"])
        for ax in (ax1, ax2):
            if i % 2 == 0:
                ax.axvspan(start, end, color="#eeeeee", alpha=0.8, lw=0)
            ax.axvline(start, color="#777777", lw=0.8, alpha=0.55)
    for c in cooldowns:
        start = dt.datetime.fromisoformat(c["started"])
        end = dt.datetime.fromisoformat(c["ended"])
        for ax in (ax1, ax2):
            ax.axvspan(start, end, color="#ffffff", alpha=1.0, lw=0)
            ax.axvline(start, color="#d0d0d0", lw=0.7, alpha=0.65)

    alloc_phase = next((p for p in phases if float(p["load_frac"]) > 1.0), None)
    if alloc_phase:
        boundary = dt.datetime.fromisoformat(alloc_phase["started"])
        for ax in (ax1, ax2):
            ax.axvline(boundary, color="#b8b8b8", lw=5, ls="--", alpha=0.9)

    ax1.plot(xs, ys("p50_us"), color="#ff3333", lw=1.8, label="Query Latency 50pct (usec)")
    ax1.plot(xs, ys("p90_us"), color="#35c94a", lw=1.8, label="Query Latency 90pct (usec)")
    ax1.plot(xs, ys("p99_us"), color="#2f9bff", lw=1.8, label="Query Latency 99pct (usec)")
    ax1.plot(xs, ys("p99_9_us"), color="#d642ff", lw=1.8, label="Query Latency 99.9pct (usec)")
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
    ax1.legend(loc="upper center", ncol=4, fontsize=9, frameon=False)
    ax1.set_ylabel("Latency (usec)")
    ax1.text(0.5, -0.25, "(a) Tail Latency", transform=ax1.transAxes,
             ha="center", va="center", fontsize=24, fontweight="bold")

    ax2.plot(xs, ys("error_qps"), color="#ff3333", lw=1.8, label="QPS")
    ax2.grid(True, ls="--", color="#bfbfbf", alpha=0.75)
    ax2.set_ylabel("Error QPS")
    ax2.legend(loc="upper right", fontsize=10, frameon=False)
    ax2.text(0.5, -0.22, "(b) Errors", transform=ax2.transAxes,
             ha="center", va="center", fontsize=24, fontweight="bold")

    if phases:
        first_start = dt.datetime.fromisoformat(phases[0]["started"])
        last_end = dt.datetime.fromisoformat(phases[-1]["ended"])
        if cooldowns:
            last_end = max(last_end, dt.datetime.fromisoformat(cooldowns[-1]["ended"]))
        ax2.set_xlim(first_start, last_end)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

    for p in phases:
        start = dt.datetime.fromisoformat(p["started"])
        end = dt.datetime.fromisoformat(p["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.13, "%d%%" % int(round(float(p["load_frac"]) * 100)),
                 transform=ax1.get_xaxis_transform(), ha="center", va="bottom",
                 fontsize=16, fontweight="bold", clip_on=False)

    below = [p for p in phases if float(p["load_frac"]) < 1.0]
    above = [p for p in phases if float(p["load_frac"]) > 1.0]
    if below:
        start = dt.datetime.fromisoformat(below[0]["started"])
        end = dt.datetime.fromisoformat(below[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.32, "Below Alloc <-", transform=ax1.get_xaxis_transform(),
                 ha="center", va="bottom", fontsize=20, fontweight="bold",
                 clip_on=False)
    if above:
        start = dt.datetime.fromisoformat(above[0]["started"])
        end = dt.datetime.fromisoformat(above[-1]["ended"])
        mid = start + (end - start) / 2
        ax1.text(mid, 1.32, "-> Above Alloc", transform=ax1.get_xaxis_transform(),
                 ha="center", va="bottom", fontsize=20, fontweight="bold",
                 clip_on=False)

    fig.suptitle("Standalone Load Ramp | %d servers | %s | %ds load + %ds cooldown"
                 % (args.servers, args.algo, args.step_seconds, args.cooldown_seconds),
                 y=0.985, fontsize=14)
    fig.tight_layout(rect=[0.03, 0.04, 0.99, 0.93])
    png = os.path.join(outdir, "common_tail_latency_errors.png")
    pdf = os.path.join(outdir, "common_tail_latency_errors.pdf")
    fig.savefig(png, dpi=300)
    fig.savefig(pdf)
    plt.close(fig)
    log.info("plot -> %s", png)


TS_FIELDS = ["algo", "servers", "load_frac", "load_pct", "target_qps", "workers",
             "bin_seconds", "bin_index", "timestamp", "elapsed_s", "p50_us",
             "p90_us", "p99_us", "p99_9_us", "avg_us", "total_responses",
             "ok_responses", "error_responses", "response_qps", "error_qps",
             "status_dist"]

PHASE_FIELDS = ["phase", "algo", "servers", "load_frac", "load_pct", "target_qps",
                "workers", "duration_s", "started", "ended", "command", "returncode",
                "total_responses", "ok_responses", "error_responses", "error_pct",
                "response_qps", "error_qps", "p50_us", "p90_us", "p99_us",
                "p99_9_us", "avg_us", "status_dist"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", type=int, default=20)
    ap.add_argument("--per-server-qps", type=int, default=25)
    ap.add_argument("--loads", type=float, nargs="+", default=DEFAULT_LOADS)
    ap.add_argument("--step-seconds", type=int, default=DEFAULT_STEP_SECONDS)
    ap.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS,
                    help="idle time between load steps; no cooldown after the last step")
    ap.add_argument("--bin-seconds", type=int, default=DEFAULT_BIN_SECONDS)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--warmup", type=int, default=15)
    ap.add_argument("--algo", choices=sorted(ALGO), default="prequal")
    ap.add_argument("--prequal-url", default="http://localhost:8080")
    ap.add_argument("--wrr-url", default="http://localhost:8081")
    ap.add_argument("--outdir", default="common_results")
    args = ap.parse_args()

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = os.path.join(os.path.expanduser(args.outdir), "run_%s" % ts)
    raw_dir = os.path.join(run_root, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    setup_logging(run_root)
    status_path = os.path.join(run_root, "STATUS.json")

    if args.bin_seconds <= 0 or args.step_seconds <= 0 or args.cooldown_seconds < 0:
        log.error("--bin-seconds/--step-seconds must be positive; --cooldown-seconds must be >= 0")
        sys.exit(2)
    if args.bin_seconds > args.step_seconds:
        log.error("--bin-seconds cannot exceed --step-seconds")
        sys.exit(2)

    meta = {
        "mode": "common_helper",
        "servers": args.servers,
        "per_server_qps": args.per_server_qps,
        "loads": args.loads,
        "step_seconds": args.step_seconds,
        "cooldown_seconds": args.cooldown_seconds,
        "bin_seconds": args.bin_seconds,
        "algo": args.algo,
        "hostname": os.uname().nodename,
        "started": ts,
    }
    with open(os.path.join(run_root, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    container = ALGO[args.algo]["container"]
    url = args.prequal_url if args.algo == "prequal" else args.wrr_url

    log.info("=" * 70)
    log.info("STANDALONE LOAD RAMP | algo=%s | servers=%d | per_server_qps=%d",
             args.algo, args.servers, args.per_server_qps)
    total_seconds = len(args.loads) * args.step_seconds + max(0, len(args.loads) - 1) * args.cooldown_seconds
    log.info("loads=%s | step=%ds | cooldown=%ds | bin=%ds | total~%.1f min | output=%s",
             args.loads, args.step_seconds, args.cooldown_seconds, args.bin_seconds,
             total_seconds / 60.0, run_root)
    log.info("=" * 70)

    rc, out = sh("sudo docker ps --format '{{.Names}}'")
    if container not in out:
        log.error("%s container not found. Run prepare.py first.", container)
        sys.exit(1)

    capture_lb_template(container)
    full_raw = get_full_lb_servers()
    healthy, dead = filter_healthy(full_raw)
    if dead:
        log.warning("Skipping %d unreachable backend(s): %s", len(dead),
                    ", ".join(e.split("=")[0] for e in dead))
    if len(healthy) < args.servers:
        log.error("Need %d healthy backends, found %d.", args.servers, len(healthy))
        sys.exit(1)
    full = ",".join(healthy)
    log.info("Configuring %s to first %d healthy backends...", container, args.servers)
    reconfigure_lb(container, full, args.servers)
    time.sleep(4)

    rc, out = sh("hey -z 3s -q 5 -c 5 %s/" % url, timeout=20)
    if "Requests/sec" not in out:
        log.error("hey/self-test failed for %s. Output:\n%s", url, out[:500])
        sys.exit(1)
    if args.warmup > 0:
        log.info("Warming up (%ds)...", args.warmup)
        sh("hey -z %ds -q 1 -c %d %s/" % (args.warmup, max(args.servers, 5), url),
           timeout=args.warmup + 30)

    all_rows = []
    phases = []
    cooldowns = []
    run_start_epoch = time.time()
    for i, load in enumerate(args.loads, 1):
        target = max(1, int(round(args.servers * args.per_server_qps * load)))
        workers = args.workers or max(10, min(target, 200))
        raw_path = os.path.join(raw_dir, "phase%02d_load%03d_%s.csv"
                                % (i, int(round(load * 100)), args.algo))
        log.info("[%d/%d] load=%d%% target=%d qps workers=%d duration=%ds",
                 i, len(args.loads), int(round(load * 100)), target, workers,
                 args.step_seconds)
        write_status(status_path, {
            "state": "RUNNING",
            "phase": i,
            "total_phases": len(args.loads),
            "load_frac": load,
            "target_qps": target,
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        })

        started_epoch = time.time()
        rc, stderr, command = run_hey_csv(url, target, args.step_seconds, workers, raw_path)
        ended_epoch = time.time()
        if rc != 0:
            log.warning("hey returned rc=%d for load=%s. stderr tail:\n%s",
                        rc, load, "\n".join(stderr.splitlines()[-5:]))

        phase = {
            "phase": i,
            "algo": args.algo,
            "servers": args.servers,
            "load_frac": load,
            "load_pct": int(round(load * 100)),
            "target_qps": target,
            "workers": workers,
            "duration_s": args.step_seconds,
            "start_epoch": started_epoch,
            "run_start_epoch": run_start_epoch,
            "started": dt.datetime.fromtimestamp(started_epoch).isoformat(timespec="seconds"),
            "ended": dt.datetime.fromtimestamp(ended_epoch).isoformat(timespec="seconds"),
            "command": command,
            "returncode": rc,
        }
        rows, summary = parse_phase_csv(raw_path, phase, args.bin_seconds)
        all_rows.extend(rows)
        phases.append(summary)

        write_csv(os.path.join(run_root, "timeseries.csv"), all_rows, TS_FIELDS)
        write_csv(os.path.join(run_root, "phases.csv"), phases, PHASE_FIELDS)
        log.info("    p50=%s p90=%s p99=%s p99.9=%s usec | err=%s%% | err_qps=%s",
                 fmt(summary.get("p50_us"), 0), fmt(summary.get("p90_us"), 0),
                 fmt(summary.get("p99_us"), 0), fmt(summary.get("p99_9_us"), 0),
                 fmt(summary.get("error_pct"), 2), fmt(summary.get("error_qps"), 2))

        if args.cooldown_seconds > 0 and i < len(args.loads):
            cool_start = time.time()
            log.info("    cooldown for %ds before next load step...", args.cooldown_seconds)
            write_status(status_path, {
                "state": "COOLDOWN",
                "after_phase": i,
                "total_phases": len(args.loads),
                "cooldown_seconds": args.cooldown_seconds,
                "updated": dt.datetime.now().isoformat(timespec="seconds"),
            })
            time.sleep(args.cooldown_seconds)
            cool_end = time.time()
            cooldown = {
                "after_phase": i,
                "algo": args.algo,
                "servers": args.servers,
                "duration_s": args.cooldown_seconds,
                "start_epoch": cool_start,
                "end_epoch": cool_end,
                "run_start_epoch": run_start_epoch,
                "started": dt.datetime.fromtimestamp(cool_start).isoformat(timespec="seconds"),
                "ended": dt.datetime.fromtimestamp(cool_end).isoformat(timespec="seconds"),
            }
            cooldowns.append(cooldown)
            all_rows.extend(cooldown_rows(cooldown, args.bin_seconds))
            write_csv(os.path.join(run_root, "timeseries.csv"), all_rows, TS_FIELDS)
            with open(os.path.join(run_root, "cooldowns.json"), "w") as f:
                json.dump(cooldowns, f, indent=2)

    plot_experiment(all_rows, phases, cooldowns, run_root, args)
    meta["finished"] = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(run_root, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)
    write_status(status_path, {
        "state": "DONE",
        "phases": len(args.loads),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    })
    log.info("=" * 70)
    log.info("STANDALONE LOAD RAMP DONE. Everything is under: %s", run_root)
    log.info("=" * 70)


if __name__ == "__main__":
    main()
