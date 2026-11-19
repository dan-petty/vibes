"""Tests for the living standard certifier.

Each pillar runs an instrument that this repository already has. The tests build small
scratch repositories: a clean one passes all seven pillars, and one that breaks the rule
of a pillar fails that pillar. A pillar with no evaluator, or with an instrument that
checked no file or could not run, is not evaluated, and the result is then FAILED.
"""

# sentinel: allow[ZeroTrustSanitization] — negative fixture: Pillar 3 must find a private address

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import living_standard_certifier as certifier
from living_standard_certifier import (
    CertificationResult,
    CertificationStatus,
    PillarEvaluation,
    PillarId,
    evaluate_living_standard,
    export_sarif_json,
    format_markdown_certificate,
    generate_attestation_payload,
    generate_shields_badge_json,
    main,
)

CLEAN_FILES: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "scratch"\nversion = "1.0.0"\n',
    "README.md": "# Scratch\n\nA scratch repository that every pillar accepts.\n",
    "app.py": (
        "def add(left: int, right: int) -> int:\n"
        '    """Return the sum of two integers."""\n'
        "    return left + right\n"
    ),
    "tests/test_app.py": (
        "from app import add\n\n\n"
        "def test_add() -> None:\n"
        '    """Add two pairs of integers."""\n'
        "    assert (add(1, 2), add(0, 0)) == (3, 0)\n"
    ),
}

BRANCHY_FUNCTION = (
    "def branchy(value: int) -> int:\n"
    + "".join(f"    if value == {n}:\n        return {n}\n" for n in range(11))
    + "    return -1\n"
)
DEEP_FUNCTION = (
    "def deep(flag: bool) -> int:\n"
    + "".join("    " * level + "if flag:\n" for level in range(1, 7))
    + "    " * 7
    + "return 1\n    return 0\n"
)
SPRAWL_TEST = "def test_sprawl() -> None:\n    value = 1\n" + "    assert value == 1\n" * 5
OVERDUE_DEPRECATION = (
    "from compat import deprecated\n\n\n"
    '@deprecated(since="1.0.0", remove_in="2.0.0", replacement="current")\n'
    "def legacy() -> int:\n"
    '    """Return the old value."""\n'
    "    return 1\n"
)
MUTABLE_ACTION = (
    "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n"
)
REMOTE_CODE_LOAD = (
    "from transformers import AutoModel\n\n"
    'MODEL = AutoModel.from_pretrained("org/model", trust_remote_code=True)\n'
)
# "IGN" spelled in invisible Unicode Tag characters (U+E0000 block).
TAG_SMUGGLED_PROMPT = "Summarize." + "".join(chr(0xE0000 + ord(char)) for char in "IGN") + "\n"

# Each case adds files to the clean repository so that exactly one pillar's rule breaks.
BROKEN_RULES: dict[str, tuple[PillarId, dict[str, str]]] = {
    "complexity": (PillarId.P1_COMPLEXITY_BOUNDS, {"branchy.py": BRANCHY_FUNCTION}),
    "nesting": (PillarId.P1_COMPLEXITY_BOUNDS, {"deep.py": DEEP_FUNCTION}),
    "assertion-sprawl": (PillarId.P2_ASSERTION_CONSOLIDATION, {"tests/test_sprawl.py": SPRAWL_TEST}),
    "private-address": (PillarId.P3_ZERO_TRUST_EGRESS, {"egress.py": 'HOST = "10.0.0.1"\n'}),
    "secret-token": (PillarId.P3_ZERO_TRUST_EGRESS, {"settings.py": f'TOKEN = "{"ghp_" + "a" * 36}"\n'}),
    "unclosed-fence": (PillarId.P4_DOCS_INTEGRITY, {"README.md": "# Scratch\n\n```python\nprint('open')\n"}),
    "remote-code": (PillarId.P5_SUPPLY_CHAIN_AIBOM, {"model.py": REMOTE_CODE_LOAD}),
    "mutable-action": (PillarId.P5_SUPPLY_CHAIN_AIBOM, {".github/workflows/ci.yml": MUTABLE_ACTION}),
    "tag-smuggling": (PillarId.P6_PROMPT_GUARDRAILS, {"prompts/system.txt": TAG_SMUGGLED_PROMPT}),
    "overdue-deprecation": (
        PillarId.P7_CHANGE_MANAGEMENT,
        {
            "pyproject.toml": '[project]\nname = "scratch"\nversion = "2.0.0"\n',
            "legacy.py": OVERDUE_DEPRECATION,
        },
    ),
}


