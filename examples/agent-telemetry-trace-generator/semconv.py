#!/usr/bin/env python3
"""Check a span against the OpenTelemetry GenAI semantic conventions.

The generator beside this file used to emit valid OTLP envelopes carrying a private
vocabulary — `ai.tokens.prompt`, `ai.model.name`, `agent.persona`. Every collector accepted
it and no backend could chart it, because **conformance to the wire format is not
conformance to the convention** and nothing in the payload distinguishes them. A span with
the wrong key is not rejected anywhere; it simply never appears in the dashboard that was
built to read it.

The convention is not restated here. It is derived from the upstream model at a pinned
commit by [`tools/semconv_snapshot.py`](../../tools/semconv_snapshot.py) and read from
`semconv_genai.json`, so this module needs nothing but the standard library and the table
cannot drift from its source without a visible diff.

The rule worth carrying away is about **renames**. `gen_ai.usage.prompt_tokens` became
`gen_ai.usage.input_tokens`; a dashboard still querying the old key returns zero rows, and
zero rows renders as *no traffic* rather than *wrong key*. The outage is in the reading,
not in the pipeline, so nothing alerts and nothing is red. Eight such renames are recorded
in the snapshot, and emitting any of them is an error here rather than a note.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SNAPSHOT_PATH: Final[Path] = Path(__file__).resolve().parent / "semconv_genai.json"

ERROR: Final[str] = "error"
WARNING: Final[str] = "warning"

# How a declared type is satisfied. `any` accepts anything by definition, and `template[x]`
# describes a family of keys rather than one, so neither is checked structurally.
_SCALARS: Final[dict[str, tuple[type, ...]]] = {
    "string": (str,),
    "int": (int,),
    "double": (int, float),
    "boolean": (bool,),
    "enum": (str,),
}
_UNCHECKED: Final[frozenset[str]] = frozenset({"any", "template[string]"})


@dataclass(frozen=True)
class Finding:
    """One disagreement between a span and the convention."""

    level: str
    code: str
    message: str


@dataclass(frozen=True)
class Convention:
    """The snapshot, loaded once and queried per span."""

    attributes: dict[str, dict[str, Any]]
    spans: dict[str, dict[str, Any]]
    external: frozenset[str]
    deprecated: dict[str, dict[str, Any]]
    source: dict[str, Any]

    @classmethod
    def load(cls, path: Path = SNAPSHOT_PATH) -> Convention:
        """Read the derived snapshot."""
        document = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            attributes=document["attributes"],
            spans=document["spans"],
            external=frozenset(document["external_attributes"]),
            deprecated=document["deprecated"],
            source=document["source"],
        )

    def required(self, span_type: str) -> list[str]:
        """Return the attributes this span type must carry."""
        declared = self.spans[span_type]["attributes"]
        return sorted(name for name, level in declared.items() if level == "required")

    def span_name(self, span_type: str, attributes: dict[str, Any]) -> str | None:
        """Render the conventional span name, or None when the type declares no template."""
        template = self.spans[span_type].get("name_template")
        if not template:
            return None
        rendered = template
        for key, value in attributes.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return None if "{" in rendered else rendered


def validate(
    convention: Convention,
    span_type: str,
    name: str,
    kind: str,
    attributes: dict[str, Any],
) -> list[Finding]:
    """Report every disagreement between one span and the convention, in one pass."""
    if span_type not in convention.spans:
        return [Finding(ERROR, "unknown_span_type",
                        f"{span_type!r} is not a declared span type; known: {sorted(convention.spans)}")]
    findings = _kind_findings(convention, span_type, kind)
    findings += _missing_findings(convention, span_type, attributes)
    findings += _name_findings(convention, span_type, name, attributes)
    for key, value in sorted(attributes.items()):
        findings += _attribute_findings(convention, key, value)
    return findings


def _kind_findings(convention: Convention, span_type: str, kind: str) -> list[Finding]:
    """A client call recorded as internal loses its place in every service graph."""
    expected = str(convention.spans[span_type]["kind"])
    if kind.lower() == expected:
        return []
    return [Finding(ERROR, "wrong_kind", f"{span_type} declares kind {expected!r}, span has {kind!r}")]


def _missing_findings(convention: Convention, span_type: str, attributes: dict[str, Any]) -> list[Finding]:
    """Only `required` is checked.

    `conditionally_required` states a condition in prose that nothing here can evaluate, and
    reporting it as missing would produce a finding the author cannot act on — which is how
    a validator trains its reader to ignore it.
    """
    return [
        Finding(ERROR, "missing_required", f"{span_type} requires {name!r}")
        for name in convention.required(span_type)
        if name not in attributes
    ]


def _name_findings(
    convention: Convention, span_type: str, name: str, attributes: dict[str, Any]
) -> list[Finding]:
    """The span name is a SHOULD, so a mismatch is a warning and an absence of one is not."""
    expected = convention.span_name(span_type, attributes)
    if expected is None or name == expected:
        return []
    return [Finding(WARNING, "span_name", f"span name should be {expected!r}, is {name!r}")]


def _attribute_findings(convention: Convention, key: str, value: Any) -> list[Finding]:
    """Judge one attribute: deprecated, unknown, mistyped, or an undeclared enum value.

    The live registry is consulted first, and it wins. The deprecation table is built from
    the repository the GenAI conventions *left*, where every one of them is marked
    deprecated with the reason "moved" — so an attribute that is current, correct and
    defined in the new registry appears in that table too. Reading the table first reported
    every conformant attribute this generator emits as deprecated: thirty errors, all of
    them wrong, on a span that was right. A record that an attribute's *definition* moved
    is not a statement that the *attribute* is obsolete.
    """
    if key in convention.attributes:
        return _type_findings(convention.attributes[key], key, value)
    entry = convention.deprecated.get(key)
    if entry and entry["status"] != "moved":
        return [_deprecation_finding(convention, key)]
    return _unknown_findings(convention, key)


def _deprecation_finding(convention: Convention, key: str) -> Finding:
    """Name the replacement, because the cost of a rename is paid by whoever reads the data."""
    entry = convention.deprecated[key]
    replacement = entry.get("replacement")
    if entry["status"] == "renamed":
        return Finding(ERROR, "renamed_attribute",
                       f"{key!r} was renamed to {replacement!r}; a query on the old key returns "
                       "zero rows, which reads as no traffic")
    return Finding(ERROR, "deprecated_attribute", f"{key!r} is deprecated ({entry['status']})")


def _unknown_findings(convention: Convention, key: str) -> list[Finding]:
    """A `gen_ai.` key the registry does not define is a typo, not an extension.

    Attributes outside the namespace are the caller's own and are left alone: the convention
    permits them, and this repository's agent metadata is exactly that. What it does not
    permit is a private key *standing in for* a conventional one.
    """
    if key in convention.external or not key.startswith("gen_ai."):
        return []
    return [Finding(ERROR, "unknown_attribute",
                    f"{key!r} is not in the GenAI registry; a misspelled convention key is "
                    "indistinguishable from an absent one")]


def _type_findings(spec: dict[str, Any], key: str, value: Any) -> list[Finding]:
    """Check the declared type, and the declared values where the attribute is an enum."""
    declared = str(spec["type"])
    if declared in _UNCHECKED:
        return []
    if declared.endswith("[]"):
        return _sequence_findings(declared, key, value)
    findings = _scalar_findings(declared, key, value)
    if declared == "enum" and isinstance(value, str) and value not in spec.get("values", []):
        # A warning, not an error: OpenTelemetry enums are open unless stated otherwise, so
        # a provider-specific value is allowed and a typo looks exactly like one.
        findings.append(Finding(WARNING, "undeclared_enum_value",
                                f"{key}={value!r} is not a well-known value for {key}"))
    return findings


def _scalar_findings(declared: str, key: str, value: Any) -> list[Finding]:
    """Refuse a boolean where a number is declared: `isinstance(True, int)` is true."""
    accepted = _SCALARS.get(declared)
    if accepted is None:
        return [Finding(WARNING, "unchecked_type", f"{key}: no structural check for type {declared!r}")]
    if isinstance(value, bool) and declared in ("int", "double"):
        return [Finding(ERROR, "wrong_type", f"{key} is {declared}, got a boolean")]
    if isinstance(value, accepted):
        return []
    return [Finding(ERROR, "wrong_type", f"{key} is {declared}, got {type(value).__name__}")]


def _sequence_findings(declared: str, key: str, value: Any) -> list[Finding]:
    """A scalar where an array is declared reaches the backend as a different column."""
    element = declared[:-2]
    if not isinstance(value, list):
        return [Finding(ERROR, "wrong_type", f"{key} is {declared}, got {type(value).__name__}")]
    bad = [item for item in value if not isinstance(item, _SCALARS.get(element, (object,)))]
    if bad:
        return [Finding(ERROR, "wrong_type", f"{key} is {declared}, contains {type(bad[0]).__name__}")]
    return []
