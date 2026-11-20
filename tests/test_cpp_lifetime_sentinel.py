"""Unit and integration test suite for C++ RAII & Lifetime Invariant Sentinel.

Validates detection of manual memory deallocations (CPP001), dangling view lifetimes (CPP002),
Rule of Five incompleteness (CPP003), unmanaged raw pointers (CPP004), and use-after-move hazards (CPP005).
All assertions use structural tuple equality checks to maintain proactive complexity headroom.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from cpp_lifetime_sentinel import (
    CppAuditSummary,
    CppFinding,
    CppLifetimeSentinel,
    CppSeverity,
    SentinelPreset,
    export_json,
    export_sarif,
    format_markdown_report,
    main,
)


def test_clean_modern_cpp_has_zero_findings() -> None:
    """Verify clean modern C++ with smart pointers and RAII yields zero findings."""
    clean_code = """
    #include <memory>
    #include <string>
    #include <vector>

    class SafeBuffer {
    public:
        SafeBuffer() = default;
        ~SafeBuffer() = default;
        SafeBuffer(const SafeBuffer&) = default;
        SafeBuffer& operator=(const SafeBuffer&) = default;
        SafeBuffer(SafeBuffer&&) noexcept = default;
        SafeBuffer& operator=(SafeBuffer&&) noexcept = default;

        void append(std::string data) {
            items_.push_back(std::move(data));
        }

    private:
        std::vector<std::string> items_;
        std::unique_ptr<int> count_ = std::make_unique<int>(0);
    };
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(clean_code, "safe_buffer.hpp")
    assert (len(summary.findings), summary.is_compliant) == (0, True)


def test_cpp001_manual_memory_management() -> None:
    """Verify delete, delete[], and free calls trigger CPP001."""
    violating_code = """
    void cleanup(int* ptr, char* arr, void* raw) {
        delete ptr;
        delete[] arr;
        free(raw);
    }
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(violating_code, "cleanup.cpp")
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        3,
        ["CPP001", "CPP001", "CPP001"],
        [CppSeverity.HIGH, CppSeverity.HIGH, CppSeverity.HIGH],
    )


def _rule_lines(source: str, rule_id: str) -> list[int]:
    """Return the line numbers of the findings of one rule in a source string."""
    findings = CppLifetimeSentinel(SentinelPreset.STRICT).audit_source(source, "rule_of_five.hpp")
    return [f.line_number for f in findings if f.rule_id == rule_id]


def _cpp001_lines(source: str) -> list[int]:
    """Return the line numbers of the CPP001 findings in a source string."""
    return _rule_lines(source, "CPP001")


def test_cpp001_ignores_deleted_functions() -> None:
    """Verify '= delete' declarations give no CPP001, and delete expressions still do."""
    source = (
        "class Handle {\n"
        "public:\n"
        "    ~Handle() { release(); }\n"
        "    Handle(const Handle&) = delete;\n"
        "    Handle& operator=(const Handle&) = delete;\n"
        "    Handle(Handle&&) noexcept = default;\n"
        "    Handle& operator=(Handle&&) noexcept = default;\n"
        "};\n"
        "void drop(int* p) { delete p; }\n"
        "struct A { A(const A&) = delete; }; void f(int* p) { delete p; }\n"
        "void drop_all(int* q) { delete[] q; }\n"
    )
    assert _cpp001_lines(source) == [9, 10, 11]


def test_cpp001_ignores_deleted_functions_split_across_lines() -> None:
    """Verify a '= delete' that a line break splits gives no CPP001, and delete expressions still do."""
    source = (
        "class Handle {\n"
        "public:\n"
        "    Handle(const Handle&)\n"
        "        = delete;\n"
        "    Handle& operator=(const Handle&) =\n"
        "        delete;\n"
        "    Handle(Handle&&) = // moved by swap only\n"
        "        delete;\n"
        "};\n"
        "void drop(int* p) { delete p; }\n"
        "int last = 0;\n"
        "void drop_split(int* p) {\n"
        "    delete\n"
        "        p;\n"
        "}\n"
    )
    assert _cpp001_lines(source) == [10, 13]


def test_cpp001_ignores_comments_and_string_literals() -> None:
    """Verify 'delete' and 'free(' in comments and string literals give no CPP001."""
    source = (
        "// delete p; in a line comment\n"
        "/* free(p) in a block comment */\n"
        'const char* verb = "delete this";\n'
        'const char* hint = "call free(x) here";\n'
        "char quote = '\"'; int* r = nullptr; delete r;\n"
    )
    assert _cpp001_lines(source) == [5]


def test_cpp002_dangling_view_return() -> None:
    """Verify string_view or span returning local temporary triggers CPP002."""
    violating_code = """
    #include <string_view>
    #include <string>

    std::string_view get_greeting() {
        std::string local_str = "hello world";
        return local_str;
    }
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(violating_code, "view_test.cpp")
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["CPP002"],
        [CppSeverity.CRITICAL],
    )


def test_cpp003_rule_of_five_incompleteness() -> None:
    """Verify class with custom destructor lacking copy/move operations triggers CPP003."""
    violating_code = """
    class DatabaseConnection {
    public:
        DatabaseConnection(const char* conn_str);
        ~DatabaseConnection() {
            close_socket();
        }
        void query();
    };
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(violating_code, "db_conn.hpp")
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["CPP003"],
        [CppSeverity.MEDIUM],
    )


def test_cpp004_unmanaged_raw_pointer() -> None:
    """Verify raw pointer new expression triggers CPP004."""
    violating_code = """
    void allocate() {
        int* counter = new int(100);
    }
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(violating_code, "alloc.cpp")
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["CPP004"],
        [CppSeverity.MEDIUM],
    )


