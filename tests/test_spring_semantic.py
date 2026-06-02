"""test_spring_semantic.py — Unit tests for spring_semantic.py Phase 1.

Coverage:
  P  parse_transactional_args — annotation arg variants
  B  TransactionBoundary — model correctness
  I  build_tx_index — index construction from CIR-shaped data
  E  effective_boundary — resolution (method > class > None)
  S  stats — counts correctness
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.spring_semantic import (
    ISOLATION_DEFAULT,
    PROPAGATION_DEFAULT,
    TIMEOUT_DEFAULT,
    TransactionBoundary,
    TransactionBoundaryIndex,
    build_tx_index,
    parse_transactional_args,
)


# ---------------------------------------------------------------------------
# Helpers — build a minimal CIR-like object for index construction
# ---------------------------------------------------------------------------

class _FakeCIR:
    def __init__(self, nodes: list[dict]):
        self._raw_ir = {"graph": {"nodes": nodes, "edges": []}}
        self.cir_hash = "deadbeef0000000000000000"


def _tx_node(
    fqn: str,
    symbol_kind: str = "method",
    raw_args: str = "",
    modifiers: list[str] | None = None,
    source_file: str = "src/main/java/Foo.java",
) -> dict:
    return {
        "fqn": fqn,
        "symbol_kind": symbol_kind,
        "annotations": ["@Transactional"],
        "annotation_values": {"@Transactional": raw_args},
        "modifiers": modifiers or [],
        "source_file": source_file,
    }


def _non_tx_node(fqn: str) -> dict:
    return {
        "fqn": fqn,
        "symbol_kind": "method",
        "annotations": ["@Service"],
        "annotation_values": {},
        "modifiers": ["public"],
        "source_file": "src/main/java/Bar.java",
    }


# ---------------------------------------------------------------------------
# P — parse_transactional_args
# ---------------------------------------------------------------------------

class TestParseTransactionalArgs:
    def test_empty_args_returns_defaults(self):
        attrs, conf = parse_transactional_args("")
        assert attrs == {}
        assert conf == "high"

    def test_no_args_string(self):
        attrs, conf = parse_transactional_args("   ")
        assert attrs == {}
        assert conf == "high"

    def test_propagation_requires_new_with_prefix(self):
        attrs, conf = parse_transactional_args("propagation = Propagation.REQUIRES_NEW")
        assert attrs["propagation"] == "REQUIRES_NEW"
        assert conf == "high"

    def test_propagation_no_prefix(self):
        attrs, conf = parse_transactional_args("propagation=REQUIRES_NEW")
        assert attrs["propagation"] == "REQUIRES_NEW"

    def test_propagation_not_supported(self):
        attrs, _ = parse_transactional_args("propagation=Propagation.NOT_SUPPORTED")
        assert attrs["propagation"] == "NOT_SUPPORTED"

    def test_propagation_never(self):
        attrs, _ = parse_transactional_args("propagation = Propagation.NEVER")
        assert attrs["propagation"] == "NEVER"

    def test_propagation_mandatory(self):
        attrs, _ = parse_transactional_args("propagation=Propagation.MANDATORY")
        assert attrs["propagation"] == "MANDATORY"

    def test_propagation_nested(self):
        attrs, _ = parse_transactional_args("propagation=Propagation.NESTED")
        assert attrs["propagation"] == "NESTED"

    def test_isolation_read_committed(self):
        attrs, conf = parse_transactional_args("isolation = Isolation.READ_COMMITTED")
        assert attrs["isolation"] == "READ_COMMITTED"
        assert conf == "high"

    def test_isolation_no_prefix(self):
        attrs, _ = parse_transactional_args("isolation=SERIALIZABLE")
        assert attrs["isolation"] == "SERIALIZABLE"

    def test_readonly_true(self):
        attrs, _ = parse_transactional_args("readOnly=true")
        assert attrs["read_only"] is True

    def test_readonly_false(self):
        attrs, _ = parse_transactional_args("readOnly=false")
        assert attrs["read_only"] is False

    def test_readonly_with_spaces(self):
        attrs, _ = parse_transactional_args("readOnly = true")
        assert attrs["read_only"] is True

    def test_timeout_positive(self):
        attrs, _ = parse_transactional_args("timeout=30")
        assert attrs["timeout"] == 30

    def test_timeout_negative_one(self):
        attrs, _ = parse_transactional_args("timeout=-1")
        assert attrs["timeout"] == -1

    def test_rollback_for_single(self):
        attrs, _ = parse_transactional_args("rollbackFor=IOException.class")
        assert attrs["rollback_for"] == ["IOException"]

    def test_rollback_for_multiple(self):
        attrs, _ = parse_transactional_args(
            "rollbackFor={IOException.class, RuntimeException.class}"
        )
        assert set(attrs["rollback_for"]) == {"IOException", "RuntimeException"}

    def test_no_rollback_for(self):
        attrs, _ = parse_transactional_args("noRollbackFor=ValidationException.class")
        assert attrs["no_rollback_for"] == ["ValidationException"]

    def test_combined_multiple_attrs(self):
        args = "propagation=Propagation.REQUIRES_NEW, readOnly=false, timeout=60"
        attrs, conf = parse_transactional_args(args)
        assert attrs["propagation"] == "REQUIRES_NEW"
        assert attrs["read_only"] is False
        assert attrs["timeout"] == 60
        assert conf == "high"

    def test_transaction_manager_ref_only(self):
        # bare string = transactionManager name — not an attribute we parse
        attrs, conf = parse_transactional_args('"customTxManager"')
        assert attrs == {}
        assert conf == "medium"

    def test_unknown_propagation_defaults(self):
        attrs, _ = parse_transactional_args("propagation=Propagation.UNKNOWN_VALUE")
        assert attrs["propagation"] == PROPAGATION_DEFAULT

    def test_unknown_isolation_defaults(self):
        attrs, _ = parse_transactional_args("isolation=UNKNOWN")
        assert attrs["isolation"] == ISOLATION_DEFAULT


# ---------------------------------------------------------------------------
# B — TransactionBoundary model
# ---------------------------------------------------------------------------

class TestTransactionBoundary:
    def _make(self, **kw) -> TransactionBoundary:
        defaults = dict(
            symbol="com.example.MyService#doWork",
            scope="method",
            modifiers=["public"],
        )
        defaults.update(kw)
        return TransactionBoundary(**defaults)

    def test_proxy_bypass_private(self):
        b = self._make(modifiers=["private"])
        assert b.is_proxy_bypass_risk is True

    def test_proxy_bypass_final(self):
        b = self._make(modifiers=["public", "final"])
        assert b.is_proxy_bypass_risk is True

    def test_no_proxy_bypass_public(self):
        b = self._make(modifiers=["public"])
        assert b.is_proxy_bypass_risk is False

    def test_proxy_bypass_class_scope_not_flagged(self):
        b = self._make(scope="class", modifiers=["private"])
        assert b.is_proxy_bypass_risk is False

    def test_defaults(self):
        b = self._make()
        assert b.propagation == PROPAGATION_DEFAULT
        assert b.isolation == ISOLATION_DEFAULT
        assert b.timeout == TIMEOUT_DEFAULT
        assert b.read_only is False

    def test_to_dict_minimal(self):
        b = self._make()
        d = b.to_dict()
        assert d["symbol"] == "com.example.MyService#doWork"
        assert d["propagation"] == PROPAGATION_DEFAULT
        assert "modifiers" in d

    def test_to_dict_excludes_default_timeout(self):
        b = self._make()
        assert "timeout" not in b.to_dict()

    def test_to_dict_includes_non_default_timeout(self):
        b = self._make(timeout=30)
        assert b.to_dict()["timeout"] == 30


# ---------------------------------------------------------------------------
# I — build_tx_index
# ---------------------------------------------------------------------------

class TestBuildTxIndex:
    def test_empty_ir_returns_empty_index(self):
        cir = _FakeCIR([])
        idx = build_tx_index(cir)
        assert idx.by_symbol == {}
        assert idx.class_level == {}

    def test_non_tx_node_ignored(self):
        cir = _FakeCIR([_non_tx_node("com.example.Foo#bar")])
        idx = build_tx_index(cir)
        assert "com.example.Foo#bar" not in idx.by_symbol

    def test_method_level_boundary(self):
        cir = _FakeCIR([_tx_node("com.example.Service#save", symbol_kind="method")])
        idx = build_tx_index(cir)
        assert "com.example.Service#save" in idx.by_symbol
        b = idx.by_symbol["com.example.Service#save"]
        assert b.scope == "method"
        assert b.propagation == PROPAGATION_DEFAULT

    def test_class_level_boundary(self):
        cir = _FakeCIR([_tx_node("com.example.Service", symbol_kind="class")])
        idx = build_tx_index(cir)
        assert "com.example.Service" in idx.class_level
        assert idx.by_symbol["com.example.Service"].scope == "class"

    def test_propagation_parsed_from_raw_args(self):
        node = _tx_node(
            "com.example.Service#create",
            raw_args="propagation=Propagation.REQUIRES_NEW",
        )
        cir = _FakeCIR([node])
        idx = build_tx_index(cir)
        assert idx.by_symbol["com.example.Service#create"].propagation == "REQUIRES_NEW"

    def test_readonly_parsed(self):
        node = _tx_node("com.example.Repo#findAll", raw_args="readOnly=true")
        cir = _FakeCIR([node])
        idx = build_tx_index(cir)
        assert idx.by_symbol["com.example.Repo#findAll"].read_only is True

    def test_private_modifier_captured(self):
        node = _tx_node(
            "com.example.Service#internal",
            modifiers=["private"],
        )
        cir = _FakeCIR([node])
        idx = build_tx_index(cir)
        b = idx.by_symbol["com.example.Service#internal"]
        assert "private" in b.modifiers
        assert b.is_proxy_bypass_risk is True

    def test_final_modifier_proxy_bypass(self):
        node = _tx_node(
            "com.example.Service#calc",
            modifiers=["public", "final"],
        )
        cir = _FakeCIR([node])
        idx = build_tx_index(cir)
        b = idx.by_symbol["com.example.Service#calc"]
        assert b.is_proxy_bypass_risk is True

    def test_by_class_populated(self):
        nodes = [
            _tx_node("com.example.Service#save", symbol_kind="method"),
            _tx_node("com.example.Service#delete", symbol_kind="method"),
        ]
        cir = _FakeCIR(nodes)
        idx = build_tx_index(cir)
        assert len(idx.by_class.get("com.example.Service", [])) == 2

    def test_mixed_class_and_method_boundaries(self):
        nodes = [
            _tx_node("com.example.Service", symbol_kind="class"),
            _tx_node(
                "com.example.Service#save",
                symbol_kind="method",
                raw_args="propagation=Propagation.REQUIRES_NEW",
            ),
        ]
        cir = _FakeCIR(nodes)
        idx = build_tx_index(cir)
        assert "com.example.Service" in idx.class_level
        assert "com.example.Service#save" in idx.by_symbol
        # Method has override
        assert idx.by_symbol["com.example.Service#save"].propagation == "REQUIRES_NEW"
        # Class-level has default
        assert idx.class_level["com.example.Service"].propagation == PROPAGATION_DEFAULT

    def test_never_raises_on_malformed_ir(self):
        class BadCIR:
            _raw_ir = None
            cir_hash = ""
        idx = build_tx_index(BadCIR())  # type: ignore[arg-type]
        assert isinstance(idx, TransactionBoundaryIndex)

    def test_build_time_recorded(self):
        cir = _FakeCIR([_tx_node("com.example.X#m")])
        idx = build_tx_index(cir)
        assert idx.build_time_ms >= 0


# ---------------------------------------------------------------------------
# E — effective_boundary resolution
# ---------------------------------------------------------------------------

class TestEffectiveBoundary:
    def _make_index(self) -> TransactionBoundaryIndex:
        nodes = [
            _tx_node("com.example.Service", symbol_kind="class"),
            _tx_node(
                "com.example.Service#create",
                raw_args="propagation=Propagation.REQUIRES_NEW",
            ),
        ]
        return build_tx_index(_FakeCIR(nodes))

    def test_method_level_takes_precedence(self):
        idx = self._make_index()
        b = idx.effective_boundary("com.example.Service#create")
        assert b is not None
        assert b.propagation == "REQUIRES_NEW"

    def test_class_level_inherited_when_no_method_override(self):
        idx = self._make_index()
        b = idx.effective_boundary("com.example.Service#findAll")
        assert b is not None
        assert b.propagation == PROPAGATION_DEFAULT  # class-level default

    def test_none_when_no_tx_at_all(self):
        idx = build_tx_index(_FakeCIR([]))
        b = idx.effective_boundary("com.example.NoTx#doWork")
        assert b is None


# ---------------------------------------------------------------------------
# S — stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_counts(self):
        nodes = [
            _tx_node("com.example.Service", symbol_kind="class"),
            _tx_node("com.example.Service#save"),
            _tx_node("com.example.Repo#findAll", raw_args="readOnly=true"),
            _tx_node(
                "com.example.Service#create",
                raw_args="propagation=Propagation.REQUIRES_NEW",
            ),
        ]
        idx = build_tx_index(_FakeCIR(nodes))
        s = idx.stats()
        assert s["total"] == 4
        assert s["class_level"] == 1
        assert s["method_level"] == 3
        assert s["read_only_count"] == 1
        assert s["propagations"]["REQUIRES_NEW"] == 1
        assert s["propagations"]["REQUIRED"] == 3
