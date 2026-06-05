"""test_spring_tx_analyzer.py — Unit tests for spring_tx_analyzer.py Phase 2.

Coverage:
  F  SpringFinding schema + ID determinism
  R  SpringAuditResult.finalize() summary
  TX1  TX-001 proxy bypass (private + final, positive + negative)
  TX2  TX-002 REQUIRES_NEW nested within REQUIRED
  TX3  TX-003 readOnly=true propagating to write callee
  TX4  TX-004 NOT_SUPPORTED / NEVER within active TX chain
  TX5  TX-005 exception swallowing (mocked source)
  E  TxPatternEngine deduplication, pattern isolation, never-raises
  A  run_tx_audit integration smoke
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.spring_findings import SpringAuditResult, SpringFinding
from sourcecode.spring_semantic import TransactionBoundaryIndex, build_tx_index
from sourcecode.spring_tx_analyzer import (
    TxPatternEngine,
    _TX001ProxyBypass,
    _TX002RequiresNewNested,
    _TX003ReadOnlyWritePropagation,
    _TX004TxSuspensionRisk,
    _TX005ExceptionSwallowing,
    run_tx_audit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeCIR:
    """Minimal CIR-alike for tests."""

    def __init__(
        self,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
    ):
        self._raw_ir = {
            "graph": {
                "nodes": nodes or [],
                "edges": edges or [],
            }
        }
        self.cir_hash = "cafebabe00000000"
        self.call_graph = edges or []
        self.symbols: list[str] = [n.get("fqn", "") for n in (nodes or [])]

    def add_node(self, fqn, kind="method", anns=None, ann_values=None, modifiers=None, src="Foo.java"):
        node = {
            "fqn": fqn,
            "symbol_kind": kind,
            "annotations": anns or [],
            "annotation_values": ann_values or {},
            "modifiers": modifiers or [],
            "source_file": src,
        }
        self._raw_ir["graph"]["nodes"].append(node)
        self.symbols.append(fqn)

    def add_edge(self, frm, to, etype="calls"):
        edge = {"from": frm, "to": to, "type": etype}
        self._raw_ir["graph"]["edges"].append(edge)
        self.call_graph.append(edge)


def _private_tx_node(fqn, src="Service.java"):
    return {
        "fqn": fqn,
        "symbol_kind": "method",
        "annotations": ["@Transactional"],
        "annotation_values": {"@Transactional": ""},
        "modifiers": ["private"],
        "source_file": src,
    }


def _public_tx_node(fqn, raw_args="", modifiers=None, src="Service.java"):
    return {
        "fqn": fqn,
        "symbol_kind": "method",
        "annotations": ["@Transactional"],
        "annotation_values": {"@Transactional": raw_args},
        "modifiers": modifiers or ["public"],
        "source_file": src,
    }


def _class_tx_node(fqn, raw_args="", src="Service.java"):
    return {
        "fqn": fqn,
        "symbol_kind": "class",
        "annotations": ["@Transactional"],
        "annotation_values": {"@Transactional": raw_args},
        "modifiers": [],
        "source_file": src,
    }


# ---------------------------------------------------------------------------
# F — SpringFinding schema + ID determinism
# ---------------------------------------------------------------------------

class TestSpringFinding:
    def test_make_id_deterministic(self):
        id1 = SpringFinding.make_id("TX-001", "com.example.Foo#bar")
        id2 = SpringFinding.make_id("TX-001", "com.example.Foo#bar")
        assert id1 == id2
        assert id1.startswith("TX-001-")

    def test_make_id_differs_by_pattern(self):
        id1 = SpringFinding.make_id("TX-001", "com.example.Foo#bar")
        id2 = SpringFinding.make_id("TX-002", "com.example.Foo#bar")
        assert id1 != id2

    def test_make_id_differs_by_symbol(self):
        id1 = SpringFinding.make_id("TX-001", "com.example.Foo#bar")
        id2 = SpringFinding.make_id("TX-001", "com.example.Foo#baz")
        assert id1 != id2

    def test_to_dict_required_fields(self):
        f = SpringFinding(
            id="TX-001-abc",
            pattern_id="TX-001",
            category="tx",
            severity="high",
            confidence="high",
            title="test",
            symbol="Foo#bar",
            source_file="Foo.java",
            evidence={"k": "v"},
            explanation="test explanation",
            fix_hint="fix it",
        )
        d = f.to_dict()
        for key in ("id", "pattern_id", "category", "severity", "confidence",
                    "title", "symbol", "source_file", "evidence", "explanation", "fix_hint"):
            assert key in d

    def test_to_dict_omits_empty_limitations(self):
        f = SpringFinding(
            id="x", pattern_id="TX-001", category="tx", severity="high",
            confidence="high", title="t", symbol="S", source_file="F",
            evidence={}, explanation="e", fix_hint="f",
        )
        assert "limitations" not in f.to_dict()

    def test_to_dict_includes_limitations_when_present(self):
        f = SpringFinding(
            id="x", pattern_id="TX-001", category="tx", severity="high",
            confidence="high", title="t", symbol="S", source_file="F",
            evidence={}, explanation="e", fix_hint="f",
            limitations=["note"],
        )
        assert "limitations" in f.to_dict()


# ---------------------------------------------------------------------------
# R — SpringAuditResult.finalize()
# ---------------------------------------------------------------------------

class TestSpringAuditResult:
    def _make_result(self, findings):
        r = SpringAuditResult(
            repo_id="abc",
            spring_detected=True,
            findings=findings,
        )
        return r.finalize()

    def _finding(self, severity="high", confidence="high", category="tx"):
        return SpringFinding(
            id=f"X-{severity}-{confidence}",
            pattern_id="TX-001",
            category=category,
            severity=severity,
            confidence=confidence,
            title="t", symbol="S", source_file="F",
            evidence={}, explanation="e", fix_hint="f",
        )

    def test_empty_findings_summary(self):
        r = self._make_result([])
        assert r.summary["total_findings"] == 0
        assert r.summary["confidence_level"] == "high"

    def test_severity_counts(self):
        findings = [
            self._finding("high"), self._finding("high"),
            self._finding("medium"), self._finding("low"),
        ]
        r = self._make_result(findings)
        assert r.summary["by_severity"]["high"] == 2
        assert r.summary["by_severity"]["medium"] == 1
        assert r.summary["by_severity"]["low"] == 1

    def test_category_counts(self):
        findings = [self._finding(category="tx"), self._finding(category="security")]
        r = self._make_result(findings)
        assert r.summary["by_category"]["tx"] == 1
        assert r.summary["by_category"]["security"] == 1

    def test_confidence_level_all_high(self):
        r = self._make_result([self._finding("high", "high")])
        assert r.summary["confidence_level"] == "high"

    def test_confidence_level_medium_when_medium_high(self):
        r = self._make_result([self._finding("high", "medium")])
        assert r.summary["confidence_level"] == "medium"

    def test_to_dict_structure(self):
        r = self._make_result([])
        d = r.to_dict()
        assert d["schema_version"] == "1.0"
        assert "findings" in d
        assert "summary" in d
        assert "limitations" in d

    def test_generated_at_set_by_finalize(self):
        r = self._make_result([])
        assert r.generated_at != ""


# ---------------------------------------------------------------------------
# TX1 — TX-001: proxy bypass
# ---------------------------------------------------------------------------

class TestTX001:
    def _run(self, nodes):
        cir = _FakeCIR(nodes)
        tx_index = build_tx_index(cir)
        return _TX001ProxyBypass().analyze(cir, tx_index, root=None)

    def test_private_method_emits_finding(self):
        findings = self._run([_private_tx_node("com.example.Service#doWork")])
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_id == "TX-001"
        assert f.severity == "high"
        assert f.confidence == "high"
        assert f.symbol == "com.example.Service#doWork"
        assert f.evidence["modifier"] == "private"

    def test_final_method_emits_finding(self):
        findings = self._run([_public_tx_node(
            "com.example.Service#calc",
            modifiers=["public", "final"],
        )])
        assert len(findings) == 1
        assert findings[0].evidence["modifier"] == "final"

    def test_public_method_no_finding(self):
        findings = self._run([_public_tx_node("com.example.Service#save")])
        assert findings == []

    def test_class_level_tx_no_finding(self):
        findings = self._run([_class_tx_node("com.example.Service")])
        assert findings == []

    def test_multiple_private_methods(self):
        findings = self._run([
            _private_tx_node("com.example.S#a"),
            _private_tx_node("com.example.S#b"),
            _public_tx_node("com.example.S#c"),
        ])
        assert len(findings) == 2
        symbols = {f.symbol for f in findings}
        assert "com.example.S#a" in symbols
        assert "com.example.S#b" in symbols

    def test_id_deterministic_across_runs(self):
        findings1 = self._run([_private_tx_node("com.example.S#x")])
        findings2 = self._run([_private_tx_node("com.example.S#x")])
        assert findings1[0].id == findings2[0].id

    def test_explanation_mentions_symbol(self):
        findings = self._run([_private_tx_node("com.example.OrderService#processPayment")])
        assert "processPayment" in findings[0].explanation

    def test_related_symbol_is_class(self):
        findings = self._run([_private_tx_node("com.example.Service#internal")])
        assert findings[0].related_symbols == ["com.example.Service"]


# ---------------------------------------------------------------------------
# TX2 — TX-002: REQUIRES_NEW nested within REQUIRED
# ---------------------------------------------------------------------------

class TestTX002:
    def _build_cir(self, caller_args, callee_args, caller_fqn, callee_fqn):
        nodes = [
            _public_tx_node(caller_fqn, raw_args=caller_args),
            _public_tx_node(callee_fqn, raw_args=callee_args),
        ]
        edges = [{"from": caller_fqn, "to": callee_fqn, "type": "calls"}]
        cir = _FakeCIR(nodes, edges)
        return cir

    def _run(self, cir):
        tx_index = build_tx_index(cir)
        return _TX002RequiresNewNested().analyze(cir, tx_index, root=None)

    def test_required_calls_requires_new_emits_finding(self):
        cir = self._build_cir(
            "",  # caller REQUIRED (default)
            "propagation=Propagation.REQUIRES_NEW",
            "com.example.A#outer",
            "com.example.B#inner",
        )
        findings = self._run(cir)
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_id == "TX-002"
        assert f.evidence["inner_propagation"] == "REQUIRES_NEW"
        assert f.evidence["outer_propagation"] == "REQUIRED"

    def test_supports_calls_requires_new_emits_finding(self):
        cir = self._build_cir(
            "propagation=Propagation.SUPPORTS",
            "propagation=Propagation.REQUIRES_NEW",
            "com.example.A#outer",
            "com.example.B#inner",
        )
        findings = self._run(cir)
        assert len(findings) == 1

    def test_requires_new_calls_required_no_finding(self):
        cir = self._build_cir(
            "propagation=Propagation.REQUIRES_NEW",
            "",  # callee REQUIRED
            "com.example.A#outer",
            "com.example.B#inner",
        )
        findings = self._run(cir)
        assert findings == []

    def test_required_calls_required_no_finding(self):
        cir = self._build_cir("", "", "com.example.A#outer", "com.example.B#inner")
        findings = self._run(cir)
        assert findings == []

    def test_no_tx_boundaries_no_finding(self):
        cir = _FakeCIR([], [{"from": "A#m", "to": "B#m", "type": "calls"}])
        tx_index = build_tx_index(cir)
        findings = _TX002RequiresNewNested().analyze(cir, tx_index, root=None)
        assert findings == []

    def test_dedup_same_pair(self):
        # Two paths leading to same caller→callee pair should produce one finding
        nodes = [
            _public_tx_node("com.example.A#outer", raw_args=""),
            _public_tx_node("com.example.B#inner", raw_args="propagation=Propagation.REQUIRES_NEW"),
        ]
        edges = [
            {"from": "com.example.A#outer", "to": "com.example.B#inner", "type": "calls"},
            {"from": "com.example.A#outer", "to": "com.example.B#inner", "type": "injects"},
        ]
        cir = _FakeCIR(nodes, edges)
        findings = self._run(cir)
        ids = [f.id for f in findings]
        assert len(ids) == len(set(ids))

    def test_confidence_is_medium(self):
        cir = self._build_cir(
            "",
            "propagation=Propagation.REQUIRES_NEW",
            "com.A#m1",
            "com.B#m2",
        )
        findings = self._run(cir)
        assert findings[0].confidence == "medium"


# ---------------------------------------------------------------------------
# TX3 — TX-003: readOnly propagating to write callee
# ---------------------------------------------------------------------------

class TestTX003:
    def _run(self, caller_fqn, callee_fqn, caller_args, callee_args):
        nodes = [
            _public_tx_node(caller_fqn, raw_args=caller_args),
            _public_tx_node(callee_fqn, raw_args=callee_args),
        ]
        edges = [{"from": caller_fqn, "to": callee_fqn, "type": "calls"}]
        cir = _FakeCIR(nodes, edges)
        tx_index = build_tx_index(cir)
        return _TX003ReadOnlyWritePropagation().analyze(cir, tx_index, root=None)

    def test_readonly_caller_write_callee_emits_finding(self):
        findings = self._run(
            "com.example.QueryService#findAll",
            "com.example.Repo#save",
            "readOnly=true",
            "",
        )
        assert len(findings) == 1
        assert findings[0].pattern_id == "TX-003"

    def test_both_readonly_no_finding(self):
        findings = self._run(
            "com.example.Q#findAll",
            "com.example.R#findById",
            "readOnly=true",
            "readOnly=true",
        )
        assert findings == []

    def test_write_caller_no_finding(self):
        findings = self._run(
            "com.example.S#save",
            "com.example.R#save",
            "",   # REQUIRED, not readOnly
            "",
        )
        assert findings == []

    def test_readonly_calls_non_write_method_no_finding(self):
        # callee name doesn't match write pattern
        findings = self._run(
            "com.example.Q#execute",
            "com.example.R#process",
            "readOnly=true",
            "",
        )
        assert findings == []

    def test_write_method_names_detected(self):
        for method in ("save", "create", "delete", "update", "persist", "merge",
                       "remove", "insert", "store", "write", "put", "add", "modify"):
            findings = self._run(
                "com.example.Q#findAll",
                f"com.example.R#{method}",
                "readOnly=true",
                "",
            )
            assert len(findings) == 1, f"Expected finding for method name: {method}"


# ---------------------------------------------------------------------------
# TX4 — TX-004: NOT_SUPPORTED / NEVER within active TX
# ---------------------------------------------------------------------------

class TestTX004:
    def _run(self, caller_args, callee_args, caller_fqn, callee_fqn):
        nodes = [
            _public_tx_node(caller_fqn, raw_args=caller_args),
            _public_tx_node(callee_fqn, raw_args=callee_args),
        ]
        edges = [{"from": caller_fqn, "to": callee_fqn, "type": "calls"}]
        cir = _FakeCIR(nodes, edges)
        tx_index = build_tx_index(cir)
        return _TX004TxSuspensionRisk().analyze(cir, tx_index, root=None)

    def test_required_calls_not_supported(self):
        findings = self._run(
            "",
            "propagation=Propagation.NOT_SUPPORTED",
            "com.A#outer",
            "com.B#inner",
        )
        assert len(findings) == 1
        assert findings[0].evidence["inner_propagation"] == "NOT_SUPPORTED"

    def test_required_calls_never_is_high_severity(self):
        findings = self._run(
            "",
            "propagation=Propagation.NEVER",
            "com.A#outer",
            "com.B#inner",
        )
        assert len(findings) == 1
        assert findings[0].severity == "high"

    def test_required_calls_not_supported_is_medium(self):
        findings = self._run(
            "",
            "propagation=Propagation.NOT_SUPPORTED",
            "com.A#outer",
            "com.B#inner",
        )
        assert findings[0].severity == "medium"

    def test_no_tx_no_finding(self):
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        findings = _TX004TxSuspensionRisk().analyze(cir, tx_index, root=None)
        assert findings == []

    def test_not_supported_calling_required_no_finding(self):
        # NOT_SUPPORTED caller is not an active TX boundary — no finding
        findings = self._run(
            "propagation=Propagation.NOT_SUPPORTED",
            "",
            "com.A#outer",
            "com.B#inner",
        )
        assert findings == []


# ---------------------------------------------------------------------------
# TX5 — TX-005: exception swallowing
# ---------------------------------------------------------------------------

class TestTX005:
    def _run_with_source(self, source_code: str, method_fqn: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src_file = root / "Service.java"
            src_file.write_text(source_code, encoding="utf-8")

            nodes = [_public_tx_node(method_fqn, src="Service.java")]
            cir = _FakeCIR(nodes)
            tx_index = build_tx_index(cir)
            return _TX005ExceptionSwallowing().analyze(cir, tx_index, root=root)

    def test_swallowed_exception_emits_finding(self):
        source = dedent("""\
            @Transactional
            public void doWork() {
                try {
                    repository.save(entity);
                } catch (Exception e) {
                    logger.error("Failed", e);
                }
            }
        """)
        findings = self._run_with_source(source, "com.example.Service#doWork")
        assert len(findings) == 1
        assert findings[0].pattern_id == "TX-005"
        assert findings[0].confidence == "medium"

    def test_rethrown_exception_no_finding(self):
        source = dedent("""\
            @Transactional
            public void doWork() {
                try {
                    repository.save(entity);
                } catch (Exception e) {
                    logger.error("Failed", e);
                    throw new RuntimeException(e);
                }
            }
        """)
        findings = self._run_with_source(source, "com.example.Service#doWork")
        assert findings == []

    def test_no_root_returns_empty(self):
        nodes = [_public_tx_node("com.example.S#m")]
        cir = _FakeCIR(nodes)
        tx_index = build_tx_index(cir)
        findings = _TX005ExceptionSwallowing().analyze(cir, tx_index, root=None)
        assert findings == []

    def test_no_catch_no_finding(self):
        source = dedent("""\
            @Transactional
            public void doWork() {
                repository.save(entity);
            }
        """)
        findings = self._run_with_source(source, "com.example.Service#doWork")
        assert findings == []

    def test_other_method_swallow_no_finding_for_clean_method(self):
        # BUG-005 regression: only the method body is scanned, not the whole file.
        # A swallowed catch in a sibling method must NOT flag a clean TX method.
        source = dedent("""\
            @Transactional
            public void doWork() {
                repository.save(entity);
            }

            @Transactional
            public void doOtherWork() {
                try {
                    riskyOp();
                } catch (Exception e) {
                    logger.error("Failed", e);
                }
            }
        """)
        findings = self._run_with_source(source, "com.example.Service#doWork")
        assert findings == [], "sibling method swallow must not flag doWork"

    def test_only_affected_method_flagged_not_sibling(self):
        # BUG-005 regression: sibling with swallow gets finding; clean sibling does not.
        source = dedent("""\
            @Transactional
            public void cleanMethod() {
                repository.save(entity);
            }

            @Transactional
            public void swallowMethod() {
                try {
                    riskyOp();
                } catch (Exception e) {
                    LOG.warn("oops", e);
                }
            }
        """)
        findings_clean = self._run_with_source(source, "com.example.Service#cleanMethod")
        findings_swallow = self._run_with_source(source, "com.example.Service#swallowMethod")
        assert findings_clean == [], "cleanMethod must not be flagged"
        assert len(findings_swallow) == 1, "swallowMethod must be flagged"

    def test_readonly_transaction_no_finding(self):
        # BUG-001 regression: TX-005 was firing on readOnly=true methods.
        # readOnly transactions do not write data — swallowed exceptions cannot
        # cause dirty commits, so TX-005 is not applicable.
        source = dedent("""\
            @Transactional(readOnly = true)
            public String getUnknownConcept() {
                try {
                    return dao.findConcept("unknown");
                } catch (Exception e) {
                    log.warn("concept not found", e);
                    return null;
                }
            }
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src_file = root / "Service.java"
            src_file.write_text(source, encoding="utf-8")
            nodes = [{
                "fqn": "org.openmrs.api.impl.ConceptServiceImpl#getUnknownConcept",
                "symbol_kind": "method",
                "annotations": ["@Transactional"],
                "annotation_values": {"@Transactional": "readOnly = true"},
                "modifiers": ["public"],
                "source_file": "Service.java",
            }]
            cir = _FakeCIR(nodes)
            tx_index = build_tx_index(cir)
            findings = _TX005ExceptionSwallowing().analyze(cir, tx_index, root=root)
        assert findings == [], "readOnly=true method must not emit TX-005"

    def test_recovery_return_in_catch_no_finding(self):
        # BUG-002 regression: _CATCH_SWALLOW_RE used [^}]* which terminated at the
        # first nested '}' inside the catch block, missing a `return method()` at
        # the end. A catch that logs AND ends with a non-trivial method-call return
        # indicates recovery, not silent swallowing — must not emit TX-005.
        source = dedent("""\
            @Transactional
            public String loadOrCreate(String key) {
                try {
                    return dao.find(key);
                } catch (NoResultException e) {
                    if (LOG.isDebugEnabled()) {
                        LOG.debug("Not found: " + key);
                    }
                    dao.create(key);
                    return loadOrCreate(key);
                }
            }
        """)
        findings = self._run_with_source(
            source, "com.example.Service#loadOrCreate"
        )
        assert findings == [], "catch with recovery return must not emit TX-005"


