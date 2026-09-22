#!/usr/bin/env python3
"""Audit what this repository depends on and what it talks to, and fix what is mechanical.

The landscape survey looks outward at the field. This looks at the supply chain: the
packages installed, the actions executed in CI, and the network endpoints the code
reaches. Both ask "can I depend on this" and both answer it with `upstream_facts`, because
two instruments that score maturity differently cannot be reconciled about risk.

Four inventories, because a dependency review that reads only `pyproject.toml` misses most
of what actually executes:

* Python requirements declared in `pyproject.toml`, runtime and extras.
* GitHub Actions invoked by workflows — code that runs with repository credentials.
* Packages installed mid-workflow by `npm install` and similar, which no manifest records.
* Network endpoints appearing in source, which is the egress surface.

Every finding carries the evidence that would falsify it, and every fix is mechanical and
reversible. A risk that needs judgement becomes a roadmap proposal instead of an edit.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from roadmap_emit import DEFAULT_MILESTONE, Proposal, insert_proposals
from source_tree_policy import iter_source_files
from upstream_facts import Maturity, RepoFacts, assess_maturity, load_snapshot

DEFAULT_PYPROJECT: Final[Path] = Path("pyproject.toml")
DEFAULT_WORKFLOWS: Final[Path] = Path(".github/workflows")
DEFAULT_PRECOMMIT: Final[Path] = Path(".pre-commit-config.yaml")
DEFAULT_SNAPSHOT: Final[Path] = Path("docs/landscape/snapshot.json")
DEFAULT_ROADMAP: Final[Path] = Path("docs/ROADMAP.md")
RISK_MARKER: Final[str] = "supply-risk"

PYPI_TIMEOUT_SECONDS: Final[int] = 20
# A maturity band at or below this means the upstream is not visibly maintained.
DORMANT_BANDS: Final[frozenset[str]] = frozenset({"Dormant", "Emerging"})

_REQUIREMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<op>>=|==|~=|>)?\s*(?P<version>[0-9][0-9A-Za-z.*+!-]*)?"
)
_ACTION_RE: Final[re.Pattern[str]] = re.compile(r"uses:\s*(?P<ref>[^\s#]+)")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_NPM_RE: Final[re.Pattern[str]] = re.compile(r"npm (?:install|i|ci)\s+(?P<args>[^\n|&;#]+)")
# A package specifier, so prose in a comment that happens to contain "npm install" cannot
# be mistaken for an install list. The first draft inventoried the words "above. Every
# earlier gate runs against a" as nine npm packages.
_NPM_SPEC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>@?[a-z0-9][a-z0-9._/-]*)(?:@(?P<version>[^\s]+))?$"
)
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+")

# Domains reserved for documentation. Deliberately a separate judgement from
# `sanitization_policy.is_documentable`, which answers "may this appear in committed
# prose". This answers "does this leave the machine", and the two differ: the cloud
# metadata address is documentable there and is emphatically not external egress here.
_ILLUSTRATIVE_SUFFIXES: Final[tuple[str, ...]] = (
    "example.com",
    "example.org",
    "example.net",
    "invalid",
    "localhost",
)


@dataclass(frozen=True)
class Dependency:
    """One declared dependency, wherever it was declared."""

    name: str
    kind: str
    declared: str
    source: str
    repo: str | None = None
    latest: str | None = None
    installed: str | None = None


@dataclass(frozen=True)
class Finding:
    """One supply-chain risk, with the evidence that would settle it."""

    risk: str
    subject: str
    severity: str
    detail: str
    evidence: str
    source: str
    fixable: bool = False

    def marker(self) -> str:
        """Return the stable identity used to keep roadmap proposals idempotent."""
        return f"{RISK_MARKER}:{self.risk}/{self.subject}"


@dataclass
class Inventory:
    """Everything this repository depends on or reaches, gathered from four sources."""

    requirements: list[Dependency] = field(default_factory=list)
    actions: list[Dependency] = field(default_factory=list)
    packages: list[Dependency] = field(default_factory=list)
    endpoints: list[Dependency] = field(default_factory=list)

    def all(self) -> list[Dependency]:
        """Return every inventoried item in one list."""
        return self.requirements + self.actions + self.packages + self.endpoints


# --- Inventory ------------------------------------------------------------------------


def parse_requirement(text: str, source: str, kind: str) -> Dependency | None:
    """Parse one PEP 508 requirement into a dependency record."""
    match = _REQUIREMENT_RE.match(text.strip())
    if not match:
        return None
    return Dependency(
        name=match.group("name").lower(),
        kind=kind,
        declared=f"{match.group('op') or ''}{match.group('version') or ''}",
        source=source,
    )


def inventory_requirements(pyproject: Path) -> list[Dependency]:
    """Read every declared Python requirement, runtime and optional alike."""
    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups = [("runtime", project.get("dependencies", []))]
    groups += list(project.get("optional-dependencies", {}).items())
    found = [
        parse_requirement(text, str(pyproject), kind)
        for kind, entries in groups
        for text in entries
    ]
    return [dep for dep in found if dep]


def inventory_actions(workflows: Path) -> list[Dependency]:
    """Read every GitHub Action a workflow invokes, with how it is pinned."""
    if not workflows.is_dir():
        return []
    actions: list[Dependency] = []
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for match in _ACTION_RE.finditer(path.read_text(encoding="utf-8")):
            name, _, ref = match.group("ref").partition("@")
            actions.append(
                Dependency(name=name, kind="action", declared=ref, source=str(path), repo=name)
            )
    return _deduplicate(actions)


def inventory_packages(paths: Sequence[Path]) -> list[Dependency]:
    """Read packages installed mid-workflow, which no manifest records.

    `npm install --no-save mermaid@11 jsdom` executes third-party code in CI without
    appearing in any dependency file. A review that reads only manifests cannot see it.
    """
    packages: list[Dependency] = []
    for path in paths:
        if not path.is_file():
            continue
        for match in _NPM_RE.finditer(_without_comments(path.read_text(encoding="utf-8"))):
            packages.extend(_npm_specs(match.group("args"), str(path)))
    return _deduplicate(packages)


def _without_comments(text: str) -> str:
    """Drop whole-line comments before scanning for commands.

    A comment explaining *why* a step follows `npm install` is prose, not an install list.
    Trimming from the `#` rightwards is not enough, because the `#` comes first.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _npm_specs(args: str, source: str) -> Iterator[Dependency]:
    """Yield each package named on one npm command line, skipping flags and prose."""
    for token in args.split():
        match = _NPM_SPEC_RE.match(token)
        if token.startswith("-") or not match:
            continue
        yield Dependency(
            name=match.group("name"),
            kind="npm",
            declared=match.group("version") or "",
            source=source,
        )


