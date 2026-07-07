#!/bin/bash
# Run Experiment 1 at 5, 10, and 20 backend-server scales:
#   bash experiment1_scaling.sh <user>@<lb-hostname>
#
# Output layout:
#   results/results_experiment1_scaling_<timestamp>/
#     servers_5/run_.../experiment1_latency_errors_cpu.png
#     servers_10/run_.../experiment1_latency_errors_cpu.png
#     servers_20/run_.../experiment1_latency_errors_cpu.png
#     experiment1_scaling_errors.png
set -euo pipefail

LB_HOST="${1:?usage: bash experiment1_scaling.sh <user>@<lb-hostname>}"

TOTAL="${TOTAL:-20}"
COUNTS="${COUNTS:-5 10 20}"
PER_SERVER_QPS="${PER_SERVER_QPS:-25}"
STEP_SECONDS="${STEP_SECONDS:-240}"
BIN_SECONDS="${BIN_SECONDS:-5}"
FIXED_WORKERS="${FIXED_WORKERS:-}"
WORKERS_PER_20="${WORKERS_PER_20:-30}"
WORKERS_BY_COUNT="${WORKERS_BY_COUNT:-5:8 10:15 20:30}"
WARMUP="${WARMUP:-15}"
HOST_CPU_INTERVAL="${HOST_CPU_INTERVAL:-5}"
ORDER="${ORDER:-wrr-prequal}"
SKIP_PREPARE="${SKIP_PREPARE:-0}"
NO_HOST_CPU="${NO_HOST_CPU:-0}"
RESUME="${RESUME:-0}"
LOADS="${LOADS:-0.75 0.83 0.93 1.03 1.14 1.27 1.41 1.57 1.74}"
REMOTE_DIR="${REMOTE_DIR:-experiment1_scaling_results}"
FULL_LB_SERVERS="${FULL_LB_SERVERS:-}"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOCAL_OUT="./results/results_experiment1_scaling_${STAMP}"

SSH_OPTS="${SSH_OPTS:--o ForwardAgent=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o StrictHostKeyChecking=accept-new}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/cloudlab_ed25519}"
if [ -f "${SSH_KEY}" ] && [[ " ${SSH_OPTS} " != *" -i "* ]]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

echo ">>> Experiment 1 scaling config"
echo "    lb host        : ${LB_HOST}"
echo "    backend nodes  : ${TOTAL}"
echo "    scale counts   : ${COUNTS}"
echo "    order          : ${ORDER}"
echo "    per-server-qps : ${PER_SERVER_QPS}"
echo "    step seconds   : ${STEP_SECONDS}"
if [ -n "${FIXED_WORKERS}" ]; then
  echo "    workers        : fixed ${FIXED_WORKERS}"
else
  echo "    workers        : scaled ${WORKERS_BY_COUNT} (fallback: ${WORKERS_PER_20}/20 servers)"
fi
echo "    warmup         : ${WARMUP}"
echo "    cpu interval   : ${HOST_CPU_INTERVAL}s"
echo "    host cpu       : $([ "${NO_HOST_CPU}" = "1" ] && echo "disabled" || echo "enabled")"
echo "    resume         : $([ "${RESUME}" = "1" ] && echo "enabled" || echo "disabled")"
echo "    loads          : ${LOADS}"
echo ""

for SERVERS in ${COUNTS}; do
  if [ "${SERVERS}" -gt "${TOTAL}" ]; then
    echo "ERROR: requested ${SERVERS} servers but TOTAL=${TOTAL}." >&2
    exit 2
  fi
done

if [ -z "${FULL_LB_SERVERS}" ]; then
  for i in $(seq 1 "${TOTAL}"); do
    entry="server${i}=10.10.1.${i}:9001"
    if [ -z "${FULL_LB_SERVERS}" ]; then
      FULL_LB_SERVERS="${entry}"
    else
      FULL_LB_SERVERS="${FULL_LB_SERVERS},${entry}"
    fi
  done
fi

workers_for_count() {
  local servers="$1"
  local pair key val
  if [ -n "${FIXED_WORKERS}" ]; then
    echo "${FIXED_WORKERS}"
    return
  fi
  for pair in ${WORKERS_BY_COUNT}; do
    key="${pair%%:*}"
    val="${pair#*:}"
    if [ "${key}" = "${servers}" ]; then
      echo "${val}"
      return
    fi
  done
  echo $(( (servers * WORKERS_PER_20 + 19) / 20 ))
}

