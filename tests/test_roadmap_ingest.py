"""Unit tests for roadmap ingestion into the SDLC backlog."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from roadmap_ingest import (
    EFFORT_POINTS,
    VALUE_POINTS,
    main,
    merge_into_backlog,
    parse_roadmap,
    parse_value_effort_matrix,
)

_MATRIX = """
| Priority Category | Feature / Deliverable | Tech | Value | Effort | Target | Status |
|---|---|---|---|---|---|---|
| **Quick Wins** | Widget Synthesizer | Python | High | Low | v0.5.0 | Scheduled |
|  | Sprawling Platform | Python | Low | High | v1.0.0 | Scheduled |
"""


def _roadmap(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ROADMAP.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_open_items_are_ingested_and_completed_ones_are_not(tmp_path: Path) -> None:
    """A checked-off deliverable is finished work, not a backlog item."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n"
                   "- [x] **Already Shipped**:\n- [ ] **Still Open**:\n")
    assert [i.title for i in parse_roadmap(doc)] == ["Still Open"]


def test_rejected_sections_are_never_scheduled(tmp_path: Path) -> None:
    """Anti-pattern rows record a decision not to build; scheduling them inverts it."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Real Work**:\n"
                   "## Anti-Patterns\n- [ ] **Toy Demo We Rejected**:\n")
    assert [i.title for i in parse_roadmap(doc)] == ["Real Work"]


def test_trailing_annotation_does_not_drop_the_item(tmp_path: Path) -> None:
    """"(partially delivered)" after the title is common and must not silently skip it."""
    doc = _roadmap(tmp_path, "### Milestone A (v1.0.0 - North Star)\n"
                   "- [ ] **Change Management Protocol** (partially delivered):\n")
    items = parse_roadmap(doc)
    assert [i.title for i in items] == ["Change Management Protocol"]
    assert items[0].note == "(partially delivered)"


def test_priority_suffix_is_honoured_when_present(tmp_path: Path) -> None:
    """A roadmap that states priority must not be overridden by the default."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n"
                   "- [ ] **Urgent Thing (P0 - Critical)**:\n- [ ] **Unmarked Thing**:\n")
    assert [i.priority for i in parse_roadmap(doc)] == ["P0_CRITICAL", "P2_MEDIUM"]


def test_value_and_effort_come_from_the_matrix(tmp_path: Path) -> None:
    """The matrix is where the judgement was already recorded; re-deriving it is guesswork."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n"
                   "- [ ] **Widget Synthesizer**:\n- [ ] **Sprawling Platform**:\n" + _MATRIX)
    items = {i.title: i for i in parse_roadmap(doc)}
    assert (items["Widget Synthesizer"].business_value, items["Widget Synthesizer"].effort_points) == (
        VALUE_POINTS["High"], EFFORT_POINTS["Low"]
    )
    assert (items["Sprawling Platform"].business_value, items["Sprawling Platform"].effort_points) == (
        VALUE_POINTS["Low"], EFFORT_POINTS["High"]
    )
    assert all(i.sized_from_matrix for i in items.values())


def test_unmatched_items_are_marked_as_assumption_ranked(tmp_path: Path) -> None:
    """A defaulted size is an unmeasured input and must not read like a recorded one."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Unlisted Feature**:\n" + _MATRIX)
    item = parse_roadmap(doc)[0]
    assert item.sized_from_matrix is False
    assert "assumption" in item.to_backlog_task(1)["prescriptive_guidance"]


def test_milestone_is_carried_from_the_section_heading(tmp_path: Path) -> None:
    """An item's milestone is the section it sits in, not a guess from its text."""
    doc = _roadmap(tmp_path, "### Near Term (v0.5.0 - Scheduled)\n- [ ] **Soon**:\n"
                   "### North Star (v1.0.0 - Vision)\n- [ ] **Later**:\n")
    assert {i.title: i.milestone for i in parse_roadmap(doc)} == {"Soon": "v0.5.0", "Later": "v1.0.0"}


