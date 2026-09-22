#!/usr/bin/env python3
"""Derive a committed snapshot of the OpenTelemetry GenAI semantic conventions.

The trace generator in [`examples/agent-telemetry-trace-generator/`](../examples/agent-telemetry-trace-generator/)
emitted valid OTLP envelopes carrying a private vocabulary — `ai.tokens.prompt`,
`ai.model.name`, `agent.persona` — so every backend could parse it and none could chart it.
Conformance to the wire format is not conformance to the convention, and the difference is
invisible in the payload.

This module closes that by taking the convention from where it is defined rather than from
memory. It fetches the upstream model files at a pinned commit, resolves their attribute
groups, and writes one JSON snapshot the sample application reads with nothing but the
standard library. That split is deliberate: parsing upstream YAML needs a dependency and
`examples/` is copy-pasteable, so the derivation lives here in `tools/` and only its result
ships beside the exhibit.

**Pin the source and record where it came from.** A hand-written convention table drifts
silently and looks authoritative while it does, which is worse than an openly private
vocabulary. The snapshot carries its repository, its commit and the files it was built
from, so the next reader can tell what it is a snapshot *of* — and `refresh` re-derives it
rather than anyone editing it by hand.

One fact this exercise turned up, which no cached knowledge would have: the GenAI
conventions no longer live in `open-telemetry/semantic-conventions`. That repository's
`docs/gen-ai/gen-ai-spans.md` is now a redirect, and the definitions moved to
`open-telemetry/semantic-conventions-genai`, which as of this snapshot has no tagged
release — so the pin is a commit, and it says so.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Final

import yaml

DEFAULT_SNAPSHOT: Final[Path] = Path("examples/agent-telemetry-trace-generator/semconv_genai.json")

RAW = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"

# Pinned deliberately. An unpinned `main` makes every refresh a different snapshot and
# every diff unreviewable, which is how a convention table becomes folklore with a URL.
SOURCES: Final[tuple[dict[str, str], ...]] = (
    {
        "role": "registry",
        "repo": "open-telemetry/semantic-conventions-genai",
        "commit": "8ffdf568e1b4391a99adb081db16e8102e36918e",
        "path": "model/gen-ai/registry.yaml",
    },
    {
        "role": "spans",
        "repo": "open-telemetry/semantic-conventions-genai",
        "commit": "8ffdf568e1b4391a99adb081db16e8102e36918e",
        "path": "model/gen-ai/spans.yaml",
    },
    {
        "role": "deprecated",
        "repo": "open-telemetry/semantic-conventions",
        "commit": "v1.44.0",
        "path": "model/gen-ai/deprecated/registry-deprecated.yaml",
    },
)

# What became of a deprecated attribute is stated in prose rather than in a field, so it is
# extracted — and every entry must land in exactly one of three outcomes. `reason:` does not
# discriminate: 58 of the 60 deprecations carry `reason: uncategorized`, covering both the
# ones that were renamed and the ones that merely moved repository under the same key.
#
#   renamed  "Replaced by `gen_ai.usage.input_tokens`, which has moved to ..."
#   moved    "Moved to the [OpenTelemetry GenAI semantic conventions repository]..."
#   removed  "Removed, no replacement at this time."
#
# An entry matching none of them raises. Collapsing `moved` into `renamed` would tell a
# reader to rewrite a key that did not change, and dropping the unmatched ones would make
# the table claim a completeness it does not have.
# Any attribute key, never only `gen_ai.*`. The first version of this pattern required
# that prefix and choked on three OpenAI attributes whose replacements are
# `openai.request.service_tier` and friends — a partial pattern over a domain that had
# no reason to be partial, in the module written to stop guessing at the convention.
_REPLACED_BY: Final[re.Pattern[str]] = re.compile(r"Replaced by `([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)`")
_MOVED: Final[re.Pattern[str]] = re.compile(r"^Moved to\b", re.IGNORECASE)
_REMOVED: Final[re.Pattern[str]] = re.compile(r"^Removed\b", re.IGNORECASE)
_NAME_TEMPLATE: Final[re.Pattern[str]] = re.compile(r"`([^`]*\{[^`]*)`")


class SnapshotError(RuntimeError):
    """Raised when upstream cannot be read or does not say what this module needs."""


def fetch(source: dict[str, str], timeout: float = 30.0) -> str:
    """Retrieve one pinned upstream file."""
    url = RAW.format(**source)
    # Fixed https URL built from a committed table; no caller-supplied component reaches it.
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def attribute_table(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reduce the registry to `{key: {type, values, brief}}`.

    An enum becomes its member values, because that is the only part a validator can act
    on and carrying the rest would make the snapshot a copy rather than a derivation.
    """
    table: dict[str, dict[str, Any]] = {}
    for attribute in registry.get("attributes", []):
        declared = attribute.get("type")
        entry: dict[str, Any] = {"brief": str(attribute.get("brief", "")).strip()}
        if isinstance(declared, dict):
            entry["type"] = "enum"
            entry["values"] = sorted(str(member["value"]) for member in declared.get("members", []))
        else:
            entry["type"] = str(declared)
        table[str(attribute["key"])] = entry
    return dict(sorted(table.items()))


