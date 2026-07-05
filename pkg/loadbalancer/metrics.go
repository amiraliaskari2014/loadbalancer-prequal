package loadbalancer

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// Metrics groups the Prometheus collectors shared by every balancer instance.
type Metrics struct {
	// requestDuration captures end-to-end proxy latency for percentile graphs.
	requestDuration *prometheus.HistogramVec

	// activeRequests tracks current in-flight work across all selected replicas.
	activeRequests *prometheus.GaugeVec

	// serverRIF records this balancer's per-server in-flight request count.
	serverRIF *prometheus.GaugeVec

	// serverLatency stores the most recent health/probe round-trip time.
	serverLatency *prometheus.GaugeVec

	// serverHealth uses 1 for healthy replicas and 0 for unhealthy replicas.
	serverHealth *prometheus.GaugeVec

	// serverCPULoad exposes the backend's simulated contention percentage.
	serverCPULoad *prometheus.GaugeVec

	// requestErrors counts routing and proxy failures by reason.
	requestErrors *prometheus.CounterVec

	// serverSelectionTotal shows how traffic is distributed among replicas.
	serverSelectionTotal *prometheus.CounterVec
}

func NewMetrics(reg prometheus.Registerer) *Metrics {
	if reg == nil {
		reg = prometheus.DefaultRegisterer
	}
	factory := promauto.With(reg)

	return &Metrics{
		requestDuration: factory.NewHistogramVec(
			prometheus.HistogramOpts{
				Name: "lb_request_duration_seconds",
				Help: "End-to-end request latency observed by the load balancer.",
				// Dense low-latency buckets make tail movement visible in Grafana.
				Buckets: []float64{
					.005, .010, .025, .050, .075,
					.100, .150, .200, .250, .300, .400, .500,
					.750, 1.0, 1.5, 2.0, 3.0, 5.0,
				},
			},
			[]string{"algorithm"},
		),
		activeRequests: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "lb_active_requests",
				Help: "Total requests currently in-flight across all servers.",
			},
			[]string{"algorithm"},
		),
		serverRIF: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "lb_server_rif",
				Help: "Client-local requests-in-flight per server replica.",
			},
			[]string{"server_id", "algorithm"},
		),
		serverLatency: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "lb_server_latency_ms",
				Help: "Latest probe latency per server replica (milliseconds).",
			},
			[]string{"server_id", "algorithm"},
		),
		serverHealth: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "lb_server_health",
				Help: "Server health status: 1=healthy, 0=unhealthy.",
			},
			[]string{"server_id", "algorithm"},
		),
		serverCPULoad: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "lb_server_cpu_load_pct",
				Help: "Antagonist-simulated CPU load percentage reported by the server.",
			},
			[]string{"server_id", "algorithm"},
		),
		requestErrors: factory.NewCounterVec(
			prometheus.CounterOpts{
				Name: "lb_request_errors_total",
				Help: "Total number of failed requests (proxy errors, no server, timeouts).",
			},
			[]string{"algorithm", "reason"},
		),
		serverSelectionTotal: factory.NewCounterVec(
			prometheus.CounterOpts{
				Name: "lb_server_selection_total",
				Help: "How many times each server was selected for routing.",
			},
			[]string{"server_id", "algorithm"},
		),
	}
}