echo ">>> [1/6] Copying experiment scripts to ${LB_HOST} ..."
scp ${SSH_OPTS} prepare.py common.py experiment1.py "${LB_HOST}:~/"

if [ "${SKIP_PREPARE}" != "1" ]; then
  echo ">>> [2/6] Preparing CloudLab topology for ${TOTAL} backend containers ..."
  ssh ${SSH_OPTS} "${LB_HOST}" "python3 prepare.py --total ${TOTAL}"
else
  echo ">>> [2/6] SKIP_PREPARE=1 -> using existing containers."
fi

NO_HOST_CPU_ARGS=""
if [ "${NO_HOST_CPU}" = "1" ]; then
  NO_HOST_CPU_ARGS="--no-host-cpu"
fi

echo ">>> [3/6] Running Experiment 1 scaling sequence on ${LB_HOST} ..."
if [ "${RESUME}" = "1" ]; then
  ssh ${SSH_OPTS} "${LB_HOST}" "mkdir -p ~/${REMOTE_DIR}"
else
  ssh ${SSH_OPTS} "${LB_HOST}" "rm -rf ~/${REMOTE_DIR} && mkdir -p ~/${REMOTE_DIR}"
fi

IDX=0
for SERVERS in ${COUNTS}; do
  IDX=$((IDX + 1))
  RUN_WORKERS="$(workers_for_count "${SERVERS}")"
  echo ">>>       scale ${IDX}: servers=${SERVERS} workers=${RUN_WORKERS} -> ~/${REMOTE_DIR}/servers_${SERVERS}"
  if [ "${RESUME}" = "1" ]; then
    DONE_RUN="$(ssh ${SSH_OPTS} "${LB_HOST}" "for s in \$(ls -td ~/${REMOTE_DIR}/servers_${SERVERS}/run_*/STATUS.json 2>/dev/null); do grep -q '\"state\": \"DONE\"' \"\$s\" && { dirname \"\$s\"; exit 0; }; done; exit 1" 2>/dev/null || true)"
    if [ -n "${DONE_RUN}" ]; then
      echo ">>>       scale ${IDX}: servers=${SERVERS} already complete at ${DONE_RUN}; skipping."
      continue
    fi
  fi
  ssh ${SSH_OPTS} "${LB_HOST}" \
    "mkdir -p ~/${REMOTE_DIR}/servers_${SERVERS} && \
     rm -rf ~/${REMOTE_DIR}/servers_${SERVERS}/run_* && \
     python3 ~/experiment1.py \
        --servers ${SERVERS} \
        --per-server-qps ${PER_SERVER_QPS} \
        --step-seconds ${STEP_SECONDS} \
        --bin-seconds ${BIN_SECONDS} \
        --workers ${RUN_WORKERS} \
        --warmup ${WARMUP} \
        --order ${ORDER} \
        --loads ${LOADS} \
        --lb-servers '${FULL_LB_SERVERS}' \
        --host-cpu-interval ${HOST_CPU_INTERVAL} \
        ${NO_HOST_CPU_ARGS} \
        --outdir ~/${REMOTE_DIR}/servers_${SERVERS}"
done

echo ">>> [4/6] Pulling scaling results to ${LOCAL_OUT} ..."
mkdir -p "${LOCAL_OUT}"
if command -v rsync >/dev/null 2>&1; then
  rsync -az -e "ssh ${SSH_OPTS}" "${LB_HOST}:~/${REMOTE_DIR}/" "${LOCAL_OUT}/"
else
  scp ${SSH_OPTS} -r "${LB_HOST}:~/${REMOTE_DIR}/"* "${LOCAL_OUT}/"
fi
scp ${SSH_OPTS} "${LB_HOST}:~/prepare.log" "${LOCAL_OUT}/" 2>/dev/null || true

echo ">>> [5/6] Building scaling error plot ..."
python3 experiment1_scaling_plot.py "${LOCAL_OUT}"

echo ">>> [6/6] Done."
echo ">>> Results: ${LOCAL_OUT}"
echo ">>> Per-scale plots:"
for SERVERS in ${COUNTS}; do
  echo ">>>   ${LOCAL_OUT}/servers_${SERVERS}/run_*/experiment1_latency_errors_cpu.png"
done
echo ">>> Scaling error plot: ${LOCAL_OUT}/experiment1_scaling_errors.png"
