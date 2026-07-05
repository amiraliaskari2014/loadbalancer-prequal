package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"strconv"
	"sync/atomic"
	"time"
)

// serverRIF counts all live backend requests, including probes and direct load.
var serverRIF int32

// concSem models active CPU capacity; concQueue models bounded backlog pressure.
var concSem chan struct{}
var concQueue chan struct{}

const queueTimeout = 400 * time.Millisecond

// latencyBuckets remembers recent response times for each observed RIF level.
type latencyBuckets struct {
	buckets [32][]int64
	mu      chan struct{}
}

func newLatencyBuckets() *latencyBuckets {
	store := &latencyBuckets{mu: make(chan struct{}, 1)}
	store.mu <- struct{}{}
	return store
}

func (lb *latencyBuckets) record(rif int32, latencyMs int64) {
	bucketIndex := rif
	if bucketIndex >= 32 {
		bucketIndex = 31
	}
	if bucketIndex < 0 {
		bucketIndex = 0
	}
	<-lb.mu
	lb.buckets[bucketIndex] = append(lb.buckets[bucketIndex], latencyMs)
	// Bound memory while keeping enough samples for a stable median.
	if len(lb.buckets[bucketIndex]) > 50 {
		lb.buckets[bucketIndex] = lb.buckets[bucketIndex][len(lb.buckets[bucketIndex])-50:]
	}
	lb.mu <- struct{}{}
}

func (lb *latencyBuckets) medianAtRIF(rif int32) int64 {
	bucketIndex := rif
	if bucketIndex >= 32 {
		bucketIndex = 31
	}
	if bucketIndex < 0 {
		bucketIndex = 0
	}
	<-lb.mu
	samples := make([]int64, len(lb.buckets[bucketIndex]))
	copy(samples, lb.buckets[bucketIndex])
	lb.mu <- struct{}{}
	if len(samples) == 0 {
		return 0
	}
	// The buckets are tiny, so insertion sort avoids extra dependencies.
	for i := 1; i < len(samples); i++ {
		for j := i; j > 0 && samples[j] < samples[j-1]; j-- {
			samples[j], samples[j-1] = samples[j-1], samples[j]
		}
	}
	return samples[len(samples)/2]
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	serverID := os.Getenv("SERVER_ID")
	if serverID == "" {
		serverID = "unknown"
	}
	cpuLoad := 0
	if v := os.Getenv("CPU_LOAD"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cpuLoad = n
		}
	}

	// Contended replicas default to fewer active slots, while tests can still
	// override the cap to shape overload behavior explicitly.
	maxConc := 5
	if cpuLoad > 0 {
		maxConc = 2
	}
	if v := os.Getenv("MAX_CONCURRENCY"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			maxConc = n
		}
	}
	concSem = make(chan struct{}, maxConc)
	concQueue = make(chan struct{}, maxConc*3)

	buckets := newLatencyBuckets()

	// Probe signal headers are sent on success and overload responses alike.
	setRIFHeaders := func(w http.ResponseWriter, arrivalRIF int32) {
		w.Header().Set("X-RIF", strconv.Itoa(int(atomic.LoadInt32(&serverRIF))))
		medianLatency := buckets.medianAtRIF(arrivalRIF)
		w.Header().Set("X-Latency-Estimate", strconv.FormatInt(medianLatency, 10))
		w.Header().Set("X-CPU-Load", strconv.Itoa(cpuLoad))
		w.Header().Set("X-Server-ID", serverID)
	}

	// antagonistDelay turns CPU_LOAD into repeatable extra latency plus jitter.
	antagonistDelay := func() time.Duration {
		baseDelay := 5*time.Millisecond + time.Duration(rand.Intn(5))*time.Millisecond
		if cpuLoad <= 0 {
			return baseDelay
		}
		// Linear scaling keeps CPU_LOAD easy to reason about in experiments.
		contentionDelay := time.Duration(float64(cpuLoad)/100.0*50) * time.Millisecond
		jitterDelay := time.Duration(rand.Intn(10)) * time.Millisecond
		return baseDelay + contentionDelay + jitterDelay
	}

	// cpuWork adds per-request computation so latency is not only sleep time.
	cpuWork := func() {
		hashIterations := 500 + rand.Intn(500)
		for i := 0; i < hashIterations; i++ {
			digest := sha256.Sum256([]byte(fmt.Sprintf("%d-%d", time.Now().UnixNano(), i)))
			_ = hex.EncodeToString(digest[:])
		}
	}

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		arrivalRIF := atomic.AddInt32(&serverRIF, 1)
		arrivalRIF--
		start := time.Now()

		defer func() {
			atomic.AddInt32(&serverRIF, -1)
			latencyMs := time.Since(start).Milliseconds()
			buckets.record(arrivalRIF, latencyMs)
		}()

		// Count RIF before queueing so overload replies still expose pressure.
		select {
		case concQueue <- struct{}{}:
		default:
			// A full backlog is reported immediately instead of blocking callers.
			setRIFHeaders(w, arrivalRIF)
			http.Error(w, `{"error":"overloaded"}`, http.StatusServiceUnavailable)
			return
		}
		defer func() { <-concQueue }()

		timer := time.NewTimer(queueTimeout)
		defer timer.Stop()
		select {
		case concSem <- struct{}{}:
			defer func() { <-concSem }()
		case <-timer.C:
			// Long waits become explicit overload instead of hidden tail latency.
			setRIFHeaders(w, arrivalRIF)
			http.Error(w, `{"error":"queue_timeout"}`, http.StatusServiceUnavailable)
			return
		}

		cpuWork()
		time.Sleep(antagonistDelay())

		duration := time.Since(start)
		setRIFHeaders(w, arrivalRIF)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"server_id":    serverID,
			"duration_ms":  duration.Milliseconds(),
			"cpu_load_pct": cpuLoad,
			"rif":          atomic.LoadInt32(&serverRIF),
		})
	})

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		// Probes skip the capacity semaphore, but they still join RIF so the header
		// reflects all work visible to the backend.
		arrivalRIF := atomic.AddInt32(&serverRIF, 1)
		arrivalRIF--
		start := time.Now()

		defer func() {
			atomic.AddInt32(&serverRIF, -1)
			latencyMs := time.Since(start).Milliseconds()
			buckets.record(arrivalRIF, latencyMs)
		}()

		time.Sleep(antagonistDelay())

		setRIFHeaders(w, arrivalRIF)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":       "healthy",
			"server_id":    serverID,
			"rif":          atomic.LoadInt32(&serverRIF),
			"cpu_load_pct": cpuLoad,
		})
	})

	log.Printf("[backend] server=%s port=%s cpu_load=%d%%", serverID, port, cpuLoad)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatal(err)
	}
}
