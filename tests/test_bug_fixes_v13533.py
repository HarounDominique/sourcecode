"""Regression tests for bugs fixed in v1.35.33 (session 90).

BUG-003: find_java_files guard misidentifies production files inside test module
         roots containing 'it' package (e.g. test-framework/*/src/main/.../it/)
BUG-004: find_java_files guard misses modules whose name ends in "-tests"/"-test"
         (e.g. jobrunr-micronaut-tests/src/main/.../it/)
BUG-005: generate-tests misses *Controller.java files (only RestController.java listed)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# BUG-003: test module roots caught by guard when path has 'it'/'test' package
# ---------------------------------------------------------------------------

class TestBug3FindJavaFilesTestModuleGuard:

    def test_test_framework_with_it_package_excluded(self, tmp_path: Path) -> None:
        """test-framework/.../src/main/java/.../it/Foo.java must be excluded.

        Path triggers is_test_path() via 'it' package; guard sees 'test-framework'
        prefix → _prefix_is_test=True → file excluded.
        """
        _write(
            tmp_path,
            "test-framework/test-providers/src/main/java/org/example/it/TestResource.java",
            "package org.example.it;\npublic class TestResource {}\n",
        )
        _write(
            tmp_path,
            "core/src/main/java/org/example/RealService.java",
            "package org.example;\npublic class RealService {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("TestResource" in f for f in files), (
            f"BUG-003: test-framework/*/src/main/.../it/ file was included. files={files}"
        )
        assert any("RealService" in f for f in files)

    def test_testsuite_module_with_tests_subdir_excluded(self, tmp_path: Path) -> None:
        """testsuite/integration/.../tests/.../src/main/.../it/Foo.java must be excluded.

        is_test_path fires due to 'tests' AND 'it' segments; guard prefix
        includes 'testsuite' which starts with 'test' → excluded.
        """
        _write(
            tmp_path,
            "testsuite/integration/tests/base/src/main/java/org/example/it/TestEndpointResource.java",
            "package org.example.it;\npublic class TestEndpointResource {}\n",
        )
        _write(
            tmp_path,
            "services/core/src/main/java/org/example/CoreService.java",
            "package org.example;\npublic class CoreService {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("TestEndpointResource" in f for f in files), (
            f"BUG-003: testsuite/.../tests/.../src/main/.../it/ file included. files={files}"
        )
        assert any("CoreService" in f for f in files)

    def test_production_test_package_still_included(self, tmp_path: Path) -> None:
        """Production files in a Java package named 'test' under src/main/ must NOT be excluded.

        BUG-001 regression guard: is_test_path fires on 'test' package segment but
        the prefix before src/main/ is empty (no test module root) → file stays in.
        """
        _write(
            tmp_path,
            "src/main/java/org/example/workflow/state/test/Helper.java",
            "package org.example.workflow.state.test;\npublic class Helper {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert any("state/test/Helper.java" in f for f in files), (
            f"BUG-001 regression: production file in com/test/ package was excluded. files={files}"
        )

    def test_production_it_package_included_when_not_test_module(self, tmp_path: Path) -> None:
        """src/main/java/.../it/Controller.java in a NON-test module must be included.

        is_test_path fires on 'it' but prefix is 'mymodule' (not a test module) → included.
        """
        _write(
            tmp_path,
            "mymodule/src/main/java/org/example/it/ProductionController.java",
            "package org.example.it;\npublic class ProductionController {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert any("ProductionController" in f for f in files), (
            f"False positive: non-test module src/main/.../it/ file was excluded. files={files}"
        )


# ---------------------------------------------------------------------------
# BUG-004: modules with "test/tests" as suffix word in hyphenated name
# ---------------------------------------------------------------------------

class TestBug4FindJavaFilesSuffixTestModules:

    def test_module_ending_in_tests_with_it_package_excluded(self, tmp_path: Path) -> None:
        """module-micronaut-tests/.../src/main/.../it/Controller.java must be excluded.

        is_test_path fires on 'it'; guard splits 'micronaut-tests' → 'tests' word
        match → _prefix_is_test=True → excluded.
        """
        _write(
            tmp_path,
            "framework/module-micronaut-tests/src/main/java/org/example/it/FunctionalController.java",
            "package org.example.it;\npublic class FunctionalController {}\n",
        )
        _write(
            tmp_path,
            "core/src/main/java/org/example/ProductionBean.java",
            "package org.example;\npublic class ProductionBean {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("FunctionalController" in f for f in files), (
            f"BUG-004: module ending in '-tests' with 'it' package included. files={files}"
        )
        assert any("ProductionBean" in f for f in files)

    def test_module_ending_in_test_with_it_package_excluded(self, tmp_path: Path) -> None:
        """module-integration-test/.../src/main/.../it/Fixture.java must be excluded."""
        _write(
            tmp_path,
            "modules/integration-test/src/main/java/com/example/it/IntegrationFixture.java",
            "package com.example.it;\npublic class IntegrationFixture {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("IntegrationFixture" in f for f in files), (
            f"BUG-004: module ending in '-test' with 'it' package included. files={files}"
        )

    def test_module_starting_with_test_still_excluded(self, tmp_path: Path) -> None:
        """BUG-003 fix regression: module starting with 'test' still excluded."""
        _write(
            tmp_path,
            "test-framework/src/main/java/org/example/it/TestResource.java",
            "package org.example.it;\npublic class TestResource {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("TestResource" in f for f in files), (
            f"BUG-004 regression: test-framework module file was included. files={files}"
        )

    def test_production_module_with_attestation_in_name_not_excluded(self, tmp_path: Path) -> None:
        """'attestation' module must NOT be excluded (no 'test' word when split)."""
        _write(
            tmp_path,
            "attestation/src/main/java/com/example/it/AttestationService.java",
            "package com.example.it;\npublic class AttestationService {}\n",
        )

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert any("AttestationService" in f for f in files), (
            f"False positive: 'attestation' module wrongly excluded. files={files}"
        )


# ---------------------------------------------------------------------------
# BUG-005: generate-tests includes *Controller.java (not only *RestController.java)
# ---------------------------------------------------------------------------

class TestBug5GenerateTestsControllerSuffix:

    def test_controller_java_in_test_gaps(self, tmp_path: Path) -> None:
        """*Controller.java files must appear in test_gaps."""
        _write(
            tmp_path,
            "src/main/java/com/example/ItemController.java",
            (
                "package com.example;\n"
                "import org.springframework.web.bind.annotation.RestController;\n"
                "import org.springframework.web.bind.annotation.GetMapping;\n"
                "@RestController\n"
                "public class ItemController {\n"
                "    @GetMapping(\"/items\") public String list() { return null; }\n"
                "    public String helper() { return null; }\n"
                "}\n"
            ),
        )

        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(root=tmp_path)
        result = builder.build("generate-tests", all_gaps=True, fast=False)

        paths = [g.get("path", g) if isinstance(g, dict) else g for g in result.test_gaps]
        assert any("ItemController" in p for p in paths), (
            f"BUG-005: ItemController.java missing from test_gaps. paths={paths}"
        )

    def test_rest_controller_java_still_in_test_gaps(self, tmp_path: Path) -> None:
        """*RestController.java files must still appear (regression guard)."""
        _write(
            tmp_path,
            "src/main/java/com/example/PaymentRestController.java",
            (
                "package com.example;\n"
                "import org.springframework.web.bind.annotation.RestController;\n"
                "@RestController\n"
                "public class PaymentRestController {\n"
                "    public String pay() { return null; }\n"
                "}\n"
            ),
        )

        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(root=tmp_path)
        result = builder.build("generate-tests", all_gaps=True, fast=False)

        paths = [g.get("path", g) if isinstance(g, dict) else g for g in result.test_gaps]
        assert any("PaymentRestController" in p for p in paths), (
            f"BUG-005 regression: PaymentRestController.java missing. paths={paths}"
        )
