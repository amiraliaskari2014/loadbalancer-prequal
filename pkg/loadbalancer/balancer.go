// Package loadbalancer contains the routing policies used by the demo.
//
// Prequal keeps a small pool of recent probe results. Request handling consumes
// older probe observations while new probes refresh the pool in the background,
// so routing decisions are based on server-local load instead of only this load
// balancer's recent request history.
package loadbalancer

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// LoadBalancer owns one routing policy instance and its shared probe state.
type LoadBalancer struct {
	servers []*Server
	pool    *ProbePool
	config  *Config
	metrics *Metrics
	logger  *slog.Logger

	mu           sync.RWMutex
	rrIdx        uint32
	removeToggle bool

	stats Stats
}

func NewLoadBalancer(cfg *Config, logger *slog.Logger, metrics *Metrics) *LoadBalancer {
	if cfg == nil {
		cfg = defaultConfig()
	}
	applyDefaults(cfg)

	return &LoadBalancer{
		servers: make([]*Server, 0),
		pool:    NewProbePool(cfg.ProbePoolSize, cfg.ProbeAgeTimeout),
		config:  cfg,
		metrics: metrics,
		logger:  logger,
	}
}

func defaultConfig() *Config {
	return &Config{}
}

func applyDefaults(cfg *Config) {
	if cfg.ProbeInterval == 0 {
		cfg.ProbeInterval = time.Second
	}
	if cfg.ProbeTimeout == 0 {
		cfg.ProbeTimeout = 2 * time.Second
	}
	if cfg.HealthCheckPath == "" {
		cfg.HealthCheckPath = "/health"
	}
	if cfg.ProbePoolSize == 0 {
		cfg.ProbePoolSize = 16
	}
	if cfg.ProbeAgeTimeout == 0 {
		cfg.ProbeAgeTimeout = time.Second
	}
	if cfg.ProbeRatePerQuery == 0 {
		cfg.ProbeRatePerQuery = 2
	}
	if cfg.ProbeRemoveRate == 0 {
		cfg.ProbeRemoveRate = 1
	}
	if cfg.DriftRate == 0 {
		cfg.DriftRate = 1.0
	}
	if cfg.SelectionChoices == 0 {
		cfg.SelectionChoices = 2
	}
	if cfg.QRIF == 0 {
		cfg.QRIF = 0.84
	}
	if cfg.Algorithm == "" {
		cfg.Algorithm = AlgorithmPrequal
	}
	if cfg.WeightUpdateInterval == 0 {
		cfg.WeightUpdateInterval = 3 * time.Second
	}
	if cfg.EWMAAlpha == 0 {
		cfg.EWMAAlpha = 0.15
	}
}

// AddServer registers a replica with optimistic WRR defaults until probes arrive.
func (lb *LoadBalancer) AddServer(s *Server) {
	s.Weight = 1.0
	s.EWMALatency = 20.0
	s.EWMAAlpha = lb.config.EWMAAlpha
	lb.mu.Lock()
	lb.servers = append(lb.servers, s)
	lb.mu.Unlock()
}

// StartBackground launches the periodic workers required by the chosen policy.
func (lb *LoadBalancer) StartBackground() {
	go lb.runPeriodicProber()
	if lb.config.Algorithm == AlgorithmPrequal {
		go lb.runProbeRemover()
	}
	if lb.config.Algorithm == AlgorithmWeightedRR {
		go lb.runWeightUpdater()
	}
}

func (lb *LoadBalancer) runPeriodicProber() {
	// Give backends a short startup window before the first health/probe batch.
	time.Sleep(200 * time.Millisecond)

	ticker := time.NewTicker(lb.config.ProbeInterval)
	defer ticker.Stop()
	for range ticker.C {
		lb.probeAllServers()
	}
}

