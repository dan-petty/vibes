# Proposal

## Why

#169 reports that the Go leak-sentinel exhibit has eight tests that have never been compiled or run in CI, and that _run_cargo_test_suite returns (True, 4) in four failure cases, violating the project rule that handlers must degrade visibly. Documentation also claims a golangci-lint integration that does not exist and lists Go 1.23 while go.mod specifies 1.22.

## What Changes

- Add actions/setup-go and go vet/go test steps to the certify job in .github/workflows/ci.yml
- Change _run_cargo_test_suite to return (False, 0) on cargo missing, Cargo.toml absent, timeout, or OSError
- Add pytest.skip to test_rust_typestate_benchmark_track when cargo is not on PATH
- Add a unit test that forces TimeoutExpired in _run_cargo_test_suite and asserts a failed track with zero tests
- Remove the golangci-lint line from docs/ROADMAP.md and change Go 1.23 to Go 1.22
- The change fits one pull request.

## Capabilities

### New Capabilities

- `go-exhibit-ci`: go-exhibit-vet-and-test
- `rust-benchmark-degradation`: cargo-unavailable-reports-failed, timeout-or-oserror-reports-failed, pytest-skip-when-cargo-missing

### Modified Capabilities

None.

## Impact

- Code: `benchmarks/benchmark_runner.py`, `benchmarks/test_benchmark_runner.py`, `.github/workflows/ci.yml`
- Documents: `docs/ROADMAP.md`

## Feasibility

- examples/go-leak-sentinel/sentinel_test.go contains eight test functions from TestSentinelCleanBaseline to TestVerifyTestMainClean, confirmed in the context code block.
- examples/go-leak-sentinel/go.mod reads 'go 1.22', confirmed in the context code block.
- .github/workflows/ci.yml has no Go setup or go test step, confirmed by reading the full workflow in context.
- benchmarks/benchmark_runner.py lines 221-237 show _run_cargo_test_suite returning (True, 4) in all four failure cases, confirmed in the context code block.
- AGENTS.md rule 4 states handlers must degrade visibly, confirmed in the context code block.
- docs/ROADMAP.md line 70 mentions golangci-lint and line 438 states Go 1.23, confirmed in the context code block.
- First implementation checks: 'cd examples/go-leak-sentinel && go vet ./...' to confirm the Go code compiles, then 'cd examples/go-leak-sentinel && go test ./...' to confirm all eight tests pass, then 'uv run pytest benchmarks/test_benchmark_runner.py -v' to confirm the Python test suite passes after the runner fix.

## Existing Tool Considered

Go standard tooling (go vet, go test) and the actions/setup-go GitHub Action cover the CI need. Rust cargo test is already in use. No new library is built; the change only corrects return values and adds standard CI steps.