def inventory_endpoints(root: Path) -> list[Dependency]:
    """Read every network endpoint literal in source, excluding local and illustrative ones."""
    endpoints = [
        endpoint
        for path in iter_source_files(root, (".py", ".mjs", ".yml", ".yaml"))
        for endpoint in _endpoints_in(path)
    ]
    return _deduplicate(endpoints)


def _endpoints_in(path: Path) -> Iterator[Dependency]:
    """Yield each genuinely external endpoint named in one file."""
    for match in _URL_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        url = match.group(0).rstrip(".,)\"'")
        if is_external_endpoint(url):
            yield Dependency(name=_host_of(url), kind="endpoint", declared=url, source=str(path))


def is_external_endpoint(url: str) -> bool:
    """Report whether a URL names a host that traffic would actually leave the machine for.

    Loopback, RFC 1918, link-local and documentation addresses are not egress. They are
    either local services or the deliberate placeholders the sanitization mandate requires,
    and reporting this repository's own documentation policy as a network risk would bury
    the one endpoint that matters under a dozen that do not.
    """
    host = _host_of(url).split(":", 1)[0].strip("[]")
    if host.endswith(_ILLUSTRATIVE_SUFFIXES) or host in _ILLUSTRATIVE_SUFFIXES:
        return False
    try:
        return not _is_internal_address(ipaddress.ip_address(host))
    except ValueError:
        return True


def _is_internal_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Report whether an address names something that never leaves the local network."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _host_of(url: str) -> str:
    """Return the host portion of a URL without importing a parser for one field."""
    return url.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0]