def _write_repository(root: Path, files: dict[str, str]) -> Path:
    """Write each file below root, creating its parent directories."""
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _pillar(result: CertificationResult, pillar_id: PillarId) -> PillarEvaluation:
    """Return the evaluation of one pillar from a certification result."""
    return next(p for p in result.pillars if p.pillar_id == pillar_id)


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """Return a scratch repository that breaks no pillar's rule."""
    return _write_repository(tmp_path / "clean", CLEAN_FILES)


def test_clean_repository_is_certified(clean_repo: Path) -> None:
    """A repository that breaks no rule passes all seven pillars, each one measured."""
    result = evaluate_living_standard(clean_repo, repo_name="scratch")

    assert (
        {p.pillar_id for p in result.pillars},
        [p.summary for p in result.pillars if not (p.evaluated and p.passed)],
        result.status,
        result.compliance_score,
    ) == (set(PillarId), [], CertificationStatus.CERTIFIED, 100.0)


@pytest.mark.parametrize("case", sorted(BROKEN_RULES))
def test_broken_rule_fails_its_pillar(tmp_path: Path, case: str) -> None:
    """A repository that breaks the rule of a pillar gets a failing result for that pillar."""
    pillar_id, files = BROKEN_RULES[case]
    root = _write_repository(tmp_path / case, {**CLEAN_FILES, **files})

    result = evaluate_living_standard(root)
    pillar = _pillar(result, pillar_id)

    assert (pillar.passed, pillar.violations_count > 0, result.status) == (
        False,
        True,
        CertificationStatus.FAILED,
    ), pillar.summary