func (lb *LoadBalancer) probeAllServers() {
	lb.mu.RLock()
	serverSnapshot := make([]*Server, len(lb.servers))
	copy(serverSnapshot, lb.servers)
	lb.mu.RUnlock()

	for _, server := range serverSnapshot {
		go func(s *Server) {
			// Stamp the probe at dispatch time so age comparisons are not biased by
			// how quickly an individual backend answers.
			firedAt := time.Now()
			result := lb.probeServer(s)
			if result == nil {
				return
			}
			lb.mu.Lock()
			s.IsHealthy = result.isHealthy
			s.LatencyMs = result.latencyMs
			if result.serverRIF >= 0 {
				s.ServerRIF = result.serverRIF
			}
			if result.cpuLoad >= 0 {
				s.CPULoad = result.cpuLoad
			}
			s.LastProbeAt = time.Now()
			lb.mu.Unlock()

			algorithmName := string(lb.config.Algorithm)
			if result.isHealthy {
				lb.metrics.serverHealth.WithLabelValues(s.ID, algorithmName).Set(1)
			} else {
				lb.metrics.serverHealth.WithLabelValues(s.ID, algorithmName).Set(0)
			}
			lb.metrics.serverLatency.WithLabelValues(s.ID, algorithmName).Set(float64(result.latencyMs))
			lb.metrics.serverCPULoad.WithLabelValues(s.ID, algorithmName).Set(float64(result.cpuLoad))

			// Periodic probes seed Prequal's pool even when request volume is low.
			if lb.config.Algorithm == AlgorithmPrequal {
				maxUses := lb.computeBreuse()
				lb.pool.Add(&ProbeEntry{
					Server:    s,
					RIF:       result.serverRIF,
					LatencyMs: result.latencyMs,
					Timestamp: firedAt,
					MaxUses:   maxUses,
				})
			}
		}(server)
	}
}

// probeResult captures the signal headers and latency from one backend probe.
type probeResult struct {
	latencyMs int64
	serverRIF int32
	cpuLoad   int32
	isHealthy bool
}

func (lb *LoadBalancer) probeServer(s *Server) *probeResult {
	ctx, cancel := context.WithTimeout(context.Background(), lb.config.ProbeTimeout)
	defer cancel()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"http://"+s.Address+lb.config.HealthCheckPath, nil)
	if err != nil {
		lb.logger.Warn("probe: create request", slog.String("server", s.ID), slog.Any("err", err))
		return &probeResult{isHealthy: false, serverRIF: -1, cpuLoad: -1}
	}

	start := time.Now()
	response, err := http.DefaultClient.Do(request)
	latencyMs := time.Since(start).Milliseconds()

	if err != nil {
		lb.logger.Debug("probe: request failed", slog.String("server", s.ID), slog.Any("err", err))
		return &probeResult{isHealthy: false, latencyMs: latencyMs, serverRIF: -1, cpuLoad: -1}
	}
	defer response.Body.Close()

	result := &probeResult{
		latencyMs: latencyMs,
		isHealthy: response.StatusCode == http.StatusOK,
		serverRIF: -1,
		cpuLoad:   -1,
	}

	// Missing signal headers stay at -1 so callers can skip stale values.
	if headerValue := response.Header.Get("X-RIF"); headerValue != "" {
		if parsedValue, err := strconv.ParseInt(headerValue, 10, 32); err == nil {
			result.serverRIF = int32(parsedValue)
		}
	}
	if headerValue := response.Header.Get("X-CPU-Load"); headerValue != "" {
		if parsedValue, err := strconv.ParseInt(headerValue, 10, 32); err == nil {
			result.cpuLoad = int32(parsedValue)
		}
	}
	return result
}

// triggerAsyncProbes refreshes the pool with per-request background probes.
func (lb *LoadBalancer) triggerAsyncProbes() {
	lb.mu.RLock()
	serverSnapshot := make([]*Server, len(lb.servers))
	copy(serverSnapshot, lb.servers)
	lb.mu.RUnlock()

	if len(serverSnapshot) == 0 {
		return
	}
	probeCount := int(lb.config.ProbeRatePerQuery)
	if probeCount > len(serverSnapshot) {
		probeCount = len(serverSnapshot)
	}

	// Permutation sampling avoids probing the same server twice for one request.
	probeOrder := rand.Perm(len(serverSnapshot))
	for i := 0; i < probeCount; i++ {
		server := serverSnapshot[probeOrder[i]]
		go func(s *Server) {
			firedAt := time.Now()
			result := lb.probeServer(s)
			if result == nil || !result.isHealthy {
				return
			}
			lb.mu.Lock()
			s.IsHealthy = result.isHealthy
			s.LatencyMs = result.latencyMs
			if result.serverRIF >= 0 {
				s.ServerRIF = result.serverRIF
			}
			lb.mu.Unlock()

			lb.pool.Add(&ProbeEntry{
				Server:    s,
				RIF:       result.serverRIF,
				LatencyMs: result.latencyMs,
				Timestamp: firedAt,
				MaxUses:   lb.computeBreuse(),
			})
		}(server)
	}
}

