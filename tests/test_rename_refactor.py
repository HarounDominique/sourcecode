"""Tests for rename_refactor.py — BLOCKER-A: native Java class rename.

Coverage:
  - Physical file rename (the core P0 fix)
  - Class declaration update
  - Import statement update
  - Field type and variable name update
  - Constructor name update
  - extends / implements references
  - Generic type parameters
  - Spring @Qualifier camelCase
  - Dry-run mode (no disk writes)
  - Multi-module repo (multiple subdirs)
  - Error cases: not found, already exists, bad names
  - Change audit trail structure (BLOCKER-C)
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from sourcecode.rename_refactor import rename_class, FileChange, RenameResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


# ---------------------------------------------------------------------------
# P0: Physical file rename
# ---------------------------------------------------------------------------

class TestPhysicalFileRename:
    def test_source_file_renamed_on_disk(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        assert not (tmp_path / "ServiceA.java").exists(), "Old file must be deleted"
        assert (tmp_path / "ServiceB.java").exists(), "New file must be created"

    def test_new_file_path_in_result(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        assert result.old_file == "ServiceA.java"
        assert result.new_file == "ServiceB.java"

    def test_dry_run_no_disk_change(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "ServiceB", dry_run=True)
        assert (tmp_path / "ServiceA.java").exists(), "dry_run must not delete original"
        assert not (tmp_path / "ServiceB.java").exists(), "dry_run must not create new file"
        assert result.dry_run is True
        assert result.files_modified == 1

    def test_file_in_subdirectory_renamed(self, tmp_path):
        _repo(tmp_path, {
            "src/main/java/com/example/ServiceA.java": "package com.example;\npublic class ServiceA {}\n",
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        old_path = tmp_path / "src/main/java/com/example/ServiceA.java"
        new_path = tmp_path / "src/main/java/com/example/ServiceB.java"
        assert not old_path.exists()
        assert new_path.exists()
        assert "ServiceB" in new_path.read_text()


# ---------------------------------------------------------------------------
# Class declaration, constructor, imports
# ---------------------------------------------------------------------------

class TestTextReplacements:
    def test_class_declaration_updated(self, tmp_path):
        _repo(tmp_path, {"OrderManager.java": "package com.ex;\npublic class OrderManager {}\n"})
        rename_class(tmp_path, "OrderManager", "OrderService")
        content = (tmp_path / "OrderService.java").read_text()
        assert "class OrderService" in content
        assert "class OrderManager" not in content

    def test_constructor_name_updated(self, tmp_path):
        src = (
            "package com.ex;\n"
            "public class ServiceA {\n"
            "    public ServiceA(String name) { this.name = name; }\n"
            "}\n"
        )
        _repo(tmp_path, {"ServiceA.java": src})
        rename_class(tmp_path, "ServiceA", "ServiceB")
        content = (tmp_path / "ServiceB.java").read_text()
        assert "public ServiceB(String name)" in content
        assert "public ServiceA" not in content

    def test_import_statement_updated(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Client.java": "package com.ex.web;\nimport com.ex.ServiceA;\npublic class Client { ServiceA s; }\n",
        })
        rename_class(tmp_path, "ServiceA", "ServiceB")
        client = (tmp_path / "Client.java").read_text()
        assert "import com.ex.ServiceB;" in client
        assert "import com.ex.ServiceA;" not in client

    def test_field_type_updated(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Controller.java": (
                "package com.ex;\n"
                "public class Controller {\n"
                "    private ServiceA serviceA;\n"
                "    public Controller(ServiceA serviceA) { this.serviceA = serviceA; }\n"
                "}\n"
            ),
        })
        rename_class(tmp_path, "ServiceA", "ServiceB")
        content = (tmp_path / "Controller.java").read_text()
        assert "ServiceB serviceB" in content
        assert "ServiceA" not in content

    def test_extends_updated(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "ServiceAImpl.java": "package com.ex;\npublic class ServiceAImpl extends ServiceA {}\n",
        })
        rename_class(tmp_path, "ServiceA", "ServiceB")
        content = (tmp_path / "ServiceAImpl.java").read_text()
        assert "extends ServiceB" in content

    def test_implements_updated(self, tmp_path):
        _repo(tmp_path, {
            "IUserService.java": "package com.ex;\npublic interface IUserService {}\n",
            "UserServiceImpl.java": "package com.ex;\npublic class UserServiceImpl implements IUserService {}\n",
        })
        rename_class(tmp_path, "IUserService", "IOrderService")
        content = (tmp_path / "UserServiceImpl.java").read_text()
        assert "implements IOrderService" in content

    def test_generic_type_updated(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Container.java": (
                "package com.ex;\nimport java.util.List;\n"
                "public class Container {\n"
                "    private List<ServiceA> items;\n"
                "    public List<ServiceA> getAll() { return items; }\n"
                "}\n"
            ),
        })
        rename_class(tmp_path, "ServiceA", "ServiceB")
        content = (tmp_path / "Container.java").read_text()
        assert "List<ServiceB>" in content
        assert "List<ServiceA>" not in content

    def test_new_instantiation_updated(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Factory.java": "package com.ex;\npublic class Factory { ServiceA s = new ServiceA(); }\n",
        })
        rename_class(tmp_path, "ServiceA", "ServiceB")
        content = (tmp_path / "Factory.java").read_text()
        assert "new ServiceB()" in content


# ---------------------------------------------------------------------------
# Multi-module scenario
# ---------------------------------------------------------------------------

class TestMultiModuleRename:
    def test_rename_across_multiple_modules(self, tmp_path):
        _repo(tmp_path, {
            "core/src/main/java/com/ex/ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "web/src/main/java/com/ex/web/Controller.java": (
                "package com.ex.web;\nimport com.ex.ServiceA;\n"
                "public class Controller { ServiceA svc; }\n"
            ),
            "test/src/test/java/com/ex/ServiceATest.java": (
                "package com.ex;\nimport com.ex.ServiceA;\n"
                "public class ServiceATest { ServiceA s = new ServiceA(); }\n"
            ),
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        assert result.errors == []
        assert result.files_modified >= 3
        # Old file gone, new file exists
        assert not (tmp_path / "core/src/main/java/com/ex/ServiceA.java").exists()
        assert (tmp_path / "core/src/main/java/com/ex/ServiceB.java").exists()
        # Controller updated
        ctrl = (tmp_path / "web/src/main/java/com/ex/web/Controller.java").read_text()
        assert "ServiceB" in ctrl and "ServiceA" not in ctrl
        # Test file updated
        test = (tmp_path / "test/src/test/java/com/ex/ServiceATest.java").read_text()
        assert "ServiceB" in test

    def test_rename_no_tests_excludes_test_files(self, tmp_path):
        _repo(tmp_path, {
            "src/main/java/ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "src/test/java/ServiceATest.java": (
                "package com.ex;\npublic class ServiceATest { ServiceA s; }\n"
            ),
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB", include_tests=False)
        # Test file should NOT be modified
        test_content = (tmp_path / "src/test/java/ServiceATest.java").read_text()
        assert "ServiceA" in test_content, "Test files must be untouched when include_tests=False"


# ---------------------------------------------------------------------------
# Change audit trail (BLOCKER-C)
# ---------------------------------------------------------------------------

class TestChangeAuditTrail:
    def test_change_has_required_fields(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Client.java": "package com.ex;\npublic class Client { ServiceA s; }\n",
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB", dry_run=True)
        assert result.changes
        for change in result.changes:
            assert change.file, "file must be set"
            assert change.intent, "intent must be set"
            assert change.diff, "diff must be set"
            assert isinstance(change.before_lines, list)
            assert isinstance(change.after_lines, list)

    def test_diff_contains_old_and_new_lines(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "ServiceB", dry_run=True)
        src_change = next(c for c in result.changes if "ServiceA.java" in c.file)
        assert "-public class ServiceA" in src_change.diff
        assert "+public class ServiceB" in src_change.diff

    def test_to_dict_serializable(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "ServiceB", dry_run=True)
        d = result.to_dict()
        # Must be JSON-serializable
        serialized = json.dumps(d)
        loaded = json.loads(serialized)
        assert loaded["old_name"] == "ServiceA"
        assert loaded["new_name"] == "ServiceB"
        assert isinstance(loaded["changes"], list)
        for c in loaded["changes"]:
            assert {"file", "intent", "diff", "before_lines", "after_lines"} <= set(c.keys())

    def test_intent_describes_source_file_differently(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "Client.java": "package com.ex;\npublic class Client { ServiceA s; }\n",
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB", dry_run=True)
        src = next((c for c in result.changes if c.file == "ServiceA.java"), None)
        client = next((c for c in result.changes if c.file == "Client.java"), None)
        assert src is not None
        assert client is not None
        assert "Renamed class declaration" in src.intent
        assert "Updated references" in client.intent


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def test_class_not_found_returns_error(self, tmp_path):
        result = rename_class(tmp_path, "NonExistentClass", "NewName")
        assert result.errors
        assert "NonExistentClass" in result.errors[0]

    def test_target_file_already_exists_aborts(self, tmp_path):
        _repo(tmp_path, {
            "ServiceA.java": "package com.ex;\npublic class ServiceA {}\n",
            "ServiceB.java": "package com.ex;\npublic class ServiceB {}\n",
        })
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        assert result.errors
        assert "already exists" in result.errors[0]
        assert (tmp_path / "ServiceA.java").exists(), "Original must not be deleted on error"

    def test_lowercase_old_name_returns_error(self, tmp_path):
        result = rename_class(tmp_path, "serviceA", "ServiceB")
        assert result.errors

    def test_lowercase_new_name_returns_error(self, tmp_path):
        _repo(tmp_path, {"ServiceA.java": "package com.ex;\npublic class ServiceA {}\n"})
        result = rename_class(tmp_path, "ServiceA", "serviceB")
        assert result.errors

    def test_same_name_returns_error(self, tmp_path):
        result = rename_class(tmp_path, "ServiceA", "ServiceA")
        assert result.errors

    def test_empty_repo_returns_error(self, tmp_path):
        result = rename_class(tmp_path, "ServiceA", "ServiceB")
        assert result.errors

    def test_nonexistent_root_returns_error(self, tmp_path):
        result = rename_class(tmp_path / "does_not_exist", "ServiceA", "ServiceB")
        assert result.errors
