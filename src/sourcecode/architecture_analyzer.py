from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from sourcecode.schema import (
    ArchitectureAnalysis,
    ArchitectureDomain,
    ArchitectureLayer,
    BoundedContext,
    ModuleGraph,
    SourceMap,
)

_TOOLING_PREFIXES = (
    ".claude/",
    ".vscode/",
    "bin/",
    ".git/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".pytest_cache/",
    "dist/",
    "build/",
)
_SRC_TRANSPARENT = {"src", "lib", "app", "pkg"}
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".rs", ".rb",
}
_GENERIC_NAMES = {"utils", "helpers", "common", "shared", "misc", "core", "root", ""}

DOMAIN_ROLES: dict[str, str] = {
    "controllers": "HTTP request handlers",
    "handlers":    "HTTP request handlers",
    "routes":      "Route definitions",
    "services":    "Business logic",
    "usecases":    "Business logic",
    "application": "Application layer",
    "repositories": "Data access",
    "repos":       "Data access",
    "store":       "Data access",
    "models":      "Domain models",
    "entities":    "Domain entities",
    "domain":      "Domain core",
    "infra":       "Infrastructure",
    "infrastructure": "Infrastructure",
    "adapters":    "Hexagonal adapters",
    "ports":       "Hexagonal ports",
    "frontend":    "Frontend/UI layer",
    "backend":     "Backend/API layer",
    "components":  "UI components",
    "pages":       "UI pages/views",
    "views":       "View layer",
    "templates":   "View templates",
    "tests":       "Test suite",
    "test":        "Test suite",
}

LAYER_PATTERNS: dict[str, dict[str, list[str]]] = {
    "mvc": {
        "controller": ["controller", "controllers", "routes", "views", "handlers"],
        "model":      ["model", "models", "entity", "entities", "domain"],
        "view":       ["views", "templates", "pages", "components"],
    },
    "layered": {
        "controller":     ["controller", "controllers", "api", "routes", "handlers", "endpoints"],
        "service":        ["service", "services", "usecase", "usecases", "application"],
        "repository":     ["repository", "repositories", "repo", "repos", "store", "storage", "dao"],
        "infrastructure": ["infra", "infrastructure", "persistence", "db", "database"],
    },
    "hexagonal": {
        "port":    ["port", "ports", "interface", "interfaces"],
        "adapter": ["adapter", "adapters"],
        "domain":  ["domain", "core", "model", "models"],
    },
    "fullstack": {
        "frontend": ["frontend", "client", "web", "ui", "pages", "components", "app"],
        "backend":  ["backend", "server", "api", "services"],
    },
}