DEFAULT_REQUIREMENT: Final[str] = "recommended"


def _requirement(level: Any) -> str | None:
    """Normalise a requirement level, returning None when the entry does not state one.

    None rather than the default, because the two are different facts and conflating them
    downgrades the convention. A span may re-reference an attribute purely to mark it
    `sampling_relevant`, stating no level — and `gen_ai.inference.client` does exactly that
    for `gen_ai.operation.name`, which its attribute group declares **required**. Reading
    the silent re-reference as `recommended` overwrote that, and a validator built on the
    result would have accepted a span missing the one attribute the convention mandates.
    """
    if isinstance(level, dict):
        return str(next(iter(level), DEFAULT_REQUIREMENT))
    return str(level) if level else None


def _group_attributes(spans: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Index the reusable attribute groups a span definition refers to by `ref_group`."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for group in spans.get("attribute_groups", []):
        groups[str(group["id"])] = [
            (str(item["ref"]), _requirement(item.get("requirement_level")) or DEFAULT_REQUIREMENT)
            for item in group.get("attributes", [])
            if "ref" in item
        ]
    return groups


def _span_attributes(span: dict[str, Any], groups: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """Resolve one span's attributes, expanding every `ref_group` it names."""
    resolved: dict[str, str] = {}
    for item in span.get("attributes", []):
        if "ref_group" in item:
            referenced = str(item["ref_group"])
            if referenced not in groups:
                raise SnapshotError(f"{span['type']}: unknown ref_group {referenced!r}")
            resolved.update(dict(groups[referenced]))
            continue
        if "ref" not in item:
            continue
        name = str(item["ref"])
        stated = _requirement(item.get("requirement_level"))
        # Only an explicit level overrides one already resolved from a group. A silent
        # re-reference states nothing, and treating nothing as `recommended` is what
        # downgraded a required attribute here.
        if stated is not None or name not in resolved:
            resolved[name] = stated or DEFAULT_REQUIREMENT
    return dict(sorted(resolved.items()))


def span_table(spans: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reduce the span definitions to kind, name template and per-attribute requirement."""
    groups = _group_attributes(spans)
    table: dict[str, dict[str, Any]] = {}
    for span in spans.get("spans", []):
        note = str((span.get("name") or {}).get("note", ""))
        templates = _NAME_TEMPLATE.findall(note)
        table[str(span["type"])] = {
            "kind": str(span.get("kind", "internal")),
            "name_template": templates[0] if templates else None,
            "attributes": _span_attributes(span, groups),
        }
    return dict(sorted(table.items()))


def deprecation_table(deprecated: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Record what became of every deprecated attribute, and refuse to guess.

    This is the half of a convention that costs the most to miss. A dashboard querying
    `gen_ai.usage.prompt_tokens` after the rename to `gen_ai.usage.input_tokens` returns
    zero rows, and zero rows renders as *no traffic* rather than *wrong key* — the outage
    is in the reading, not in the pipeline, so nothing alerts.
    """
    table: dict[str, dict[str, str | None]] = {}
    unresolved: list[str] = []
    for group in deprecated.get("groups", []):
        for attribute in group.get("attributes", []):
            key = attribute.get("id")
            if not key or "deprecated" not in attribute:
                continue
            outcome = _outcome(f"{attribute['deprecated'].get('note', '')}".strip())
            if outcome is None:
                unresolved.append(str(key))
            else:
                table[str(key)] = outcome
    if unresolved:
        raise SnapshotError(f"unreadable deprecation note for {sorted(unresolved)}")
    return dict(sorted(table.items()))


def _outcome(note: str) -> dict[str, str | None] | None:
    """Classify one deprecation note, or return None so the caller can refuse."""
    replaced = _REPLACED_BY.search(note)
    if replaced:
        return {"status": "renamed", "replacement": replaced.group(1)}
    if _MOVED.match(note):
        return {"status": "moved", "replacement": None}
    if _REMOVED.match(note):
        return {"status": "removed", "replacement": None}
    return None


def build(documents: dict[str, dict[str, Any]], sources: tuple[dict[str, str], ...]) -> dict[str, Any]:
    """Assemble the snapshot from the three upstream documents."""
    attributes = attribute_table(documents["registry"])
    spans = span_table(documents["spans"])
    return {
        "source": {
            "fetched": dt.date.today().isoformat(),
            "files": [
                {"repo": s["repo"], "commit": s["commit"], "path": s["path"]} for s in sources
            ],
        },
        "attributes": attributes,
        "spans": spans,
        "external_attributes": _external(attributes, spans),
        "deprecated": deprecation_table(documents["deprecated"]),
    }


def _external(attributes: dict[str, Any], spans: dict[str, Any]) -> list[str]:
    """Name every span attribute the GenAI registry does not define.

    `error.type`, `server.address` and `server.port` are general OpenTelemetry attributes
    that GenAI spans reuse, so they are absent from this registry and present on the spans.
    Listing them is the point: the alternative is a validator that filters them out by
    prefix, which is indistinguishable from a validator that has never heard of them.
    """
    referenced = {name for span in spans.values() for name in span["attributes"]}
    return sorted(referenced - set(attributes))


def _handle_refresh(args: argparse.Namespace) -> int:
    """Fetch, derive and write the snapshot."""
    documents = {source["role"]: yaml.safe_load(fetch(source)) for source in SOURCES}
    snapshot = build(documents, SOURCES)
    args.snapshot.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    renamed = sum(1 for e in snapshot["deprecated"].values() if e["status"] == "renamed")
    print(f"{args.snapshot}: {len(snapshot['attributes'])} attribute(s), "
          f"{len(snapshot['spans'])} span type(s), {len(snapshot['deprecated'])} deprecation(s) "
          f"of which {renamed} renamed")
    return 0


def _handle_verify(args: argparse.Namespace) -> int:
    """Check the committed snapshot without a network, which is what CI can do.

    Refreshing needs upstream and gating must not, so this asserts the shape and the
    internal agreement instead: every attribute a span requires must be one the registry
    defines, or the validator built on it would report a rule it cannot evaluate.
    """
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    known = set(snapshot["attributes"]) | set(snapshot["external_attributes"])
    dangling = sorted({
        name
        for span in snapshot["spans"].values()
        for name in span["attributes"]
        if name not in known
    })
    if dangling:
        print(f"❌ span attributes neither defined nor declared external: {dangling}", file=sys.stderr)
        return 1
    print(f"{args.snapshot}: {len(known)} attribute(s), {len(snapshot['spans'])} span type(s), "
          f"{len(snapshot['deprecated'])} deprecation(s), 0 dangling")
    return 0


_HANDLERS: Final[dict[str, Any]] = {"refresh": _handle_refresh, "verify": _handle_verify}


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the snapshot tool."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(_HANDLERS))
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the semantic convention snapshot."""
    args = build_arg_parser().parse_args(argv)
    try:
        return int(_HANDLERS[args.command](args))
    except (SnapshotError, OSError) as err:
        print(f"❌ {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
