"""test_spring_event_topology.py — Unit tests for EventTopologyOrchestrator.

Coverage:
  EVT-01  Publishers found for exact event class FQN
  EVT-02  Spring @EventListener consumers found
  EVT-03  @TransactionalEventListener consumer — TX phase extracted
  EVT-04  BEFORE_COMMIT phase → risk=high
  EVT-05  No publishers/consumers → confidence=low, risk=low
  EVT-06  Event class not found → not_found resolution
  EVT-07  Event graph edges: publishes + consumes
  EVT-08  Level-2 propagation: consumer re-publishes secondary event
  EVT-09  Risk fanout > 5 → high
  EVT-10  Risk 2–5 consumers → medium
  EVT-11  Risk ≤1 consumer → low
  EVT-12  Kafka/Rabbit counts in metadata via raw IR node scan
  EVT-13  EventTopologyResult.to_dict() — all required keys, JSON-serializable
  EVT-14  run_event_topology() no model → builds internally, no raise
  EVT-15  TX context after_commit_consumers populated
  EVT-16  Simple class name resolution (class_expanded)
  EVT-17  TX context before_commit_risks populated
  EVT-18  Multiple publishers listed
  EVT-19  Partial resolution emits ambiguity warning
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.cir_graphs import ImplementationGraph, InjectionGraph
from sourcecode.spring_event_topology import (
    EventConsumer,
    EventTopologyOrchestrator,
    EventTopologyResult,
    _compute_event_risk,
    _extract_tx_phase,
    _resolve_event_symbol,
    run_event_topology,
)
from sourcecode.spring_model import (
    BeanGraph,
    CallAdjacency,
    EndpointIndex,
    EventGraph,
    InheritanceGraph,
    SpringSemanticModel,
)
from sourcecode.spring_semantic import TransactionBoundaryIndex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVT_CLS = "com.example.OrderCreatedEvent"
_PUBLISHER = "com.example.OrderService"
_LISTENER_A = "com.example.EmailNotifier#onOrderCreated"
_LISTENER_B = "com.example.AuditService#handleOrderCreated"

_SEC_EVT = "com.example.SecondaryEvent"
_SEC_LISTENER = "com.example.SecondaryConsumer#onSecondary"


def _pub_edge(from_sym: str, to_sym: str) -> dict:
    return {"from": from_sym, "to": to_sym, "type": "publishes_event", "confidence": "medium"}


def _listen_edge(from_sym: str, to_sym: str, ann: str = "@EventListener") -> dict:
    return {
        "from": from_sym,
        "to": to_sym,
        "type": "listens_to_event",
        "confidence": "high",
        "evidence": {"type": "annotation", "value": ann},
    }


def _node(fqn: str, annotations: list[str], ann_values: Optional[dict] = None,
          source_file: str = "") -> dict:
    return {
        "fqn": fqn,
        "annotations": annotations,
        "annotation_values": ann_values or {},
        "source_file": source_file or f"{fqn.replace('.', '/').replace('#', '_')}.java",
        "type": "method" if "#" in fqn else "class",
    }


class _FakeCIR:
    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        call_graph: Optional[list[dict]] = None,
        raw_nodes: Optional[list[dict]] = None,
    ):
        self.symbols = symbols or []
        self.call_graph = call_graph or []
        self.dependencies: list[dict] = []
        self.reverse_graph: dict = {}
        self.files: list[str] = []
        self.metadata: dict = {}
        self.endpoints: list = []
        self.cir_hash = "deadbeef00000000"
        self.implementation_graph = ImplementationGraph.build([], set())
        self.injection_graph = InjectionGraph.build([])
        nodes = raw_nodes or []
        self._raw_ir = {"graph": {"nodes": nodes, "edges": self.call_graph}}


def _make_model(
    call_graph: Optional[list[dict]] = None,
    tx_index: Optional[TransactionBoundaryIndex] = None,
) -> SpringSemanticModel:
    cg = call_graph or []

    class _MinCIR:
        pass

    _c = _MinCIR()
    _c.call_graph = cg  # type: ignore[attr-defined]

    eg = EventGraph.build(_c)  # type: ignore[arg-type]
    return SpringSemanticModel(
        tx_index=tx_index or TransactionBoundaryIndex(),
        call_adj=CallAdjacency(),
        inheritance=InheritanceGraph(),
        bean_graph=BeanGraph(),
        endpoint_index=EndpointIndex(),
        event_graph=eg,
        build_time_ms=0.0,
    )


# ---------------------------------------------------------------------------
# EVT-01  Publishers found
# ---------------------------------------------------------------------------

class TestPublishersFound:
    def test_evt01_publishers_listed(self):
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS)]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER], call_graph=cg)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.resolution == "exact"
        assert _PUBLISHER in result.publishers

    def test_evt18_multiple_publishers(self):
        pub2 = "com.example.InventoryService"
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS), _pub_edge(pub2, _EVT_CLS)]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER, pub2], call_graph=cg)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert _PUBLISHER in result.publishers
        assert pub2 in result.publishers
        assert len(result.publishers) == 2


# ---------------------------------------------------------------------------
# EVT-02  Spring @EventListener consumers found
# ---------------------------------------------------------------------------

class TestEventListenerConsumers:
    def test_evt02_spring_consumer_found(self):
        cg = [
            _pub_edge(_PUBLISHER, _EVT_CLS),
            _listen_edge(_LISTENER_A, _EVT_CLS),
        ]
        nodes = [_node(_LISTENER_A, ["@EventListener"])]
        cir = _FakeCIR(
            symbols=[_EVT_CLS, _PUBLISHER, _LISTENER_A],
            call_graph=cg,
            raw_nodes=nodes,
        )
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        fqns = [c["fqn"] for c in result.consumers]
        assert _LISTENER_A in fqns

    def test_evt02_consumer_type_spring_event(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS)]
        nodes = [_node(_LISTENER_A, ["@EventListener"])]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        consumer = next(c for c in result.consumers if c["fqn"] == _LISTENER_A)
        assert consumer["type"] == "spring_event"
        assert "transactional_phase" not in consumer


# ---------------------------------------------------------------------------
# EVT-03  @TransactionalEventListener — TX phase extracted
# ---------------------------------------------------------------------------

class TestTransactionalEventListenerPhase:
    def test_evt03_after_commit_phase(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(
            _LISTENER_A,
            ["@TransactionalEventListener"],
            ann_values={"@TransactionalEventListener": "phase=TransactionPhase.AFTER_COMMIT"},
        )]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        consumer = next(c for c in result.consumers if c["fqn"] == _LISTENER_A)
        assert consumer["type"] == "transactional"
        assert consumer["transactional_phase"] == "AFTER_COMMIT"

    def test_evt03_default_phase_when_no_args(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(_LISTENER_A, ["@TransactionalEventListener"])]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        consumer = next(c for c in result.consumers if c["fqn"] == _LISTENER_A)
        assert consumer["transactional_phase"] == "AFTER_COMMIT"

    def test_evt03_before_commit_phase(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(
            _LISTENER_A,
            ["@TransactionalEventListener"],
            ann_values={"@TransactionalEventListener": "phase=TransactionPhase.BEFORE_COMMIT"},
        )]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        consumer = next(c for c in result.consumers if c["fqn"] == _LISTENER_A)
        assert consumer["transactional_phase"] == "BEFORE_COMMIT"


# ---------------------------------------------------------------------------
# EVT-04  BEFORE_COMMIT → risk=high
# ---------------------------------------------------------------------------

class TestBeforeCommitRisk:
    def test_evt04_before_commit_raises_risk(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(
            _LISTENER_A,
            ["@TransactionalEventListener"],
            ann_values={"@TransactionalEventListener": "phase=TransactionPhase.BEFORE_COMMIT"},
        )]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.risk_level == "high"
        assert _LISTENER_A in result.transaction_context["before_commit_risks"]


# ---------------------------------------------------------------------------
# EVT-05  No publishers/consumers → confidence=low
# ---------------------------------------------------------------------------

class TestEmptyTopology:
    def test_evt05_empty_topology(self):
        cir = _FakeCIR(symbols=[_EVT_CLS])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.resolution == "exact"
        assert result.publishers == []
        assert result.consumers == []
        assert result.confidence == "low"
        assert result.risk_level == "low"


# ---------------------------------------------------------------------------
# EVT-06  Event class not found
# ---------------------------------------------------------------------------

class TestEventClassNotFound:
    def test_evt06_not_found(self):
        cir = _FakeCIR(symbols=["com.example.Other"])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, "NonExistentEvent")
        assert result.resolution == "not_found"
        assert result.publishers == []
        assert result.consumers == []
        assert result.confidence == "low"

    def test_evt06_not_found_returns_valid_dict(self):
        cir = _FakeCIR(symbols=[])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, "Ghost")
        d = result.to_dict()
        assert json.dumps(d)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# EVT-07  Event graph edges
# ---------------------------------------------------------------------------

class TestEventGraphEdges:
    def test_evt07_publishes_edge_present(self):
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS)]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER], call_graph=cg)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        edges = result.event_graph["edges"]
        pub_edges = [e for e in edges if e["type"] == "publishes" and e["from"] == _PUBLISHER]
        assert len(pub_edges) == 1
        assert pub_edges[0]["to"] == _EVT_CLS

    def test_evt07_consumes_edge_present(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS)]
        nodes = [_node(_LISTENER_A, ["@EventListener"])]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        edges = result.event_graph["edges"]
        con_edges = [e for e in edges if e["type"] == "consumes" and e["to"] == _LISTENER_A]
        assert len(con_edges) == 1
        assert con_edges[0]["from"] == _EVT_CLS


# ---------------------------------------------------------------------------
# EVT-08  Level-2 propagation
# ---------------------------------------------------------------------------

class TestLevel2Propagation:
    def test_evt08_consumer_republishes_secondary_event(self):
        # _LISTENER_A listens to _EVT_CLS AND publishes _SEC_EVT
        cg = [
            _pub_edge(_PUBLISHER, _EVT_CLS),
            _listen_edge(_LISTENER_A, _EVT_CLS),
            _pub_edge(_LISTENER_A, _SEC_EVT),
            _listen_edge(_SEC_LISTENER, _SEC_EVT),
        ]
        nodes = [
            _node(_LISTENER_A, ["@EventListener"]),
            _node(_SEC_LISTENER, ["@EventListener"]),
        ]
        cir = _FakeCIR(
            symbols=[_EVT_CLS, _PUBLISHER, _LISTENER_A, _SEC_EVT, _SEC_LISTENER],
            call_graph=cg,
            raw_nodes=nodes,
        )
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        # Level-2 event should appear in metadata
        assert _SEC_EVT in result.metadata.get("level2_events", [])
        # re_publishes edge should be present
        edges = result.event_graph["edges"]
        repub_edges = [e for e in edges if e["type"] == "re_publishes"]
        assert len(repub_edges) >= 1


# ---------------------------------------------------------------------------
# EVT-09  Risk fanout > 5 → high
# ---------------------------------------------------------------------------

class TestRiskFanout:
    def test_evt09_high_fanout_risk(self):
        # 6 consumers → risk=high
        assert _compute_event_risk(1, 6, 0, False) == "high"

    def test_evt10_medium_fanout_risk(self):
        assert _compute_event_risk(1, 3, 0, False) == "medium"

    def test_evt11_low_fanout_risk(self):
        assert _compute_event_risk(1, 1, 0, False) == "low"

    def test_cross_module_raises_risk(self):
        assert _compute_event_risk(1, 1, 0, True) == "high"


# ---------------------------------------------------------------------------
# EVT-12  Kafka/Rabbit counts in metadata
# ---------------------------------------------------------------------------

class TestKafkaRabbitMetadata:
    def test_evt12_kafka_count_in_metadata(self):
        nodes = [
            _node("com.example.Processor#consume", ["@KafkaListener"]),
        ]
        cir = _FakeCIR(symbols=[_EVT_CLS], raw_nodes=nodes)
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.metadata["kafka_listeners_in_repo"] == 1
        assert result.metadata["rabbit_listeners_in_repo"] == 0

    def test_evt12_rabbit_count_in_metadata(self):
        nodes = [
            _node("com.example.Listener#onMessage", ["@RabbitListener"]),
        ]
        cir = _FakeCIR(symbols=[_EVT_CLS], raw_nodes=nodes)
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.metadata["rabbit_listeners_in_repo"] == 1

    def test_evt12_limitation_note_added(self):
        nodes = [_node("com.example.KConsumer#consume", ["@KafkaListener"])]
        cir = _FakeCIR(symbols=[_EVT_CLS], raw_nodes=nodes)
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert any("Kafka" in lim for lim in result.limitations)


# ---------------------------------------------------------------------------
# EVT-13  EventTopologyResult.to_dict() — all keys, JSON-serializable
# ---------------------------------------------------------------------------

class TestResultContract:
    _REQUIRED_KEYS = {
        "schema_version", "event_class", "resolution", "publishers",
        "consumers", "event_graph", "transaction_context",
        "risk_level", "confidence", "limitations", "metadata",
    }

    def test_evt13_all_keys_present(self):
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS), _listen_edge(_LISTENER_A, _EVT_CLS)]
        nodes = [_node(_LISTENER_A, ["@EventListener"])]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        d = result.to_dict()
        assert self._REQUIRED_KEYS.issubset(set(d.keys()))

    def test_evt13_json_serializable(self):
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS)]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER], call_graph=cg)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert json.dumps(result.to_dict())  # must not raise

    def test_evt13_schema_version_is_10(self):
        cir = _FakeCIR(symbols=[_EVT_CLS])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.to_dict()["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# EVT-14  run_event_topology() no model → builds internally, no raise
# ---------------------------------------------------------------------------

class TestRunEventTopologyConvenience:
    def test_evt14_no_model_no_raise(self):
        cg = [_pub_edge(_PUBLISHER, _EVT_CLS)]
        cir = _FakeCIR(symbols=[_EVT_CLS, _PUBLISHER], call_graph=cg)
        result = run_event_topology(cir, _EVT_CLS)
        assert isinstance(result, EventTopologyResult)

    def test_evt14_internal_error_returns_result(self):
        # Passing None as cir should trigger exception path and return safely
        result = run_event_topology(None, "SomeEvent")  # type: ignore[arg-type]
        assert isinstance(result, EventTopologyResult)
        assert result.confidence == "low"


# ---------------------------------------------------------------------------
# EVT-15  TX context after_commit_consumers
# ---------------------------------------------------------------------------

class TestTxContext:
    def test_evt15_after_commit_consumers_populated(self):
        cg = [_listen_edge(_LISTENER_A, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(
            _LISTENER_A,
            ["@TransactionalEventListener"],
            ann_values={"@TransactionalEventListener": "phase=TransactionPhase.AFTER_COMMIT"},
        )]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_A], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert _LISTENER_A in result.transaction_context["after_commit_consumers"]
        assert result.transaction_context["before_commit_risks"] == []

    def test_evt17_before_commit_risks_populated(self):
        cg = [_listen_edge(_LISTENER_B, _EVT_CLS, "@TransactionalEventListener")]
        nodes = [_node(
            _LISTENER_B,
            ["@TransactionalEventListener"],
            ann_values={"@TransactionalEventListener": "phase=BEFORE_COMMIT"},
        )]
        cir = _FakeCIR(symbols=[_EVT_CLS, _LISTENER_B], call_graph=cg, raw_nodes=nodes)
        model = _make_model(cg)
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert _LISTENER_B in result.transaction_context["before_commit_risks"]


# ---------------------------------------------------------------------------
# EVT-16  Simple class name resolution
# ---------------------------------------------------------------------------

class TestClassNameResolution:
    def test_evt16_class_expanded_resolution(self):
        cir = _FakeCIR(symbols=[_EVT_CLS])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, "OrderCreatedEvent")
        assert result.resolution == "class_expanded"
        assert result.event_class == _EVT_CLS

    def test_evt16_exact_resolution(self):
        cir = _FakeCIR(symbols=[_EVT_CLS])
        model = _make_model([])
        result = EventTopologyOrchestrator().query(cir, model, _EVT_CLS)
        assert result.resolution == "exact"


# ---------------------------------------------------------------------------
# EVT-19  _resolve_event_symbol helpers
# ---------------------------------------------------------------------------

class TestResolveEventSymbol:
    def test_exact_fqn(self):
        fqn, res, warns = _resolve_event_symbol(_EVT_CLS, type("C", (), {"symbols": [_EVT_CLS]})())
        assert res == "exact"
        assert fqn == _EVT_CLS
        assert warns == []

    def test_class_expanded(self):
        fqn, res, warns = _resolve_event_symbol(
            "OrderCreatedEvent", type("C", (), {"symbols": [_EVT_CLS]})()
        )
        assert res == "class_expanded"
        assert fqn == _EVT_CLS

    def test_not_found(self):
        fqn, res, warns = _resolve_event_symbol("Ghost", type("C", (), {"symbols": [_EVT_CLS]})())
        assert res == "not_found"
        assert fqn == ""
        assert len(warns) == 1


# ---------------------------------------------------------------------------
# _extract_tx_phase unit tests
# ---------------------------------------------------------------------------

class TestExtractTxPhase:
    def test_no_transactional_annotation_returns_empty(self):
        assert _extract_tx_phase({}, ["@EventListener"]) == ""

    def test_annotation_present_no_args_returns_default(self):
        assert _extract_tx_phase({}, ["@TransactionalEventListener"]) == "AFTER_COMMIT"

    def test_after_commit_parsed(self):
        vals = {"@TransactionalEventListener": "phase=TransactionPhase.AFTER_COMMIT"}
        assert _extract_tx_phase(vals, ["@TransactionalEventListener"]) == "AFTER_COMMIT"

    def test_before_commit_parsed(self):
        vals = {"@TransactionalEventListener": "phase=BEFORE_COMMIT"}
        assert _extract_tx_phase(vals, ["@TransactionalEventListener"]) == "BEFORE_COMMIT"

    def test_after_rollback_parsed(self):
        vals = {"@TransactionalEventListener": "phase = TransactionPhase.AFTER_ROLLBACK"}
        assert _extract_tx_phase(vals, ["@TransactionalEventListener"]) == "AFTER_ROLLBACK"


# ---------------------------------------------------------------------------
# BUG-003 regression — Javadoc false positive in publishEvent detection
# BUG-004 regression — same-package event FQN resolution
# ---------------------------------------------------------------------------

class TestBUG003JavadocFalsePositive:
    """BUG-003: _PUBLISH_EVENT_RE must not match publishEvent() inside Javadoc comments."""

    def test_javadoc_publishevent_does_not_produce_edge(self):
        """publishEvent() inside /** ... */ Javadoc must not generate a publisher edge."""
        from sourcecode.repository_ir import _strip_java_comments, _PUBLISH_EVENT_RE

        javadoc_source = '''\
/**
 * To publish an event:
 * <pre>
 * appCtx.publishEvent(new OrderCreatedEvent(order));
 * </pre>
 */
public class OrderService {
    // no publishEvent in real code
}
'''
        stripped = _strip_java_comments(javadoc_source)
        matches = list(_PUBLISH_EVENT_RE.finditer(stripped))
        assert matches == [], (
            f"publishEvent in Javadoc must not produce edges, but got: {matches}"
        )

    def test_real_publishevent_still_detected(self):
        """publishEvent() in real code must still generate a publisher edge."""
        from sourcecode.repository_ir import _strip_java_comments, _PUBLISH_EVENT_RE

        real_source = '''\
public class OrderService {
    public void placeOrder(Order o) {
        eventPublisher.publishEvent(new OrderCreatedEvent(o));
    }
}
'''
        stripped = _strip_java_comments(real_source)
        matches = list(_PUBLISH_EVENT_RE.finditer(stripped))
        assert len(matches) == 1
        assert matches[0].group(1) == "OrderCreatedEvent"


# ---------------------------------------------------------------------------
# EVT-APP-LISTENER — ApplicationListener<T> and AbstractXxxEventListener<T>
# consumer detection via class signature (EVT-003 / EVT-004)
# ---------------------------------------------------------------------------

class TestApplicationListenerConsumerDetection:
    """Regression tests for class-level consumer detection patterns.

    EVT-APP-1: implements ApplicationListener<T> — standard Spring interface regex
    EVT-APP-2: extends AbstractXxxEventListener<T> — abstract base class regex
    EVT-APP-3: No false positives for unrelated generic parents
    EVT-APP-4: Full pipeline — ApplicationListener<T> produces listens_to_event edge
    EVT-APP-5: Full pipeline — AbstractBroadleafApplicationEventListener<T>
    EVT-APP-6: BUG-EVT-001 — implements BroadleafApplicationListener<T> (prefixed subinterface)
    EVT-APP-7: BUG-EVT-001 — full pipeline for prefixed subinterface produces edge
    """

    def _parse(self, source: str) -> list:
        """Run _extract_symbols + _build_relations on a Java source snippet."""
        from sourcecode.repository_ir import _build_relations, _extract_symbols
        package, symbols, raw_imports = _extract_symbols(source, "com/example/Foo.java")
        return _build_relations(
            symbols=symbols,
            raw_imports=raw_imports,
            source=source,
            package=package or "com.example",
            rel_path="com/example/Foo.java",
        )

    def test_evt_app1_app_listener_regex(self):
        """_APP_LISTENER_RE matches ApplicationListener<XxxEvent> in signature."""
        from sourcecode.repository_ir import _APP_LISTENER_RE
        sig = "class CartStateRefresher implements ApplicationListener<OrderPersistedEvent>"
        m = _APP_LISTENER_RE.search(sig)
        assert m is not None, "regex must match ApplicationListener<T>"
        assert m.group(1) == "OrderPersistedEvent"

    def test_evt_app2_abstract_listener_regex(self):
        """_ABSTRACT_LISTENER_RE matches AbstractXxxEventListener<T> in signature."""
        from sourcecode.repository_ir import _ABSTRACT_LISTENER_RE
        sig = "class Foo extends AbstractBroadleafApplicationEventListener<RegisterCustomerEvent>"
        m = _ABSTRACT_LISTENER_RE.search(sig)
        assert m is not None, "regex must match AbstractXxxEventListener<T>"
        assert m.group(1) == "RegisterCustomerEvent"

    def test_evt_app3_no_false_positive_unrelated_generic(self):
        """_ABSTRACT_LISTENER_RE must NOT match ExtensionManager<T>."""
        from sourcecode.repository_ir import _ABSTRACT_LISTENER_RE
        sig = "class Foo extends ExtensionManager<CartStateRequestProcessorExtensionHandler>"
        assert _ABSTRACT_LISTENER_RE.search(sig) is None, (
            "ExtensionManager<T> must not match abstract listener pattern"
        )

    def test_evt_app4_full_pipeline_application_listener(self):
        """Full parse: implements ApplicationListener<T> → listens_to_event edge."""
        source = """\