# ---------------------------------------------------------------------------
# E — TxPatternEngine
# ---------------------------------------------------------------------------

class TestTxPatternEngine:
    def test_empty_cir_returns_empty(self):
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        engine = TxPatternEngine()
        findings = engine.analyze(cir, tx_index)
        assert findings == []

    def test_deduplication_by_id(self):
        # Two patterns that would emit same ID should deduplicate
        class _DupePattern:
            pattern_id = "TX-001"
            severity = "high"

            def analyze(self, cir, tx_index, root):
                return [SpringFinding(
                    id="TX-001-dupetest",
                    pattern_id="TX-001", category="tx", severity="high",
                    confidence="high", title="t", symbol="S", source_file="F",
                    evidence={}, explanation="e", fix_hint="f",
                )]

        engine = TxPatternEngine(patterns=[_DupePattern(), _DupePattern()])  # type: ignore
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        findings = engine.analyze(cir, tx_index)
        assert len(findings) == 1

    def test_pattern_exception_does_not_crash_engine(self):
        class _BrokenPattern:
            pattern_id = "TX-999"
            severity = "high"

            def analyze(self, cir, tx_index, root):
                raise RuntimeError("broken")

        engine = TxPatternEngine(patterns=[_BrokenPattern()])  # type: ignore
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        findings = engine.analyze(cir, tx_index)
        assert findings == []

    def test_findings_sorted_by_severity(self):
        class _MixedPattern:
            pattern_id = "TX-001"
            severity = "high"

            def analyze(self, cir, tx_index, root):
                return [
                    SpringFinding(id="x1", pattern_id="TX-001", category="tx",
                                  severity="medium", confidence="high", title="t",
                                  symbol="B", source_file="F", evidence={},
                                  explanation="e", fix_hint="f"),
                    SpringFinding(id="x2", pattern_id="TX-001", category="tx",
                                  severity="high", confidence="high", title="t",
                                  symbol="A", source_file="F", evidence={},
                                  explanation="e", fix_hint="f"),
                ]

        engine = TxPatternEngine(patterns=[_MixedPattern()])  # type: ignore
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        findings = engine.analyze(cir, tx_index)
        assert findings[0].severity == "high"
        assert findings[1].severity == "medium"

    def test_custom_patterns_override_defaults(self):
        class _CountingPattern:
            pattern_id = "TX-001"
            severity = "high"
            count = 0

            def analyze(self, cir, tx_index, root):
                _CountingPattern.count += 1
                return []

        p = _CountingPattern()
        engine = TxPatternEngine(patterns=[p])  # type: ignore
        cir = _FakeCIR()
        tx_index = build_tx_index(cir)
        engine.analyze(cir, tx_index)
        assert _CountingPattern.count == 1