def _deduplicate(items: Sequence[Dependency]) -> list[Dependency]:
    """Collapse repeats by (name, declared), keeping the first source that named each."""
    seen: dict[tuple[str, str], Dependency] = {}
    for item in items:
        seen.setdefault((item.name, item.declared), item)
    return list(seen.values())


def build_inventory(root: Path) -> Inventory:
    """Gather all four inventories for one repository."""
    workflow_files = sorted((root / DEFAULT_WORKFLOWS).glob("*.yml"))
    return Inventory(
        requirements=inventory_requirements(root / DEFAULT_PYPROJECT),
        actions=inventory_actions(root / DEFAULT_WORKFLOWS),
        packages=inventory_packages([*workflow_files, root / DEFAULT_PRECOMMIT]),
        endpoints=inventory_endpoints(root),
    )


# --- Risk assessment ------------------------------------------------------------------


def fetch_latest_version(name: str) -> str | None:
    """Return the newest version PyPI publishes for a package, or None if unreachable."""
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/json", timeout=PYPI_TIMEOUT_SECONDS
        ) as response:
            return str(json.load(response)["info"]["version"])
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return None


def installed_version(name: str) -> str | None:
    """Return the version actually installed here, which is what the gates really tested."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.12+
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _major(version_text: str) -> int | None:
    """Return the leading major component of a version, or None if it has none."""
    match = re.match(r"^[^0-9]*(\d+)", version_text)
    return int(match.group(1)) if match else None


def _compat_series(version_text: str) -> tuple[int, int] | None:
    """Return the components that change when compatibility does.

    For 1.0 and later that is the major alone. Below 1.0, SemVer puts the breaking-change
    axis on the minor instead, so `ruff>=0.6` against a current 0.16 is the same drift as
    `pytest>=8` against 9 — and comparing majors alone reports neither.
    """
    match = re.match(r"^[^0-9]*(\d+)(?:\.(\d+))?", version_text)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, 0) if major > 0 else (0, minor)


def check_floor_drift(dep: Dependency) -> Finding | None:
    """Flag a declared floor that permits a major version nobody tests.

    CI installs the newest release, so the configuration the gates certify is the newest
    one. A floor several majors below it is a claim about compatibility that no run has
    ever checked — and the first person to resolve an old version discovers it alone.
    """
    if not dep.latest or not dep.declared.startswith(">="):
        return None
    if not _floor_has_drifted(dep):
        return None
    admits = _series_label(_compat_series(dep.declared) or (0, 0)) + ".x"
    return Finding(
        risk="floor_drift",
        subject=dep.name,
        severity="medium",
        detail=(
            f"Floor {dep.declared} admits {admits} while {dep.latest} is current "
            f"and {dep.installed or 'the newest release'} is what CI actually tests."
        ),
        evidence=f"pypi.org/project/{dep.name} latest={dep.latest}; declared {dep.declared}",
        source=dep.source,
        fixable=True,
    )


def _series_label(series: tuple[int, int]) -> str:
    """Render a compatibility series the way a version floor is written."""
    return f"{series[0]}" if series[0] else f"0.{series[1]}"


def _floor_has_drifted(dep: Dependency) -> bool:
    """Report whether a declared floor admits a compatibility series nobody tests."""
    declared = _compat_series(dep.declared)
    latest = _compat_series(dep.latest or "")
    return declared is not None and latest is not None and declared < latest


def check_action_pinning(dep: Dependency) -> Finding | None:
    """Flag an action pinned to a mutable tag rather than an immutable commit."""
    if _SHA_RE.match(dep.declared):
        return None
    return Finding(
        risk="mutable_action_ref",
        subject=dep.name,
        severity="high",
        detail=(
            f"Pinned to `{dep.declared}`, a tag the upstream can repoint at any commit. "
            "This action runs with repository credentials."
        ),
        evidence=f"{dep.source}: uses: {dep.name}@{dep.declared}",
        source=dep.source,
        fixable=True,
    )


def check_package_pinning(dep: Dependency) -> Finding | None:
    """Flag a mid-workflow package install with no version constraint at all."""
    if dep.declared:
        return None
    return Finding(
        risk="unpinned_package",
        subject=dep.name,
        severity="medium",
        detail=(
            "Installed during CI with no version constraint, so every run may execute "
            "different third-party code. No manifest records it."
        ),
        evidence=f"{dep.source}: npm install ... {dep.name}",
        source=dep.source,
        fixable=False,
    )


def check_upstream_health(dep: Dependency, maturity: Maturity, facts: RepoFacts) -> Finding | None:
    """Flag a dependency whose upstream is archived or no longer visibly maintained."""
    if facts.archived:
        return _upstream_finding(dep, "archived_upstream", "high", "Upstream is archived.", facts)
    if maturity.band in DORMANT_BANDS:
        detail = (
            f"Upstream maturity {maturity.score} ({maturity.band}); "
            f"last push {facts.pushed_at[:10] or 'unknown'}."
        )
        return _upstream_finding(dep, "dormant_upstream", "medium", detail, facts)
    return None


def _upstream_finding(
    dep: Dependency, risk: str, severity: str, detail: str, facts: RepoFacts
) -> Finding:
    """Build a finding about an upstream project, citing the facts it was judged on."""
    return Finding(
        risk=risk,
        subject=dep.name,
        severity=severity,
        detail=detail,
        evidence=(
            f"github.com/{facts.full_name}: pushed {facts.pushed_at[:10] or '—'}, "
            f"{facts.releases} versions ({facts.release_source}), fetched {facts.fetched_at[:10]}"
        ),
        source=dep.source,
        fixable=False,
    )


def check_plaintext_endpoint(dep: Dependency) -> Finding | None:
    """Flag an external endpoint reached over plaintext HTTP."""
    if not dep.declared.startswith("http://"):
        return None
    return Finding(
        risk="plaintext_endpoint",
        subject=dep.name,
        severity="high",
        detail="External endpoint reached over plaintext HTTP, so traffic is interceptable.",
        evidence=f"{dep.source}: {dep.declared}",
        source=dep.source,
        fixable=False,
    )


# --- Audit ----------------------------------------------------------------------------

SEVERITY_ORDER: Final[dict[str, int]] = {"high": 0, "medium": 1, "low": 2}


def audit(inventory: Inventory, snapshot: dict[str, RepoFacts]) -> list[Finding]:
    """Run every check against every inventoried item, most severe first."""
    findings: list[Finding | None] = []
    for dep in inventory.requirements:
        findings.append(check_floor_drift(dep))
    for dep in inventory.actions:
        findings.append(check_action_pinning(dep))
        findings.extend(_upstream_checks(dep, snapshot))
    for dep in inventory.packages:
        findings.append(check_package_pinning(dep))
    for dep in inventory.endpoints:
        findings.append(check_plaintext_endpoint(dep))
    present = [finding for finding in findings if finding]
    return sorted(present, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.risk, f.subject))


def _upstream_checks(dep: Dependency, snapshot: dict[str, RepoFacts]) -> list[Finding]:
    """Assess the upstream project behind a dependency, when facts have been fetched."""
    facts = snapshot.get(dep.repo or "")
    if facts is None or facts.error:
        return []
    finding = check_upstream_health(dep, assess_maturity(facts), facts)
    return [finding] if finding else []


def enrich_versions(deps: Sequence[Dependency], offline: bool) -> list[Dependency]:
    """Attach the installed and newest published versions to each requirement."""
    enriched = []
    for dep in deps:
        latest = None if offline else fetch_latest_version(dep.name)
        enriched.append(
            Dependency(**{**asdict(dep), "latest": latest, "installed": installed_version(dep.name)})
        )
    return enriched


# --- Fixes ----------------------------------------------------------------------------


def raise_floor(pyproject_text: str, dep: Dependency) -> tuple[str, bool]:
    """Raise one requirement's floor to the major version that is actually tested.

    The floor moves to `>=<latest major>.0`, not to the exact newest release: the claim
    being repaired is "which majors are supported", and pinning a patch would replace an
    unverified claim with an over-tight one.
    """
    if not dep.latest:
        return pyproject_text, False
    series = _compat_series(dep.latest)
    if series is None:
        return pyproject_text, False
    floor = _floor_text(series)
    pattern = re.compile(rf'"{re.escape(dep.name)}{re.escape(dep.declared)}"')
    replacement = f'"{dep.name}>={floor}"'
    updated, count = pattern.subn(replacement, pyproject_text)
    return updated, count > 0


def _floor_text(series: tuple[int, int]) -> str:
    """Render the floor a compatibility series implies."""
    return f"{series[0]}.0" if series[0] else f"0.{series[1]}"


def resolve_action_sha(repo: str, ref: str) -> str | None:
    """Resolve a mutable action tag to the immutable commit it currently points at."""
    from upstream_facts import gh_json

    try:
        sha = gh_json(f"/repos/{repo}/commits/{ref}", ".sha")
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None
    return str(sha) if sha else None


def pin_action(workflow_text: str, dep: Dependency, sha: str) -> tuple[str, bool]:
    """Rewrite one action reference to a commit SHA, keeping the tag as a comment.

    The tag is preserved beside the SHA because a bare 40-character hash tells a reader
    nothing about which version they are on, and a pin nobody can read is a pin nobody
    will ever update.
    """
    pattern = re.compile(rf"uses:\s*{re.escape(dep.name)}@{re.escape(dep.declared)}(?!\S)")
    updated, count = pattern.subn(f"uses: {dep.name}@{sha} # {dep.declared}", workflow_text)
    return updated, count > 0


def apply_fixes(root: Path, findings: Sequence[Finding], inventory: Inventory) -> list[str]:
    """Apply every mechanical fix, returning a line per change made."""
    applied: list[str] = []
    by_subject = {dep.name: dep for dep in inventory.all()}
    applied += _apply_floor_fixes(root, findings, by_subject)
    applied += _apply_pin_fixes(root, findings, by_subject)
    return applied


def _apply_floor_fixes(
    root: Path, findings: Sequence[Finding], by_subject: dict[str, Dependency]
) -> list[str]:
    """Raise every drifted floor in one rewrite of the manifest."""
    targets = [f for f in findings if f.risk == "floor_drift"]
    if not targets:
        return []
    path = root / DEFAULT_PYPROJECT
    text = path.read_text(encoding="utf-8")
    applied: list[str] = []
    for dep in [by_subject[f.subject] for f in targets if f.subject in by_subject]:
        text, applied = _raise_one_floor(text, dep, applied)
    if applied:
        path.write_text(text, encoding="utf-8")
    return applied


def _raise_one_floor(
    text: str, dep: Dependency, applied: list[str]
) -> tuple[str, list[str]]:
    """Raise one floor in the manifest text, recording the change if it landed."""
    updated, changed = raise_floor(text, dep)
    if not changed:
        return text, applied
    floor = _floor_text(_compat_series(dep.latest or "") or (0, 0))
    return updated, [*applied, f"floor: {dep.name}{dep.declared} -> >={floor}"]


def _apply_pin_fixes(
    root: Path, findings: Sequence[Finding], by_subject: dict[str, Dependency]
) -> list[str]:
    """Pin every mutably-referenced action to the commit its tag currently names."""
    applied: list[str] = []
    for finding in [f for f in findings if f.risk == "mutable_action_ref"]:
        dep = by_subject.get(finding.subject)
        if dep is None or dep.repo is None:
            continue
        sha = resolve_action_sha(dep.repo, dep.declared)
        if sha is None:
            continue
        applied += _pin_in_workflows(root, dep, sha)
    return applied


def _pin_in_workflows(root: Path, dep: Dependency, sha: str) -> list[str]:
    """Rewrite one action reference everywhere it appears across the workflow directory."""
    applied: list[str] = []
    for path in sorted((root / DEFAULT_WORKFLOWS).glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        updated, changed = pin_action(text, dep, sha)
        if changed:
            path.write_text(updated, encoding="utf-8")
            applied.append(f"pin: {dep.name}@{dep.declared} -> {sha[:12]}… in {path.name}")
    return applied


def risk_proposals(findings: Sequence[Finding]) -> list[Proposal]:
    """Turn the risks that need judgement into roadmap proposals."""
    return [
        Proposal(
            marker=finding.marker(),
            title=f"Supply chain: {finding.risk.replace('_', ' ')} in {finding.subject}",
            context=finding.detail,
            evidence=finding.evidence,
        )
        for finding in findings
        if not finding.fixable
    ]


# --- CLI ------------------------------------------------------------------------------

SEVERITY_ICON: Final[dict[str, str]] = {"high": "🔴", "medium": "🟠", "low": "🟡"}


def _load(args: argparse.Namespace) -> tuple[Inventory, dict[str, RepoFacts]]:
    """Build the inventory and load cached upstream facts."""
    inventory = build_inventory(args.root)
    inventory.requirements = enrich_versions(inventory.requirements, args.offline)
    return inventory, load_snapshot(args.snapshot)


def _handle_inventory(args: argparse.Namespace) -> int:
    """Print everything this repository depends on or reaches."""
    inventory, _ = _load(args)
    groups = (
        ("Python requirements", inventory.requirements),
        ("GitHub Actions", inventory.actions),
        ("Workflow package installs", inventory.packages),
        ("External network endpoints", inventory.endpoints),
    )
    for title, deps in groups:
        print(f"\n{title} ({len(deps)}):")
        for dep in sorted(deps, key=lambda d: d.name):
            trailer = f" (installed {dep.installed}, latest {dep.latest})" if dep.latest else ""
            print(f"  {dep.name:<28} {dep.declared or '<unpinned>':<16}{trailer}")
    return 0


def _handle_audit(args: argparse.Namespace) -> int:
    """Report supply-chain risks, most severe first."""
    inventory, snapshot = _load(args)
    findings = audit(inventory, snapshot)
    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2))
        return 0
    print(f"{len(findings)} finding(s) across {len(inventory.all())} inventoried items:\n")
    for finding in findings:
        icon = SEVERITY_ICON.get(finding.severity, "•")
        fixable = " [fixable]" if finding.fixable else ""
        print(f"{icon} {finding.risk}: {finding.subject}{fixable}")
        print(f"    {finding.detail}")
        print(f"    evidence: {finding.evidence}")
    return 1 if args.strict and findings else 0


def _handle_fix(args: argparse.Namespace) -> int:
    """Apply the mechanical fixes, or show what they would be."""
    inventory, snapshot = _load(args)
    findings = audit(inventory, snapshot)
    fixable = [finding for finding in findings if finding.fixable]
    if not args.write:
        print(f"{len(fixable)} mechanical fix(es) available:")
        for finding in fixable:
            print(f"  {finding.risk}: {finding.subject} — {finding.detail}")
        print("\nDry run. Pass --write to apply, then re-run the gates.")
        return 0
    applied = apply_fixes(args.root, findings, inventory)
    for line in applied:
        print(f"  {line}")
    print(f"\nApplied {len(applied)} change(s). Re-run the full gate set before committing.")
    return 0


def _handle_roadmap(args: argparse.Namespace) -> int:
    """Propose roadmap deliverables for the risks that need judgement."""
    inventory, snapshot = _load(args)
    proposals = risk_proposals(audit(inventory, snapshot))
    original = args.roadmap.read_text(encoding="utf-8")
    updated, added, skipped = insert_proposals(original, proposals, args.milestone)
    print(f"{len(added)} new, {len(skipped)} already tracked.")
    for title in added:
        print(f"  + {title}")
    if not args.write:
        print("\nDry run. Pass --write to apply.")
        return 0
    args.roadmap.write_text(updated, encoding="utf-8")
    print(f"\nWrote {len(added)} item(s) to {args.roadmap}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the supply chain audit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--offline", action="store_true", help="Skip PyPI lookups; report only local signals"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inventory", help="List every dependency, action, package and endpoint")

    audit_cmd = sub.add_parser("audit", help="Report supply-chain risks")
    audit_cmd.add_argument("--json", action="store_true")
    audit_cmd.add_argument("--strict", action="store_true", help="Exit non-zero on any finding")

    fix = sub.add_parser("fix", help="Apply the mechanical fixes")
    fix.add_argument("--write", action="store_true", help="Apply; otherwise dry-run")

    roadmap = sub.add_parser("roadmap", help="Propose deliverables for risks needing judgement")
    roadmap.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP)
    roadmap.add_argument("--milestone", default=DEFAULT_MILESTONE)
    roadmap.add_argument("--write", action="store_true")
    return parser


_COMMANDS: Final[dict[str, Any]] = {
    "inventory": _handle_inventory,
    "audit": _handle_audit,
    "fix": _handle_fix,
    "roadmap": _handle_roadmap,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the supply chain audit."""
    args = build_arg_parser().parse_args(argv)
    return int(_COMMANDS[args.command](args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