def test_cpp004_ignores_dereference_expressions() -> None:
    """Verify unary dereferences give no CPP004, and raw pointer declarations still do."""
    source = (
        "struct Node {\n"
        "    Node& operator=(const Node& other) {\n"
        "        value_ = other.value_;\n"
        "        return *this;\n"
        "    }\n"
        "    int read(int* p) {\n"
        "        return *p;\n"
        "    }\n"
        "    void fail(Error* e) {\n"
        "        throw *e;\n"
        "    }\n"
        "    Node* parent_;\n"
        "    int value_;\n"
        "};\n"
        "void make() {\n"
        "    T* p = new T;\n"
        "}\n"
    )
    assert _rule_lines(source, "CPP004") == [12, 16]


def test_compliant_rule_of_five_class_has_zero_findings() -> None:
    """Verify the compliant Rule of Five class of observation 04 yields zero findings."""
    source = """
    class ConnectionSession {
    public:
        explicit ConnectionSession(int fd) : fd_(fd) {}
        ~ConnectionSession() { if (fd_ >= 0) close(fd_); }

        ConnectionSession(const ConnectionSession&) = delete;
        ConnectionSession& operator=(const ConnectionSession&) = delete;
        ConnectionSession(ConnectionSession&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
        ConnectionSession& operator=(ConnectionSession&& other) noexcept {
            if (this != &other) {
                if (fd_ >= 0) close(fd_);
                fd_ = std::exchange(other.fd_, -1);
            }
            return *this;
        }
    private:
        int fd_;
    };
    """
    summary = CppLifetimeSentinel(SentinelPreset.STRICT).audit_string(source, "connection_session.hpp")
    assert (summary.findings, summary.is_compliant) == ((), True)


def test_cpp005_use_after_move_hazard() -> None:
    """Verify accessing a variable after std::move triggers CPP005."""
    violating_code = """
    void process_payload(std::string payload) {
        sink(std::move(payload));
        payload.clear();
    }
    """
    sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    summary = sentinel.audit_string(violating_code, "payload.cpp")
    rule_ids = [f.rule_id for f in summary.findings]
    severities = [f.severity for f in summary.findings]
    assert (len(summary.findings), rule_ids, severities) == (
        1,
        ["CPP005"],
        [CppSeverity.HIGH],
    )


def test_relaxed_preset_filters_raw_pointer() -> None:
    """Verify relaxed preset skips unmanaged raw pointer rules (CPP004)."""
    code = """
    void allocate() {
        int* counter = new int(100);
    }
    """
    strict_sentinel = CppLifetimeSentinel(SentinelPreset.STRICT)
    strict_summary = strict_sentinel.audit_string(code, "alloc.cpp")

    relaxed_sentinel = CppLifetimeSentinel(SentinelPreset.RELAXED)
    relaxed_summary = relaxed_sentinel.audit_string(code, "alloc.cpp")

    assert (len(strict_summary.findings), len(relaxed_summary.findings)) == (1, 0)


def test_sarif_export_structure() -> None:
    """Verify SARIF export matches OASIS SARIF 2.1.0 schema structure."""
    finding = CppFinding(
        rule_id="CPP001",
        severity=CppSeverity.CRITICAL,
        message="Manual deletion detected",
        location="main.cpp:12",
        line_number=12,
        recommendation="Use std::unique_ptr",
        finding_hash="test_hash_123",
    )
    summary = CppAuditSummary(
        total_files=1,
        total_lines=20,
        total_violations=1,
        is_compliant=False,
        findings=(finding,),
    )
    sarif_data = export_sarif(summary, "main.cpp")
    run_driver = sarif_data["runs"][0]["tool"]["driver"]
    results = sarif_data["runs"][0]["results"]
    assert (
        sarif_data["version"],
        run_driver["name"],
        len(results),
        results[0]["ruleId"],
        results[0]["level"],
    ) == (
        "2.1.0",
        "vibes-cpp-lifetime-sentinel",
        1,
        "CPP001",
        "error",
    )


def test_json_and_markdown_reports() -> None:
    """Verify JSON and Markdown report formatting."""
    summary = CppAuditSummary(
        total_files=2,
        total_lines=50,
        total_violations=0,
        is_compliant=True,
        findings=(),
    )
    json_out = export_json(summary)
    data = json.loads(json_out)
    md_out = format_markdown_report(summary)
    assert (
        data["total_files"],
        data["is_compliant"],
        "Zero lifetime or RAII hazards detected" in md_out,
    ) == (2, True, True)


def test_cli_execution_with_temp_files(tmp_path: Path) -> None:
    """Verify CLI audit end-to-end with temporary C++ files and exports."""
    src = tmp_path / "sample.cpp"
    src.write_text(
        "void bad(int* p) {\n    delete p;\n}\n",
        encoding="utf-8",
    )
    sarif_file = tmp_path / "out.sarif"
    json_file = tmp_path / "out.json"
    md_file = tmp_path / "out.md"

    exit_code = main(
        [
            str(src),
            "--preset",
            "strict",
            "--export-sarif",
            str(sarif_file),
            "--export-json",
            str(json_file),
            "--export-md",
            str(md_file),
        ]
    )

    assert (
        exit_code,
        sarif_file.is_file(),
        json_file.is_file(),
        md_file.is_file(),
    ) == (1, True, True, True)


def test_cli_no_cpp_files(tmp_path: Path) -> None:
    """Verify CLI behavior when no C++ source files match."""
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    exit_code = main([str(empty_dir)])
    assert exit_code == 0
