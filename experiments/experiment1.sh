#!/bin/bash
# Run Experiment 1 from your laptop:
#   bash experiment1.sh <user>@<lb-hostname>
#
# Designed for cloudlab_profile.py:
#   20 physical backend nodes, one backend container per node.
#
# Fixes:
#   fixed workers=30 by default, 2xx-only latency percentiles, real host CPU.
#
# Env knobs:
#   SKIP_PREPARE=1 TOTAL=20 SERVERS=20 PER_SERVER_QPS=25 STEP_SECONDS=240
#   WORKERS=30 BIN_SECONDS=5 ORDER=wrr-prequal LOADS="0.75 0.83 ..."
set -euo pipefail

LB_HOST="${1:?usage: bash experiment1.sh <user>@<lb-hostname>}"

TOTAL="${TOTAL:-20}"
SERVERS="${SERVERS:-20}"
PER_SERVER_QPS="${PER_SERVER_QPS:-25}"
STEP_SECONDS="${STEP_SECONDS:-240}"
BIN_SECONDS="${BIN_SECONDS:-5}"
WORKERS="${WORKERS:-30}"
ORDER="${ORDER:-wrr-prequal}"
SKIP_PREPARE="${SKIP_PREPARE:-0}"
LOADS="${LOADS:-0.75 0.83 0.93 1.03 1.14 1.27 1.41 1.57 1.74}"
REMOTE_DIR="${REMOTE_DIR:-experiment1_results}"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOCAL_OUT="./results/results_experiment1_${STAMP}"

SSH_OPTS="${SSH_OPTS:--o ForwardAgent=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o StrictHostKeyChecking=accept-new}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/cloudlab_ed25519}"
if [ -f "${SSH_KEY}" ] && [[ " ${SSH_OPTS} " != *" -i "* ]]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

echo ">>> Experiment 1 config"
echo "    lb host        : ${LB_HOST}"
echo "    backend nodes  : ${TOTAL}"
echo "    measured svrs  : ${SERVERS}"
echo "    order          : ${ORDER}"
echo "    per-server-qps : ${PER_SERVER_QPS}"
echo "    step seconds   : ${STEP_SECONDS}"
echo "    workers        : ${WORKERS}"
echo "    loads          : ${LOADS}"
echo ""

if [ "${TOTAL}" != "${SERVERS}" ]; then
  echo "ERROR: Experiment 1 expects TOTAL and SERVERS to match for one server per node." >&2
  exit 2
fi

echo ">>> [1/5] Copying experiment scripts to ${LB_HOST} ..."
scp ${SSH_OPTS} prepare.py common.py experiment1.py "${LB_HOST}:~/"

if [ "${SKIP_PREPARE}" != "1" ]; then
  echo ">>> [2/5] Preparing CloudLab topology for ${TOTAL} backend containers ..."
  ssh ${SSH_OPTS} "${LB_HOST}" "python3 prepare.py --total ${TOTAL}"
else
  echo ">>> [2/5] SKIP_PREPARE=1 -> using existing containers."
fi

echo ">>> [3/5] Running Experiment 1 on ${LB_HOST} ..."
ssh ${SSH_OPTS} "${LB_HOST}" \
  "rm -rf ~/${REMOTE_DIR} && python3 ~/experiment1.py \
      --servers ${SERVERS} \
      --per-server-qps ${PER_SERVER_QPS} \
      --step-seconds ${STEP_SECONDS} \
      --bin-seconds ${BIN_SECONDS} \
      --workers ${WORKERS} \
      --order ${ORDER} \
      --loads ${LOADS} \
      --outdir ~/${REMOTE_DIR}"

echo ">>> [4/5] Pulling results to ${LOCAL_OUT} ..."
mkdir -p "${LOCAL_OUT}"
if command -v rsync >/dev/null 2>&1; then
  rsync -az -e "ssh ${SSH_OPTS}" "${LB_HOST}:~/${REMOTE_DIR}/" "${LOCAL_OUT}/"
else
  scp ${SSH_OPTS} -r "${LB_HOST}:~/${REMOTE_DIR}/"* "${LOCAL_OUT}/"
fi
scp ${SSH_OPTS} "${LB_HOST}:~/prepare.log" "${LOCAL_OUT}/" 2>/dev/null || true

echo ">>> [5/5] Done."
echo ">>> Results: ${LOCAL_OUT}"
echo ">>> Main plot: ${LOCAL_OUT}/run_*/experiment1_latency_errors_cpu.png"
