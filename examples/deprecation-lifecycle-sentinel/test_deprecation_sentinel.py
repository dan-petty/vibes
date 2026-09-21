"""Unit tests for the Deprecation Lifecycle Sentinel."""

import warnings
from pathlib import Path

import pytest
from deprecation_sentinel import (
    DeprecationDefect,
    audit_paths,
    audit_record,
    collect_deprecations,
    deprecated,
    main,
    parse_semver,
)


def _module(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_semver_orders_versions() -> None:
    """Version comparison must be numeric, not lexical: 1.10.0 follows 1.9.0."""
    assert parse_semver("v1.9.0") < parse_semver("1.10.0") < parse_semver("2.0.0")


def test_deprecated_decorator_announces_contract_at_call_time() -> None:
    """The runtime warning carries the same contract the sentinel enforces statically."""

    @deprecated(since="1.2.0", remove_in="2.0.0", replacement="new_api()")
    def legacy() -> int:
        return 42

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = legacy()

    message = str(caught[0].message)
    assert (result, caught[0].category) == (42, DeprecationWarning)
    assert all(token in message for token in ("1.2.0", "2.0.0", "new_api()"))


def test_deprecated_decorator_rejects_incomplete_contract() -> None:
    """A deprecation cannot be declared without a removal version and replacement."""
    with pytest.raises(ValueError, match="remove_in"):
        deprecated(since="1.2.0", remove_in="", replacement="new_api()")


def test_missing_metadata_is_flagged(tmp_path: Path) -> None:
    """A deprecation without a deadline cannot be planned around."""
    module = _module(tmp_path, "api.py", '@deprecated(since="1.2.0")\ndef vague(): ...\n')
    record = collect_deprecations(module)[0]

    defects = {v.defect for v in audit_record(record, "1.3.0")}
    assert DeprecationDefect.MISSING_METADATA in defects


def test_overdue_removal_is_flagged(tmp_path: Path) -> None:
    """Reaching the declared removal version makes deletion mandatory, not optional."""
    module = _module(
        tmp_path, "api.py",
        '@deprecated(since="1.0.0", remove_in="2.0.0", replacement="fresh")\ndef stale(): ...\n',
    )
    record = collect_deprecations(module)[0]

    assert [v.defect for v in audit_record(record, "2.0.0")] == [DeprecationDefect.OVERDUE_REMOVAL]
    assert audit_record(record, "1.9.0") == []


def test_removal_inside_same_major_is_flagged(tmp_path: Path) -> None:
    """SemVer promises callers that no minor release removes what they depend on."""
    module = _module(
        tmp_path, "api.py",
        '@deprecated(since="1.2.0", remove_in="1.5.0", replacement="fresh")\ndef early(): ...\n',
    )
    record = collect_deprecations(module)[0]

    assert DeprecationDefect.NON_MAJOR_REMOVAL in {v.defect for v in audit_record(record, "1.3.0")}


def test_pre_1_0_removal_inside_same_major_is_permitted(tmp_path: Path) -> None:
    """Before 1.0 the SemVer contract does not yet bind; deletion stays free."""
    module = _module(
        tmp_path, "api.py",
        '@deprecated(since="0.4.0", remove_in="0.5.0", replacement="fresh")\ndef early(): ...\n',
    )
    record = collect_deprecations(module)[0]

    assert audit_record(record, "0.4.1") == []


def test_silent_deprecation_is_flagged(tmp_path: Path) -> None:
    """A bare marker that never reaches the runtime is discovered at deletion."""
    module = _module(tmp_path, "api.py", "@deprecated\ndef silent(): ...\n")
    record = collect_deprecations(module)[0]

    assert DeprecationDefect.SILENT_DEPRECATION in {v.defect for v in audit_record(record, "1.0.0")}


def test_unmigrated_internal_call_site_is_flagged(tmp_path: Path) -> None:
    """Owners migrate before asking downstream callers to."""
    _module(
        tmp_path, "api.py",
        '@deprecated(since="1.0.0", remove_in="2.0.0", replacement="fresh")\ndef legacy(): ...\n',
    )
    _module(tmp_path, "caller.py", "def run():\n    return legacy()\n")

    report = audit_paths([tmp_path], current_version="1.4.0")
    assert [v.defect for v in report.violations] == [DeprecationDefect.UNMIGRATED_CALL_SITE]
    assert report.violations[0].file_path.endswith("caller.py")


def test_declaring_module_is_not_its_own_unmigrated_caller(tmp_path: Path) -> None:
    """A deprecated function delegating to its replacement is the shim working as intended."""
    _module(
        tmp_path, "api.py",
        '@deprecated(since="1.0.0", remove_in="2.0.0", replacement="fresh")\n'
        "def legacy():\n    return legacy_impl()\n",
    )

    assert audit_paths([tmp_path], current_version="1.4.0").is_clean


def test_inventory_orders_by_removal_deadline(tmp_path: Path) -> None:
    """The inventory answers 'what must go next', so deadline order is the only useful one."""
    _module(
        tmp_path, "api.py",
        '@deprecated(since="1.0.0", remove_in="4.0.0", replacement="a")\ndef later(): ...\n'
        '@deprecated(since="1.0.0", remove_in="2.0.0", replacement="b")\ndef sooner(): ...\n',
    )

    report = audit_paths([tmp_path], current_version="1.5.0")
    assert [r.symbol for r in report.due_before("9.0.0")] == ["sooner", "later"]
    assert [r.symbol for r in report.due_before("2.0.0")] == ["sooner"]


def test_missing_target_fails_instead_of_certifying_nothing(tmp_path: Path) -> None:
    """A typo'd path must fail rather than report a clean audit of zero files."""
    report = audit_paths([tmp_path / "absent"], current_version="1.0.0")
    assert not report.is_clean


def test_main_audits_every_supplied_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Pre-commit passes N filenames; a violation in any of them must fail the run."""
    clean = _module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    dirty = _module(tmp_path, "dirty.py", '@deprecated(since="1.0.0")\ndef vague(): ...\n')

    exit_code = main(["sentinel", str(clean), str(dirty), "--current-version", "1.4.0"])
    assert (exit_code, "MissingMetadata" in capsys.readouterr().out) == (1, True)
