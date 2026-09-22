"""Tests for the supply chain audit: inventory, risk assessment, and mechanical fixes.

Two properties matter. The inventory must see what actually executes, not only what a
manifest declares — a package installed mid-workflow runs third-party code that no
dependency file records. And a fix must be mechanical and reversible, because anything
needing judgement belongs on the roadmap instead.
"""

# sentinel: allow[ZeroTrustSanitization] — one fixture needs a subdomain to prove endpoint
# classification matches documentation domains by suffix rather than by exact equality

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import supply_chain_audit
from supply_chain_audit import (
    Dependency,
    Inventory,
    audit,
    build_inventory,
    check_action_pinning,
    check_floor_drift,
    check_package_pinning,
    check_upstream_health,
    inventory_actions,
    inventory_endpoints,
    inventory_packages,
    inventory_requirements,
    is_external_endpoint,
    pin_action,
    raise_floor,
    risk_proposals,
)
from supply_chain_audit import (
    main as audit_main,
)
from upstream_facts import RepoFacts, assess_maturity

PYPROJECT = """
[project]
name = "demo"
dependencies = ["markdown-it-py>=3.0", "networkx>=3.0"]

[project.optional-dependencies]
test = ["pytest>=8.0"]
"""

WORKFLOW = """
jobs:
  build:
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@0123456789abcdef0123456789abcdef01234567
      - name: Diagrams
        run: |
          # Deliberately after the npm install above. Every earlier gate is clean.
          npm install --no-save mermaid@11 jsdom
"""


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    return tmp_path


def test_requirements_are_read_from_runtime_and_extras(tmp_path: Path) -> None:
    """An audit that reads only `dependencies` misses everything CI also installs."""
    deps = inventory_requirements(_repo(tmp_path) / "pyproject.toml")
    assert {(d.name, d.kind, d.declared) for d in deps} == {
        ("markdown-it-py", "runtime", ">=3.0"),
        ("networkx", "runtime", ">=3.0"),
        ("pytest", "test", ">=8.0"),
    }


def test_actions_are_inventoried_with_how_they_are_pinned(tmp_path: Path) -> None:
    """The pin is the whole risk, so it must survive into the inventory."""
    actions = {d.name: d.declared for d in inventory_actions(_repo(tmp_path) / ".github/workflows")}
    assert actions == {
        "actions/checkout": "v7",
        "actions/setup-python": "0123456789abcdef0123456789abcdef01234567",
    }


def test_packages_installed_mid_workflow_are_inventoried(tmp_path: Path) -> None:
    """`npm install` in a CI step executes third-party code no manifest records."""
    packages = inventory_packages([_repo(tmp_path) / ".github/workflows/ci.yml"])
    assert {(p.name, p.declared) for p in packages} == {("mermaid", "11"), ("jsdom", "")}


def test_prose_mentioning_an_install_is_not_inventoried(tmp_path: Path) -> None:
    """A comment explaining why a step follows `npm install` is prose, not an install list.

    The first draft inventoried the words of that very comment as nine npm packages.
    """
    packages = inventory_packages([_repo(tmp_path) / ".github/workflows/ci.yml"])
    assert {"Every", "earlier", "above."} & {p.name for p in packages} == set()


def test_only_genuinely_external_endpoints_count_as_egress() -> None:
    """Loopback, private, link-local and documentation hosts are not egress."""
    verdicts = {
        url: is_external_endpoint(url)
        for url in (
            "https://api.github.com/repos",
            "http://localhost:4318/v1/traces",
            "http://127.0.0.1:8080",
            "http://192.168.1.10",
            "http://10.0.0.5",
            "http://172.16.50.1",
            "http://169.254.169.254/latest/meta-data",
            "http://192.0.2.1/docs",
            "https://sub.example.com/v1",
        )
    }
    assert verdicts == {
        "https://api.github.com/repos": True,
        "http://localhost:4318/v1/traces": False,
        "http://127.0.0.1:8080": False,
        "http://192.168.1.10": False,
        "http://10.0.0.5": False,
        "http://172.16.50.1": False,
        "http://169.254.169.254/latest/meta-data": False,
        "http://192.0.2.1/docs": False,
        "https://sub.example.com/v1": False,
    }