// runProbeRemover keeps stale or overloaded observations from dominating HCL.
func (lb *LoadBalancer) runProbeRemover() {
	ticker := time.NewTicker(lb.config.ProbeInterval / 2)
	defer ticker.Stop()
	for range ticker.C {
		rifThreshold := lb.currentRIFThreshold()
		lb.removeToggle = !lb.removeToggle
		lb.pool.RemoveWorst(rifThreshold, lb.removeToggle)
	}
}

// runWeightUpdater refreshes WRR from periodic probe observations.
func (lb *LoadBalancer) runWeightUpdater() {
	ticker := time.NewTicker(lb.config.WeightUpdateInterval)
	defer ticker.Stop()
	for range ticker.C {
		lb.recomputeWRRWeights()
	}
}

// recomputeWRRWeights converts smoothed probe latency into routing weight.
func (lb *LoadBalancer) recomputeWRRWeights() {
	lb.mu.Lock()
	defer lb.mu.Unlock()
	for _, s := range lb.servers {
		sampleLatency := float64(s.LatencyMs)
		if sampleLatency <= 0 {
			sampleLatency = s.EWMALatency
		}
		if sampleLatency <= 0 {
			continue
		}
		// Smooth probe samples so one noisy health check does not swing traffic.
		if s.EWMALatency <= 0 {
			s.EWMALatency = sampleLatency
		} else {
			s.EWMALatency = s.EWMAAlpha*sampleLatency + (1-s.EWMAAlpha)*s.EWMALatency
		}
		s.Weight = 1000.0 / s.EWMALatency
	}
}

// computeBreuse returns the reuse budget for a probe-pool entry:
//
//	breuse = max(1, (1 + drift) / ((1 - m/n) * rprobe - rremove))
//
// The small docker-compose testbed has fewer replicas than the paper assumes,
// so non-positive denominators use a bounded fallback that keeps the pool full.
func (lb *LoadBalancer) computeBreuse() int {
	lb.mu.RLock()
	replicaCount := float64(len(lb.servers))
	lb.mu.RUnlock()

	if replicaCount == 0 {
		return 1
	}
	poolSize := float64(lb.config.ProbePoolSize)
	probesPerQuery := lb.config.ProbeRatePerQuery
	removeRate := lb.config.ProbeRemoveRate
	driftRate := lb.config.DriftRate

	denominator := (1.0-poolSize/replicaCount)*probesPerQuery - removeRate
	if denominator <= 0 {
		// Reuse roughly one pool round per replica when formula (1) is undefined.
		reuseBudget := int(poolSize/replicaCount) + 1
		if reuseBudget < 1 {
			return 1
		}
		return reuseBudget
	}
	reuseBudget := (1.0 + driftRate) / denominator
	if reuseBudget < 1 {
		return 1
	}
	return int(reuseBudget)
}

// currentRIFThreshold calculates the hot/cold split for the current pool.
func (lb *LoadBalancer) currentRIFThreshold() int32 {
	freshEntries := lb.pool.Fresh()
	if len(freshEntries) == 0 {
		return 0
	}
	return rifQuantile(freshEntries, lb.config.QRIF)
}

func (lb *LoadBalancer) SelectServer() *Server {
	switch lb.config.Algorithm {
	case AlgorithmPrequal:
		return lb.selectPrequal()
	case AlgorithmWeightedRR:
		return lb.selectWeightedRR()
	case AlgorithmRoundRobin:
		return lb.selectRoundRobin()
	case AlgorithmRandom:
		return lb.selectRandom()
	case AlgorithmLeastLoaded:
		return lb.selectLeastLoaded()
	case AlgorithmLLPo2C:
		return lb.selectLLPo2C()
	default:
		return lb.selectPrequal()
	}
}

