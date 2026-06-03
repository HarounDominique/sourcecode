"""spring_event_topology.py — Event Topology: event_class → publishers, consumers, propagation graph.

Pipeline:
  1. Resolve event symbol from CIR
  2. Find publishers via model.event_graph
  3. Find Spring consumers via model.event_graph (EventListener + TransactionalEventListener)
  4. Enrich consumers with TX phase from raw IR annotation_values
  5. Build event propagation graph (BFS depth ≤ 2)
  6. Attach TX semantics: AFTER_COMMIT consumers, BEFORE_COMMIT risks
  7. Compute risk level + confidence

Hard constraints:
  - NO guessing / LLM inference
  - ONLY CIR + AST-derived annotation data
  - Deterministic: identical CIR → identical result
  - Reuses model.event_graph + model.tx_index — no duplicate CIR traversal

Usage:
    model = SpringSemanticModel.build(cir)
    result = run_event_topology(cir, "com.example.OrderCreatedEvent", model=model)
    output = json.dumps(result.to_dict(), indent=2)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR
    from sourcecode.spring_model import SpringSemanticModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "1.0"

# Phase annotation value parser: phase=TransactionPhase.AFTER_COMMIT
_TX_PHASE_RE = re.compile(r'phase\s*=\s*(?:TransactionPhase\.)?(\w+)')

# TransactionalEventListener default phase (Spring default)
_DEFAULT_TX_PHASE = "AFTER_COMMIT"

_RISK_FANOUT_HIGH = 5
_RISK_FANOUT_MEDIUM = 2


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EventConsumer:
    """Single consumer of a Spring application event."""
    fqn: str
    type: str               # "spring_event" | "transactional"
    transactional_phase: str  # "" | "BEFORE_COMMIT" | "AFTER_COMMIT" | "AFTER_ROLLBACK" | "AFTER_COMPLETION"
    source_file: str

    def to_dict(self) -> dict:
        d: dict = {
            "fqn": self.fqn,
            "type": self.type,
            "source_file": self.source_file,
        }
        if self.transactional_phase:
            d["transactional_phase"] = self.transactional_phase
        return d


@dataclass
class EventTopologyResult:
    """Output contract for event topology query.

    Stable contract — do not remove or rename fields.
    """
    schema_version: str = _SCHEMA_VERSION
    event_class: str = ""
    resolution: str = "not_found"   # "exact" | "class_expanded" | "partial" | "not_found"
    publishers: list[str] = field(default_factory=list)
    consumers: list[dict] = field(default_factory=list)
    event_graph: dict = field(default_factory=dict)
    transaction_context: dict = field(default_factory=dict)
    risk_level: str = "low"
    confidence: str = "high"
    limitations: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event_class": self.event_class,
            "resolution": self.resolution,
            "publishers": self.publishers,
            "consumers": self.consumers,
            "event_graph": self.event_graph,
            "transaction_context": self.transaction_context,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "limitations": self.limitations,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _class_of(fqn: str) -> str:
    if "#" in fqn:
        return fqn.split("#")[0]
    return fqn


def _build_fqn_index(cir: "CanonicalRepositoryIR") -> dict[str, dict]:
    """Build FQN → raw IR node dict for fast annotation lookup.

    Reads once from cir._raw_ir.  Returns {} if raw IR not available.
    """
    raw = getattr(cir, "_raw_ir", {}) or {}
    nodes = (raw.get("graph") or {}).get("nodes") or []
    return {n["fqn"]: n for n in nodes if "fqn" in n}


def _extract_tx_phase(annotation_values: dict, annotations: list) -> str:
    """Extract @TransactionalEventListener phase from annotation_values.

    Returns empty string for plain @EventListener (no TX constraint).
    Returns _DEFAULT_TX_PHASE when annotation present but phase not specified.
    """
    if "@TransactionalEventListener" not in annotations:
        return ""
    raw_args = annotation_values.get("@TransactionalEventListener", "")
    if not raw_args:
        return _DEFAULT_TX_PHASE
    m = _TX_PHASE_RE.search(raw_args)
    if m:
        return m.group(1).upper()
    return _DEFAULT_TX_PHASE


def _resolve_event_symbol(
    event_class: str,
    cir: "CanonicalRepositoryIR",
) -> tuple[str, str, list[str]]:
    """Resolve event_class to CIR FQN.

    Returns (resolved_fqn, resolution, warnings).
    resolution: "exact" | "class_expanded" | "not_found"
    """
    symbols = cir.symbols
    warnings: list[str] = []

    # 1. Exact FQN match
    if event_class in symbols:
        return event_class, "exact", []

    # 2. Class-part suffix match (handles simple names like "OrderCreatedEvent")
    candidates = [
        s for s in symbols
        if _class_of(s) == event_class or _class_of(s).endswith("." + event_class)
    ]
    unique_classes = {_class_of(s) for s in candidates}
    if len(unique_classes) == 1:
        return next(iter(unique_classes)), "class_expanded", []
    if len(unique_classes) > 1:
        # Multiple matches — take first alphabetically, warn
        chosen = sorted(unique_classes)[0]
        warnings.append(
            f"Ambiguous event class '{event_class}': matched {sorted(unique_classes)}. "
            f"Using '{chosen}'."
        )
        return chosen, "partial", warnings

    return "", "not_found", [f"Event class '{event_class}' not found in CIR."]


def _find_kafka_rabbit_counts(
    fqn_index: dict[str, dict],
) -> tuple[int, int]:
    """Count @KafkaListener and @RabbitListener methods in raw IR."""
    kafka_count = 0
    rabbit_count = 0
    for node in fqn_index.values():
        anns = node.get("annotations") or []
        if "@KafkaListener" in anns:
            kafka_count += 1
        if "@RabbitListener" in anns:
            rabbit_count += 1
    return kafka_count, rabbit_count


def _compute_event_risk(
    publisher_count: int,
    consumer_count: int,
    before_commit_count: int,
    cross_module: bool,
) -> str:
    """Deterministic risk scoring per spec.

    high: fanout > 5 OR cross-module propagation OR BEFORE_COMMIT consumers
    medium: 2–5 consumers
    low: ≤1 consumer
    """
    if consumer_count > _RISK_FANOUT_HIGH or cross_module or before_commit_count > 0:
        return "high"
    if consumer_count >= _RISK_FANOUT_MEDIUM:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class EventTopologyOrchestrator:
    """Stateless query engine: event_class → EventTopologyResult.

    Consumes pre-built CIR + SpringSemanticModel. Never re-derives data
    already present in the model.
    """

    def query(
        self,
        cir: "CanonicalRepositoryIR",
        model: "SpringSemanticModel",
        event_class: str,
    ) -> EventTopologyResult:
        """Execute event topology query.

        Args:
            cir:         CanonicalRepositoryIR from build_canonical_ir().
            model:       Pre-built SpringSemanticModel.
            event_class: Event class FQN or simple name.
        """
        t0 = time.monotonic()
        warnings: list[str] = []
        limitations: list[str] = [
            "Only Spring annotations detected (@EventListener, @TransactionalEventListener).",
            "No runtime dynamic event routing — static analysis only.",
        ]

        # ── 1. Resolve event symbol ────────────────────────────────────────
        resolved_fqn, resolution, sym_warnings = _resolve_event_symbol(event_class, cir)
        warnings.extend(sym_warnings)

        if resolution == "not_found" or not resolved_fqn:
            elapsed = round((time.monotonic() - t0) * 1000, 2)
            return EventTopologyResult(
                event_class=event_class,
                resolution="not_found",
                limitations=limitations + [f"Event class '{event_class}' not found in CIR."],
                confidence="low",
                metadata={"query_time_ms": elapsed},
            )

        # ── 2. Build FQN index for annotation lookups ──────────────────────
        fqn_index = _build_fqn_index(cir)

        # ── 3. Find publishers ─────────────────────────────────────────────
        publishers = sorted(model.event_graph.publishers_of(resolved_fqn))

        # ── 4. Find Spring consumers, enrich with TX phase ─────────────────
        raw_consumer_fqns = model.event_graph.listeners_of(resolved_fqn)
        consumers: list[EventConsumer] = []
        for fqn in sorted(raw_consumer_fqns):
            node = fqn_index.get(fqn) or {}
            anns = node.get("annotations") or []
            ann_vals = node.get("annotation_values") or {}
            source_file = node.get("source_file") or ""
            tx_phase = _extract_tx_phase(ann_vals, anns)
            consumer_type = "transactional" if tx_phase else "spring_event"
            consumers.append(EventConsumer(
                fqn=fqn,
                type=consumer_type,
                transactional_phase=tx_phase,
                source_file=source_file,
            ))

        # ── 5. Build event propagation graph (BFS ≤ 2) ────────────────────
        graph_edges: list[dict] = []

        # Level 0 → 1: publisher → event_class, event_class → consumer
        for pub in publishers:
            graph_edges.append({"from": pub, "to": resolved_fqn, "type": "publishes"})
        for c in consumers:
            graph_edges.append({"from": resolved_fqn, "to": c.fqn, "type": "consumes"})

        # Level 1 → 2: consumers that also publish other events
        level2_events: dict[str, list[str]] = {}  # secondary_event → [l2_consumers]
        for c in consumers:
            # Check if this consumer FQN also publishes events
            # The event_graph publishers dict is indexed by event_type → [publisher_fqns]
            for evt_type, pub_fqns in model.event_graph.publishers.items():
                if evt_type == resolved_fqn:
                    continue  # same event, skip
                consumer_class = _class_of(c.fqn)
                matching = [p for p in pub_fqns
                            if p == c.fqn or _class_of(p) == consumer_class]
                if matching:
                    graph_edges.append({
                        "from": c.fqn,
                        "to": evt_type,
                        "type": "re_publishes",
                    })
                    l2_listeners = model.event_graph.listeners_of(evt_type)
                    level2_events[evt_type] = sorted(l2_listeners)
                    for l2 in sorted(l2_listeners):
                        graph_edges.append({
                            "from": evt_type,
                            "to": l2,
                            "type": "consumes",
                        })

        # ── 6. Kafka/Rabbit counts (metadata only — not linkable to event_class) ──
        kafka_count, rabbit_count = _find_kafka_rabbit_counts(fqn_index)
        if kafka_count > 0 or rabbit_count > 0:
            limitations.append(
                f"Kafka/RabbitMQ consumer binding not supported in v1 — "
                f"{kafka_count} @KafkaListener and {rabbit_count} @RabbitListener "
                f"method(s) found in repo; cannot link to event class without "
                f"explicit topic-to-event mapping."
            )

        # ── 7. TX context ──────────────────────────────────────────────────
        after_commit = [c.fqn for c in consumers if c.transactional_phase == "AFTER_COMMIT"]
        before_commit_risks = [c.fqn for c in consumers if c.transactional_phase == "BEFORE_COMMIT"]
        tx_context = {
            "after_commit_consumers": after_commit,
            "before_commit_risks": before_commit_risks,
        }

        # ── 8. Cross-module detection ──────────────────────────────────────
        # Simple heuristic: publisher and consumer in different top-level packages
        pub_packages = {_class_of(p).rsplit(".", 2)[0] for p in publishers if "." in _class_of(p)}
        con_packages = {_class_of(c.fqn).rsplit(".", 2)[0] for c in consumers if "." in _class_of(c.fqn)}
        cross_module = bool(pub_packages and con_packages and not pub_packages.isdisjoint(con_packages) is False)
        # More precise: cross-module if top-2-segment packages differ
        def _top2(fqn: str) -> str:
            parts = _class_of(fqn).split(".")
            return ".".join(parts[:2]) if len(parts) >= 2 else fqn

        pub_top2 = {_top2(p) for p in publishers}
        con_top2 = {_top2(c.fqn) for c in consumers}
        cross_module = bool(pub_top2 and con_top2 and pub_top2 != con_top2)

        # ── 9. Risk ────────────────────────────────────────────────────────
        risk_level = _compute_event_risk(
            publisher_count=len(publishers),
            consumer_count=len(consumers),
            before_commit_count=len(before_commit_risks),
            cross_module=cross_module,
        )

        # ── 10. Confidence ─────────────────────────────────────────────────
        if resolution == "partial" or warnings:
            confidence = "medium"
        elif not publishers and not consumers:
            confidence = "low"
        else:
            confidence = "high"

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

        return EventTopologyResult(
            schema_version=_SCHEMA_VERSION,
            event_class=resolved_fqn,
            resolution=resolution,
            publishers=publishers,
            consumers=[c.to_dict() for c in consumers],
            event_graph={
                "edges": graph_edges,
                "level2_events": level2_events,
            },
            transaction_context=tx_context,
            risk_level=risk_level,
            confidence=confidence,
            limitations=limitations,
            metadata={
                "query_time_ms": elapsed_ms,
                "publisher_count": len(publishers),
                "consumer_count": len(consumers),
                "kafka_listeners_in_repo": kafka_count,
                "rabbit_listeners_in_repo": rabbit_count,
                "before_commit_risk_count": len(before_commit_risks),
                "level2_events": list(level2_events.keys()),
                "cross_module": cross_module,
                "model_build_time_ms": model.build_time_ms,
            },
        )


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def run_event_topology(
    cir: "CanonicalRepositoryIR",
    event_class: str,
    *,
    model: "Optional[SpringSemanticModel]" = None,
) -> EventTopologyResult:
    """Run event topology query from a CIR.

    Args:
        cir:         CanonicalRepositoryIR from build_canonical_ir().
        event_class: Event class FQN or simple name.
        model:       Pre-built SpringSemanticModel. Built internally if None.

    Returns EventTopologyResult — always JSON-serializable, never raises.
    """
    from sourcecode.spring_model import SpringSemanticModel as _SSM

    try:
        if model is None:
            model = _SSM.build(cir)
        orchestrator = EventTopologyOrchestrator()
        return orchestrator.query(cir, model, event_class)
    except Exception as exc:
        return EventTopologyResult(
            event_class=event_class,
            resolution="not_found",
            limitations=[f"Internal error: {type(exc).__name__}: {exc}"],
            confidence="low",
        )
