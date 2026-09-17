// Package sentinel provides goroutine leak detection and stack profiling invariants.
package sentinel

import (
	"bytes"
	"context"
	"fmt"
	"runtime"
	"runtime/pprof"
	"strings"
	"sync"
	"time"
)

// LeakReport summarizes active goroutines compared against baseline.
type LeakReport struct {
	BaselineCount int
	CurrentCount  int
	LeakedCount   int
	StackDump     string
	Passed        bool
}

// LeakSentinel monitors goroutine allocations across concurrent execution lifecycles.
type LeakSentinel struct {
	baseline int
}

// NewSentinel creates and initializes a sentinel with current baseline goroutine count.
func NewSentinel() *LeakSentinel {
	return &LeakSentinel{
		baseline: runtime.NumGoroutine(),
	}
}

// Baseline returns the recorded initial goroutine count.
func (s *LeakSentinel) Baseline() int {
	return s.baseline
}

// CheckLeaked inspects runtime goroutine count and captures stack profiles if leaks exist.
func (s *LeakSentinel) CheckLeaked(toleratedDelta int) LeakReport {
	current := runtime.NumGoroutine()
	delta := current - s.baseline

	if delta <= toleratedDelta {
		return LeakReport{
			BaselineCount: s.baseline,
			CurrentCount:  current,
			LeakedCount:   0,
			Passed:        true,
		}
	}

	var buf bytes.Buffer
	_ = pprof.Lookup("goroutine").WriteTo(&buf, 2)

	return LeakReport{
		BaselineCount: s.baseline,
		CurrentCount:  current,
		LeakedCount:   delta,
		StackDump:     buf.String(),
		Passed:        false,
	}
}

// AwaitVerification polls until goroutines settle or timeout expires.
func (s *LeakSentinel) AwaitVerification(timeout time.Duration) LeakReport {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		report := s.CheckLeaked(0)
		if report.Passed {
			return report
		}
		time.Sleep(10 * time.Millisecond)
	}
	return s.CheckLeaked(0)
}

// --- Leaky vs. Safe Concurrency Patterns for AI Agents ---

// LeakyChannelWorker simulates an unbuffered channel send where the goroutine hangs forever.
func LeakyChannelWorker(val int) <-chan int {
	ch := make(chan int) // Unbuffered channel
	go func() {
		// Leaks if the caller abandons reading from ch
		ch <- val
	}()
	return ch
}

// SafeChannelWorker uses a buffered channel to guarantee non-blocking send and clean exit.
func SafeChannelWorker(val int) <-chan int {
	ch := make(chan int, 1) // Buffered channel prevents deadlock
	go func() {
		defer close(ch)
		ch <- val
	}()
	return ch
}

// LeakyContextWorker simulates a background worker that ignores context cancellation.
func LeakyContextWorker(stopCh <-chan struct{}) {
	go func() {
		// Flawed pattern: infinite loop without select on stopCh
		for {
			time.Sleep(100 * time.Millisecond)
		}
	}()
}

// SafeContextWorker uses select on ctx.Done() for clean, deterministic shutdown.
func SafeContextWorker(ctx context.Context, wg *sync.WaitGroup) {
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(10 * time.Millisecond):
				// Perform work step
			}
		}
	}()
}

// FormatReport outputs a human-readable diagnosis of leak metrics.
func FormatReport(r LeakReport) string {
	if r.Passed {
		return fmt.Sprintf("CLEAN: Baseline=%d Current=%d Leaked=0", r.BaselineCount, r.CurrentCount)
	}
	return fmt.Sprintf("LEAK DETECTED: Baseline=%d Current=%d Leaked=%d\n--- Stack Trace ---\n%s",
		r.BaselineCount, r.CurrentCount, r.LeakedCount, strings.TrimSpace(r.StackDump))
}
