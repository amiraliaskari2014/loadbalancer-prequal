// Command balancer starts one HTTP load-balancer instance.
//
// The same binary is configured by environment so docker-compose can run both
// the Prequal instance and the weighted round-robin comparison.
//
// Supported environment variables:
//
//	LB_PORT        HTTP port to bind, default "8080"
//	LB_ALGORITHM   selection policy: prequal, weightedrr, roundrobin, random,
//	                leastloaded, or ll-po2c
//	LB_SERVERS     comma-separated id=host:port backend list
//	LB_QRIF        Prequal hot/cold RIF quantile, default "0.84"
//	LB_PROBE_RATE  Prequal probes started for each request, default "2"
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/prequal/loadbalancer/pkg/loadbalancer"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	port := envOr("LB_PORT", "8080")
	algorithm := loadbalancer.Algorithm(envOr("LB_ALGORITHM", "prequal"))
	serverSpecs := envOr("LB_SERVERS", "server1=server1:80,server2=server2:80,server3=server3:80")
	qrifQuantile := envFloat("LB_QRIF", 0.84)
	probesPerRequest := envFloat("LB_PROBE_RATE", 2.0)
	weightRefreshInterval := envDuration("LB_WEIGHT_INTERVAL", 3*time.Second)

	// A private registry prevents metric-name collisions in in-process tests.
	registry := prometheus.NewRegistry()
	metrics := loadbalancer.NewMetrics(registry)

	config := &loadbalancer.Config{
		ProbeInterval:        200 * time.Millisecond,
		ProbeTimeout:         2 * time.Second,
		HealthCheckPath:      "/health",
		ProbePoolSize:        16,
		ProbeAgeTimeout:      5 * time.Second,
		ProbeRatePerQuery:    probesPerRequest,
		ProbeRemoveRate:      1.0,
		DriftRate:            1.0,
		SelectionChoices:     2,
		Algorithm:            algorithm,
		QRIF:                 qrifQuantile,
		WeightUpdateInterval: weightRefreshInterval,
		EWMAAlpha:            0.15,
	}

	balancer := loadbalancer.NewLoadBalancer(config, logger, metrics)

	for _, serverSpec := range strings.Split(serverSpecs, ",") {
		serverSpec = strings.TrimSpace(serverSpec)
		if serverSpec == "" {
			continue
		}
		serverParts := strings.SplitN(serverSpec, "=", 2)
		if len(serverParts) != 2 {
			logger.Warn("bad server spec, skipping", slog.String("spec", serverSpec))
			continue
		}
		balancer.AddServer(&loadbalancer.Server{
			ID:        strings.TrimSpace(serverParts[0]),
			Address:   strings.TrimSpace(serverParts[1]),
			IsHealthy: true,
		})
		logger.Info("registered server",
			slog.String("id", serverParts[0]),
			slog.String("addr", serverParts[1]))
	}

	balancer.StartBackground()
	logger.Info("load balancer started",
		slog.String("algorithm", string(algorithm)),
		slog.Float64("qrif", qrifQuantile))

	mux := http.NewServeMux()
	mux.Handle("/", balancer)
	mux.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", balancer.Healthz)
	mux.HandleFunc("/info", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		registeredServers := balancer.Servers()
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"algorithm":%q,"servers":[`, string(algorithm))
		for index, server := range registeredServers {
			if index > 0 {
				fmt.Fprint(w, ",")
			}
			fmt.Fprintf(w, `{"id":%q,"addr":%q,"healthy":%v}`,
				server.ID, server.Address, server.IsHealthy)
		}
		fmt.Fprint(w, `]}`)
	})

	httpServer := &http.Server{
		Addr:         ":" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Handle container stop signals without dropping in-flight proxy requests.
	go func() {
		signalChannel := make(chan os.Signal, 1)
		signal.Notify(signalChannel, syscall.SIGINT, syscall.SIGTERM)
		<-signalChannel
		logger.Info("shutting down")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(ctx)
	}()

	logger.Info("listening", slog.String("port", port))
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server error", slog.Any("err", err))
		os.Exit(1)
	}
}

func envOr(key, def string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if value := os.Getenv(key); value != "" {
		if parsedFloat, err := strconv.ParseFloat(value, 64); err == nil {
			return parsedFloat
		}
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if value := os.Getenv(key); value != "" {
		if parsedDuration, err := time.ParseDuration(value); err == nil {
			return parsedDuration
		}
	}
	return def
}