def test_endpoint_inventory_skips_documentation_fixtures(tmp_path: Path) -> None:
    """The sanitization mandate's own placeholders must not read as network risk."""
    (tmp_path / "svc.py").write_text(
        'A = "https://api.github.com/x"\nB = "http://192.0.2.7/y"\n', encoding="utf-8"
    )
    hosts = {d.name for d in inventory_endpoints(tmp_path)}
    assert hosts == {"api.github.com"}


def test_a_floor_below_the_tested_major_is_drift() -> None:
    """CI certifies the newest release, so an older floor is an unverified claim."""
    drifted = check_floor_drift(
        Dependency(name="pytest", kind="test", declared=">=8.0", source="p", latest="9.1.1")
    )
    current = check_floor_drift(
        Dependency(name="networkx", kind="runtime", declared=">=3.0", source="p", latest="3.7")
    )
    assert (drifted is not None, current) == (True, None)


def test_below_one_point_zero_the_minor_carries_compatibility() -> None:
    """`ruff>=0.6` against 0.16 is the same drift as `pytest>=8` against 9."""
    finding = check_floor_drift(
        Dependency(name="ruff", kind="lint", declared=">=0.6", source="p", latest="0.16.8")
    )
    assert finding is not None and "0.6.x" in finding.detail


def test_an_exact_pin_is_never_reported_as_drift() -> None:
    """Only a `>=` floor makes a compatibility claim; `==` makes none."""
    assert check_floor_drift(
        Dependency(name="pytest", kind="test", declared="==8.0", source="p", latest="9.1.1")
    ) is None


def test_a_tag_reference_is_a_high_severity_finding_and_a_sha_is_not() -> None:
    """A tag can be repointed; an action runs with repository credentials."""
    tagged = check_action_pinning(
        Dependency(name="actions/checkout", kind="action", declared="v7", source="w")
    )
    pinned = check_action_pinning(
        Dependency(name="actions/checkout", kind="action", declared="a" * 40, source="w")
    )
    assert (tagged.severity if tagged else None, pinned) == ("high", None)


def test_an_unversioned_workflow_install_is_flagged_but_not_auto_fixable() -> None:
    """Which version to pin is a judgement, so it becomes a proposal rather than an edit."""
    finding = check_package_pinning(Dependency(name="jsdom", kind="npm", declared="", source="w"))
    assert finding is not None and finding.fixable is False


def test_an_archived_upstream_outranks_a_merely_quiet_one() -> None:
    """Archived is a decision by the maintainer; quiet is an observation about them."""
    dep = Dependency(name="thing", kind="action", declared="v1", source="w", repo="o/thing")
    archived = RepoFacts(
        requested="o/thing", full_name="o/thing", archived=True, fetched_at="2026-01-01T00:00:00Z"
    )
    finding = check_upstream_health(dep, assess_maturity(archived), archived)
    assert finding is not None and (finding.risk, finding.severity) == ("archived_upstream", "high")


def test_raising_a_floor_targets_the_major_not_the_exact_release() -> None:
    """The claim being repaired is which majors are supported, not which patch was tested."""
    dep = Dependency(name="pytest", kind="test", declared=">=8.0", source="p", latest="9.1.1")
    updated, changed = raise_floor('deps = ["pytest>=8.0"]', dep)
    assert (updated, changed) == ('deps = ["pytest>=9.0"]', True)