# ---------------------------------------------------------------------------
# A — run_tx_audit integration smoke
# ---------------------------------------------------------------------------

class TestRunTxAudit:
    def test_empty_repo_no_findings(self):
        cir = _FakeCIR()
        result = run_tx_audit(cir)
        assert isinstance(result, SpringAuditResult)
        assert result.spring_detected is False  # empty CIR has no Spring beans or @Transactional
        assert result.findings == []
        assert result.summary["total_findings"] == 0

    def test_private_tx_method_found(self):
        cir = _FakeCIR([_private_tx_node("com.example.S#m")])
        result = run_tx_audit(cir)
        assert any(f.pattern_id == "TX-001" for f in result.findings)

    def test_result_has_metadata(self):
        cir = _FakeCIR([_private_tx_node("com.example.S#m")])
        result = run_tx_audit(cir)
        assert "tx_boundaries_found" in result.metadata
        assert "analysis_time_ms" in result.metadata

    def test_min_severity_filters(self):
        cir = _FakeCIR([_private_tx_node("com.example.S#m")])
        result_all = run_tx_audit(cir, min_severity="low")
        result_high = run_tx_audit(cir, min_severity="high")
        # TX-001 is high severity — present in both
        assert any(f.pattern_id == "TX-001" for f in result_all.findings)
        assert any(f.pattern_id == "TX-001" for f in result_high.findings)

    def test_to_dict_is_json_serializable(self):
        import json
        cir = _FakeCIR([_private_tx_node("com.example.S#m")])
        result = run_tx_audit(cir)
        d = result.to_dict()
        raw = json.dumps(d)
        assert '"TX-001"' in raw
