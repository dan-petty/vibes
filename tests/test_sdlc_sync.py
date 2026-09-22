"""Unit tests for backlog reconciliation and lifecycle advancement."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from sdlc_project_manager import (
    LifecycleState,
    SDLCProjectManager,
    advance_ready_item,
    load_resources_from_dicts,
    main,
    reconcile_backlog,
)


def _card(number: int, title: str, **overrides: object) -> dict:
    card = {
        "resource_id": f"fb-{number}",
        "kind": "issue",
        "number": number,
        "title": title,
        "labels": ["enhancement"],
        "lifecycle_state": "Backlog",
        "priority": "P2_MEDIUM",
        "effort_points": 2,
        "business_value": 5,
    }
    return {**card, **overrides}


def test_a_finding_that_stops_being_reported_closes_its_card() -> None:
    """A defect card exists because a scan reported it; silence means it was fixed."""
    existing = [_card(1, "[DECAY] fixed since"), _card(2, "[DECAY] still present")]
    merged, result = reconcile_backlog(existing, [_card(2, "[DECAY] still present")])

    states = {c["title"]: c["lifecycle_state"] for c in merged}
    assert states["[DECAY] fixed since"] == LifecycleState.DONE.value
    assert states["[DECAY] still present"] == LifecycleState.BACKLOG.value
    assert result.closed == ["[DECAY] fixed since"]


def test_a_new_finding_opens_a_card() -> None:
    """A scan reporting something the board has never seen must add it."""
    merged, result = reconcile_backlog([], [_card(1, "[DECAY] brand new")])
    assert result.opened == ["[DECAY] brand new"]
    assert len(merged) == 1


def test_roadmap_cards_are_never_closed_by_a_scan() -> None:
    """No scan can observe an unbuilt feature, so its absence means nothing."""
    roadmap = _card(2001, "[ROADMAP] Build the thing", resource_id="roadmap-2001")
    merged, result = reconcile_backlog([roadmap], [])

    assert result.closed == []
    assert merged[0]["lifecycle_state"] == LifecycleState.BACKLOG.value


def test_cards_are_matched_by_title_not_number() -> None:
    """Export order assigns numbers, so keying on them would churn every card each pass."""
    existing = [_card(1, "[DECAY] same finding")]
    merged, result = reconcile_backlog(existing, [_card(97, "[DECAY] same finding")])

    assert (result.opened, result.closed, result.unchanged) == ([], [], 1)
    assert len(merged) == 1


def test_already_closed_cards_are_not_reclosed() -> None:
    """Reconciliation is idempotent; a second pass over the same state changes nothing."""
    done = _card(1, "[DECAY] long fixed", lifecycle_state=LifecycleState.DONE.value)
    merged, result = reconcile_backlog([done], [])
    assert (result.closed, result.opened) == ([], [])
    assert merged == []


def test_reconciliation_is_stable_across_repeated_passes() -> None:
    """Running the daemon twice over unchanged input must not oscillate."""
    current = [_card(1, "[DECAY] persistent")]
    first, _ = reconcile_backlog([], current)
    second, result = reconcile_backlog(first, current)
    assert (result.opened, result.closed) == ([], [])
    assert first == second


def test_exactly_one_card_is_promoted_per_pass() -> None:
    """A board where everything is Ready states no order, which states no priority."""
    cards = [
        _card(1, "low value", business_value=2, effort_points=8),
        _card(2, "high value", business_value=8, effort_points=2),
        _card(3, "middling", business_value=5, effort_points=5),
    ]
    manager = SDLCProjectManager(load_resources_from_dicts(cards))
    promoted, target = advance_ready_item(cards, manager)

    assert target == "high value"
    ready = [c["title"] for c in promoted if c["lifecycle_state"] == LifecycleState.READY.value]
    assert ready == ["high value"]


def test_promotion_is_a_no_op_on_an_empty_board() -> None:
    """Nothing to promote must not raise."""
    manager = SDLCProjectManager([])
    promoted, target = advance_ready_item([], manager)
    assert (promoted, target) == ([], None)


def test_sync_cli_persists_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """One pass writes the reconciled board and says what it changed."""
    board = tmp_path / "board.json"
    scan = tmp_path / "scan.json"
    board.write_text(json.dumps([_card(1, "[DECAY] fixed since")]), encoding="utf-8")
    scan.write_text(json.dumps([_card(2, "[DECAY] newly found")]), encoding="utf-8")

    assert main(["sync", "--file", str(board), "--scan", str(scan)]) == 0
    out = capsys.readouterr().out
    assert "closed 1" in out and "opened 1" in out

    written = {c["title"]: c["lifecycle_state"] for c in json.loads(board.read_text(encoding="utf-8"))}
    assert written["[DECAY] fixed since"] == LifecycleState.DONE.value


def test_sync_watch_stops_after_max_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A bounded watch must terminate rather than run until interrupted."""
    board = tmp_path / "board.json"
    board.write_text(json.dumps([_card(1, "[DECAY] steady")]), encoding="utf-8")

    assert main(["sync", "--file", str(board), "--watch", "--interval", "0", "--max-passes", "3"]) == 0
    assert capsys.readouterr().out.count("[sync ") == 3
