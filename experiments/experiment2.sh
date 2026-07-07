#!/bin/bash
# Run Experiment 2 from your laptop:
#   bash experiment2.sh <user>@<lb-hostname>
#
# Probe-rate sweep, Figure-8-style:
#   fixed hot load, Prequal only, seven probe-rate windows.
#
# Designed for cloudlab_profile.py:
#   20 physical backend nodes, one backend container per node.
#
# Env knobs:
#   SKIP_PREPARE=1 TOTAL=20 SERVERS=20 PER_SERVER_QPS=25 LOAD=1.5
#   STEP_SECONDS=240 WORKERS=30 BIN_SECONDS=5 WARMUP=15
#   PROBE_RATES="4 2.8284271247461903 2 1.4142135623730951 1 0.7071067811865476 0.5"
set -euo pipefail

LB_HOST="${1:?usage: bash experiment2.sh <user>@<lb-hostname>}"

TOTAL="${TOTAL:-20}"
SERVERS="${SERVERS:-20}"
PER_SERVER_QPS="${PER_SERVER_QPS:-25}"
LOAD="${LOAD:-1.5}"
STEP_SECONDS="${STEP_SECONDS:-240}"
BIN_SECONDS="${BIN_SECONDS:-5}"
WORKERS="${WORKERS:-30}"
WARMUP="${WARMUP:-15}"
QRIF="${QRIF:-0.84}"
RIF_SAMPLE_INTERVAL="${RIF_SAMPLE_INTERVAL:-0.5}"
SMOOTH_BINS="${SMOOTH_BINS:-9}"
RIF_SMOOTH_SAMPLES="${RIF_SMOOTH_SAMPLES:-60}"
SKIP_PREPARE="${SKIP_PREPARE:-0}"
PROBE_RATES="${PROBE_RATES:-4 2.8284271247461903 2 1.4142135623730951 1 0.7071067811865476 0.5}"
REMOTE_DIR="${REMOTE_DIR:-experiment2_results}"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOCAL_OUT="./results/results_experiment2_${STAMP}"

SSH_OPTS="${SSH_OPTS:--o ForwardAgent=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o StrictHostKeyChecking=accept-new}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/cloudlab_ed25519}"
if [ -f "${SSH_KEY}" ] && [[ " ${SSH_OPTS} " != *" -i "* ]]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

echo ">>> Experiment 2 config"
echo "    lb host        : ${LB_HOST}"
echo "    backend nodes  : ${TOTAL}"
echo "    measured svrs  : ${SERVERS}"
echo "    load           : ${LOAD}x allocation"
echo "    per-server-qps : ${PER_SERVER_QPS}"
echo "    step seconds   : ${STEP_SECONDS}"
echo "    workers        : ${WORKERS}"
echo "    warmup         : ${WARMUP}"
echo "    qrif           : ${QRIF}"
echo "    probe rates    : ${PROBE_RATES}"
echo ""

if [ "${TOTAL}" != "${SERVERS}" ]; then
  echo "ERROR: Experiment 2 expects TOTAL and SERVERS to match for one-server-per-node runs." >&2
  exit 2
fi

echo ">>> [1/5] Copying experiment scripts to ${LB_HOST} ..."
scp ${SSH_OPTS} prepare.py common.py experiment2.py "${LB_HOST}:~/"

if [ "${SKIP_PREPARE}" != "1" ]; then
  echo ">>> [2/5] Preparing CloudLab topology for ${TOTAL} backend containers ..."
  ssh ${SSH_OPTS} "${LB_HOST}" "python3 prepare.py --total ${TOTAL}"
else
  echo ">>> [2/5] SKIP_PREPARE=1 -> using existing containers."
fi

echo ">>> [3/5] Running Experiment 2 on ${LB_HOST} ..."
ssh ${SSH_OPTS} "${LB_HOST}" \
  "rm -rf ~/${REMOTE_DIR} && python3 ~/experiment2.py \
      --servers ${SERVERS} \
      --per-server-qps ${PER_SERVER_QPS} \
      --load ${LOAD} \
      --step-seconds ${STEP_SECONDS} \
      --bin-seconds ${BIN_SECONDS} \
      --workers ${WORKERS} \
      --warmup ${WARMUP} \
      --qrif ${QRIF} \
      --rif-sample-interval ${RIF_SAMPLE_INTERVAL} \
      --smooth-bins ${SMOOTH_BINS} \
      --rif-smooth-samples ${RIF_SMOOTH_SAMPLES} \
      --probe-rates ${PROBE_RATES} \
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
echo ">>> Main plot: ${LOCAL_OUT}/run_*/experiment2_probe_rate.png"
