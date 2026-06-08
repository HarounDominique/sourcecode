"""Phase 4 tests — spring-audit CLI, MCP get_spring_audit, RIS update_ris_spring_audit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import _set_detected_path, _preprocess_args, app
from sourcecode.ris import (
    update_ris_spring_audit,
    load_ris,
    RepositoryIntelligenceSnapshot,
    RIS_SCHEMA_VERSION,
    _now_iso,
)

_runner = CliRunner()


def invoke(args: list[str]):
    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_CONTROLLER = """\
package com.example;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class GreetingController {
    @GetMapping("/hello")
    public String hello() { return "hi"; }
}
"""

_TX_PRIVATE_JAVA = """\
package com.example;

import org.springframework.transaction.annotation.Transactional;
import org.springframework.stereotype.Service;

@Service
public class BadService {
    @Transactional
    private void doStuff() {}
}
"""


@pytest.fixture
def java_repo(tmp_path: Path) -> Path:
    """Minimal Java repo with a controller and a bad @Transactional."""
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "GreetingController.java").write_text(_MINIMAL_CONTROLLER)
    (src / "BadService.java").write_text(_TX_PRIVATE_JAVA)
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>0.0.1</version></project>"
    )
    return tmp_path


@pytest.fixture
def non_java_repo(tmp_path: Path) -> Path:
    """Python-only repo — no Java files."""
    (tmp_path / "main.py").write_text("print('hello')")
    return tmp_path


@pytest.fixture
def _sample_audit_result() -> dict:
    return {
        "schema_version": "1.0",
        "repo_id": "abc123",
        "git_head": "",
        "generated_at": "2026-06-02T00:00:00+00:00",
        "spring_detected": True,
        "scope": "all",
        "summary": {
            "total_findings": 3,
            "by_severity": {"critical": 0, "high": 1, "medium": 2, "low": 0},
            "by_category": {"tx": 2, "security": 1},
            "confidence_level": "medium",
        },
        "findings": [],
        "limitations": [],
        "metadata": {"endpoints_analyzed": 5},
    }


# ---------------------------------------------------------------------------
# TestSpringAuditCLI — CLI command tests
# ---------------------------------------------------------------------------

class TestSpringAuditCLI:

    def test_help_available(self):
        result = invoke(["spring-audit", "--help"])
        assert result.exit_code == 0
        assert "spring-audit" in result.output.lower() or "Spring" in result.output

    def test_non_java_repo_returns_spring_detected_false(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["spring_detected"] is False
        assert data["summary"]["total_findings"] == 0

    def test_non_java_has_limitations(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo)])
        data = json.loads(result.output)
        assert any("No Java" in lim for lim in data["limitations"])

    def test_invalid_path_exits_1(self, tmp_path: Path):
        result = invoke(["spring-audit", str(tmp_path / "does_not_exist")])
        assert result.exit_code == 1

    def test_invalid_scope_exits_1(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--scope", "bogus"])
        assert result.exit_code == 1

    def test_invalid_min_severity_exits_1(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--min-severity", "extreme"])
        assert result.exit_code == 1

    def test_java_repo_returns_schema_version(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"

    def test_java_repo_spring_detected(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo)])
        data = json.loads(result.output)
        assert data["spring_detected"] is True

    def test_scope_all_default(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo)])
        data = json.loads(result.output)
        assert data["scope"] == "all"

    def test_scope_tx_only(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--scope", "tx"])
        data = json.loads(result.output)
        assert data["scope"] == "tx"
        pattern_ids = {f["pattern_id"] for f in data["findings"]}
        assert all(pid.startswith("TX-") for pid in pattern_ids)

    def test_scope_security_only(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--scope", "security"])
        data = json.loads(result.output)
        assert data["scope"] == "security"
        pattern_ids = {f["pattern_id"] for f in data["findings"]}
        assert all(pid.startswith("SEC-") for pid in pattern_ids)

    def test_tx001_detected_on_private_transactional(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--scope", "tx"])
        data = json.loads(result.output)
        ids = {f["pattern_id"] for f in data["findings"]}
        assert "TX-001" in ids

    def test_summary_present(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo)])
        data = json.loads(result.output)
        assert "total_findings" in data["summary"]
        assert "by_severity" in data["summary"]
        assert "by_category" in data["summary"]

    def test_output_file(self, java_repo: Path, tmp_path: Path):
        out = tmp_path / "audit.json"
        result = invoke(["spring-audit", str(java_repo), "--output", str(out)])
        assert result.exit_code == 0
        assert out.exists()
        data = json.loads(out.read_text())
        assert "findings" in data

    def test_min_severity_high_filters_medium(self, java_repo: Path):
        result_all = invoke(["spring-audit", str(java_repo)])
        result_high = invoke(["spring-audit", str(java_repo), "--min-severity", "high"])
        data_all = json.loads(result_all.output)
        data_high = json.loads(result_high.output)
        # high filter should not include medium/low findings
        for f in data_high["findings"]:
            assert f["severity"] in ("critical", "high")
        # high filter should have ≤ findings than all
        assert data_high["summary"]["total_findings"] <= data_all["summary"]["total_findings"]

    def test_format_yaml(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo), "--format", "yaml"])
        assert result.exit_code == 0
        assert "spring_detected" in result.output

    def test_deterministic_output(self, java_repo: Path):
        r1 = invoke(["spring-audit", str(java_repo)])
        r2 = invoke(["spring-audit", str(java_repo)])
        d1 = json.loads(r1.output)
        d2 = json.loads(r2.output)
        # generated_at timestamp differs — compare everything else
        d1.pop("generated_at", None)
        d2.pop("generated_at", None)
        # analysis_time_ms in metadata varies — strip it
        for d in (d1, d2):
            d.get("metadata", {}).pop("analysis_time_ms", None)
        assert d1 == d2

    def test_findings_have_required_fields(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo)])
        data = json.loads(result.output)
        for f in data["findings"]:
            assert "id" in f
            assert "pattern_id" in f
            assert "severity" in f
            assert "title" in f
            assert "symbol" in f
            assert "explanation" in f
            assert "fix_hint" in f


# ---------------------------------------------------------------------------
# TestUpdateRisSpringAudit
# ---------------------------------------------------------------------------

class TestUpdateRisSpringAudit:

    @pytest.fixture(autouse=True)
    def _isolate_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / ".cache"))

    def _make_ris(self, tmp_path: Path) -> RepositoryIntelligenceSnapshot:
        from sourcecode.ris import save_ris
        ris = RepositoryIntelligenceSnapshot(
            repo_id="test-repo",
            created_at=_now_iso(),
            last_updated_at=_now_iso(),
            git_head="abc123",
            version=RIS_SCHEMA_VERSION,
            structural_map={},
            api_surface={},
            dependency_graph={},
            compact_summary={},
            agent_index={},
            git_context_snapshot={},
            metadata={"snapshot_source": "test", "confidence": 1.0, "partial": False},
        )
        save_ris(tmp_path, ris)
        return ris

    def test_updates_metadata_spring_audit(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        assert ris is not None
        sa = ris.metadata.get("spring_audit")
        assert sa is not None
        assert sa["total_findings"] == 3

    def test_preserves_existing_metadata(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        assert ris.metadata.get("snapshot_source") == "test"

    def test_stores_summary_by_severity(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        sa = ris.metadata["spring_audit"]
        assert sa["by_severity"]["high"] == 1
        assert sa["by_severity"]["medium"] == 2

    def test_stores_scope(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        assert ris.metadata["spring_audit"]["scope"] == "all"

    def test_stores_spring_detected(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        assert ris.metadata["spring_audit"]["spring_detected"] is True

    def test_creates_ris_stub_when_none_exists(self, tmp_path: Path, _sample_audit_result: dict):
        assert load_ris(tmp_path) is None
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        ris = load_ris(tmp_path)
        assert ris is not None
        assert ris.metadata.get("spring_audit") is not None

    def test_never_raises_on_invalid_input(self, tmp_path: Path):
        update_ris_spring_audit(tmp_path, None)   # type: ignore[arg-type]
        update_ris_spring_audit(tmp_path, "bad")   # type: ignore[arg-type]
        update_ris_spring_audit(tmp_path, 42)      # type: ignore[arg-type]

    def test_overwrites_previous_spring_audit(self, tmp_path: Path, _sample_audit_result: dict):
        self._make_ris(tmp_path)
        update_ris_spring_audit(tmp_path, _sample_audit_result)
        updated = dict(_sample_audit_result)
        updated["summary"] = {**_sample_audit_result["summary"], "total_findings": 99}
        update_ris_spring_audit(tmp_path, updated)
        ris = load_ris(tmp_path)
        assert ris.metadata["spring_audit"]["total_findings"] == 99


# ---------------------------------------------------------------------------
# TestGetSpringAuditMCP — validation layer (no subprocess)
# ---------------------------------------------------------------------------

def _mcp_success(result) -> bool:
    """Extract success=True from MCP result (dict or CallToolResult)."""
    if isinstance(result, dict):
        return result.get("success") is True
    # CallToolResult — parse JSON payload
    import json as _json
    try:
        payload = _json.loads(result.content[0].text)
        return payload.get("success") is True
    except Exception:
        return False


def _mcp_is_error(result) -> bool:
    """Return True when the MCP result signals failure."""
    if isinstance(result, dict):
        return result.get("success") is False
    # CallToolResult
    return getattr(result, "isError", False)


class TestGetSpringAuditMCP:

    def test_invalid_repo_path_type(self):
        from sourcecode.mcp.server import get_spring_audit
        result = get_spring_audit(repo_path=123)   # type: ignore[arg-type]
        assert _mcp_is_error(result)

    def test_invalid_scope(self, tmp_path: Path):
        from sourcecode.mcp.server import get_spring_audit
        result = get_spring_audit(repo_path=str(tmp_path), scope="bad_scope")
        assert _mcp_is_error(result)

    def test_nonexistent_path(self, tmp_path: Path):
        from sourcecode.mcp.server import get_spring_audit
        result = get_spring_audit(repo_path=str(tmp_path / "no_such_dir"))
        assert _mcp_is_error(result)

    def test_valid_scopes_accepted(self, non_java_repo: Path):
        from sourcecode.mcp.server import get_spring_audit
        for scope in ("all", "tx", "security"):
            result = get_spring_audit(repo_path=str(non_java_repo), scope=scope)
            assert _mcp_success(result), f"scope={scope!r} should succeed"


# ---------------------------------------------------------------------------
# TestSpringAuditCIFlag — --ci exit code + --format github-comment
# ---------------------------------------------------------------------------

class TestSpringAuditCIFlag:

    def test_ci_exits_1_when_findings(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--ci"])
        assert result.exit_code == 1

    def test_ci_exits_0_when_no_findings(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo), "--ci"])
        assert result.exit_code == 0

    def test_ci_with_min_severity_high_exits_0_when_only_medium(self, tmp_path: Path):
        # A repo that only triggers medium/low findings should exit 0 under --min-severity high
        src = tmp_path / "src"
        src.mkdir()
        # Create a file that triggers no high/critical findings
        (src / "Clean.java").write_text(
            "package com.example;\npublic class Clean {}\n"
        )
        result = invoke(["spring-audit", str(tmp_path), "--ci", "--min-severity", "high"])
        assert result.exit_code == 0

    def test_ci_output_still_emitted_before_exit(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--ci"])
        # Output should still be valid JSON even when exit_code == 1
        data = json.loads(result.output)
        assert "findings" in data

    def test_ci_with_output_file_writes_and_exits_1(self, java_repo: Path, tmp_path: Path):
        out = tmp_path / "audit.json"
        result = invoke(["spring-audit", str(java_repo), "--ci", "--output", str(out)])
        assert result.exit_code == 1
        assert out.exists()

    def test_format_github_comment_exits_0_no_ci(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo), "--format", "github-comment"])
        assert result.exit_code == 0

    def test_format_github_comment_contains_header(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo), "--format", "github-comment"])
        assert "Spring Audit" in result.output

    def test_format_github_comment_no_findings_shows_checkmark(self, non_java_repo: Path):
        result = invoke(["spring-audit", str(non_java_repo), "--format", "github-comment"])
        assert "✅" in result.output

    def test_format_github_comment_with_findings_shows_table(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--format", "github-comment"])
        assert result.exit_code == 0
        assert "| Sev |" in result.output
        assert "TX-001" in result.output

    def test_format_github_comment_with_ci_exits_1_on_findings(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--ci", "--format", "github-comment"])
        assert result.exit_code == 1
        assert "Spring Audit" in result.output

    def test_format_github_comment_details_section(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--format", "github-comment"])
        assert "<details>" in result.output
        assert "Finding details" in result.output

    def test_invalid_format_exits_1(self, java_repo: Path):
        result = invoke(["spring-audit", str(java_repo), "--format", "xml"])
        assert result.exit_code == 1

    def test_github_comment_output_file(self, java_repo: Path, tmp_path: Path):
        out = tmp_path / "comment.md"
        result = invoke([
            "spring-audit", str(java_repo),
            "--format", "github-comment",
            "--output", str(out),
        ])
        assert result.exit_code == 0
        content = out.read_text()
        assert "Spring Audit" in content


# ---------------------------------------------------------------------------
# TestGetMigrationReadinessMCP — validation layer (no subprocess)
# ---------------------------------------------------------------------------

class TestGetMigrationReadinessMCP:

    def test_invalid_repo_path_type(self):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=123)  # type: ignore[arg-type]
        assert _mcp_is_error(result)

    def test_invalid_min_severity(self, tmp_path: Path):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=str(tmp_path), min_severity="extreme")
        assert _mcp_is_error(result)

    def test_nonexistent_path(self, tmp_path: Path):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=str(tmp_path / "no_such_dir"))
        assert _mcp_is_error(result)

    def test_valid_on_non_java_repo(self, non_java_repo: Path):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=str(non_java_repo))
        assert _mcp_success(result)

    def test_valid_severities_accepted(self, non_java_repo: Path):
        from sourcecode.mcp.server import get_migration_readiness
        for sev in ("critical", "high", "medium", "low"):
            result = get_migration_readiness(repo_path=str(non_java_repo), min_severity=sev)
            assert _mcp_success(result), f"min_severity={sev!r} should succeed"

    def test_returns_readiness_score(self, non_java_repo: Path):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=str(non_java_repo))
        assert _mcp_success(result)
        data = result["data"] if isinstance(result, dict) else json.loads(result.content[0].text)["data"]
        assert "readiness_score" in data
        assert data["readiness_score"] == 100

    def test_returns_schema_version(self, non_java_repo: Path):
        from sourcecode.mcp.server import get_migration_readiness
        result = get_migration_readiness(repo_path=str(non_java_repo))
        data = result["data"] if isinstance(result, dict) else json.loads(result.content[0].text)["data"]
        assert data.get("schema_version") == "1.2"

    def test_java_repo_with_javax_findings(self, tmp_path: Path):
        from sourcecode.mcp.server import get_migration_readiness
        src = tmp_path / "src"
        src.mkdir()
        (src / "OldEntity.java").write_text(
            "package com.example;\nimport javax.persistence.Entity;\n@Entity public class OldEntity {}\n"
        )
        result = get_migration_readiness(repo_path=str(tmp_path))
        assert _mcp_success(result)
        data = result["data"] if isinstance(result, dict) else json.loads(result.content[0].text)["data"]
        assert data["readiness_score"] < 100
        assert data["blocking_count"] > 0
        rule_ids = {f["rule_id"] for f in data["findings"]}
        assert "MIG-001" in rule_ids
