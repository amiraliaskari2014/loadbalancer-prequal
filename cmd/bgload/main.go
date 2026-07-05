// Command bgload creates direct backend traffic outside the load balancers.
//
// This makes selected backends look busy through X-RIF probes while keeping
// that traffic invisible to the balancers' own forwarding counters.
//
// Supported environment variables:
//
//	BG_TARGETS   comma-separated host:port targets, default "server1:80,server2:80"
//	BG_RATE      requests per second per target, default "15"
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func main() {
	targetSpecs := os.Getenv("BG_TARGETS")
	if targetSpecs == "" {
		targetSpecs = "server1:80,server2:80"
	}
	targetList := strings.Split(targetSpecs, ",")

	requestsPerSecond := 15.0
	if value := os.Getenv("BG_RATE"); value != "" {
		if parsedRate, err := strconv.ParseFloat(value, 64); err == nil && parsedRate > 0 {
			requestsPerSecond = parsedRate
		}
	}

	requestInterval := time.Duration(float64(time.Second) / requestsPerSecond)
	log.Printf("[bgload] targets=%v rate=%.1f/s interval=%s", targetList, requestsPerSecond, requestInterval)

	httpClient := &http.Client{Timeout: 5 * time.Second}

	requestTicker := time.NewTicker(requestInterval)
	defer requestTicker.Stop()
	for range requestTicker.C {
		for _, target := range targetList {
			go func(addr string) {
				response, err := httpClient.Get("http://" + addr + "/")
				if err == nil {
					response.Body.Close()
				}
			}(strings.TrimSpace(target))
		}
	}
}