package com.example;
import com.example.events.OrderPersistedEvent;
@Component
public class CartStateRefresher implements ApplicationListener<OrderPersistedEvent> {
    public void onApplicationEvent(OrderPersistedEvent event) {}
}
"""
        edges = self._parse(source)
        listen_edges = [e for e in edges if e.type == "listens_to_event"]
        assert any(
            e.from_symbol == "com.example.CartStateRefresher"
            and "OrderPersistedEvent" in e.to_symbol
            for e in listen_edges
        ), f"ApplicationListener<T> pipeline failed. listen_edges={[(e.from_symbol, e.to_symbol) for e in listen_edges]}"

    def test_evt_app5_full_pipeline_abstract_broadleaf_listener(self):
        """Full parse: extends AbstractBroadleafApplicationEventListener<T> → listens_to_event."""
        source = """\
package com.example;
import com.example.events.RegisterCustomerEvent;
@Component
public class NotificationRegisterCustomerEventListener extends AbstractBroadleafApplicationEventListener<RegisterCustomerEvent> {
    protected void handleApplicationEvent(RegisterCustomerEvent event) {}
}
"""
        edges = self._parse(source)
        listen_edges = [e for e in edges if e.type == "listens_to_event"]
        assert any(
            e.from_symbol == "com.example.NotificationRegisterCustomerEventListener"
            and "RegisterCustomerEvent" in e.to_symbol
            for e in listen_edges
        ), f"AbstractBroadleafEventListener<T> pipeline failed. listen_edges={[(e.from_symbol, e.to_symbol) for e in listen_edges]}"

    def test_evt_app6_broadleaf_prefixed_listener_regex(self):
        """BUG-EVT-001 — _APP_LISTENER_RE matches BroadleafApplicationListener<T>.

        Before fix: regex used \\b before 'ApplicationListener', which fails when
        preceded by a word character (e.g. 'f' in 'BroadleafApplicationListener').
        After fix: \\w* prefix allows any word-character prefix before the interface name.
        """
        from sourcecode.repository_ir import _APP_LISTENER_RE
        sig = "class TransactionLifecycleMonitor implements BroadleafApplicationListener<TransactionLifecycleEvent>"
        m = _APP_LISTENER_RE.search(sig)
        assert m is not None, (
            "BUG-EVT-001: _APP_LISTENER_RE must match BroadleafApplicationListener<T> "
            "(word-boundary before 'ApplicationListener' breaks on prefix)"
        )
        assert m.group(1) == "TransactionLifecycleEvent"

    def test_evt_app7_full_pipeline_broadleaf_prefixed_listener(self):
        """BUG-EVT-001 — full pipeline: implements BroadleafApplicationListener<T> → edge."""
        source = """\
