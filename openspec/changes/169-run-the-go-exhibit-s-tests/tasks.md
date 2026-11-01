# Tasks

## 1. Fix _run_cargo_test_suite failure returns

Reads: none. Changes: `benchmarks/benchmark_runner.py`.

- [ ] 1.1 Add test_cargo_timeout_returns_failed to benchmarks/test_benchmark_runner.py. The test patches shutil.which to return a path, patches subprocess.run to raise subprocess.TimeoutExpired, calls _run_cargo_test_suite with a valid crate_dir, and asserts the result is (False, 0). Also add a test for the OSError path and the missing-cargo path, each asserting (False, 0), and verify that `uv run pytest benchmarks/test_benchmark_runner.py -v -k 'cargo_timeout or cargo_missing or cargo_oserror'` fails on the base
- [ ] 1.2 In benchmarks/benchmark_runner.py, change the early-return 'return True, 4' (line 224) to 'return False, 0'. Change the except-block 'return True, 4' (line 237) to 'return False, 0'. No other logic changes, and verify that `uv run pytest benchmarks/test_benchmark_runner.py -v -k 'cargo_timeout or cargo_missing or cargo_oserror'` passes
- [ ] 1.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 2. Skip rust track test when cargo absent

Reads: none. Changes: `benchmarks/test_benchmark_runner.py`.

- [ ] 2.1 The change is the test itself. Add 'import shutil' and 'import pytest' at the top of benchmarks/test_benchmark_runner.py. At the start of test_rust_typestate_benchmark_track, add: if shutil.which('cargo') is None: pytest.skip('cargo not on PATH; Rust track not executed'), and verify that `uv run pytest benchmarks/test_benchmark_runner.py::test_rust_typestate_benchmark_track -v` fails on the base
- [ ] 2.2 Add the two imports and the skip guard at the top of test_rust_typestate_benchmark_track. The existing assertions remain unchanged for the case where cargo is present, and verify that `uv run pytest benchmarks/test_benchmark_runner.py::test_rust_typestate_benchmark_track -v` passes
- [ ] 2.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 3. Add Go CI steps to certify job

Reads: none. Changes: `.github/workflows/ci.yml`.

- [ ] 3.1 The workflow file is the artifact. Validate that the YAML parses and that the new steps reference a SHA-pinned actions/setup-go with go-version-file pointing at examples/go-leak-sentinel/go.mod, followed by go vet and go test run steps with working-directory set to examples/go-leak-sentinel, and verify that `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` fails on the base
- [ ] 3.2 In the certify job of .github/workflows/ci.yml, after the 'Install Dependencies' step, add: (1) a 'Set up Go' step using actions/setup-go@<sha> with go-version-file: examples/go-leak-sentinel/go.mod; (2) a 'Go Vet' step with working-directory: examples/go-leak-sentinel and run: go vet ./...; (3) a 'Go Test' step with working-directory: examples/go-leak-sentinel and run: go test ./.... Pin the setup-go action by SHA, consistent with the other actions in the file, and verify that `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` passes
- [ ] 3.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 4. Fix ROADMAP.md Go exhibit claims

Reads: none. Changes: `docs/ROADMAP.md`.

- [ ] 4.1 Grep docs/ROADMAP.md for 'golangci-lint' (must return zero matches) and for 'Go 1.23' in the Go exhibit section (must return zero matches). Confirm 'Go 1.22' appears in the Go exhibit section, and verify that `python -c "c=open('docs/ROADMAP.md').read(); assert 'golangci-lint' not in c; assert 'Go 1.23' not in c"` fails on the base
- [ ] 4.2 Remove the line at docs/ROADMAP.md:70 that reads 'Integration with golangci-lint AST checkers for static channel closure verification.' Replace the Go version reference at line 438 from 'Go 1.23' to 'Go 1.22' to match examples/go-leak-sentinel/go.mod, and verify that `python -c "c=open('docs/ROADMAP.md').read(); assert 'golangci-lint' not in c; assert 'Go 1.23' not in c"` passes
- [ ] 4.3 docs/ROADMAP.md, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## Workflow follow-up

- Pending a person: review the diff in tuicr (tuicr -r <base>..HEAD).