def test_context_bullet_becomes_prescriptive_guidance(tmp_path: Path) -> None:
    """The rationale already written under the item is what an implementer needs."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Thing**:\n"
                   "  - *Context & Rationale*: Because the loop cannot see features.\n")
    assert "Because the loop cannot see features" in parse_roadmap(doc)[0].to_backlog_task(1)["prescriptive_guidance"]


def test_merge_preserves_defects_and_replaces_prior_roadmap_entries(tmp_path: Path) -> None:
    """Defects and features share one backlog; a checked-off item must not linger in it."""
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps([
        {"resource_id": "fb-1", "title": "[STRUCTURAL_DECAY] real defect"},
        {"resource_id": "roadmap-2000", "title": "[ROADMAP] stale, since completed"},
    ]), encoding="utf-8")
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Current Work**:\n")

    tasks = merge_into_backlog(parse_roadmap(doc), backlog)
    titles = [t["title"] for t in tasks]
    assert "[STRUCTURAL_DECAY] real defect" in titles
    assert "[ROADMAP] stale, since completed" not in titles
    assert "[ROADMAP] Current Work" in titles


def test_merge_survives_a_corrupt_backlog(tmp_path: Path) -> None:
    """The backlog is a derived artifact; a damaged one must not stop ingestion."""
    backlog = tmp_path / "backlog.json"
    backlog.write_text("{not json", encoding="utf-8")
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Work**:\n")
    assert len(merge_into_backlog(parse_roadmap(doc), backlog)) == 1


def test_matrix_parser_ignores_the_header_and_separator_rows() -> None:
    """Only rows carrying a Value and Effort cell describe a deliverable."""
    assert set(parse_value_effort_matrix(_MATRIX.splitlines())) == {
        "widget synthesizer", "sprawling platform"
    }


def test_main_reports_a_missing_roadmap_instead_of_succeeding(tmp_path: Path) -> None:
    """A path that does not exist must not be reported as an empty roadmap."""
    assert main(["ingest", "--roadmap", str(tmp_path / "absent.md")]) == 1


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Dry run is for inspection; it must leave the backlog untouched."""
    doc = _roadmap(tmp_path, "### Milestone A (v0.5.0 - Scheduled)\n- [ ] **Work**:\n")
    backlog = tmp_path / "backlog.json"
    assert main(["ingest", "--roadmap", str(doc), "--backlog", str(backlog), "--dry-run"]) == 0
    assert not backlog.exists()
    assert "nothing written" in capsys.readouterr().out


def test_blocked_annotation_is_parsed_with_its_reason(tmp_path: Path) -> None:
    """A recorded blocker carries its reason out of the roadmap and into the backlog."""
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "### Milestone (v0.3.0)\n\n"
        "- [ ] **Blocked Thing (P0 - Critical)** (blocked: needs live review threads):\n"
        "- [ ] **Open Thing (P1 - High)**:\n",
        encoding="utf-8",
    )
    items = {item.title: item for item in parse_roadmap(roadmap)}
    assert (
        sorted(items),
        items["Blocked Thing"].blocked_reason,
        items["Open Thing"].blocked_reason,
    ) == (["Blocked Thing", "Open Thing"], "needs live review threads", "")


def test_a_blocked_annotation_does_not_drop_the_item(tmp_path: Path) -> None:
    """The colon inside the annotation must not stop the item's line from matching.

    The previous grammar ended the note at the first colon, so annotating an item was
    enough to delete it from the backlog entirely — silently losing exactly the items
    someone had taken the trouble to explain.
    """
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "### Milestone (v0.3.0)\n\n"
        "- [ ] **Alpha** (blocked: waiting on an upstream release):\n"
        "- [ ] **Beta** (partially delivered):\n"
        "- [ ] **Gamma (P2 - Medium)**:\n",
        encoding="utf-8",
    )
    assert sorted(item.title for item in parse_roadmap(roadmap)) == ["Alpha", "Beta", "Gamma"]


def test_blocked_items_are_labelled_and_explained_in_the_backlog(tmp_path: Path) -> None:
    """The backlog task records the blocker so the prioritizer can act on it."""
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "### Milestone (v0.3.0)\n\n- [ ] **Thing** (blocked: no upstream API yet):\n",
        encoding="utf-8",
    )
    task = parse_roadmap(roadmap)[0].to_backlog_task(2001)
    assert (
        task["blocked_reason"],
        "blocked" in task["labels"],
        str(task["prescriptive_guidance"]).startswith("Blocked: no upstream API yet."),
    ) == ("no upstream API yet", True, True)
