#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_experiment3.py -- set up the paper-style QRIF experiment over SSH.

Run this on the lb node through experiment3.sh. It discovers the backend hosts,
installs Docker, clones the repo, builds images, and starts one backend
container per server node.

Unlike prepare.py, this setup is specific to the paper's Figure 9 / QRIF
experiment:

  * no bgload antagonist container
  * odd-numbered replicas are fast
  * even-numbered replicas are slow and do 2x query work
  * all replicas use the same concurrency cap

You can run it directly on the lb node:
    python3 prepare_experiment3.py --total 20
"""

import argparse
import concurrent.futures
import logging
import os
import re
import subprocess
import sys
import time

REPO = "https://github.com/amiraliaskari2014/loadbalancer-prequal.git"
NET = "10.10.1."
LB_IP = NET + "254"
MON_IP = NET + "252"
BASE_PORT = 9000
LOCAL = os.uname().nodename.split(".")[0]

log = logging.getLogger("prepare_experiment3")


def setup_logging():
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt); ch.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.expanduser("~/prepare_experiment3.log")); fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    log.addHandler(ch); log.addHandler(fh)


def run_on(node, script, timeout=1800):
    """Run a bash script on `node` (locally if it's this node, else via ssh)."""
    if node == LOCAL:
        cmd = ["bash", "-c", script]; kw = {}
    else:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
               "-o", "BatchMode=yes", node, "bash -s"]; kw = {"input": script}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ds" % timeout


