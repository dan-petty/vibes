## Purpose
Defines how the Rust type-state benchmark track reports its result when cargo is unavailable, the crate is missing, or the test suite times out.

## ADDED Requirements

### Requirement: cargo-unavailable-reports-failed
When cargo is not on PATH or Cargo.toml is absent, `_run_cargo_test_suite` SHALL return a failed track with zero tests.

#### Scenario: cargo not on PATH
- **WHEN** shutil.which('cargo') returns None
- **THEN** _run_cargo_test_suite returns (False, 0)

#### Scenario: Cargo.toml missing
- **WHEN** crate_dir / 'Cargo.toml' is not a file
- **THEN** _run_cargo_test_suite returns (False, 0)

### Requirement: timeout-or-oserror-reports-failed
When `cargo test` exceeds its timeout or raises OSError, `_run_cargo_test_suite` SHALL return a failed track with zero tests.

#### Scenario: timeout expired
- **WHEN** subprocess.run raises subprocess.TimeoutExpired
- **THEN** _run_cargo_test_suite returns (False, 0)

#### Scenario: OSError raised
- **WHEN** subprocess.run raises OSError
- **THEN** _run_cargo_test_suite returns (False, 0)

### Requirement: pytest-skip-when-cargo-missing
The test `test_rust_typestate_benchmark_track` SHALL skip with a stated reason when cargo is not on PATH.

#### Scenario: cargo absent at test time
- **WHEN** shutil.which('cargo') returns None at test execution time
- **THEN** pytest reports the test as SKIPPED with a reason mentioning cargo
