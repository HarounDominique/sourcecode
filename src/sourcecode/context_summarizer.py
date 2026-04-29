from __future__ import annotations

"""Generates a compact, high-signal ContextSummary for AI agent consumption.

Uses all available analysis: stacks, architecture, graph analytics, entry points.
Fast path: O(1) from pre-computed data. Falls back to inline ArchitectureAnalyzer if needed.
"""

from pathlib import Path
from typing import Any, Optional

from sourcecode.schema import ContextSummary, SourceMap

_STACK_LABELS: dict[str, str] = {
    "python": "Python",
    "nodejs": "Node.js/TypeScript",
    "go": "Go",
    "rust": "Rust",
    "dotnet": "C#/.NET",
    "java": "Java",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "ruby": "Ruby",
    "php": "PHP",
    "dart": "Dart/Flutter",
    "elixir": "Elixir",
}

_TYPE_LABELS: dict[str, str] = {
    "api": "REST API",
    "webapp": "Web app",
    "fullstack": "Full-stack app",
    "cli": "CLI tool",
    "worker": "Background worker / service",
    "library": "Library / package",
    "unknown": "Application",
}

_LAYER_HINTS: dict[str, str] = {
    "domain": "Core domain logic and entities",
    "application": "Application / use-case orchestration",
    "infrastructure": "External integrations (DB, messaging, APIs)",
    "controller": "HTTP handlers and route definitions",
    "service": "Business logic services",
    "repository": "Data access / persistence",
    "commands": "CQRS write side (commands)",
    "queries": "CQRS read side (queries)",
    "ports": "Hexagonal ports (interfaces)",
    "adapters": "Hexagonal adapters (implementations)",
    "processing": "Data processing / analysis",
    "orchestration": "Orchestration / coordination",
    "data": "Data models / schemas",
    "apps": "Application packages (monorepo apps)",
    "packages": "Shared packages (monorepo libs)",
}


class ContextSummarizer:
    """Generates ContextSummary from a fully-populated SourceMap."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def generate(self, sm: SourceMap) -> Optional[ContextSummary]:
        try:
            return self._build(sm)
        except Exception:
            return None

    def _build(self, sm: SourceMap) -> ContextSummary:
        arch = sm.architecture
        if arch is None and sm.file_paths:
            from sourcecode.architecture_analyzer import ArchitectureAnalyzer
            arch = ArchitectureAnalyzer().analyze(self.root, sm)

        return ContextSummary(
            runtime_shape=self._infer_runtime_shape(sm),
            dominant_pattern=self._dominant_pattern(arch),
            critical_modules=self._collect_critical_modules(sm),
            layer_map=self._build_layer_map(arch),
            edit_hints=self._generate_edit_hints(arch, sm),
            coupling_notes=self._coupling_notes(sm),
        )

    def _infer_runtime_shape(self, sm: SourceMap) -> str:
        runtime = _TYPE_LABELS.get(sm.project_type or "", "Application")

        # Gather framework names from all stacks (deduplicated, max 3)
        fw_names: list[str] = []
        seen: set[str] = set()
        for stack in sm.stacks:
            for fw in stack.frameworks:
                if fw.name not in seen:
                    seen.add(fw.name)
                    fw_names.append(fw.name)
        fw_names = fw_names[:3]

        if fw_names:
            return f"{runtime} — {', '.join(fw_names)}"

        # Fallback: stack label
        primary = next((s for s in sm.stacks if s.primary), sm.stacks[0] if sm.stacks else None)
        if primary:
            label = _STACK_LABELS.get(primary.stack, primary.stack)
            return f"{runtime} — {label}"

        return runtime

    def _dominant_pattern(self, arch: Any) -> Optional[str]:
        if arch is None:
            return None
        pattern = arch.pattern
        if pattern in (None, "unknown", "flat"):
            return None
        return pattern

    def _collect_critical_modules(self, sm: SourceMap) -> list[str]:
        critical: list[str] = []
        seen: set[str] = set()

        for ep in sm.entry_points[:3]:
            if ep.path and ep.path not in seen:
                seen.add(ep.path)
                critical.append(ep.path)

        mgr = sm.module_graph_summary
        if mgr:
            for hub in mgr.hubs[:4]:
                path = hub.removeprefix("module:")
                if path and path not in seen:
                    seen.add(path)
                    critical.append(path)

        return critical[:6]

    def _build_layer_map(self, arch: Any) -> dict[str, list[str]]:
        if arch is None or not arch.layers:
            return {}
        result: dict[str, list[str]] = {}
        for layer in arch.layers[:6]:
            dirs: set[str] = set()
            for f in layer.files[:8]:
                parts = f.replace("\\", "/").split("/")
                if len(parts) >= 2:
                    dirs.add("/".join(parts[:-1]) + "/")
                else:
                    dirs.add(f)
            if dirs:
                result[layer.name] = sorted(dirs)[:3]
        return result

    def _generate_edit_hints(self, arch: Any, sm: SourceMap) -> list[str]:
        hints: list[str] = []
        if arch is None or not arch.layers:
            return hints

        for layer in arch.layers[:5]:
            hint_label = _LAYER_HINTS.get(layer.name, layer.name)
            dirs: list[str] = []
            seen_dirs: set[str] = set()
            for f in layer.files[:6]:
                parts = f.replace("\\", "/").split("/")
                d = "/".join(parts[:-1]) + "/" if len(parts) >= 2 else f
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    dirs.append(d)
            if dirs:
                dir_str = ", ".join(dirs[:2])
                hints.append(f"{hint_label} → {dir_str}")

        return hints[:4]

    def _coupling_notes(self, sm: SourceMap) -> list[str]:
        notes: list[str] = []
        mgr = sm.module_graph_summary
        if mgr is None:
            return notes

        if mgr.cycle_count > 0:
            s = "s" if mgr.cycle_count > 1 else ""
            notes.append(f"{mgr.cycle_count} circular import cycle{s} detected")

        if mgr.hubs:
            names = [h.removeprefix("module:").split("/")[-1] for h in mgr.hubs[:3]]
            notes.append(f"High-coupling hubs: {', '.join(names)}")

        orphan_count = len(mgr.orphans)
        if orphan_count >= 3:
            notes.append(f"{orphan_count} orphan modules (unreferenced)")

        return notes
