"""Block 2 coverage-honesty tests.

Validates three areas that previously gave false coverage signals:
1. Spring Boot env-map: ${VAR:default}, profile detection, coverage summary
2. Architecture: evidence structure, tentative flag, filesystem-only honesty
3. Java in semantics/docs: explicit language_coverage_details, unsupported notices
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(files: dict[str, str]) -> tuple[Path, dict]:
    """Create a temp directory with given files and return (root, file_tree)."""
    tmp = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Build a minimal file_tree dict (path → None for files)
    file_tree: dict = {}
    for rel in files:
        parts = rel.split("/")
        node = file_tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None
    return tmp, file_tree


# ===========================================================================
# 1. env_analyzer — Spring Boot improvements
# ===========================================================================

class TestSpringEnvMap:
    """${VAR:default} parsing, profile detection, coverage summary."""

    def _analyze(self, files: dict[str, str]):
        from sourcecode.env_analyzer import EnvAnalyzer
        root, tree = _make_tree(files)
        records, summary = EnvAnalyzer().analyze(root, tree)
        return records, summary

    def test_spring_env_var_no_default(self):
        """${DB_HOST} without default → required=True, default=None."""
        records, summary = self._analyze({
            "application.yml": "spring:\n  datasource:\n    url: jdbc:${DB_HOST}/mydb\n"
        })
        keys = {r.key: r for r in records}
        assert "DB_HOST" in keys
        r = keys["DB_HOST"]
        assert r.required is True
        assert r.default is None

    def test_spring_env_var_with_default(self):
        """${DB_HOST:localhost} → required=False, default='localhost'."""
        records, summary = self._analyze({
            "application.yml": "spring:\n  datasource:\n    url: jdbc:${DB_HOST:localhost}/mydb\n"
        })
        keys = {r.key: r for r in records}
        assert "DB_HOST" in keys
        r = keys["DB_HOST"]
        assert r.required is False
        assert r.default == "localhost"

    def test_spring_env_var_empty_default(self):
        """${TOKEN:} (empty default after colon) → required=False, default=None (empty string not stored)."""
        records, summary = self._analyze({
            "application.yml": "security:\n  token: ${TOKEN:}\n"
        })
        keys = {r.key: r for r in records}
        assert "TOKEN" in keys
        assert keys["TOKEN"].required is False

    def test_spring_multiple_vars_same_file(self):
        """Multiple ${VAR} refs in one file all captured."""
        records, summary = self._analyze({
            "application.yml": (
                "db:\n  url: ${DB_URL}\n"
                "  user: ${DB_USER:admin}\n"
                "  pass: ${DB_PASS}\n"
            )
        })
        keys = {r.key for r in records}
        assert {"DB_URL", "DB_USER", "DB_PASS"}.issubset(keys)

    def test_spring_profile_default(self):
        """application.yml → profile='default'."""
        records, summary = self._analyze({
            "application.yml": "server:\n  port: ${SERVER_PORT:8080}\n"
        })
        assert "default" in summary.profiles_scanned
        keys = {r.key: r for r in records}
        assert keys["SERVER_PORT"].profile == "default"

    def test_spring_profile_named(self):
        """application-prod.yml → profile='prod'."""
        records, summary = self._analyze({
            "application-prod.yml": "db:\n  url: ${PROD_DB_URL}\n"
        })
        assert "prod" in summary.profiles_scanned
        keys = {r.key: r for r in records}
        assert keys["PROD_DB_URL"].profile == "prod"

    def test_spring_multiple_profiles(self):
        """application.yml + application-dev.yml + application-prod.yml → 3 profiles scanned."""
        records, summary = self._analyze({
            "application.yml":      "db:\n  url: ${DB_URL:default}\n",
            "application-dev.yml":  "db:\n  url: ${DB_URL:devhost}\n",
            "application-prod.yml": "db:\n  url: ${DB_URL}\n",
        })
        assert len(summary.profiles_scanned) == 3
        assert "default" in summary.profiles_scanned
        assert "dev" in summary.profiles_scanned
        assert "prod" in summary.profiles_scanned

    def test_spring_candidates_count(self):
        """summary.spring_candidates = total ${...} refs found across Spring files."""
        records, summary = self._analyze({
            "application.yml": (
                "db:\n  url: ${DB_URL}\n"
                "  user: ${DB_USER}\n"
            ),
            "application-dev.yml": "db:\n  url: ${DB_URL:devhost}\n",
        })
        # 3 refs total across the two files
        assert summary.spring_candidates == 3

    def test_spring_properties_profile(self):
        """application-m3.properties → profile='m3'."""
        records, summary = self._analyze({
            "application-m3.properties": "datasource.url=${DATASOURCE_URL}\n"
        })
        assert "m3" in summary.profiles_scanned

    def test_spring_dotted_property_ref_captured(self):
        """${spring.datasource.url} lowercase dotted refs are also captured."""
        records, summary = self._analyze({
            "application.yml": "custom:\n  url: ${spring.datasource.url:fallback}\n"
        })
        keys = {r.key for r in records}
        assert "spring.datasource.url" in keys

    def test_spring_default_with_url_value(self):
        """${REDIS_URL:redis://localhost:6379} — default contains colons."""
        records, summary = self._analyze({
            "application.yml": "redis:\n  url: ${REDIS_URL:redis://localhost:6379}\n"
        })
        keys = {r.key: r for r in records}
        assert "REDIS_URL" in keys
        r = keys["REDIS_URL"]
        assert r.required is False
        # Default is everything after the first colon up to the closing }
        assert r.default is not None and "redis" in r.default

    def test_non_spring_source_code_unaffected(self):
        """Java source ${...} code-pattern still captured via java_spring_value."""
        records, summary = self._analyze({
            "src/MyService.java": (
                '@Value("${DB_HOST}")\n'
                'private String dbHost;\n'
            )
        })
        keys = {r.key for r in records}
        assert "DB_HOST" in keys


# ===========================================================================
# 2. architecture_analyzer — evidence & tentative
# ===========================================================================

class TestArchitectureEvidence:
    """Architecture analysis must include structured evidence and tentative flag."""

    def _analyze(self, paths: list[str], graph=None):
        from sourcecode.architecture_analyzer import ArchitectureAnalyzer
        from sourcecode.schema import SourceMap
        sm = SourceMap(file_paths=paths)
        return ArchitectureAnalyzer().analyze(Path("."), sm, graph)

    def test_evidence_field_always_present(self):
        """ArchitectureAnalysis always has an evidence list."""
        result = self._analyze(["src/controller/user.py", "src/service/user.py", "src/model/user.py"])
        assert hasattr(result, "evidence")
        assert isinstance(result.evidence, list)

    def test_tentative_field_always_present(self):
        """ArchitectureAnalysis always has a tentative bool."""
        result = self._analyze(["src/controller/user.py", "src/service/user.py"])
        assert hasattr(result, "tentative")
        assert isinstance(result.tentative, bool)

    def test_filesystem_inference_has_low_confidence_evidence(self):
        """Filesystem-only inference → evidence.type == 'filesystem_naming', confidence in ('low','medium')."""
        result = self._analyze([
            "src/controllers/user.py",
            "src/services/user.py",
            "src/repositories/user.py",
        ])
        assert result.method == "filesystem_inference"
        assert len(result.evidence) >= 1
        ev = result.evidence[0]
        assert ev["type"] in ("filesystem_naming", "workspace_config")
        assert ev["confidence"] in ("low", "medium")
        assert "reason" in ev

    def test_workspace_config_produces_high_confidence_evidence(self):
        """Workspace config file → evidence.type == 'workspace_config', confidence == 'high'."""
        result = self._analyze([
            "turbo.json",
            "packages/api/src/index.ts",
            "packages/web/src/index.ts",
            "packages/core/src/index.ts",
        ])
        ws_evidence = [e for e in result.evidence if e["type"] == "workspace_config"]
        assert len(ws_evidence) >= 1
        assert ws_evidence[0]["confidence"] == "high"

    def test_insufficient_paths_returns_unknown(self):
        """< 2 source files → pattern='unknown', evidence explains why."""
        result = self._analyze(["src/main.py"])
        assert result.pattern == "unknown"
        assert len(result.evidence) >= 1

    def test_tentative_true_when_filesystem_only_and_low_confidence(self):
        """Low-confidence filesystem-only inference → tentative=True."""
        # Flat file layout — no strong directory signals
        result = self._analyze(["main.py", "utils.py", "config.py"])
        # confidence should be low, tentative should be True
        if result.confidence == "low":
            assert result.tentative is True

    def test_filesystem_only_limitation_present(self):
        """Filesystem inference without graph → limitations contain graph note."""
        result = self._analyze([
            "src/controllers/orders.java",
            "src/services/orders.java",
            "src/repositories/orders.java",
        ])
        # At least one limitation should mention import graph
        lim_text = " ".join(result.limitations)
        # Either a graph confirmation note or insufficient_evidence note
        assert any(
            kw in lim_text
            for kw in ("import graph", "filesystem", "insufficient_evidence", "not confirmed")
        )

    def test_pattern_with_no_evidence_is_tentative(self):
        """When only file-naming heuristic matches, tentative must be True."""
        result = self._analyze([
            "my_controller.py",
            "my_service.py",
            "my_repository.py",
        ])
        # File-naming heuristic detection → confidence low → tentative
        assert result.tentative is True

    def test_monorepo_pattern_not_tentative(self):
        """Monorepo detected via workspace config is not tentative."""
        result = self._analyze([
            "turbo.json",
            "packages/backend/src/index.ts",
            "packages/frontend/src/index.ts",
            "packages/shared/src/index.ts",
            "packages/cli/src/index.ts",
        ])
        assert result.pattern == "monorepo"
        assert result.tentative is False


# ===========================================================================
# 3. semantic_analyzer — Java language_coverage_details
# ===========================================================================

class TestSemanticJavaCoverage:
    """Java in --semantics must expose explicit heuristic-only notice."""

    def _analyze(self, files: dict[str, str]):
        from sourcecode.semantic_analyzer import SemanticAnalyzer
        root, tree = _make_tree(files)
        _, _, _, summary = SemanticAnalyzer(root=root).analyze(root, tree)
        return summary

    def test_java_in_language_coverage(self):
        """Java files → language_coverage['java'] == 'heuristic'."""
        summary = self._analyze({
            "src/MyService.java": (
                "public class MyService {\n"
                "    public void doWork() {}\n"
                "}\n"
            )
        })
        assert "java" in summary.language_coverage
        assert summary.language_coverage["java"] == "heuristic"

    def test_java_language_coverage_details_present(self):
        """Java → language_coverage_details['java'] is a dict with supported/status/reason."""
        summary = self._analyze({
            "src/OrderController.java": (
                "public class OrderController {\n"
                "    public void getOrders() {}\n"
                "}\n"
            )
        })
        assert "java" in summary.language_coverage_details
        details = summary.language_coverage_details["java"]
        assert details["supported"] is True
        assert details["status"] == "heuristic"
        assert "reason" in details
        assert len(details["reason"]) > 10  # non-trivial explanation

    def test_java_reason_mentions_no_cross_file(self):
        """Java reason must explicitly mention lack of cross-file resolution."""
        summary = self._analyze({
            "src/Foo.java": "public class Foo { public void bar() {} }\n"
        })
        reason = summary.language_coverage_details.get("java", {}).get("reason", "")
        assert "cross-file" in reason.lower() or "cross file" in reason.lower()

    def test_python_still_full_in_mixed_project(self):
        """Python+Java project → Python=full, Java=heuristic."""
        summary = self._analyze({
            "app/main.py": "def main(): pass\n",
            "src/Service.java": "public class Service {}\n",
        })
        assert summary.language_coverage.get("python") == "full"
        assert summary.language_coverage.get("java") == "heuristic"
        assert summary.language_coverage_details.get("python", {}).get("status") == "full"
        assert summary.language_coverage_details.get("java", {}).get("status") == "heuristic"

    def test_java_symbol_extraction_works(self):
        """Java symbols are extracted (class + method names)."""
        from sourcecode.semantic_analyzer import SemanticAnalyzer
        root, tree = _make_tree({
            "src/UserService.java": (
                "public class UserService {\n"
                "    public void createUser() {}\n"
                "    private void validateUser() {}\n"
                "}\n"
            )
        })
        _, symbols, _, _ = SemanticAnalyzer(root=root).analyze(root, tree)
        java_symbols = [s for s in symbols if s.language == "java"]
        symbol_names = {s.symbol for s in java_symbols}
        assert "UserService" in symbol_names
        assert "createUser" in symbol_names

    def test_java_only_project_status_ok(self):
        """Java-only project with valid files → status='ok', not 'failed'."""
        summary = self._analyze({
            "src/Main.java": "public class Main { public static void main(String[] args) {} }\n"
        })
        assert summary.status == "ok"

    def test_empty_project_status_failed(self):
        """Project with no source files → status='failed'."""
        summary = self._analyze({
            "README.md": "# project\n"
        })
        assert summary.status == "failed"


# ===========================================================================
# 4. doc_analyzer — Java/Go language_coverage honesty
# ===========================================================================

class TestDocAnalyzerLanguageCoverage:
    """--docs must explicitly mark unsupported languages."""

    def _analyze(self, files: dict[str, str]):
        from sourcecode.doc_analyzer import DocAnalyzer
        root, tree = _make_tree(files)
        records, summary = DocAnalyzer().analyze(root, tree)
        return records, summary

    def test_java_marked_unsupported(self):
        """Java files → language_coverage['java'] == 'unsupported'."""
        _, summary = self._analyze({
            "src/UserService.java": (
                "/** Creates a user. */\n"
                "public class UserService {}\n"
            )
        })
        assert summary.language_coverage.get("java") == "unsupported"

    def test_python_marked_supported(self):
        """Python files → language_coverage['python'] == 'supported'."""
        _, summary = self._analyze({
            "app/main.py": '"""Entry point."""\ndef main(): pass\n'
        })
        assert summary.language_coverage.get("python") == "supported"

    def test_typescript_marked_supported(self):
        """TypeScript files → language_coverage['typescript'] == 'supported'."""
        _, summary = self._analyze({
            "src/index.ts": "/** Main module. */\nexport function main() {}\n"
        })
        assert summary.language_coverage.get("typescript") == "supported"

    def test_java_no_doc_records_emitted(self):
        """Java files must NOT produce DocRecords (no false coverage)."""
        records, summary = self._analyze({
            "src/Service.java": "/** Does stuff. */\npublic class Service {}\n"
        })
        java_records = [r for r in records if r.language == "java"]
        assert java_records == []

    def test_java_unsupported_limitation_emitted(self):
        """Java files → limitation entry stating docs not extracted."""
        _, summary = self._analyze({
            "src/Service.java": "public class Service {}\n"
        })
        lim_text = " ".join(summary.limitations)
        assert "java" in lim_text.lower()
        assert any(
            kw in lim_text.lower()
            for kw in ("unsupported", "not supported", "docs_unavailable", "docs_not_extracted")
        )

    def test_mixed_py_java_coverage(self):
        """Mixed Python+Java → Python supported, Java unsupported; Python docs extracted."""
        records, summary = self._analyze({
            "app/main.py": '"""Entry point."""\ndef run(): pass\n',
            "src/Svc.java": "/** Java service. */\npublic class Svc {}\n",
        })
        assert summary.language_coverage.get("python") == "supported"
        assert summary.language_coverage.get("java") == "unsupported"
        py_records = [r for r in records if r.language == "python"]
        assert len(py_records) > 0

    def test_go_marked_unsupported(self):
        """Go files → language_coverage['go'] == 'unsupported'."""
        _, summary = self._analyze({
            "main.go": "// Package main.\npackage main\nfunc main() {}\n"
        })
        assert summary.language_coverage.get("go") == "unsupported"

    def test_language_coverage_field_present_on_summary(self):
        """DocSummary always has language_coverage dict."""
        _, summary = self._analyze({"app/mod.py": "pass\n"})
        assert hasattr(summary, "language_coverage")
        assert isinstance(summary.language_coverage, dict)