package com.example.persistence;
import com.example.events.TransactionLifecycleEvent;
@Component
public class TransactionLifecycleMonitor
        implements BroadleafApplicationListener<TransactionLifecycleEvent> {
    public void onApplicationEvent(TransactionLifecycleEvent event) {}
}
"""
        edges = self._parse(source)
        listen_edges = [e for e in edges if e.type == "listens_to_event"]
        assert any(
            "TransactionLifecycleMonitor" in e.from_symbol
            and "TransactionLifecycleEvent" in e.to_symbol
            for e in listen_edges
        ), (
            "BUG-EVT-001: BroadleafApplicationListener<T> must produce listens_to_event edge. "
            f"listen_edges={[(e.from_symbol, e.to_symbol) for e in listen_edges]}"
        )


# ---------------------------------------------------------------------------
# F-009  Sync @EventListener inside @Transactional publisher → risk=high
# ---------------------------------------------------------------------------

class TestSyncEventListenerInTxRisk:
    """Regression tests for F-009: sync @EventListener not flagged as TX risk.

    When a @Transactional publisher fires an event and the consumer is a plain
    @EventListener (not @TransactionalEventListener), the listener runs
    synchronously inside the TX. A listener exception rolls back the outer TX.
    """

    def _run(self, pub_fqn, listener_fqn, pub_annotations, listener_annotations,
             listener_ann_values=None):
        cg = [
            _pub_edge(pub_fqn, _EVT_CLS),
            _listen_edge(listener_fqn, _EVT_CLS),
        ]
        raw_nodes = [
            _node(pub_fqn, pub_annotations),
            _node(listener_fqn, listener_annotations, listener_ann_values or {}),
            _node(_EVT_CLS, []),
        ]
        cir = _FakeCIR(
            symbols=[_EVT_CLS, pub_fqn, listener_fqn],
            call_graph=cg,
            raw_nodes=raw_nodes,
        )
        model = _make_model(cg)
        return EventTopologyOrchestrator().query(cir, model, _EVT_CLS)

    def test_sync_event_listener_in_tx_is_high_risk(self):
        """@Transactional publisher + plain @EventListener → risk=high, sync_in_tx_risks populated."""
        result = self._run(
            pub_fqn=_PUBLISHER,
            listener_fqn=_LISTENER_A,
            pub_annotations=["@Transactional"],
            listener_annotations=["@EventListener"],
        )
        assert result.risk_level == "high", (
            f"F-009 regression: sync @EventListener in TX must be high risk, got {result.risk_level}"
        )
        sync_risks = result.transaction_context.get("sync_in_tx_risks", [])
        assert _LISTENER_A in sync_risks, (
            f"F-009 regression: listener not in sync_in_tx_risks. got={sync_risks}"
        )

    def test_sync_in_tx_count_in_metadata(self):
        """sync_in_tx_risk_count must appear in metadata."""
        result = self._run(
            pub_fqn=_PUBLISHER,
            listener_fqn=_LISTENER_A,
            pub_annotations=["@Transactional"],
            listener_annotations=["@EventListener"],
        )
        assert "sync_in_tx_risk_count" in result.metadata, (
            "F-009 regression: sync_in_tx_risk_count missing from metadata"
        )
        assert result.metadata["sync_in_tx_risk_count"] == 1

    def test_transactional_after_commit_not_flagged_as_sync_risk(self):
        """@TransactionalEventListener(AFTER_COMMIT) must NOT appear in sync_in_tx_risks."""
        result = self._run(
            pub_fqn=_PUBLISHER,
            listener_fqn=_LISTENER_A,
            pub_annotations=["@Transactional"],
            listener_annotations=["@TransactionalEventListener"],
            listener_ann_values={"@TransactionalEventListener": "phase=TransactionPhase.AFTER_COMMIT"},
        )
        sync_risks = result.transaction_context.get("sync_in_tx_risks", [])
        assert _LISTENER_A not in sync_risks, (
            f"F-009 regression: AFTER_COMMIT listener falsely flagged as sync risk. got={sync_risks}"
        )

    def test_no_transactional_publisher_no_sync_risk(self):
        """Plain (non-TX) publisher + @EventListener → no sync_in_tx_risk."""
        result = self._run(
            pub_fqn=_PUBLISHER,
            listener_fqn=_LISTENER_A,
            pub_annotations=["@Service"],    # no @Transactional
            listener_annotations=["@EventListener"],
        )
        sync_risks = result.transaction_context.get("sync_in_tx_risks", [])
        assert len(sync_risks) == 0, (
            f"F-009 regression: non-TX publisher produced sync_in_tx_risks. got={sync_risks}"
        )

    def test_compute_event_risk_sync_in_tx(self):
        """_compute_event_risk must return high when sync_in_tx_count > 0."""
        from sourcecode.spring_event_topology import _compute_event_risk
        assert _compute_event_risk(1, 1, 0, False, sync_in_tx_count=1) == "high"
        assert _compute_event_risk(1, 1, 0, False, sync_in_tx_count=0) == "low"

    def test_transactional_event_listener_before_commit_still_flagged(self):
        """BEFORE_COMMIT phase must still be flagged (existing behavior preserved)."""
        result = self._run(
            pub_fqn=_PUBLISHER,
            listener_fqn=_LISTENER_A,
            pub_annotations=["@Service"],
            listener_annotations=["@TransactionalEventListener"],
            listener_ann_values={"@TransactionalEventListener": "phase=TransactionPhase.BEFORE_COMMIT"},
        )
        assert result.risk_level == "high", (
            f"F-009 regression: BEFORE_COMMIT no longer flagged as high risk"
        )
        assert _LISTENER_A in result.transaction_context.get("before_commit_risks", [])