def sh_local(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def ensure_internal_ssh():
    """Make this node able to SSH to the others.

    CloudLab puts your *public* key in every node's authorized_keys, but no node
    has a private key to offer the others. Since the home dir is NFS-shared, we
    generate a key here and append it to the shared authorized_keys -> every node
    trusts it immediately.
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    key = os.path.join(ssh_dir, "id_ed25519")
    if not os.path.exists(key):
        log.info("Generating an inter-node SSH key on %s ...", LOCAL)
        sh_local("ssh-keygen -t ed25519 -N '' -q -f %s" % key)
    pub = open(key + ".pub").read().strip()
    keymat = pub.split()[1] if len(pub.split()) > 1 else pub
    ak = os.path.join(ssh_dir, "authorized_keys")
    existing = open(ak).read() if os.path.exists(ak) else ""
    if keymat not in existing:
        with open(ak, "a") as f:
            f.write("\n" + pub + "\n")
        os.chmod(ak, 0o600)
        log.info("Authorized the inter-node key via NFS-shared authorized_keys.")
    time.sleep(2)  # let NFS propagate


def discover_hosts(num_fallback):
    names = set()
    try:
        with open("/etc/hosts") as f:
            for line in f:
                for tok in line.split():
                    if re.fullmatch(r"bhost\d+", tok):
                        names.add(tok)
    except OSError:
        pass
    hosts = sorted(names, key=lambda s: int(s[5:]))
    if not hosts:
        log.warning("No bhost* in /etc/hosts; assuming bhost1..bhost%d", num_fallback)
        hosts = ["bhost%d" % i for i in range(1, num_fallback + 1)]
    return hosts


def host_ip(name):
    return NET + name[5:]   # bhost3 -> 10.10.1.3


COMMON = """set -eux
export DEBIAN_FRONTEND=noninteractive
sudo apt-get -o DPkg::Lock::Timeout=600 update
sudo apt-get -o DPkg::Lock::Timeout=600 install -y docker.io git bc gawk curl wget
sudo systemctl enable --now docker
if [ ! -d /opt/prequal/.git ]; then
  sudo git clone %s /opt/prequal
else
  cd /opt/prequal
  sudo git fetch --depth 1 origin
  sudo git reset --hard origin/main
fi
sudo chmod -R a+rX /opt/prequal
cd /opt/prequal
""" % REPO


def backend_script(items):
    s = COMMON + "sudo docker build -f backend/Dockerfile -t backend .\n"
    for (g, port, cpu, conc, work_multiplier, work_base, work_jitter) in items:
        s += "sudo docker rm -f backend_server%d 2>/dev/null || true\n" % g
        s += ("sudo docker run -d --restart unless-stopped --name backend_server%d "
              "--network=host -e PORT=%d -e SERVER_ID=server%d -e CPU_LOAD=%d "
              "-e MAX_CONCURRENCY=%d -e WORK_MULTIPLIER=%.6g "
              "-e WORK_BASE_ITERATIONS=%d -e WORK_JITTER_ITERATIONS=%d backend\n"
              % (g, port, g, cpu, conc, work_multiplier, work_base, work_jitter))
    return s


def lb_script(lb_servers):
    return COMMON + (
        "sudo docker build -f Dockerfile -t lb .\n"
        "sudo docker rm -f lb-prequal lb-weightedrr 2>/dev/null || true\n"
        "sudo docker run -d --restart unless-stopped --name lb-prequal --network=host"
        " -e LB_PORT=8080 -e LB_ALGORITHM=prequal -e 'LB_SERVERS=%s'"
        " -e LB_QRIF=0.84 -e LB_PROBE_RATE=3 lb\n"
        "sudo docker run -d --restart unless-stopped --name lb-weightedrr --network=host"
        " -e LB_PORT=8081 -e LB_ALGORITHM=weightedrr -e 'LB_SERVERS=%s'"
        " -e LB_WEIGHT_INTERVAL=3600s lb\n"
        "sudo apt-get -o DPkg::Lock::Timeout=600 install -y golang-go git curl\n"
        "sudo rm -f /usr/local/bin/hey /usr/bin/hey\n"
        # rakyll/hey + its deps now need Go >= 1.21 (use of builtin max). Ubuntu 22.04
        # ships 1.18, and the prebuilt binaries 403. So: ensure a recent Go, then build.
        "GO=go\n"
        "MINOR=$(go version | grep -oP '1\\.\\K[0-9]+' | head -1)\n"
        "if [ \"${MINOR:-0}\" -lt 24 ]; then\n"
        "  curl -fLs -o /tmp/go.tgz https://go.dev/dl/go1.24.0.linux-amd64.tar.gz\n"
        "  sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz\n"
        "  GO=/usr/local/go/bin/go\n"
        "fi\n"
        "rm -rf /tmp/heybuild\n"
        "git clone --depth 1 https://github.com/rakyll/hey /tmp/heybuild\n"
        # local toolchain only; writable /tmp build cache (NFS ~/.cache is read-only)
        "( cd /tmp/heybuild && GOTOOLCHAIN=local GOCACHE=/tmp/gocache GOPATH=/tmp/gopath "
        "GOFLAGS=-mod=mod GO111MODULE=on $GO build -o /tmp/hey . )\n"
        "sudo install -m755 /tmp/hey /usr/local/bin/hey\n"
        "sudo ln -sf /usr/local/bin/hey /usr/bin/hey\n"
        # self-test: hey must actually RUN and emit a summary
        "HEY_OUT=$(/usr/local/bin/hey -z 1s -q 1 -c 1 http://localhost:8080/ 2>&1 || true)\n"
        "echo \"$HEY_OUT\" | grep -q 'Requests/sec' "
        "|| { echo 'FATAL: hey does not run:'; echo \"$HEY_OUT\" | head -5; exit 1; }\n"
        "echo 'hey OK'\n"
        "sudo apt-get -o DPkg::Lock::Timeout=600 install -y python3 python3-pip python3-numpy python3-matplotlib\n"
        % (lb_servers, lb_servers))


def stop_bgload_script():
    return COMMON + "sudo docker rm -f bgload 2>/dev/null || true\n"


def monitor_script():
    return COMMON + (
        "sudo sed -i 's|lb-prequal:8080|%s:8080|g' config/prometheus/prometheus.yml\n"
        "sudo sed -i 's|lb-weightedrr:8080|%s:8081|g' config/prometheus/prometheus.yml\n"
        "sudo sed -i 's|http://prometheus:9090|http://localhost:9090|g'"
        " config/grafana/provisioning/datasources/*.y*ml 2>/dev/null || true\n"
        "sudo docker rm -f prometheus grafana 2>/dev/null || true\n"
        "sudo docker run -d --restart unless-stopped --name prometheus --network=host"
        " -v /opt/prequal/config/prometheus:/etc/prometheus:ro prom/prometheus:v2.51.0"
        " --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus\n"
        "sudo docker run -d --restart unless-stopped --name grafana --network=host"
        " -e GF_SECURITY_ADMIN_PASSWORD=admin -e GF_USERS_ALLOW_SIGN_UP=false"
        " -e GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/loadbalancer.json"
        " -v /opt/prequal/config/grafana/provisioning:/etc/grafana/provisioning:ro"
        " -v /opt/prequal/config/grafana/dashboards:/var/lib/grafana/dashboards:ro"
        " grafana/grafana:10.4.2\n" % (LB_IP, LB_IP))


def task(node, script, retries=2):
    for attempt in range(1, retries + 2):
        log.info("[%s] setup attempt %d ...", node, attempt)
        rc, out = run_on(node, script)
        tail = "\n".join(out.strip().splitlines()[-4:])
        if rc == 0:
            log.info("[%s] OK", node)
            log.debug("[%s] output tail:\n%s", node, tail)
            return node, True, tail
        log.warning("[%s] failed (rc=%d). tail:\n%s", node, rc, tail)
        time.sleep(5)
    return node, False, tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=20, help="total backend containers")
    ap.add_argument("--per-host", type=int, default=0, help="containers per host (0=auto)")
    ap.add_argument("--num-hosts", type=int, default=20, help="fallback if discovery fails")
    ap.add_argument("--fast-work", type=float, default=1.0)
    ap.add_argument("--slow-work", type=float, default=2.0)
    ap.add_argument("--max-concurrency", type=int, default=4)
    ap.add_argument("--work-base-iterations", type=int, default=5000)
    ap.add_argument("--work-jitter-iterations", type=int, default=5000)
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()

    setup_logging()
    log.info("=" * 70)
    log.info("PREPARE EXPERIMENT 3 | this node = %s | total backends = %d", LOCAL, args.total)

    ensure_internal_ssh()

    hosts = discover_hosts(args.num_hosts)
    nh = len(hosts)

    # preflight: confirm inter-node SSH actually works before the long setup
    probe = hosts[0]
    rc, out = run_on(probe, "echo ok", timeout=20)
    if rc != 0:
        log.error("Inter-node SSH to %s still failing:\n%s", probe, out.strip()[-300:])
        log.error("Fix: from your laptop run  ssh -A <username>@<lb-host>  "
                  "(agent forwarding), or ensure your key is loaded (ssh-add) and retry.")
        sys.exit(2)
    log.info("Inter-node SSH OK (reached %s).", probe)
    per = args.per_host or ((args.total + nh - 1) // nh)
    log.info("backend hosts: %s", hosts)
    log.info("per-host=%d", per)
    log.info("fast_work=%.3f slow_work=%.3f max_concurrency=%d work_iters=%d+%d",
             args.fast_work, args.slow_work, args.max_concurrency,
             args.work_base_iterations, args.work_jitter_iterations)

    # Round-robin assignment, sequential server numbering. Odd replicas are fast,
    # even replicas are slow, matching the paper's Figure 9 setup.
    port_ctr = {h: 0 for h in hosts}
    server_entries, host_items = [], {h: [] for h in hosts}
    fast_count = slow_count = 0
    for g in range(1, args.total + 1):
        h = hosts[(g - 1) % nh]
        port_ctr[h] += 1
        port = BASE_PORT + port_ctr[h]
        is_fast = (g % 2 == 1)
        work_multiplier = args.fast_work if is_fast else args.slow_work
        fast_count += 1 if is_fast else 0
        slow_count += 0 if is_fast else 1
        cpu, conc = 0, args.max_concurrency
        server_entries.append("server%d=%s:%d" % (g, host_ip(h), port))
        host_items[h].append((g, port, cpu, conc, work_multiplier,
                              args.work_base_iterations,
                              args.work_jitter_iterations))
    lb_servers = ",".join(server_entries)
    log.info("LB_SERVERS has %d backends (%d fast, %d slow)",
             len(server_entries), fast_count, slow_count)

    tasks = [(h, backend_script(host_items[h])) for h in hosts]
    tasks.append(("lb", lb_script(lb_servers)))
    tasks.append(("bgload", stop_bgload_script()))
    tasks.append(("monitor", monitor_script()))

    log.info("Setting up %d nodes (parallel, jobs=%d)... this takes a few minutes.", len(tasks), args.jobs)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(task, n, s): n for n, s in tasks}
        for fut in concurrent.futures.as_completed(futs):
            node, ok, _tail = fut.result()
            results[node] = ok

    ok_nodes = [n for n, ok in results.items() if ok]
    bad_nodes = [n for n, ok in results.items() if not ok]
    log.info("-" * 70)
    log.info("setup OK on: %s", ok_nodes)
    if bad_nodes:
        log.warning("setup FAILED on: %s (see ~/prepare_experiment3.log)", bad_nodes)

    # verify: LBs up + how many backends actually answer
    log.info("Verifying...")
    rc, ps = sh_local("sudo docker ps --format '{{.Names}}'")
    for need in ("lb-prequal", "lb-weightedrr"):
        log.info("  %s: %s", need, "UP" if need in ps else "MISSING")
    healthy = 0
    for entry in server_entries:
        ip_port = entry.split("=", 1)[1]
        ip, port = ip_port.split(":")
        rc, _ = sh_local("curl -sS -m3 -o /dev/null http://%s:%s/" % (ip, port), timeout=6)
        if rc == 0:
            healthy += 1
    log.info("  backends answering: %d / %d", healthy, len(server_entries))
    log.info("=" * 70)
    if healthy == 0 or "lb-prequal" not in ps:
        log.error("Setup incomplete. Fix the failed nodes above, then re-run prepare_experiment3.py.")
        sys.exit(1)
    log.info("PREPARE EXPERIMENT 3 DONE. You can now run experiment3.py.")


if __name__ == "__main__":
    main()