// selectPrequal applies the hot-cold lexicographic decision rule.
func (lb *LoadBalancer) selectPrequal() *Server {
	freshEntries := lb.pool.Fresh()

	// With too little probe history, random selection is less brittle than HCL.
	if len(freshEntries) < 2 {
		return lb.selectRandom()
	}

	rifThreshold := rifQuantile(freshEntries, lb.config.QRIF)

	var coldEntries, hotEntries []*ProbeEntry
	for _, entry := range freshEntries {
		if !entry.Server.IsHealthy {
			continue
		}
		if entry.RIF > rifThreshold {
			hotEntries = append(hotEntries, entry)
		} else {
			coldEntries = append(coldEntries, entry)
		}
	}

	var chosen *ProbeEntry
	if len(coldEntries) > 0 {
		// Among cold replicas, prefer the one with the best recent latency.
		chosen = coldEntries[0]
		for _, entry := range coldEntries[1:] {
			if entry.LatencyMs < chosen.LatencyMs {
				chosen = entry
			}
		}
	} else if len(hotEntries) > 0 {
		// If every replica is hot, use the least-hot RIF observation.
		chosen = hotEntries[0]
		for _, entry := range hotEntries[1:] {
			if entry.RIF < chosen.RIF {
				chosen = entry
			}
		}
	}

	if chosen == nil {
		return lb.selectRandom()
	}

	lb.pool.MarkUsed(chosen)
	return chosen.Server
}

// selectWeightedRR samples healthy replicas according to their current weights.
func (lb *LoadBalancer) selectWeightedRR() *Server {
	lb.mu.RLock()
	candidates := lb.healthyServersLocked()
	lb.mu.RUnlock()

	if len(candidates) == 0 {
		return nil
	}

	totalWeight := 0.0
	for _, candidate := range candidates {
		totalWeight += candidate.Weight
	}
	if totalWeight <= 0 {
		return candidates[rand.Intn(len(candidates))]
	}

	pick := rand.Float64() * totalWeight
	cumulativeWeight := 0.0
	for _, candidate := range candidates {
		cumulativeWeight += candidate.Weight
		if pick <= cumulativeWeight {
			return candidate
		}
	}
	return candidates[len(candidates)-1]
}

// updateWRRAfterRequest lets WRR react to latency from proxied traffic too.
func (lb *LoadBalancer) updateWRRAfterRequest(s *Server, latencyMs int64) {
	lb.mu.Lock()
	defer lb.mu.Unlock()
	if s.EWMALatency <= 0 {
		s.EWMALatency = float64(latencyMs)
	} else {
		s.EWMALatency = s.EWMAAlpha*float64(latencyMs) + (1-s.EWMAAlpha)*s.EWMALatency
	}
	if s.EWMALatency < 1 {
		s.EWMALatency = 1
	}
	// Higher observed latency lowers the chance of choosing this server.
	s.Weight = 1000.0 / s.EWMALatency
}

func (lb *LoadBalancer) selectRoundRobin() *Server {
	lb.mu.RLock()
	candidates := lb.healthyServersLocked()
	lb.mu.RUnlock()
	if len(candidates) == 0 {
		return nil
	}
	nextIndex := atomic.AddUint32(&lb.rrIdx, 1)
	return candidates[int(nextIndex-1)%len(candidates)]
}

func (lb *LoadBalancer) selectRandom() *Server {
	lb.mu.RLock()
	candidates := lb.healthyServersLocked()
	lb.mu.RUnlock()
	if len(candidates) == 0 {
		return nil
	}
	return candidates[rand.Intn(len(candidates))]
}

// selectLeastLoaded only sees requests currently forwarded by this balancer.
func (lb *LoadBalancer) selectLeastLoaded() *Server {
	lb.mu.RLock()
	candidates := lb.healthyServersLocked()
	lb.mu.RUnlock()
	if len(candidates) == 0 {
		return nil
	}
	selected := candidates[0]
	for _, candidate := range candidates[1:] {
		if atomic.LoadInt32(&candidate.ClientRIF) < atomic.LoadInt32(&selected.ClientRIF) {
			selected = candidate
		}
	}
	return selected
}

// selectLLPo2C is power-of-two-choices using this balancer's in-flight counts.
func (lb *LoadBalancer) selectLLPo2C() *Server {
	lb.mu.RLock()
	candidates := lb.healthyServersLocked()
	lb.mu.RUnlock()
	if len(candidates) == 0 {
		return nil
	}
	if len(candidates) == 1 {
		return candidates[0]
	}
	// Pick two distinct healthy candidates.
	firstIndex, secondIndex := rand.Intn(len(candidates)), rand.Intn(len(candidates)-1)
	if secondIndex >= firstIndex {
		secondIndex++
	}
	firstChoice, secondChoice := candidates[firstIndex], candidates[secondIndex]
	if atomic.LoadInt32(&firstChoice.ClientRIF) <= atomic.LoadInt32(&secondChoice.ClientRIF) {
		return firstChoice
	}
	return secondChoice
}

