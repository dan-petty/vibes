# Design

## Decisions

- **Add Go steps to the existing certify matrix job on one leg rather than a separate job.**: Reuses the checkout step and keeps the workflow file simple. Go steps do not depend on the Python version matrix, so one leg is sufficient.
- **Skip test_rust_typestate_benchmark_track with a stated reason when cargo is not on PATH.**: A contributor without a Rust toolchain is not blocked. The track is not reported as passed. This satisfies the AGENTS.md rule that handlers must degrade visibly.
- **Return (False, 0) for all four failure cases in _run_cargo_test_suite.**: AGENTS.md rule 4 requires handlers to degrade visibly. Reporting zero tests when cargo did not run is the honest result. The four cases are: cargo missing, Cargo.toml absent, TimeoutExpired, and OSError.

## Rejected alternatives

- **Create a separate CI job for Go tests.**: Duplicates the checkout step and adds a job that runs on every push. The issue body proposes adding to the existing certify job. The Go steps do not depend on the Python matrix.
- **Fail test_rust_typestate_benchmark_track when cargo is not on PATH.**: Would block every contributor without a Rust toolchain. The issue body proposes skip with a stated reason. AGENTS.md requires visible degradation, not a hard failure for an optional exhibit.
