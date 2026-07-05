// Package loadbalancer defines shared data structures and policy settings.
package loadbalancer

import (
	"sync"
	"time"
)

// Algorithm names the strategy used when choosing a healthy replica.
type Algorithm string

const (
	// AlgorithmPrequal uses asynchronous probe history and server-local RIF.
	AlgorithmPrequal Algorithm = "prequal"

	// AlgorithmWeightedRR routes by weights derived from smoothed latency.
	AlgorithmWeightedRR Algorithm = "weightedrr"

	// AlgorithmRoundRobin cycles through healthy replicas evenly.
	AlgorithmRoundRobin Algorithm = "roundrobin"

	// AlgorithmRandom chooses any healthy replica with equal probability.
	AlgorithmRandom Algorithm = "random"

	// AlgorithmLeastLoaded compares only this balancer's in-flight requests.
	AlgorithmLeastLoaded Algorithm = "leastloaded"

	// AlgorithmLLPo2C applies power-of-two-choices to client-local RIF.
	AlgorithmLLPo2C Algorithm = "ll-po2c"
)

// Server stores the health, load, and weighting signals for one backend replica.
type Server struct {
	ID        string
	Address   string
	IsHealthy bool

	// ClientRIF is changed atomically around proxied requests.
	ClientRIF int32

	// Probe fields are refreshed from backend response headers.
	ServerRIF   int32
	LatencyMs   int64
	CPULoad     int32
	LastProbeAt time.Time

	// Weighted round-robin state is updated while the load balancer lock is held.
	Weight      float64
	EWMALatency float64
	EWMAAlpha   float64
}

// ProbeEntry is a single reusable observation in the Prequal probe pool.
type ProbeEntry struct {
	Server    *Server
	RIF       int32
	LatencyMs int64
	Timestamp time.Time
	UseCount  int
	MaxUses   int
}

// ProbePool stores recent probe observations and expires them by age or reuse.
type ProbePool struct {
	entries  []*ProbeEntry
	maxSize  int
	ageLimit time.Duration
	mu       sync.Mutex
}

func NewProbePool(maxSize int, ageLimit time.Duration) *ProbePool {
	if maxSize <= 0 {
		maxSize = 16
	}
	if ageLimit <= 0 {
		ageLimit = time.Second
	}
	return &ProbePool{
		entries:  make([]*ProbeEntry, 0, maxSize),
		maxSize:  maxSize,
		ageLimit: ageLimit,
	}
}

// Add stores a probe result, evicting the oldest entry when the pool is full.
func (pp *ProbePool) Add(e *ProbeEntry) {
	pp.mu.Lock()
	defer pp.mu.Unlock()

	if len(pp.entries) >= pp.maxSize {
		pp.removeOldestLocked()
	}
	pp.entries = append(pp.entries, e)
}

// Fresh returns a copy of entries still inside the configured age window.
func (pp *ProbePool) Fresh() []*ProbeEntry {
	pp.mu.Lock()
	defer pp.mu.Unlock()

	cutoffTime := time.Now()
	freshEntries := make([]*ProbeEntry, 0, len(pp.entries))
	for _, entry := range pp.entries {
		if cutoffTime.Sub(entry.Timestamp) <= pp.ageLimit {
			freshEntries = append(freshEntries, entry)
		}
	}
	return freshEntries
}

// MarkUsed consumes one reuse slot and removes entries that reach their budget.
func (pp *ProbePool) MarkUsed(e *ProbeEntry) {
	pp.mu.Lock()
	defer pp.mu.Unlock()

	e.UseCount++
	if e.UseCount >= e.MaxUses {
		pp.removeEntryLocked(e)
	}
}

// RemoveWorst drops either the oldest entry or the least useful load observation.
func (pp *ProbePool) RemoveWorst(rifThreshold int32, removeOldest bool) {
	pp.mu.Lock()
	defer pp.mu.Unlock()

	if len(pp.entries) == 0 {
		return
	}
	if removeOldest {
		pp.removeOldestLocked()
		return
	}
	// Prefer evicting hot entries; otherwise evict the slowest cold sample.
	worstEntry := pp.entries[0]
	for _, entry := range pp.entries[1:] {
		if entry.RIF > rifThreshold {
			if worstEntry.RIF <= rifThreshold || entry.RIF > worstEntry.RIF {
				worstEntry = entry
			}
		} else if worstEntry.RIF <= rifThreshold && entry.LatencyMs > worstEntry.LatencyMs {
			worstEntry = entry
		}
	}
	pp.removeEntryLocked(worstEntry)
}

// Len returns the number of stored entries before age filtering.
func (pp *ProbePool) Len() int {
	pp.mu.Lock()
	defer pp.mu.Unlock()
	return len(pp.entries)
}

func (pp *ProbePool) removeOldestLocked() {
	if len(pp.entries) == 0 {
		return
	}
	oldestIndex := 0
	for entryIndex, entry := range pp.entries {
		if entry.Timestamp.Before(pp.entries[oldestIndex].Timestamp) {
			oldestIndex = entryIndex
		}
	}
	pp.entries = append(pp.entries[:oldestIndex], pp.entries[oldestIndex+1:]...)
}

func (pp *ProbePool) removeEntryLocked(target *ProbeEntry) {
	for entryIndex, entry := range pp.entries {
		if entry == target {
			pp.entries = append(pp.entries[:entryIndex], pp.entries[entryIndex+1:]...)
			return
		}
	}
}

// Config holds policy tuning values shared by the balancer and probe pool.
type Config struct {
	// Probe settings control freshness, pool size, and reuse pressure.
	ProbeInterval     time.Duration
	ProbeTimeout      time.Duration
	HealthCheckPath   string
	ProbePoolSize     int
	ProbeAgeTimeout   time.Duration
	ProbeRatePerQuery float64
	ProbeRemoveRate   float64
	DriftRate         float64

	// Selection settings choose the algorithm and Prequal hot/cold threshold.
	Algorithm        Algorithm
	SelectionChoices int
	QRIF             float64

	// WRR settings tune how quickly latency changes affect weights.
	WeightUpdateInterval time.Duration
	EWMAAlpha            float64
}

// Stats contains counters read and written with sync/atomic.
type Stats struct {
	TotalRequests      uint64
	SuccessfulRequests uint64
	FailedRequests     uint64
}
