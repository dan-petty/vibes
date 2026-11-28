# Tasks

## 1. Move to the template of dan-petty/github-workflows

Kind: other.

- [x] 1.1 Set the new `_src_path` and run `copier recopy --skip-answered --defaults --overwrite --vcs-ref v0.2.0` with the answer `extra_labels`
  - Verification: `$ copier recopy --skip-answered --defaults --overwrite --vcs-ref v0.2.0 .` changes no file (`git status --short` prints nothing)
- [x] 1.2 Fix the statements that name dan-petty/infra as the template or `.github/labels.yml` as the source of the `type/` labels
  - Verification: `$ grep -rn "template of dan-petty/infra\|names that .github/labels.yml declares" AGENTS.md CONTRIBUTING.md tools patterns tests` prints nothing
- [x] 1.3 Check the changed files
  - Verification: `$ pre-commit run --from-ref origin/main --to-ref HEAD` and `$ pytest -q tests/test_sdlc_project_manager.py tests/test_roadmap_ingest.py` exit 0
