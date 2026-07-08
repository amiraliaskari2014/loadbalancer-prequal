# Load Balancer Prequal

This repository contains a Go implementation of a Prequal-style HTTP load
balancer, a weighted round-robin baseline, synthetic backend servers, and the
CloudLab experiments used to compare the policies under load.

The project is organized around two use cases:

- Local development with Docker Compose.
- Reproducible CloudLab experiments with one backend server per physical node.

## What Is Included

- `cmd/balancer`: HTTP load balancer binary.
- `pkg/loadbalancer`: routing policies and probe logic.
- `backend`: synthetic backend service that reports RIF, latency estimates, and
  CPU/load signals.
- `cmd/bgload`: background load generator used during experiments.
- `docker-compose.yml`: local three-backend demo with Prequal, WRR, Prometheus,
  and Grafana.
- `experiments`: clean CloudLab experiment suite.
- `report`: final report, generated figures, and PDF output.

## Local Quick Start

Requirements:

- Docker
- Docker Compose

Run the local demo:

```bash
docker compose up --build
```

Useful endpoints:

```bash
curl http://localhost:8080/info   # Prequal load balancer
curl http://localhost:8081/info   # Weighted round-robin load balancer
curl http://localhost:8080/       # Send one request through Prequal
curl http://localhost:8081/       # Send one request through WRR
```

Monitoring:

- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3001`
- Grafana credentials: `admin` / `admin`

Stop the local stack:

```bash
docker compose down -v
```

## Load Balancer Configuration

The balancer is configured with environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `LB_PORT` | `8080` | HTTP port inside the container. |
| `LB_ALGORITHM` | `prequal` | Routing policy: `prequal`, `weightedrr`, `roundrobin`, `random`, `leastloaded`, or `ll-po2c`. |
| `LB_SERVERS` | `server1=server1:80,server2=server2:80,server3=server3:80` | Comma-separated backend list. |
| `LB_QRIF` | `0.84` | Prequal RIF quantile threshold. |
| `LB_PROBE_RATE` | `2` | Prequal probes per request. |
| `LB_WEIGHT_INTERVAL` | `3s` | WRR weight refresh interval. |

The backend service accepts:

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8080` | Backend HTTP port. |
| `SERVER_ID` | `unknown` | Server identifier returned in responses. |
| `CPU_LOAD` | `0` | Synthetic contention level. |
| `MAX_CONCURRENCY` | derived from load | Backend active request capacity. |

## CloudLab Experiments

The clean experiment workflow lives in `experiments/`.

Create a CloudLab experiment from:

```text
experiments/cloudlab_profile.py
```

The profile creates:

- `bhost1` through `bhost20`: one physical backend node per server.
- `lb`: load-balancer node.
- `bgload`: background-load node.
- `monitor`: monitoring/helper node.

From your laptop:

```bash
cd experiments

export SSH_OPTS="-A -o ForwardAgent=yes -o IdentitiesOnly=yes \
  -i $HOME/.ssh/cloudlab_ed25519 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=20 \
  -o StrictHostKeyChecking=accept-new"
```

Run the main experiments:

```bash
bash experiment1.sh <username>@<lb-host>
bash experiment1_scaling.sh <username>@<lb-host>
bash experiment2.sh <username>@<lb-host>
```

If the CloudLab containers are already prepared and still running, skip setup:

```bash
SKIP_PREPARE=1 bash experiment1.sh <username>@<lb-host>
SKIP_PREPARE=1 bash experiment1_scaling.sh <username>@<lb-host>
SKIP_PREPARE=1 bash experiment2.sh <username>@<lb-host>
```

Results are copied back automatically to:

```text
experiments/results/
```

## Experiment Summary

### Experiment 1: Load Ramp

Compares WRR and Prequal across load levels:

```text
75%, 83%, 93%, 103%, 114%, 127%, 141%, 157%, 174%
```

Default settings:

- `TOTAL=20`
- `SERVERS=20`
- `PER_SERVER_QPS=25`
- `STEP_SECONDS=240`
- `WORKERS=30`
- `ORDER=wrr-prequal`

Run:

```bash
bash experiment1.sh <username>@<lb-host>
```

### Experiment 1 Scaling

Runs Experiment 1 at 5, 10, and 20 backend servers and creates an aggregate
scaling error plot.

Default worker scaling:

```text
5 servers  -> 8 workers
10 servers -> 15 workers
20 servers -> 30 workers
```

Run:

```bash
bash experiment1_scaling.sh <username>@<lb-host>
```

### Experiment 2: Probe Rate Sweep

Runs a fixed-load Prequal experiment while sweeping the probe rate:

```text
4, 2*sqrt(2), 2, sqrt(2), 1, sqrt(1/2), 1/2
```

Run:

```bash
bash experiment2.sh <username>@<lb-host>
```

## Post-Processing

Experiment 1 has optional post-processing helpers:

```bash
python3 postprocess_smooth.py <experiment1-result-dir>
python3 postprocess_deadline.py <experiment1-result-dir>
```

The smoothing helper redraws less noisy figures. The deadline helper creates a
paper-style latency view where non-2xx responses can be represented using a
5-second deadline policy while preserving the explicit error-rate plot.

## Report

The final written report is in:

```text
report/report.md
```

Figures are stored in:

```text
report/figures/
```

The PDF can be regenerated from `report/report.md` with `md2pdf` when its
native dependencies are installed.

## Notes

- CloudLab builds Docker images on the remote nodes from this repository.
- `experiments/prepare.py` also builds the `hey` load generator from
  `https://github.com/rakyll/hey`.
- The WRR policy here is a local baseline for comparison. It is not a full
  reproduction of Google's production WRR implementation.
