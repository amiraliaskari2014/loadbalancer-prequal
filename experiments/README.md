# Prequal Experiments

This folder contains the clean experiment set for the 20-host CloudLab profile.

## Files

- `cloudlab_profile.py`: CloudLab profile, creates `bhost1..bhost20`, `lb`, `bgload`, and `monitor`.
- `prepare.py`: clones the app repo, builds Docker images, and starts services
  for the load-ramp/background-load experiments.
- `common.py`: shared helper code used by the experiments.
- `experiment1.py` / `experiment1.sh`: fixed WRR vs Prequal load-ramp experiment.
- `experiment1_scaling.sh` / `experiment1_scaling_plot.py`: runs Experiment 1 at 5, 10, and 20 servers, then plots scaling errors.
- `experiment2.py` / `experiment2.sh`: probe-rate sweep.
- `postprocess_smooth.py`: optional smoothing for Experiment 1 outputs.
- `postprocess_deadline.py`: optional paper-style 5s deadline post-processing.

## App Repo

`prepare.py` currently clones:

```text
https://github.com/amiraliaskari2014/loadbalancer-prequal.git
```

Docker images for the backend, load balancer, and bgload are built on CloudLab
from that cloned repo under `/opt/prequal`.

## Run

From this `experiments/` folder, after creating a CloudLab experiment with
`cloudlab_profile.py`:

```bash
bash experiment1.sh <username>@<lb-host>
bash experiment1_scaling.sh <username>@<lb-host>
bash experiment2.sh <username>@<lb-host>
```

If containers are already prepared and still running:

```bash
SKIP_PREPARE=1 bash experiment1.sh <username>@<lb-host>
SKIP_PREPARE=1 bash experiment1_scaling.sh <username>@<lb-host>
SKIP_PREPARE=1 bash experiment2.sh <username>@<lb-host>
```

Defaults are `TOTAL=20`, `SERVERS=20`, `PER_SERVER_QPS=25`, and `WORKERS=30`
for single experiments. The scaling runner uses `COUNTS="5 10 20"` and
`WORKERS_BY_COUNT="5:8 10:15 20:30"` by default. To intentionally force one
worker count for every scale, set `FIXED_WORKERS`; plain `WORKERS` is ignored by
the scaling runner to avoid accidental fixed-worker runs. The scaling runner
writes:

```text
results/results_experiment1_scaling_<timestamp>/
  servers_5/run_.../
  servers_10/run_.../
  servers_20/run_.../
  experiment1_scaling_errors.png
```
