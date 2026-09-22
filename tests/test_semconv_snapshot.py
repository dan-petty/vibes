"""Tests for deriving the GenAI semantic convention snapshot.

Two defects in this derivation were silent and would have travelled straight into the
validator built on it: a requirement level downgraded from `required` to `recommended`, and
a replacement pattern narrow enough to choke on three attributes. Both are pinned here on
fixtures, because both were found by reading output rather than by anything failing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from semconv_snapshot import (
    DEFAULT_SNAPSHOT,
    SOURCES,
    SnapshotError,
    _external,
    _outcome,
    _requirement,
    _span_attributes,
    attribute_table,
    deprecation_table,
    main,
    span_table,
)

SNAPSHOT = json.loads((REPO_ROOT / DEFAULT_SNAPSHOT).read_text(encoding="utf-8"))


# --- Requirement levels ------------------------------------------------------------------


def test_an_entry_that_states_no_level_is_distinguishable_from_one_that_does() -> None:
    """None and the default are different facts, and conflating them downgraded a convention."""
    assert (_requirement(None), _requirement("required"),
            _requirement({"conditionally_required": "If available."})) == (
        None, "required", "conditionally_required")


def test_a_silent_re_reference_does_not_downgrade_a_group_requirement() -> None:
    """The regression, on the exact shape upstream uses.

    A span re-references an attribute purely to mark it `sampling_relevant`, stating no
    requirement level, while its attribute group declares the attribute **required**.
    Reading the silent re-reference as `recommended` overwrote that, and the validator
    built on the result would accept a span missing a mandated attribute.
    """
    document = {
        "attribute_groups": [{
            "id": "g",
            "attributes": [{"ref": "gen_ai.operation.name", "requirement_level": "required"}],
        }],
        "spans": [{
            "type": "s",
            "attributes": [
                {"ref_group": "g"},
                {"ref": "gen_ai.operation.name", "sampling_relevant": True},
            ],
        }],
    }
    assert span_table(document)["s"]["attributes"]["gen_ai.operation.name"] == "required"


def test_an_explicit_level_still_overrides_a_group() -> None:
    """The override has to keep working, or the fix above would freeze every group level."""
    groups = {"g": [("a", "recommended")]}
    span = {"type": "s", "attributes": [
        {"ref_group": "g"}, {"ref": "a", "requirement_level": "required"}]}
    assert _span_attributes(span, groups) == {"a": "required"}


def test_an_unknown_group_reference_is_refused() -> None:
    """Expanding to nothing would produce a span declaring fewer attributes than it has."""
    span = {"type": "s", "attributes": [{"ref_group": "missing"}]}
    with pytest.raises(SnapshotError, match="unknown ref_group"):
        _span_attributes(span, {})


# --- Deprecations ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("Replaced by `gen_ai.usage.input_tokens`, which has moved to the repo.",
         {"status": "renamed", "replacement": "gen_ai.usage.input_tokens"}),
        ("Replaced by `openai.request.service_tier`, which has moved.",
         {"status": "renamed", "replacement": "openai.request.service_tier"}),
        ("Moved to the OpenTelemetry GenAI semantic conventions repository.",
         {"status": "moved", "replacement": None}),
        ("Removed, no replacement at this time.",
         {"status": "removed", "replacement": None}),
        ("Something nobody anticipated.", None),
    ],
)
def test_every_deprecation_note_lands_in_exactly_one_outcome(
    note: str, expected: dict[str, str] | None
) -> None:
    """`reason:` does not discriminate: 58 of 60 deprecations carry `uncategorized`.

    The second case is the one that caught a partial pattern. The first version required
    the replacement to start with `gen_ai.` and raised on three OpenAI attributes whose
    replacements do not — a narrow pattern over a domain with no reason to be narrow, in
    the module written to stop guessing at the convention.
    """
    assert _outcome(note) == expected


def test_a_note_nothing_can_classify_refuses_rather_than_dropping_the_entry() -> None:
    """A table that silently loses entries claims a completeness it does not have."""
    document = {"groups": [{"attributes": [
        {"id": "gen_ai.x", "deprecated": {"note": "inscrutable"}}]}]}
    with pytest.raises(SnapshotError, match="unreadable deprecation note"):
        deprecation_table(document)


# --- The table ---------------------------------------------------------------------------


def test_an_enum_is_reduced_to_its_declared_values() -> None:
    """Only the values are actionable; carrying the rest would make this a copy."""
    registry = {"attributes": [{"key": "a", "brief": "b", "type": {"members": [
        {"value": "two"}, {"value": "one"}]}}]}
    assert attribute_table(registry) == {"a": {"brief": "b", "type": "enum",
                                              "values": ["one", "two"]}}


def test_attributes_the_registry_does_not_define_are_named_rather_than_filtered() -> None:
    """A validator that filters by prefix is indistinguishable from one that never heard of them."""
    spans = {"s": {"attributes": {"gen_ai.a": "required", "server.address": "recommended"}}}
    assert _external({"gen_ai.a": {}}, spans) == ["server.address"]


# --- The committed snapshot ---------------------------------------------------------------


def test_the_committed_snapshot_records_where_it_came_from() -> None:
    """A convention table with no provenance drifts silently and looks authoritative doing it."""
    files = SNAPSHOT["source"]["files"]
    assert ([f["repo"] for f in files], all(f["commit"] for f in files)) == (
        [s["repo"] for s in SOURCES], True)


def test_verify_passes_on_the_committed_snapshot_without_a_network() -> None:
    """Refreshing needs upstream and gating must not, so this is what CI can run."""
    assert main(["verify", "--snapshot", str(REPO_ROOT / DEFAULT_SNAPSHOT)]) == 0


def test_verify_fails_when_a_span_names_an_attribute_nothing_defines(tmp_path: Path) -> None:
    """The check has to be able to fail, or passing says nothing."""
    broken = dict(SNAPSHOT)
    broken["spans"] = {"s": {"kind": "client", "name_template": None,
                             "attributes": {"gen_ai.invented": "required"}}}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert main(["verify", "--snapshot", str(path)]) == 1
