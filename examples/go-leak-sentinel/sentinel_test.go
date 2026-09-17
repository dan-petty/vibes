package sentinel

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestSentinelCleanBaseline(t *testing.T) {
	sentinel := NewSentinel()
	if sentinel.Baseline() <= 0 {
		t.Fatalf("expected positive baseline goroutine count, got %d", sentinel.Baseline())
	}

	report := sentinel.CheckLeaked(0)
	if !report.Passed {
		t.Fatalf("expected initial report to pass, got: %s", FormatReport(report))
	}
}

func TestSafeChannelWorkerReclaimsGoroutine(t *testing.T) {
	sentinel := NewSentinel()

	ch := SafeChannelWorker(42)
	val := <-ch
	if val != 42 {
		t.Fatalf("expected value 42, got %d", val)
	}

	report := sentinel.AwaitVerification(100 * time.Millisecond)
	if !report.Passed {
		t.Fatalf("expected zero leaks for SafeChannelWorker, got report: %s", FormatReport(report))
	}
}

func TestSafeContextWorkerShutsDownDeterministically(t *testing.T) {
	sentinel := NewSentinel()
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup

	SafeContextWorker(ctx, &wg)

	// Let worker cycle once
	time.Sleep(20 * time.Millisecond)
	cancel()
	wg.Wait()

	report := sentinel.AwaitVerification(100 * time.Millisecond)
	if !report.Passed {
		t.Fatalf("expected zero leaks after context cancellation, got report: %s", FormatReport(report))
	}
}

func TestLeakyChannelWorkerDetected(t *testing.T) {
	sentinel := NewSentinel()

	// Launch leaky worker without reading from channel
	_ = LeakyChannelWorker(99)
	time.Sleep(30 * time.Millisecond)

	report := sentinel.CheckLeaked(0)
	if report.Passed {
		t.Fatal("expected LeakyChannelWorker to be detected as leak, but report passed")
	}
	if report.LeakedCount < 1 {
		t.Fatalf("expected at least 1 leaked goroutine, got %d", report.LeakedCount)
	}
	if report.StackDump == "" {
		t.Fatal("expected non-empty stack dump on leak detection")
	}
}
