## Purpose
Defines the CI verification steps for the Go leak-sentinel exhibit, including go vet and go test execution on the certify matrix.

## ADDED Requirements

### Requirement: go-exhibit-vet-and-test
The certify job SHALL run `go vet ./...` and `go test ./...` in the `examples/go-leak-sentinel` directory, and the job SHALL fail when either command exits non-zero.

#### Scenario: go vet passes and go test passes
- **WHEN** The Go code in examples/go-leak-sentinel compiles without vet errors and all eight tests pass
- **THEN** Both steps exit with code 0 and the certify job continues to the next step

#### Scenario: go test fails
- **WHEN** A test in sentinel_test.go fails
- **THEN** The go test step exits non-zero and the certify job fails
