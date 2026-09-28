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

// TestingT captures the subset of *testing.T required to report test failures.
type TestingT interface {
	Errorf(format string, args ...any)
}

// Option configures test harness leak verification behaviors.
type Option func(*verifyConfig)

type verifyConfig struct {
	timeout        time.Duration
	toleratedDelta int
	ignoredFilters []string
}

// WithTimeout sets maximum duration to wait for background goroutines to settle.
func WithTimeout(d time.Duration) Option {
	return func(c *verifyConfig) {
		c.timeout = d
	}
}

// WithToleratedDelta allows a bounded number of acceptable background delta goroutines.
func WithToleratedDelta(delta int) Option {
	return func(c *verifyConfig) {
		c.toleratedDelta = delta
	}
}

// IgnoreGoroutine excludes goroutines whose stack traces contain the given substring.
func IgnoreGoroutine(substr string) Option {
	return func(c *verifyConfig) {
		c.ignoredFilters = append(c.ignoredFilters, substr)
	}
}

// Check takes a baseline goroutine snapshot and returns a teardown function.
// When the teardown function executes (typically via defer sentinel.Check(t)()),
// it verifies that no leaked goroutines outlive the test function.
func Check(t TestingT, opts ...Option) func() {
	cfg := verifyConfig{
		timeout:        50 * time.Millisecond,
		toleratedDelta: 0,
	}
	for _, opt := range opts {
		opt(&cfg)
	}
	s := NewSentinel()
	return func() {
		report := s.AwaitVerification(cfg.timeout)
		if !report.Passed {
			t.Errorf("goroutine leak detected: %s", FormatReport(report))
		}
	}
}

// VerifyNone fails t immediately if any goroutines leaked relative to current baseline.
func VerifyNone(t TestingT, opts ...Option) {
	Check(t, opts...)()
}

// TestMainRunner is the interface for testing.M execution.
type TestMainRunner interface {
	Run() int
}

// VerifyTestMain runs m and verifies no goroutines leaked across the entire test suite.
// Returns the exit code from m.Run() or 1 if leaks were detected.
func VerifyTestMain(m TestMainRunner, opts ...Option) int {
	s := NewSentinel()
	code := m.Run()
	if code != 0 {
		return code
	}
	cfg := verifyConfig{
		timeout:        100 * time.Millisecond,
		toleratedDelta: 0,
	}
	for _, opt := range opts {
		opt(&cfg)
	}
	report := s.AwaitVerification(cfg.timeout)
	if !report.Passed {
		fmt.Printf("FAIL: goroutine leak detected in TestMain: %s\n", FormatReport(report))
		return 1
	}
	return 0
}