func (lb *LoadBalancer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	atomic.AddUint64(&lb.stats.TotalRequests, 1)
	algorithmName := string(lb.config.Algorithm)

	// Fire probes before selection; their results will help later requests.
	if lb.config.Algorithm == AlgorithmPrequal {
		go lb.triggerAsyncProbes()
	}

	server := lb.SelectServer()
	if server == nil {
		atomic.AddUint64(&lb.stats.FailedRequests, 1)
		lb.metrics.requestErrors.WithLabelValues(algorithmName, "no_server").Inc()
		lb.logger.Error("no available server")
		http.Error(w, "no available servers", http.StatusServiceUnavailable)
		return
	}

	lb.metrics.serverSelectionTotal.WithLabelValues(server.ID, algorithmName).Inc()

	start := time.Now()
	lb.forwardRequest(server, w, r)
	duration := time.Since(start)

	lb.metrics.requestDuration.WithLabelValues(algorithmName).Observe(duration.Seconds())
	atomic.AddUint64(&lb.stats.SuccessfulRequests, 1)
}

func (lb *LoadBalancer) forwardRequest(server *Server, w http.ResponseWriter, r *http.Request) {
	algorithmName := string(lb.config.Algorithm)
	atomic.AddInt32(&server.ClientRIF, 1)
	lb.metrics.activeRequests.WithLabelValues(algorithmName).Inc()
	lb.metrics.serverRIF.WithLabelValues(server.ID, algorithmName).Set(
		float64(atomic.LoadInt32(&server.ClientRIF)))

	defer func() {
		atomic.AddInt32(&server.ClientRIF, -1)
		lb.metrics.activeRequests.WithLabelValues(algorithmName).Dec()
		lb.metrics.serverRIF.WithLabelValues(server.ID, algorithmName).Set(
			float64(atomic.LoadInt32(&server.ClientRIF)))
	}()

	backendURL, err := url.Parse("http://" + server.Address)
	if err != nil {
		lb.logger.Error("invalid server address",
			slog.String("server", server.ID), slog.Any("err", err))
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	reverseProxy := httputil.NewSingleHostReverseProxy(backendURL)
	reverseProxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		lb.logger.Error("proxy error",
			slog.String("server", server.ID), slog.Any("err", err))
		atomic.AddUint64(&lb.stats.FailedRequests, 1)
		lb.metrics.requestErrors.WithLabelValues(algorithmName, "proxy_error").Inc()
		http.Error(w, "upstream error", http.StatusBadGateway)
	}
	reverseProxy.ServeHTTP(w, r)
}

func (lb *LoadBalancer) healthyServersLocked() []*Server {
	healthyServers := make([]*Server, 0, len(lb.servers))
	for _, server := range lb.servers {
		if server.IsHealthy {
			healthyServers = append(healthyServers, server)
		}
	}
	return healthyServers
}

// rifQuantile returns the q-quantile of server-local RIF observations.
func rifQuantile(entries []*ProbeEntry, q float64) int32 {
	if len(entries) == 0 {
		return 0
	}
	rifValues := make([]int32, 0, len(entries))
	for _, entry := range entries {
		rifValues = append(rifValues, entry.RIF)
	}
	sort.Slice(rifValues, func(i, j int) bool { return rifValues[i] < rifValues[j] })
	quantileIndex := int(float64(len(rifValues)-1) * q)
	if quantileIndex >= len(rifValues) {
		quantileIndex = len(rifValues) - 1
	}
	return rifValues[quantileIndex]
}

// Servers returns a snapshot used by the info endpoint.
func (lb *LoadBalancer) Servers() []*Server {
	lb.mu.RLock()
	defer lb.mu.RUnlock()
	snapshot := make([]*Server, len(lb.servers))
	copy(snapshot, lb.servers)
	return snapshot
}

// Stats returns atomically loaded aggregate counters.
func (lb *LoadBalancer) Stats() Stats {
	return Stats{
		TotalRequests:      atomic.LoadUint64(&lb.stats.TotalRequests),
		SuccessfulRequests: atomic.LoadUint64(&lb.stats.SuccessfulRequests),
		FailedRequests:     atomic.LoadUint64(&lb.stats.FailedRequests),
	}
}

// Healthz reports process health and basic request counters.
func (lb *LoadBalancer) Healthz(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	stats := lb.Stats()
	fmt.Fprintf(w, `{"status":"ok","total":%d,"ok":%d,"err":%d}`,
		stats.TotalRequests, stats.SuccessfulRequests, stats.FailedRequests)
}
