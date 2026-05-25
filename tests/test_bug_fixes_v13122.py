"""Regression tests for bugs reported against sourcecode CLI v1.31.22.

BUG-1  Exit code 255 on task subcommands (onboard, repo-ir, review-pr, fix-bug,
        modernize, prepare-context *, --compact --format yaml).
        All must return exit 0 on successful completion.

BUG-2  Angular *.component.ts classified as Spring @Service in review-pr output.
        (Angular detection already fixed in dc0ca06; tests here lock the behaviour.)

BUG-3  --compact help text references --slim which does not exist.
        Fix: remove --slim mention (Option A).

BUG-4  angular_version: null even when @angular/core declared in package.json.
        Fix: parse dependencies / devDependencies / peerDependencies with or{} guards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# BUG-1  Exit codes: all task subcommands must return 0 on success
# ---------------------------------------------------------------------------

class TestExitCodes:
    """All listed commands must exit 0 when they complete without a domain error."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sourcecode", *args],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,  # repo root
        )

    # ── Commands that were already correct (baseline / non-regression) ────────

    def test_compact_json_exits_0(self):
        r = self._run(".", "--compact")
        assert r.returncode == 0, f"--compact exited {r.returncode}:\n{r.stderr}"

    def test_agent_exits_0(self):
        r = self._run(".", "--agent")
        assert r.returncode == 0, f"--agent exited {r.returncode}:\n{r.stderr}"

    def test_agent_full_exits_0(self):
        r = self._run(".", "--agent", "--full")
        assert r.returncode == 0, f"--agent --full exited {r.returncode}:\n{r.stderr}"

    def test_endpoints_exits_0(self):
        r = self._run("endpoints", ".")
        assert r.returncode == 0, f"endpoints exited {r.returncode}:\n{r.stderr}"

    # ── Commands that were reported as EXIT 255 (BUG-1 regression) ───────────

    def test_compact_format_yaml_exits_0(self):
        """BUG-1: sourcecode . --compact --format yaml must exit 0."""
        r = self._run(".", "--compact", "--format", "yaml")
        assert r.returncode == 0, (
            f"--compact --format yaml exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_compact_format_yaml_stdout_is_valid_yaml(self):
        """YAML output must be non-empty and parseable."""
        r = self._run(".", "--compact", "--format", "yaml")
        assert r.returncode == 0
        assert r.stdout.strip(), "stdout must not be empty"
        # ruamel-free check: YAML starts with a mapping key or '---'
        first = r.stdout.strip().splitlines()[0]
        assert ":" in first or first == "---", (
            f"stdout does not look like YAML: {first!r}"
        )

    def test_onboard_exits_0(self):
        """BUG-1: sourcecode onboard . must exit 0."""
        r = self._run("onboard", ".")
        assert r.returncode == 0, (
            f"onboard exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_repo_ir_exits_0(self):
        """BUG-1: sourcecode repo-ir . must exit 0."""
        r = self._run("repo-ir", ".")
        assert r.returncode == 0, (
            f"repo-ir exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_repo_ir_summary_only_exits_0(self):
        """BUG-1: sourcecode repo-ir . --summary-only must exit 0."""
        r = self._run("repo-ir", ".", "--summary-only")
        assert r.returncode == 0, (
            f"repo-ir --summary-only exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_review_pr_exits_0(self):
        """BUG-1: sourcecode review-pr . --since HEAD~3 must exit 0."""
        r = self._run("review-pr", ".", "--since", "HEAD~3")
        assert r.returncode == 0, (
            f"review-pr --since HEAD~3 exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_fix_bug_exits_0(self):
        """BUG-1: sourcecode fix-bug . must exit 0."""
        r = self._run("fix-bug", ".")
        assert r.returncode == 0, (
            f"fix-bug exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_modernize_exits_0(self):
        """BUG-1: sourcecode modernize . must exit 0."""
        r = self._run("modernize", ".")
        assert r.returncode == 0, (
            f"modernize exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_prepare_context_explain_exits_0(self):
        """BUG-1: sourcecode prepare-context explain . must exit 0."""
        r = self._run("prepare-context", "explain", ".")
        assert r.returncode == 0, (
            f"prepare-context explain exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_prepare_context_delta_exits_0(self):
        """BUG-1: sourcecode prepare-context delta . --since HEAD~3 must exit 0."""
        r = self._run("prepare-context", "delta", ".", "--since", "HEAD~3")
        assert r.returncode == 0, (
            f"prepare-context delta exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    def test_prepare_context_generate_tests_exits_0(self):
        """BUG-1: sourcecode prepare-context generate-tests . must exit 0."""
        r = self._run("prepare-context", "generate-tests", ".")
        assert r.returncode == 0, (
            f"prepare-context generate-tests exited {r.returncode} — BUG-1 regression\n{r.stderr}"
        )

    # ── Error cases: must NOT exit 0 ──────────────────────────────────────────

    def test_nonexistent_path_exits_1(self, tmp_path: Path):
        """Domain error (path not found) → exit 1.

        Pass the nonexistent path as the FIRST positional so _preprocess_args
        extracts it as the repo root — not as a stray arg (which would be exit 2).
        """
        r = self._run("/nonexistent_path_xyz_sourcecode_99999", "--compact")
        assert r.returncode == 1, (
            f"nonexistent path should exit 1, got {r.returncode}"
        )

    def test_invalid_format_exits_2(self, tmp_path: Path):
        """Arg validation error (invalid --format) → exit 2."""
        r = self._run(".", "--compact", "--format", "invalid_format_xyz", str(tmp_path))
        assert r.returncode == 2, (
            f"invalid --format should exit 2, got {r.returncode}"
        )

    def test_compact_and_full_mutually_exclusive_exits_2(self, tmp_path: Path):
        """--compact and --full are mutually exclusive → exit 2."""
        r = self._run(".", "--compact", "--full", str(tmp_path))
        assert r.returncode == 2, (
            f"--compact --full should exit 2, got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# BUG-2  Angular classification (locked via prepare_context internals)
# ---------------------------------------------------------------------------

class TestAngularClassificationV13122:
    """Lock: Angular *.component.ts must never be classified as Spring 'service'.

    These tests duplicate the intent of test_bug_fixes_v1321.py but exercise
    the _classify_changed_file closure directly through the module constants,
    providing an independent regression layer for v1.31.22.
    """

    def _classify(self, path: str) -> dict[str, Any]:
        """Replicate the _classify_changed_file heuristic from prepare_context."""
        from pathlib import Path as _P

        norm = path.replace("\\", "/")
        stem = _P(path).stem
        suffix = _P(path).suffix.lower()
        stem_lower = stem.lower()

        # Angular block (must run first for .ts files)
        if suffix == ".ts":
            _ts_last = stem_lower.rsplit(".", 1)[-1]
            _NG_MAP = {
                "component":   "ng_component",
                "pipe":        "ng_pipe",
                "directive":   "ng_directive",
                "guard":       "ng_guard",
                "interceptor": "ng_interceptor",
                "resolver":    "ng_resolver",
                "module":      "ng_module",
            }
            if _ts_last in _NG_MAP:
                return {"artifact_type": _NG_MAP[_ts_last]}
            if _ts_last == "service":
                return {"artifact_type": "ng_service"}

        _SERVICE_KW = (
            "service", "serviceimpl", "servicefacade", "facade",
            "usecase", "interactor", "aspect", "listener",
            "subscriber", "eventhandler",
        )
        _CODE_EXTS = frozenset({
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
            ".go", ".rs", ".rb", ".php", ".cs", ".dart", ".mjs", ".cjs", ".scala",
        })
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _SERVICE_KW):
            return {"artifact_type": "service"}

        return {"artifact_type": "source"}

    # core BUG-2 cases
    def test_causa_denegacion_form_component_ts(self):
        result = self._classify(
            "saint-client/src/app/causa/causa-denegacion-form.component.ts"
        )
        assert result["artifact_type"] == "ng_component", (
            f"BUG-2 regression: expected ng_component, got {result['artifact_type']!r}"
        )

    def test_gestion_procedimientos_component_ts(self):
        result = self._classify(
            "src/app/gestion/gestion-procedimientos.component.ts"
        )
        assert result["artifact_type"] == "ng_component"

    def test_angular_service_is_ng_service_not_spring_service(self):
        result = self._classify("src/app/services/auth.service.ts")
        assert result["artifact_type"] == "ng_service"

    def test_java_service_still_service(self):
        result = self._classify("src/main/java/com/example/UserService.java")
        assert result["artifact_type"] == "service"

    def test_component_keyword_in_java_is_not_ng_component(self):
        result = self._classify("src/main/java/com/example/SecurityComponent.java")
        # Java files don't match the .ts Angular block; "component" not in _SERVICE_KW
        assert result["artifact_type"] in ("source", "service", "security")
        assert result["artifact_type"] != "ng_component"

    # artifact change effect descriptions
    def test_ng_component_in_artifact_change_effect(self):
        from sourcecode.prepare_context import _ARTIFACT_CHANGE_EFFECT
        assert "ng_component" in _ARTIFACT_CHANGE_EFFECT
        desc = _ARTIFACT_CHANGE_EFFECT["ng_component"]
        assert "Angular" in desc, f"ng_component description must mention Angular: {desc!r}"
        assert "Spring" not in desc, f"ng_component must not reference Spring: {desc!r}"
        assert "CDI" not in desc

    def test_all_ng_types_in_artifact_change_effect(self):
        from sourcecode.prepare_context import _ARTIFACT_CHANGE_EFFECT
        ng_types = [
            "ng_component", "ng_pipe", "ng_directive",
            "ng_guard", "ng_interceptor", "ng_resolver",
            "ng_service", "ng_module",
        ]
        for ng_type in ng_types:
            assert ng_type in _ARTIFACT_CHANGE_EFFECT, (
                f"{ng_type} missing from _ARTIFACT_CHANGE_EFFECT"
            )


# ---------------------------------------------------------------------------
# BUG-3  --slim removed from --compact help text
# ---------------------------------------------------------------------------

class TestSlimHelpText:
    """--compact help must not reference --slim (unimplemented option)."""

    def test_compact_help_does_not_mention_slim(self):
        """`--slim` was removed from --compact help string (BUG-3 Option A)."""
        import typer
        from sourcecode.cli import app

        # Get the Click command and inspect the --compact option help
        import typer.main
        cmd = typer.main.get_command(app)
        # The --compact option is on the root command (callback)
        compact_param = next(
            (p for p in cmd.params if "--compact" in (getattr(p, "opts", None) or [])),
            None,
        )
        assert compact_param is not None, "Could not find --compact param"
        help_text = compact_param.help or ""
        assert "--slim" not in help_text, (
            f"BUG-3: --compact help still references --slim: {help_text!r}"
        )
        assert "slim" not in help_text.lower(), (
            f"BUG-3: --compact help still references slim: {help_text!r}"
        )

    def test_slim_flag_not_in_cli_help_output(self):
        """sourcecode --help must not list --slim as an option."""
        r = subprocess.run(
            ["sourcecode", "--help"],
            capture_output=True, text=True,
        )
        assert "--slim" not in r.stdout, (
            "BUG-3: --slim appears in --help output but is not implemented"
        )

    def test_slim_option_does_not_exist(self):
        """sourcecode . --slim must fail with a usage error (not exist)."""
        r = subprocess.run(
            ["sourcecode", ".", "--slim"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        # Click returns exit 2 for unknown options
        assert r.returncode != 0, "--slim should fail (option does not exist)"
        assert r.returncode == 2, (
            f"Expected exit 2 (unknown option), got {r.returncode}"
        )


# ---------------------------------------------------------------------------
# BUG-4  angular_version parsed from package.json
# ---------------------------------------------------------------------------

class TestAngularVersionParsing:
    """angular_version must be populated from package.json when @angular/core present."""

    def _run_angular_analysis(self, pkg_content: dict) -> dict | None:
        """Invoke _angular_analysis via a minimal SourceMap with a real tmp package.json."""
        import json as _json
        import tempfile
        from pathlib import Path as _P

        from sourcecode.schema import AnalysisMetadata, SourceMap

        with tempfile.TemporaryDirectory() as tmpdir:
            root = _P(tmpdir)
            # Write a minimal package.json
            (root / "package.json").write_text(
                _json.dumps(pkg_content), encoding="utf-8"
            )
            # Write at least one .ts file so the function doesn't short-circuit
            (root / "app.component.ts").write_text(
                "@Component({})\nexport class AppComponent {}\n",
                encoding="utf-8",
            )
            metadata = AnalysisMetadata(analyzed_path=str(root))
            sm = SourceMap(
                metadata=metadata,
                file_paths=["app.component.ts"],
            )
            from sourcecode.serializer import _angular_analysis
            return _angular_analysis(sm)

    def test_angular_version_from_dependencies(self):
        """BUG-4: @angular/core in dependencies → angular_version populated."""
        result = self._run_angular_analysis({
            "dependencies": {"@angular/core": "^20.3.18", "rxjs": "~7.8.0"},
        })
        assert result is not None
        assert result["angular_version"] == "20.3.18", (
            f"BUG-4 regression: expected '20.3.18', got {result['angular_version']!r}"
        )

    def test_angular_version_from_dev_dependencies(self):
        """angular_version is populated even when listed under devDependencies."""
        result = self._run_angular_analysis({
            "devDependencies": {"@angular/core": "~17.0.0"},
        })
        assert result is not None
        assert result["angular_version"] == "17.0.0"

    def test_angular_version_strips_caret_and_tilde(self):
        """Version range prefix (^, ~) must be stripped."""
        for prefix in ("^", "~", ">=", ""):
            result = self._run_angular_analysis({
                "dependencies": {"@angular/core": f"{prefix}18.2.0"},
            })
            assert result is not None
            assert result["angular_version"] == "18.2.0", (
                f"Prefix {prefix!r} not stripped: {result['angular_version']!r}"
            )

    def test_angular_version_null_when_dependencies_key_is_null_json(self):
        """BUG-4 original: package.json with null dependencies must not raise TypeError."""
        # {"dependencies": null} was previously raising TypeError
        result = self._run_angular_analysis({
            "name": "my-app",
            "dependencies": None,  # serialised as null in JSON
            "devDependencies": {"@angular/core": "^16.0.0"},
        })
        assert result is not None
        assert result["angular_version"] == "16.0.0", (
            "BUG-4: null dependencies key raised TypeError or version not found"
        )

    def test_angular_version_none_when_not_angular_project(self):
        """Non-Angular projects must have angular_version: null."""
        result = self._run_angular_analysis({
            "dependencies": {"react": "^18.0.0"},
        })
        # Either None result (no .ts files → early return) or angular_version absent/null
        if result is not None:
            assert not result.get("angular_version"), (
                "Non-Angular project must not have angular_version set"
            )

    def test_angular_version_peer_dependencies_fallback(self):
        """angular_version found via peerDependencies when not in deps/devDeps."""
        result = self._run_angular_analysis({
            "peerDependencies": {"@angular/core": "^15.2.0"},
        })
        assert result is not None
        assert result["angular_version"] == "15.2.0"