def test_pinning_an_action_keeps_the_tag_readable() -> None:
    """A bare 40-character hash tells a reader nothing about which version they are on."""
    dep = Dependency(name="actions/checkout", kind="action", declared="v7", source="w")
    updated, changed = pin_action("      - uses: actions/checkout@v7\n", dep, "b" * 40)
    assert (updated.strip(), changed) == (f"- uses: actions/checkout@{'b' * 40} # v7", True)


def test_pinning_does_not_touch_a_different_version_of_the_same_action() -> None:
    """A workflow may legitimately hold two refs; only the audited one moves."""
    dep = Dependency(name="actions/checkout", kind="action", declared="v7", source="w")
    updated, _ = pin_action("uses: actions/checkout@v7\nuses: actions/checkout@v6\n", dep, "c" * 40)
    assert "actions/checkout@v6" in updated


def test_findings_are_ranked_with_the_severe_first(tmp_path: Path) -> None:
    """An audit nobody can triage is an audit nobody reads."""
    inventory = build_inventory(_repo(tmp_path))
    inventory.requirements = [
        Dependency(name="pytest", kind="test", declared=">=8.0", source="p", latest="9.1.1")
    ]
    severities = [finding.severity for finding in audit(inventory, {})]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1}[s])


def test_only_unfixable_risks_become_roadmap_proposals() -> None:
    """A risk a script can repair should be repaired, not scheduled."""
    inventory = Inventory(
        packages=[Dependency(name="jsdom", kind="npm", declared="", source="w")],
        actions=[Dependency(name="a/b", kind="action", declared="v1", source="w", repo="a/b")],
    )
    proposals = risk_proposals(audit(inventory, {}))
    assert [p.marker for p in proposals] == ["supply-risk:unpinned_package/jsdom"]


def test_cli_audit_is_quiet_by_default_and_strict_on_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate must be able to fail the build; a review must be able not to."""
    monkeypatch.setattr(supply_chain_audit, "fetch_latest_version", lambda name: None)
    root = _repo(tmp_path)
    lenient = audit_main(["--root", str(root), "--snapshot", str(tmp_path / "none.json"), "audit"])
    strict = audit_main(
        ["--root", str(root), "--snapshot", str(tmp_path / "none.json"), "audit", "--strict"]
    )
    capsys.readouterr()
    assert (lenient, strict) == (0, 1)


def test_cli_fix_is_a_dry_run_unless_told_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting a manifest is a side effect, so it is opt-in."""
    monkeypatch.setattr(supply_chain_audit, "fetch_latest_version", lambda name: "9.1.1")
    root = _repo(tmp_path)
    original = (root / "pyproject.toml").read_text(encoding="utf-8")
    exit_code = audit_main(["--root", str(root), "--snapshot", str(tmp_path / "n.json"), "fix"])
    capsys.readouterr()
    assert (exit_code, (root / "pyproject.toml").read_text(encoding="utf-8")) == (0, original)


def test_cli_fix_raises_floors_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repaired manifest must declare what CI actually tests."""
    monkeypatch.setattr(supply_chain_audit, "fetch_latest_version", lambda name: "9.1.1")
    monkeypatch.setattr(supply_chain_audit, "resolve_action_sha", lambda repo, ref: None)
    root = _repo(tmp_path)
    exit_code = audit_main(
        ["--root", str(root), "--snapshot", str(tmp_path / "n.json"), "fix", "--write"]
    )
    capsys.readouterr()
    written = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert (exit_code, "pytest>=9.0" in written, "markdown-it-py>=9.0" in written) == (
        0,
        True,
        True,
    )


def test_cli_inventory_lists_every_class_of_dependency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four inventories are the point; collapsing them would hide the npm install."""
    monkeypatch.setattr(supply_chain_audit, "fetch_latest_version", lambda name: None)
    audit_main(["--root", str(_repo(tmp_path)), "--offline", "inventory"])
    out = capsys.readouterr().out
    assert all(
        heading in out
        for heading in (
            "Python requirements",
            "GitHub Actions",
            "Workflow package installs",
            "External network endpoints",
        )
    )
