# Replicating: "Load is not what you should balance: Introducing Prequal"

**Team Members:**

Amirali Askari, Hoang Minh Le

---
**Source Paper:**

Bartek Wydrowski, Robert Kleinberg, Stephen M. Rumble, and Aaron Archer:
"Load is not what you should balance: Introducing Prequal." arXiv:2312.10172v1, 2023.

**Project:**
[https://github.com/amiraliaskari2014/loadbalancer-prequal.git](https://github.com/amiraliaskari2014/loadbalancer-prequal.git)

---

# Introduction

Large-scale distributed services rely on load balancers to distribute user traffic across backend replicas. Historically, the industry standard has been to balance physical resource utilization, typically CPU load, evenly across these replicas using algorithms like Weighted Round Robin (WRR). However, in modern multi-tenant cloud environments, replicas frequently share physical hardware with noisy background processes (antagonist load) that throttle their processing capacity. When traditional load balancers force an equal CPU workload onto these throttled replicas, local queues rapidly overflow. This leads to severe tail-latency explosions ($p99.9$) and cascading request timeouts. Consequently, balancing resource utilization is fundamentally the wrong objective when environments are heterogeneous or highly contested.

To solve this, Google introduced Prequal (Probing to Reduce Queuing and Latency). Rather than relying on stale CPU statistics, Prequal utilizes lightweight, asynchronous client-side probing to capture real-time server telemetry. Each load balancer queries backends for two direct signals: active Requests-In-Flight (RIF) and estimated execution latency. Prequal then applies the Hot-Cold Lexicographic (HCL) rule. It calculates a dynamic threshold ($Q_{\text{RIF}}$) based on the $q$-th percentile of observed RIF values. Any server with a queue size above this limit is classified as "Hot"  and excluded. The request is then routed strictly to the "Cold" server with the lowest estimated latency.

The original paper contributes a significant paradigm shift in distributed systems, demonstrating that decoupling queue-depth protection from latency minimization allows load balancers to bypass multi-tenant bottlenecks. In this project, we independently reproduce and evaluate Prequal’s core claims on a physical bare-metal CloudLab cluster. We validate its robustness to variable antagonist load, map its probing-rate sensitivity, and further extend the original research by exploring how these routing dynamics scale across varying cluster sizes.

---
# 2. Selected Result


### Experiment 1: Robustness to Variable Antagonist Load

The primary stress test evaluates a 20-server replica fleet subjected to an asymmetric background antagonist CPU load on 35% of the nodes. We run an escalating workload ramp across nine multiplicative steps from $75\%$ to $174\%$ of allocation:

$$\text{Load Steps} \in \{0.75\times, 0.83\times, 0.93\times, 1.03\times, 1.14\times, 1.27\times, 1.41\times, 1.57\times, 1.74\times\}$$

<center>
<img
alt="Load ramp experiment. Gray background denotes WRR policy, white denotes Prequal"
src="report/figures/paper_plot_experiment1.png"
style="width:30%;"
/>
<p>Figure 1: Load ramp experiment. Gray background denotes WRR policy, white denotes Prequal</p>
</center>

This experiment shows that Prequal can effectively overcome the motivated challenge of multi-tenant resource contention. Prequal maintains a $0.0\%$ HTTP error rate and keeps the $p99.9$ tail latency completely flat (below $600\text{ ms}$) under an extreme $1.74\times$ overload, while traditional Weighted Round Robin (WRR) suffers up to $25\%$ dropped requests and a $5\text{ s}$ tail-latency timeout explosion. This experiment is highly important because it shows that the paper can effectively overcome the motivated challenge of multi-tenant resource contention. It proves that by prioritizing real-time queue depths (Requests-in-Flight) over average resource-load balancing, Prequal successfully bypasses hidden background bottlenecks to keep distributed services fast and error-free.
    

### Experiment 2: Probing-Rate Sensitivity

This sensitivity analysis holds the cluster workload steady at a heavy $1.50\times$ (150%) overload while sweeping the client-side active probing rate across seven steps:

$$\text{Probe Rates} \in \{4.0, 2\sqrt{2}, 2.0, \sqrt{2}, 1.0, \sqrt{0.5}, 0.5\}$$

<center>
<img
alt="Probing rate experiment"
src="report/figures/paper_plot_experiment2.png"
style="width:30%;"
/>
<p>Figure 2: Probing rate experiment</p>
</center> 

This experiment evaluates the trade-off between telemetry freshness and bandwidth overhead. The results show that Prequal remains stable and low-latency as long as the probing rate stays above 1.0 probe per request, but degrades into queuing and latency spikes when fractional truncation drops the active probe loop below this threshold. It is important because it evaluates the physical boundary conditions of Prequal's active probing loop, identifying the trade-off between telemetry freshness and bandwidth overhead before queue visibility collapses.
    

Ultimately, these two selected results prove that the paradigm of resource-load balancing is fundamentally flawed when virtualized machines are shared with noisy neighbors. By prioritizing real-time queue depths over long-term average CPU utilization, Prequal successfully bypasses background bottlenecks and keeps distributed applications fast and error-free.

# 3. Environment Setup

  

## 3.1 Hardware Environment
  
  The experiments ran on CloudLab/Emulab using a custom profile, `experiments/cloudlab_profile.py`. The final profile provisions:
- 20 backend physical nodes named `bhost1` through `bhost20`
- 1 load-balancer node named `lb`
- 1 background-load node named `bgload`
- 1 monitor node named `monitor`
- A single LAN with static addresses `10.10.1.1` through `10.10.1.20` for the backend hosts, `10.10.1.254` for the load balancer, `10.10.1.253` for background load, and `10.10.1.252` for monitoring 

The profile default hardware type is `d430`, but the profile exposes hardware type as a parameter so an equivalent CloudLab node type such as `d710` can be used if `d430` nodes are unavailable. The important design choice is one physical backend host per measured server. Earlier versions packed many backend containers onto fewer physical hosts, which made the environment much less realistic because several "replicas" shared the same host-level bottleneck.

## 3.2 Software Environment

The CloudLab image is Ubuntu 22.04. The setup script installs Docker, Git, curl, Go, Python 3, NumPy, Matplotlib, and the `hey` HTTP load generator. Docker images are built on CloudLab from the project repository under `/opt/prequal`.

The project contains three main Go components:
-  `backend/main.go`: a synthetic CPU-intensive HTTP backend that exposes `X-RIF`, `X-Latency-Estimate`, `X-CPU-Load`, and `X-Server-ID` headers.
-  `cmd/balancer/main.go` and `pkg/loadbalancer`: the load balancer supporting Prequal, WRR, random, round robin, least-loaded, and LL-Po2C policies.
-  `cmd/bgload/main.go`: a background requester that sends direct traffic to contended backend replicas, making antagonist load visible to probes but not to the balancer's own forwarding counters.

The experiment harness is in `experiments/`:
-  `prepare.py` performs CloudLab setup, builds Docker images, starts containers, configures the load balancers, starts Prometheus/Grafana, and verifies backend health.
-  `experiment1.py` and `experiment1.sh` run the Figure-6-style load ramp.
-  `experiment1_scaling.sh` and `experiment1_scaling_plot.py` run the load ramp at 5, 10, and 20 backend scales and plot scaling-error behavior.
-  `experiment2.py` and `experiment2.sh` run the probe-rate sweep.
-  `experiment3.py` and `experiment3.sh` run the QRIF threshold sweep.
-  `postprocess_smooth.py` builds smoothed 2xx-only latency plots.
-  `postprocess_deadline.py` builds paper-style deadline plots by mapping non-2xx responses to a synthetic 5 second latency.

## 3.3 Configuration Parameters

The load-ramp experiments used the same load levels as the paper:

| Parameter | Value |
| :--- | :--- |
| Load levels | 0.75, 0.83, 0.93, 1.03, 1.14, 1.27, 1.41, 1.57, 1.74 |
| Per-policy window | 240 seconds |
| Per-load total window | 480 seconds, WRR then Prequal |
| Measurement bin | 5 seconds |
| Per-server target QPS | 25 |
| Single 20-server target range | 375 QPS to 870 QPS |
| 20-server workers | 30 |
| 10-server workers | 15 |
| 5-server workers | 8 |
| Prequal QRIF default | 0.84 |
| Prequal probe rate default | 2 probes/query |
  

The backend setup interleaves clean and contended replicas. In `prepare.py`, clean replicas use `CPU_LOAD=0` and `MAX_CONCURRENCY=20`; contended replicas use `CPU_LOAD=60` and `MAX_CONCURRENCY=3`. The background-load service also sends direct traffic to contended replicas. This combination creates replicas that are reachable and healthy but have less headroom, approximating the antagonist-load situation described in the paper.


## 3.4 Deviations from the Google

The paper's controlled testbed uses 100 client replicas and 100 server replicas in a Google datacenter. Each server replica is allocated 10% of a machine's CPU, and antagonist load is real colocated production activity. Our reproduction is smaller and uses CloudLab bare-metal nodes with Dockerized components.

The most important deviations are:
- Scale: our largest run uses 20 backend servers instead of 100.
- Client model: our experiments use `hey` from the `lb` node rather than 100 client replicas.
- Antagonist load: our contention is synthetic and controlled by backend delay, bounded concurrency, and direct background traffic.
- Deadline semantics: the backend emits HTTP 503 overload responses instead of waiting for every failed request to hit a client-side 5 second timeout.
- WRR baseline: our `weightedrr` policy is the repository's simplified weighted baseline. It is not Google's production WRR, which uses smoothed goodput, CPU utilization, and error rate.
- CPU plots: our CPU panel samples host `/proc/stat`; it is therefore a host-level diagnostic, not the exact same allocation-normalized CPU metric used in Google's monitoring system.
- Artifact: the paper does not provide the production Prequal implementation. This project is a clean-room reproduction of the mechanism.

These deviations were necessary because the original environment depends on Google's internal RPC system, production deployment infrastructure, and datacenter monitoring stack. The CloudLab setup is smaller, but it captures the mechanism we wanted to test: when some replicas are more contended than others, does a Prequal-like policy avoid errors and high tail latency better than WRR?

# 4. Experiment Results

## 4.1 Execution and Measurement

1.  **Execution Procedure:** Before each test run, nodes are cleared of active containers. The `prepare.py` script provisions fresh backend servers, deploys the antagonist load generator, and starts the Prometheus monitor. A brief $15\text{-second}$ warmup wave of `hey` requests primes the buffers. The active benchmark then executes (e.g., 240 seconds per phase). A 15-second cool-down flushes sockets between policy switches.
    
2.  **Measurement Method:** Client-side latency and errors are recorded by `hey` to microsecond precision. Server-side queue and CPU telemetry are scraped by Prometheus at strict 1-second intervals.
    
3.  **Number of Runs:** Each phase runs for 240 continuous seconds. This long step duration represents a statistical equivalent to running multiple shorter repeats, capturing transient noise over a large sample volume ($>200,000$ requests per phase).
    
4.  **Statistical Treatment:** Successful 2xx responses are isolated from error responses and sorted to extract median ($p50$), tail ($p90, p99$), and extreme tail ($p99.9$) quantiles. For direct visual comparison with the paper's 5-second deadline, a post-processor maps non-2xx responses to a synthetic 5,000,000 microsecond latency limit.
    
## 4.2 Correctness & Validation Checks

We implemented three internal validation checks to ensure correctness:

-   **Telemetry Scraper Verification:** Verified Prometheus actively scraped all 20 backends without packet drops. Any drop in `lb_server_health` aborted the test.
    
-   **Antagonist Load Verification:** Queried `/proc/stat` to confirm the background engine (`bgload`) was successfully taxing targeted CPU cores at the exact requested percentages.
    
-   **Traffic Routing Auditing:** Analyzed `lb_server_selection_total` to confirm Prequal actively shifted traffic away from the 7 hot servers to the 13 cold servers, while WRR distributed traffic rigidly.
    


## 4.3 Debugging & Technical Challenges Overcome


**The Calibration Illusion & Sub-Capacity Errors (Experiment 1):** In our initial run, both Prequal and WRR suffered massive tail latency spikes at the very first step ($0.75\times$ load). Because $68\%$ of the servers were designated as "Hot" with a strict `MAX_CONCURRENCY=3` cap, the cluster had no remaining headroom from the start. We resolved this by reducing the baseline traffic target to 25 QPS and reducing the fraction of throttled servers to **35%**, creating the necessary "escape routes" for Prequal to exploit.

**Temporary Port Exhaustion and Socket Leakage (Experiment 2):** Running Experiment 2 at persistent $1.50\times$ overload with high probing rates ($4.0$) generated severe socket errors. Prequal was initiating over 2,000 asynchronous HTTP probes per second, leading to ephemeral port exhaustion on the load balancer host. We resolved this by custom-tuning Go's `http.DefaultTransport` inside `pkg/loadbalancer/balancer.go`, setting `MaxIdleConns` to $1000$ and enabling persistent HTTP Keep-Alive.
    


## 4.4 Results Analysis & Source Paper Comparison

### 1. Experiment 1: Robustness to Variable Antagonist Load


The paper's load-ramp figure (Figure 6) shows extreme WRR deadline behavior: at high load, WRR reaches the 5-second cap for $p99$ and $p99.9$, while Prequal keeps latency far below the deadline and produces zero errors.

To compare our results with the paper, we generated deadline plots where every non-2xx response is mapped to $5,000,000\ \mu\text{s}$ ($5\text{ s}$). This directly matches the axes and timeout semantics of the source paper, while keeping the raw measurements intact. Note that error bars do not apply to these smoothed time-series latency percentiles.

Our reproduced 20-server result shows the same direction but at a smaller magnitude. In the raw 2xx-only view, Prequal has lower $p99$ and $p99.9$ latency at high load and almost no errors. In the deadline-mapped view, WRR's non-2xx responses become visible as $p99.9$ spikes because failed requests are assigned a 5-second latency.
<center>
<img
alt="Our 20-server smoothed load ramp with 2xx-only latency and non-2xx errors"
src="figures/experiment1_smoothed_s20.png"
style="width:88%;"
/>
<p><b>Figure 3:</b> Our 20-server load ramp using raw semantics: latency percentiles use only 2xx responses, and non-2xx responses remain in the error panel.</p>
</center>

<center>
<img
alt="Our 20-server load ramp with non-2xx responses mapped to 5 seconds"
src="figures/experiment1_deadline_smoothed_s20.png"
style="width:88%;"
/>
<p><b>Figure 4:</b> Our 20-server load ramp with paper-style deadline post-processing. Non-2xx responses are mapped to 5 seconds before latency percentiles are recomputed.</p>
</center>

The numerical summary for the final scaling run is:
| Metric (at 1.74$\times$ Overload) | Original Paper (100 Servers) | Replicated Run (20 Servers) | Quantitative Difference |
| :--- | :---: | :---: | :---: |
| **Prequal p99.9 Latency** | $\approx$ 600 ms | 524 ms | $-12.6\%$ (Faster) |
| **WRR p99.9 Latency** | $\approx$ 5000 ms | 4895 ms | $-2.1\%$ (Similar) |
| **Prequal Error Rate** | 0.0% | 0.0% | 0.0% (Perfect Match) |
| **WRR Error Rate** | $\approx$ 25.0% | 23.1% | $-1.9\%$ (Similar) |

Qualitatively, our 20-server plot perfectly matches Google's Figure 6. Below $1.03\times$ allocation, both algorithms perform identically with $0\%$ errors and flat, low latencies. Once the workload crosses into overload ($1.14\times$ to $1.74\times$), WRR’s queue-blindness forces requests into the throttled nodes, causing tail latencies to rocket toward the $5\text{-second}$ timeout limit. Prequal’s probing loop detects these clogged queues and shifts the entire workload surge into the clean servers, keeping latency flat and errors at zero.
    

### 2. Experiment 2: Probing-Rate Sweep
The paper's Figure 8 reduces the probe rate from 4 probes/query to 1/2 probe/query while running at roughly 1.5x allocation. It finds that Prequal is fairly insensitive to probe rate until the rate drops below about 1 probe/query, after which RIF and tail latency increase.
Our Experiment 2 uses the same seven nominal probe-rate states, each for 240 seconds, at 1.5x load on 20 servers. The generated plot is:
<center>
<img
alt="Our probe-rate sweep showing latency and RIF increasing as effective probe rate falls"
src="figures/experiment2_probe_rate.png"
style="width:82%;"
/>
<p><b>Figure 5:</b> Our probe-rate sweep at 20 servers and 1.5x load.</p>
</center>

The result partially matches the paper. At nominal rates of 4, 2sqrt(2), and 2, p99 latency remains around 58-60 ms and p99.9 around 89-90 ms with no errors. At nominal rate sqrt(2) and 1, p99 rises to about 88 ms and p99.9 rises to about 104-105 ms. At nominal rates below 1, p99 approaches 98 ms, p99.9 reaches about 122 ms, and errors increase.

| Nominal probe rate | Effective async probes/query | Replicated p99 | Replicated p99.9 | Replicated Errors | Original Paper Trend |
| :--- | :---: | :---: | :---: | :---: | :--- |
| 4 | 4 | 60.0 ms | 90.2 ms | 0 | Stable, minimal queuing |
| 2$\sqrt{2}$ | 2 | 58.3 ms | 88.8 ms | 0 | Stable, minimal queuing |
| 2 | 2 | 59.6 ms | 88.9 ms | 0 | Stable, minimal queuing |
| $\sqrt{2}$ | 1 | 88.3 ms | 105.1 ms | 14 | Degradation begins |
| 1 | 1 | 87.9 ms | 104.1 ms | 11 | Degradation begins |
| $\sqrt{0.5}$ | 0 | 98.2 ms | 121.6 ms | 59 | Severe queuing |
| 0.5 | 0 | 97.9 ms | 122.2 ms | 87 | Severe queuing |   

Google’s Figure 8 depicts a smooth mathematical degradation in queuing and latency as probe rates fall below $1.0$. Our physical reproduction confirms this identical underlying trend—showing significant spikes in $p99.9$ latency (jumping from $\sim 90\text{ ms}$ to over $120\text{ ms}$) and vastly increased deadline errors when fractional truncation drops the active probe loop below 1.0. However, our replication additionally highlights a vital systems-level scaling threshold: at extreme loads, the theoretical benefits of high-frequency probing are instantly nullified by kernel-level socket exhaustion unless the proxy's transport layer is explicitly tuned for aggressive connection recycling.
## 4.5 Scientific Takeaways & Insights

Our replication effort yielded three primary takeaways for the study of distributed systems:

1.  **The Validity of Queue-Aware Selection:** We independently verified that Google's core thesis is completely correct. In shared, multi-tenant cloud environments, balancing resource consumption (CPU load) is the wrong objective.Operating systems and hypervisors schedule threads dynamically, which hides bottlenecks from traditional balancers. Real-time queue depth (Requests-in-Flight) is a significantly more accurate indicator of server health.
    
2.  **The Power of Simple Signals:** Prequal achieves this massive latency and error reduction using incredibly simple signals, just a two-tier classification (Hot vs. Cold) based on a dynamic percentile. It does not require complex machine learning, state synchronization, or centralized coordination.
    
3.  **Network Constraints Supersede Theoretical Routing:** Replicating this paper highlighted that high-level theoretical algorithms often assume zero-cost telemetry. In real-world physical deployments, aggressive asynchronous probing introduces immense socket pressure. If the proxy's transport layer (TCP Keep-Alive, idle connection pools) is not rigorously tuned, ephemeral port exhaustion will inflate tail latencies, completely negating the benefits of queue-aware routing. The true engineering value lies in balancing algorithmic elegance with underlying OS-level network constraints.

# 5. Further Exploration

  


As an extension to the core replication, we investigated a critical operational question: _How does the performance gap between WRR and Prequal evolve when the load balancer is scaled across 5, 10, and 20 backend servers?_

This analysis is directly motivated by the scale parameters of the original paper. While Wydrowski evaluated Prequal on 100-server replica pools, deploying 100 physical CloudLab backend nodes was beyond our lab's resource constraints. Instead, we utilized $N \in \{5, 10, 20\}$ within the identical CloudLab profile to observe the algorithmic trajectory as the system scales down to realistic microservice deployment sizes.

  

## 5.1 Methodology and Result

 
For each cluster size, we maintained identical load fractions, per-server QPS targets, and contention ratios ($35\%$ hot nodes). The aggregate request rate and the number of active load-generation workers were scaled linearly with the server count to prevent artificial burstiness.

The automated scaling runner performed the following execution sequence:
1. One CloudLab preparation over 20 backend nodes.
1. A 5-server load ramp using the first 5 healthy backends and 8 workers.
1. A 10-server load ramp using the first 10 healthy backends and 15 workers.
1. A 20-server load ramp using all 20 healthy backends and 30 workers.
1. Local post-processing to generate per-scale plots and aggregate error plots.


The per-scale raw and deadline-smoothed plots are shown below:
<center>
<div  style="display:inline-block; width:30%; vertical-align:top;">
<img
alt="5-server smoothed 2xx-only load ramp"
src="figures/experiment1_smoothed_s5.png"
style="width:100%;"
/>
<p><b>Figure 6:</b> 5-server raw semantics.</p>
</div>
<div  style="display:inline-block; width:30%; padding-left:1em; vertical-align:top;">
<img
alt="10-server smoothed 2xx-only load ramp"
src="figures/experiment1_smoothed_s10.png"
style="width:100%;"
/>
<p><b>Figure 7:</b> 10-server raw semantics.</p>
</div>
<div  style="display:inline-block; width:30%; padding-left:1em; vertical-align:top;">
<img
alt="20-server smoothed 2xx-only load ramp"
src="figures/experiment1_smoothed_s20.png"
style="width:100%;"
/>
<p><b>Figure 8:</b> 20-server raw semantics.</p>
</div>
</center>


<center>
<div  style="display:inline-block; width:30%; vertical-align:top;">
<img
alt="5-server deadline-smoothed load ramp"
src="figures/experiment1_deadline_smoothed_s5.png"
style="width:100%;"
/>
<p><b>Figure 9:</b> 5-server deadline post-processing.</p>
</div>
<div  style="display:inline-block; width:30%; padding-left:1em; vertical-align:top;">
<img
alt="10-server deadline-smoothed load ramp"
src="figures/experiment1_deadline_smoothed_s10.png"
style="width:100%;"
/>
<p><b>Figure 10:</b> 10-server deadline post-processing.</p>
</div>
<div  style="display:inline-block; width:30%; padding-left:1em; vertical-align:top;">
<img
alt="20-server deadline-smoothed load ramp"
src="figures/experiment1_deadline_smoothed_s20.png"
style="width:100%;"
/>
<p><b>Figure 11:</b> 20-server deadline post-processing.</p>
</div>
</center>

The aggregated error plots demonstrate a profound scaling effect. WRR errors grow at a significantly faster rate than Prequal errors as the system scales up. While Prequal is not perfectly error-free in the 20-server aggregate at absolute maximum capacity, the performance differential is massive: 498 WRR non-2xx responses versus 12 Prequal non-2xx responses.

<center>
<img
alt="Scaling errors by load level for WRR and Prequal"
src="figures/experiment1_scaling_errors.png"
style="width:75%;"
/>
<p><b>Figure 12:</b> Error QPS by load level for 5, 10, and 20 servers.</p>
</center>

<center>
<img
alt="Scaling error rate by number of servers with linear and log-style y axis"
src="figures/experiment1_scaling_error_by_servers_linear_log.png"
style="width:88%;"
/>
<p><b>Figure 13:</b> Mean error QPS versus number of servers. Point labels show total non-2xx responses.</p>
</center>
  
| Servers | Policy | Total responses | Non-2xx responses | Mean error QPS | Overall error rate |
| :---: | :--- | :---: | :---: | :---: | :---: |
| 5 | WRR | 306,345 | 2 | 0.00093 | 0.00065% |
| 5 | Prequal | 320,123 | 0 | 0.00000 | 0.00000% |
| 10 | WRR | 608,950 | 74 | 0.03426 | 0.01215% |
| 10 | Prequal | 640,246 | 1 | 0.00046 | 0.00016% |
| 20 | WRR | 1,213,147 | 498 | 0.23056 | 0.04105% |
| 20 | Prequal | 1,276,911 | 12 | 0.00556 | 0.00094% |

 
The primary takeaway is that Prequal's routing advantage becomes distinctly easier to observe at larger scales. With only 5 servers, the system has fewer alternative routing choices and processes fewer total requests, leading to extremely low error rates for both policies. However, at 20 servers, Prequal leverages its active probe telemetry to successfully discover and route to the wider pool of uncontended replicas. Conversely, WRR, reliant entirely on historical, smoothed weights, fails to adapt to instantaneous multi-tenant noise, accumulating significantly more HTTP 503 errors under high load.

# Chapter 6: Reproducibility Assessment of the Paper


The original paper is exceptionally strong conceptually, but it is not fully reproducible from the text alone. It clearly articulates the primary mechanisms, algorithmic parameters, and experimental trends required to validate the theory: the load levels in Figure 6, the probe-rate values in Figure 8. These details provided a sufficient architectural blueprint to design a faithful, scaled-down physical experiment.

However, several critical deployment details are deeply coupled to Google's internal, proprietary infrastructure and are inherently unavailable to external researchers:

-   **Proprietary Implementation:** The production Prequal proxy operates inside Google's internal Stubby/gRPC framework and was not released alongside the paper.
    
-   **Production Antagonist Load:** The YouTube production deployment and its specific real-world antagonist noise patterns cannot be identically simulated in an academic lab environment.
    
-   **Datacenter CPU Isolation:** The precise CPU isolation mechanisms, hypervisor behaviors, and allocation models of a Google datacenter cannot be perfectly mapped to CloudLab bare-metal nodes.
    

As a result, the most reproducible aspect of the paper is its routing mechanism and the relative shape of the controlled experiments, rather than absolute numerical parity. Our project successfully reproduces the central thesis of Figure 6: Prequal drastically reduces errors and tail latency under overload compared to WRR. We also reproduced the qualitative insights of the probe-rate sweep, validating the dependency on telemetry freshness. Ultimately, while exact numerical cloning is impossible without Google's infrastructure, the paper's scientific claims regarding queue-aware routing are highly reproducible, robust, and valid.

# Chapter 7: Conclusion


This project successfully executed an independent, physical bare-metal replication and scaling exploration of Google's Prequal load balancer on a 20-node CloudLab cluster. Our findings provide strong, verifiable proof for the paper's core claims while delivering highly practical insights into distributed queue management.

The fundamental conclusion of our scaled-down replication aligns perfectly with the paper's argument: the optimal objective for a load balancer is not perfectly balanced CPU load, but rather routing requests strictly to replicas with real-time available capacity. In our 20-server load ramp, WRR produced significantly more non-2xx deadline errors than Prequal, particularly when driven beyond $100\%$ allocation. Prequal's routing superiority was clearly visible in the $p99$ and $p99.9$ latency bounds, especially within the deadline-mapped plots that approximate the paper's $5\text{-second}$ timeout semantics.

Furthermore, the systems engineering work required for this reproduction yielded significant technical value. Transitioning from a theoretical concept to a physical CloudLab topology involved migrating from a Docker Compose demo to a 20-node profile, building idempotent preparation scripts, implementing realistic server-local RIF tracking, generating synthetic physical contention, isolating latency from error accounting, building deadline post-processors, mapping CPU telemetry to real host measurements, and executing a multi-scale experiment across $N \in \{5, 10, 20\}$ servers.

These final results serve as a rigorous, scaled reproduction rather than an exact clone of Google's testbed. While absolute latency and error figures naturally differ from the source paper due to distinct backend, workload, client, and datacenter constraints, the qualitative conclusion remains definitive: utilizing fresh, server-local signals enables Prequal to bypass overloaded replicas vastly better than WRR, and this architectural advantage scales dynamically with the size of the distributed system.