class ArchitectureAnalyzer:
    """Analiza la arquitectura de un proyecto a partir de su estructura de ficheros y grafo de modulos."""

    def analyze(
        self,
        root: Path,
        sm: SourceMap,
        graph: Optional[ModuleGraph] = None,
    ) -> ArchitectureAnalysis:
        limitations: list[str] = []

        # Step 1: filter paths
        filtered = self._filter_paths(sm.file_paths)
        if len(filtered) < 2:
            return ArchitectureAnalysis(
                requested=True,
                pattern="unknown",
                limitations=["Arquitectura no inferida: proyecto sin archivos de codigo suficientes"],
            )

        # Step 2: domain clustering
        domains = self._cluster_domains(filtered)

        # Step 3: layer detection
        pattern, layers = self._detect_layers(filtered)
        if pattern in (None, "flat", "unknown"):
            if pattern == "flat":
                limitations.append("Patron de capas no detectado: proyecto con estructura plana")
            elif pattern == "unknown":
                limitations.append("Patron de capas no reconocido: estructura de directorios sin senales claras")

        # Step 4: bounded context inference
        bounded_contexts = self._infer_bounded_contexts(domains, graph)

        # Overall confidence
        confidence: Literal["high", "medium", "low"]
        if len(domains) >= 3 and pattern not in (None, "unknown"):
            confidence = "high"
        elif len(domains) >= 1:
            confidence = "medium"
        else:
            confidence = "low"

        method = "graph+heuristic" if graph is not None else "heuristic"

        return ArchitectureAnalysis(
            requested=True,
            pattern=pattern,
            domains=domains,
            layers=layers,
            bounded_contexts=bounded_contexts,
            confidence=confidence,
            method=method,
            limitations=limitations,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_tooling(self, path: str) -> bool:
        norm = path.replace("\\", "/")
        return any(norm.startswith(p) for p in _TOOLING_PREFIXES)

    def _filter_paths(self, paths: list[str]) -> list[str]:
        result = []
        for p in paths:
            norm = p.replace("\\", "/")
            if self._is_tooling(norm):
                continue
            ext = Path(norm).suffix.lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            result.append(norm)
        return result

    def _extract_domain_segment(self, path: str) -> str:
        parts = path.replace("\\", "/").split("/")
        if len(parts) == 1:
            return "root"
        first = parts[0]
        if first in _SRC_TRANSPARENT and len(parts) >= 3:
            return parts[1]
        return first

    def _cluster_domains(self, paths: list[str]) -> list[ArchitectureDomain]:
        groups: dict[str, list[str]] = {}
        for p in paths:
            seg = self._extract_domain_segment(p)
            groups.setdefault(seg, []).append(p)

        domains: list[ArchitectureDomain] = []
        for name, files in groups.items():
            if len(files) < 2:
                continue
            role = DOMAIN_ROLES.get(name, "")
            domain_confidence: Literal["high", "medium", "low"]
            if name in DOMAIN_ROLES:
                domain_confidence = "high"
            elif len(name) >= 5 and name not in _GENERIC_NAMES:
                domain_confidence = "medium"
            else:
                domain_confidence = "low"
            domains.append(ArchitectureDomain(name=name, files=files, role=role, confidence=domain_confidence))
        return domains

    def _detect_layers(self, paths: list[str]) -> tuple[str, list[ArchitectureLayer]]:
        dir_names: set[str] = set()
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            for part in parts[:-1]:
                dir_names.add(part.lower())

        best_pattern = ""
        best_score = 0
        best_matched: dict[str, list[str]] = {}

        for pattern_name, layer_keys in LAYER_PATTERNS.items():
            matched: dict[str, list[str]] = {}
            for layer_key, keywords in layer_keys.items():
                matched_dirs = [d for d in dir_names if d in keywords]
                if matched_dirs:
                    matched[layer_key] = matched_dirs
            score = len(matched)
            if score > best_score:
                best_score = score
                best_pattern = pattern_name
                best_matched = matched

        if best_score < 2:
            # Check depth for flat vs unknown
            max_depth = max(
                len(p.replace("\\", "/").split("/")) - 1
                for p in paths
            )
            flat_pattern = "flat" if max_depth <= 2 else "unknown"
            return flat_pattern, []

        # Build ArchitectureLayer list
        layer_confidence: Literal["high", "medium", "low"] = "high" if best_score >= 3 else "medium"
        layers: list[ArchitectureLayer] = []
        for layer_key, matched_dirs in best_matched.items():
            matched_files = [
                p for p in paths
                if any(
                    seg.lower() in matched_dirs
                    for seg in p.replace("\\", "/").split("/")[:-1]
                )
            ]
            layers.append(ArchitectureLayer(
                name=layer_key,
                pattern=best_pattern,
                files=matched_files,
                confidence=layer_confidence,
            ))

        return best_pattern or "unknown", layers

    def _infer_bounded_contexts(
        self,
        domains: list[ArchitectureDomain],
        graph: Optional[ModuleGraph],
    ) -> list[BoundedContext]:
        # Priority 1: use graph SCCs when available
        if graph is not None:
            sccs = self._find_sccs(graph)
            bc_list: list[BoundedContext] = []
            for scc in sccs:
                if len(scc) < 2:
                    continue
                # Derive name from most frequent directory segment among nodes
                seg_counts: dict[str, int] = {}
                for node_id in scc:
                    parts = node_id.replace("\\", "/").split("/")
                    seg = parts[0] if len(parts) == 1 else parts[-2] if len(parts) >= 2 else parts[0]
                    seg_counts[seg] = seg_counts.get(seg, 0) + 1
                name = max(seg_counts, key=lambda k: seg_counts[k])
                if name in _GENERIC_NAMES:
                    continue
                bc_conf: Literal["high", "medium", "low"] = "high" if name in DOMAIN_ROLES else "medium"
                bc_list.append(BoundedContext(name=name, modules=scc, confidence=bc_conf))
            if bc_list:
                return bc_list

        # Priority 2: use domains as candidates
        bc_from_domains: list[BoundedContext] = []
        for d in domains:
            if d.name in _GENERIC_NAMES:
                continue
            if d.confidence == "low":
                continue
            bc_from_domains.append(BoundedContext(
                name=d.name,
                modules=d.files,
                confidence="medium",
            ))
        return bc_from_domains

    def _find_sccs(self, graph: ModuleGraph) -> list[list[str]]:
        """Kosaraju's algorithm for strongly connected components."""
        import_edges = [
            (e.source, e.target)
            for e in graph.edges
            if e.kind == "imports"
        ]
        node_ids = [n.id for n in graph.nodes]

        # Build adjacency lists
        adj: dict[str, list[str]] = {n: [] for n in node_ids}
        radj: dict[str, list[str]] = {n: [] for n in node_ids}
        for src, tgt in import_edges:
            if src in adj and tgt in adj:
                adj[src].append(tgt)
                radj[tgt].append(src)

        visited: set[str] = set()
        order: list[str] = []

        def dfs1(v: str) -> None:
            stack = [(v, False)]
            while stack:
                node, done = stack.pop()
                if done:
                    order.append(node)
                    continue
                if node in visited:
                    continue
                visited.add(node)
                stack.append((node, True))
                for nb in adj[node]:
                    if nb not in visited:
                        stack.append((nb, False))

        for n in node_ids:
            if n not in visited:
                dfs1(n)

        visited2: set[str] = set()
        sccs: list[list[str]] = []

        def dfs2(v: str) -> list[str]:
            comp: list[str] = []
            stack = [v]
            while stack:
                node = stack.pop()
                if node in visited2:
                    continue
                visited2.add(node)
                comp.append(node)
                for nb in radj[node]:
                    if nb not in visited2:
                        stack.append(nb)
            return comp

        for n in reversed(order):
            if n not in visited2:
                comp = dfs2(n)
                if len(comp) >= 2:
                    sccs.append(comp)

        return sccs