def test_missing_evaluator_is_reported_not_evaluated(
    clean_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pillar with no evaluator is not evaluated, and the certificate is FAILED."""
    monkeypatch.setitem(certifier.PILLAR_EVALUATORS, PillarId.P4_DOCS_INTEGRITY, None)

    result = evaluate_living_standard(clean_repo)
    pillar = _pillar(result, PillarId.P4_DOCS_INTEGRITY)
    row = next(line for line in format_markdown_certificate(result).splitlines() if "P4_DOCS" in line)

    assert (pillar.evaluated, pillar.passed, pillar.score, "NOT EVALUATED" in row, result.status) == (
        False,
        False,
        0.0,
        True,
        CertificationStatus.FAILED,
    )


def test_instrument_error_is_reported_not_evaluated(
    clean_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instrument that cannot run leaves its pillar not evaluated and names the error."""

    def unreadable(root: Path) -> object:
        raise OSError(f"cannot read {root.name}")

    monkeypatch.setitem(certifier.PILLAR_EVALUATORS, PillarId.P2_ASSERTION_CONSOLIDATION, unreadable)

    result = evaluate_living_standard(clean_repo)
    pillar = _pillar(result, PillarId.P2_ASSERTION_CONSOLIDATION)
    named = "OSError: cannot read clean" in pillar.summary

    assert (pillar.evaluated, pillar.passed, named, result.status) == (
        False,
        False,
        True,
        CertificationStatus.FAILED,
    )


def test_empty_repository_is_not_evaluated(tmp_path: Path) -> None:
    """Instruments that check no file measure nothing, so no pillar passes."""
    result = evaluate_living_standard(tmp_path)

    assert (
        [p.pillar_id for p in result.pillars if p.evaluated or p.passed],
        result.status,
        result.compliance_score,
    ) == ([], CertificationStatus.FAILED, 0.0)


def test_pillar_7_needs_a_project_version(tmp_path: Path) -> None:
    """Without a version in pyproject.toml no deprecation deadline can be checked."""
    root = _write_repository(tmp_path, {**CLEAN_FILES, "pyproject.toml": '[project]\nname = "scratch"\n'})

    pillar = _pillar(evaluate_living_standard(root), PillarId.P7_CHANGE_MANAGEMENT)

    assert (pillar.evaluated, "version" in pillar.summary) == (False, True)


def test_overall_status_needs_every_pillar_to_pass() -> None:
    """One failed or not evaluated pillar makes the whole result FAILED."""
    passed = PillarEvaluation(PillarId.P1_COMPLEXITY_BOUNDS, "Bounds", True, 100.0, 0, "Clean")
    failed = PillarEvaluation(PillarId.P2_ASSERTION_CONSOLIDATION, "Asserts", False, 0.0, 1, "Sprawl")
    skipped = PillarEvaluation(
        PillarId.P3_ZERO_TRUST_EGRESS, "Egress", False, 0.0, 0, "Skip", evaluated=False
    )

    assert (
        certifier._calculate_overall_status((passed, passed)),
        certifier._calculate_overall_status((passed, failed)),
        certifier._calculate_overall_status((passed, skipped)),
    ) == (CertificationStatus.CERTIFIED, CertificationStatus.FAILED, CertificationStatus.FAILED)


def test_shields_badge_json_format() -> None:
    """Verify compliance badge conforms to Shields.io Endpoint specification."""
    dummy_pillar = PillarEvaluation(
        pillar_id=PillarId.P1_COMPLEXITY_BOUNDS,
        name="Bounds",
        passed=True,
        score=100.0,
        violations_count=0,
        summary="Clean",
    )
    result = CertificationResult(
        standard_version="v1.0.0",
        status=CertificationStatus.CERTIFIED,
        compliance_score=100.0,
        pillars=(dummy_pillar,),
        repository_name="test-repo",
        commit_sha="abcd123",
        timestamp="2026-09-24T00:00:00Z",
        attestation_digest="hash123",
    )
    badge = generate_shields_badge_json(result)

    assert (
        badge.get("schemaVersion"),
        badge.get("color"),
        "v1.0.0" in badge.get("label", ""),
        "CERTIFIED" in badge.get("message", ""),
    ) == (
        1,
        "brightgreen",
        True,
        True,
    )


def test_failed_result_has_red_badges(tmp_path: Path) -> None:
    """A FAILED result shows red in the badge JSON and in the Markdown certificate badge."""
    result = evaluate_living_standard(tmp_path)

    badge_color = generate_shields_badge_json(result).get("color")

    assert (badge_color, "-red)" in format_markdown_certificate(result)) == ("red", True)


def test_generate_attestation_payload(clean_repo: Path) -> None:
    """Verify the structure of the unsigned in-toto statement attestation payload."""
    result = evaluate_living_standard(clean_repo, repo_name="vibes_attest")
    attestation = generate_attestation_payload(result)
    first = attestation["predicate"]["pillars_evaluated"][0]

    assert (
        attestation.get("type"),
        attestation.get("predicateType"),
        attestation.get("subject", [{}])[0].get("name"),
        (first["evaluated"], first["violations_count"]),
    ) == (
        "https://in-toto.io/Statement/v1",
        "https://vibes.dev/attestations/living-standard/v1",
        "vibes_attest",
        (True, 0),
    )


def test_format_markdown_certificate(clean_repo: Path) -> None:
    """Verify Markdown certificate output contains badge link and all 7 pillars."""
    result = evaluate_living_standard(clean_repo, repo_name="vibes_md")
    report = format_markdown_certificate(result)

    assert (
        "Vibes Invariant Gate v1.0.0" in report,
        "Seven Pillars of Agentic Discipline Audit" in report,
        "P1_COMPLEXITY_BOUNDS" in report,
        "P7_CHANGE_MANAGEMENT" in report,
        report.count("✓ PASS"),
    ) == (
        True,
        True,
        True,
        True,
        7,
    )


def test_export_sarif_json(clean_repo: Path) -> None:
    """Verify SARIF 2.1.0 export contains valid driver and rules."""
    result = evaluate_living_standard(clean_repo, repo_name="vibes_sarif")
    sarif = export_sarif_json(result)

    assert (
        sarif.get("version"),
        sarif.get("runs", [{}])[0].get("tool", {}).get("driver", {}).get("name"),
        len(sarif.get("runs", [{}])[0].get("tool", {}).get("driver", {}).get("rules", [])),
    ) == (
        "2.1.0",
        "vibes-living-standard-certifier",
        7,
    )


def test_main_cli_execution_badge(clean_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main entry point with badge format."""
    ret = main([str(clean_repo), "--format", "badge"])
    parsed = json.loads(capsys.readouterr().out)

    assert (ret, parsed.get("schemaVersion"), parsed.get("color")) == (0, 1, "brightgreen")


def test_main_cli_fails_on_broken_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI exits 1 and shows a red badge when a pillar fails."""
    root = _write_repository(tmp_path, {**CLEAN_FILES, "branchy.py": BRANCHY_FUNCTION})

    ret = main([str(root), "--format", "badge"])
    parsed = json.loads(capsys.readouterr().out)

    assert (ret, parsed.get("color")) == (1, "red")


def test_main_cli_execution_attestation(clean_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main entry point with attestation format."""
    ret = main([str(clean_repo), "--format", "attestation"])
    parsed = json.loads(capsys.readouterr().out)

    assert (ret, parsed.get("type")) == (0, "https://in-toto.io/Statement/v1")
