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

_WORKSPACE_CONFIG_FILES: frozenset[str] = frozenset({
    "turbo.json", "nx.json", "pnpm-workspace.yaml", "lerna.json", "rush.json",
})

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

_TEST_DIRS: frozenset[str] = frozenset({"tests", "test", "spec", "specs", "__tests__", "e2e"})
_BENCHMARK_DIRS: frozenset[str] = frozenset({
    "benchmark", "benchmarks", "bench",
    "example", "examples",
    "demo", "demos",
    "playground", "playgrounds",
    "fixture", "fixtures",
    "sandbox",
})
_DOCS_DIRS: frozenset[str] = frozenset({"docs", "doc", "documentation", "wiki"})
_TOOLING_DIRS: frozenset[str] = frozenset({"scripts", "script", "tools", "tool", "ci"})
# All dirs that are not part of the runtime source architecture
_NON_SOURCE_DIRS: frozenset[str] = _TEST_DIRS | _BENCHMARK_DIRS | _DOCS_DIRS | _TOOLING_DIRS

# Exact file stems that signal a specific architectural layer
_LAYER_STEM_EXACT: dict[str, str] = {
    "cli":      "orchestration",
    "main":     "orchestration",
    "app":      "orchestration",
    "server":   "orchestration",
    "schema":   "data",
    "model":    "data",
    "models":   "data",
    "config":   "data",
    "settings": "data",
    "store":    "data",
}

# Suffix patterns (stem == suffix OR stem ends with "_" + suffix) → layer
_LAYER_STEM_SUFFIXES: list[tuple[str, str]] = [
    ("analyzer",   "processing"),
    ("processor",  "processing"),
    ("parser",     "processing"),
    ("detector",   "processing"),
    ("scanner",    "processing"),
    ("handler",    "orchestration"),
    ("controller", "orchestration"),
    ("repository", "data"),
    ("repo",       "data"),
    ("serializer", "data"),
    ("formatter",  "data"),
]

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
    "cqrs": {
        "commands": ["commands", "command"],
        "queries":  ["queries", "query"],
    },
    "clean": {
        "domain":         ["domain", "domains"],
        "application":    ["application", "usecases", "usecase"],
        "infrastructure": ["infrastructure", "infra", "adapters", "persistence"],
    },
    "onion": {
        "domain":      ["domain", "core"],
        "application": ["application", "usecases"],
        "ports":       ["ports", "interfaces"],
        "adapters":    ["adapters", "secondary"],
    },
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
    "monorepo": {
        "apps":     ["apps", "applications"],
        "packages": ["packages", "libs", "modules"],
    },
    "fullstack": {
        "frontend": ["frontend", "client", "web", "ui", "pages", "components", "app"],
        "backend":  ["backend", "server", "api", "services"],
    },
}

# Higher value = wins when score ties
_PATTERN_PRIORITY: dict[str, int] = {
    "cqrs":       8,
    "clean":      7,
    "onion":      6,
    "hexagonal":  5,
    "monorepo":   4,
    "mvc":        3,
    "layered":    2,
    "fullstack":  1,
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
        evidence: list[dict] = []

        # Step 1: filter paths
        filtered = self._filter_paths(sm.file_paths)
        if len(filtered) < 2:
            return ArchitectureAnalysis(
                requested=True,
                pattern="unknown",
                limitations=["Arquitectura no inferida: proyecto sin archivos de codigo suficientes"],
                evidence=[{"type": "none", "paths": [], "reason": "insufficient source files", "confidence": "high"}],
                tentative=False,
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

        # Step 3b: monorepo override — workspace config is hard evidence.
        # Overrides all weak inferred patterns; only truly specialised patterns
        # (cqrs, clean, onion, hexagonal) take precedence over workspace config.
        has_workspace = self._has_workspace_config(sm.file_paths)
        if has_workspace and pattern not in (
            "monorepo", "cqrs", "clean", "onion", "hexagonal"
        ):
            mono_layers = self._detect_monorepo_packages(filtered)
            # Override whenever: monorepo packages detected, OR pattern is any weak/generic type.
            # "fullstack", "layered", "mvc", "microservices", "modular", "flat", "unknown", None
            # all yield to workspace config evidence.
            _WEAK_PATTERNS = {None, "unknown", "flat", "modular", "layered",
                              "fullstack", "mvc", "microservices"}
            if mono_layers or pattern in _WEAK_PATTERNS:
                pattern = "monorepo"
                layers = mono_layers
                limitations.append(
                    "Workspace config detectado — arquitectura refleja topologia de paquetes"
                )
                ws_files = [p for p in sm.file_paths if p.split("/")[-1] in _WORKSPACE_CONFIG_FILES]
                evidence.append({
                    "type": "workspace_config",
                    "paths": ws_files[:4],
                    "reason": "Monorepo workspace config file(s) detected — hard evidence for monorepo topology",
                    "confidence": "high",
                })

        # Step 4: bounded context inference
        bounded_contexts = self._infer_bounded_contexts(domains, graph)

        # Overall confidence — based on domain quality, not raw count
        confidence: Literal["high", "medium", "low"]
        strong_domains = [d for d in domains if d.confidence in ("high", "medium")]
        all_layers_weak = layers and all(l.confidence == "low" for l in layers)

        method = "graph+structure" if graph is not None else "filesystem_inference"
        # High-confidence evidence (workspace config) makes pattern non-tentative.
        tentative = not any(e.get("confidence") == "high" for e in evidence)

        # _hard_evidence: high-confidence evidence was already set (e.g. workspace_config).
        # When True, tentative must stay False and confidence must stay at least "medium".
        _hard_evidence = not tentative  # tentative=False iff high-conf evidence present

        if pattern not in (None, "unknown", "flat"):
            if graph is not None:
                # Import graph provided — structural validation available
                confidence = "medium" if len(strong_domains) >= 3 else "low"
                evidence.append({
                    "type": "import_graph",
                    "paths": [n.id for n in graph.nodes[:6]],
                    "reason": f"Module import graph with {len(graph.nodes)} nodes used for pattern validation",
                    "confidence": "medium",
                })
            elif all_layers_weak:
                # Layers came from file-naming heuristic only, not directory structure
                confidence = "low"
                if not _hard_evidence:
                    tentative = True
                limitations.append(
                    "Low confidence inference: pattern inferred from filenames only, without import graph confirmation"
                )
                evidence.append({
                    "type": "filesystem_naming",
                    "paths": [l.files[0] for l in layers if l.files][:6],
                    "reason": (
                        f"Pattern '{pattern}' inferred from file stem naming conventions only "
                        "(e.g. *_controller.py, *_service.py). "
                        "No directory structure or import graph confirmation."
                    ),
                    "confidence": "low",
                })
            else:
                # Directory structure match (or monorepo/workspace override with no layers)
                confidence = "medium" if (_hard_evidence or len(strong_domains) >= 3) else "low"
                if confidence == "low" and not _hard_evidence:
                    tentative = True
                if not _hard_evidence:
                    limitations.append(
                        "Pattern not confirmed by module import graph; run with --graph-modules for structural validation"
                    )
                if not _hard_evidence:
                    matched_dirs = sorted({
                        p.replace("\\", "/").split("/")[0]
                        for layer in layers for p in layer.files
                    })
                    evidence.append({
                        "type": "filesystem_naming",
                        "paths": matched_dirs[:8],
                        "reason": (
                            f"Pattern '{pattern}' inferred from directory names matching layer keywords. "
                            "Import graph not available — structural direction of dependencies unverified."
                        ),
                        "confidence": "low" if confidence == "low" else "medium",
                    })
        elif len(strong_domains) >= 1:
            confidence = "medium"
            if not _hard_evidence:
                tentative = True
            evidence.append({
                "type": "filesystem_naming",
                "paths": [d.name for d in strong_domains[:6]],
                "reason": "Domain clustering from directory names; no layer pattern confirmed",
                "confidence": "low",
            })
        else:
            confidence = "low"
            if not _hard_evidence:
                tentative = True
            if not evidence:
                limitations.append(
                    "insufficient_evidence: no recognizable architectural signals found; "
                    "filesystem structure does not match known patterns"
                )
                evidence.append({
                    "type": "filesystem_naming",
                    "paths": filtered[:6],
                    "reason": "Only filesystem paths available; no pattern matched",
                    "confidence": "low",
                })

        return ArchitectureAnalysis(
            requested=True,
            pattern=pattern,
            domains=domains,
            layers=layers,
            bounded_contexts=bounded_contexts,
            confidence=confidence,
            method=method,
            limitations=limitations,
            evidence=evidence,
            tentative=tentative,
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
            # Exclude non-source dirs at every path segment (benchmarks, docs, tests, scripts…)
            parts = norm.split("/")
            if any(part.lower() in _NON_SOURCE_DIRS for part in parts[:-1]):
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
        if first in _SRC_TRANSPARENT:
            if len(parts) == 2:
                return "root"
            if len(parts) >= 4:
                # src/pkg/subpkg/file.py → subpkg is the meaningful module
                return parts[2]
            # src/pkg/file.py → pkg
            return parts[1]
        return first

    def _cluster_domains(self, paths: list[str]) -> list[ArchitectureDomain]:
        groups: dict[str, list[str]] = {}
        for p in paths:
            seg = self._extract_domain_segment(p)
            if seg.lower() in _TEST_DIRS:
                continue  # tests are infrastructure, not architecture domains
            groups.setdefault(seg, []).append(p)

        domains: list[ArchitectureDomain] = []
        for name, files in groups.items():
            if len(files) < 2:
                continue
            if name.lower() in _NON_SOURCE_DIRS:
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
        # Exclude non-source paths (tests, benchmarks, docs, tooling) from layer scoring
        source_paths = [
            p for p in paths
            if not any(part.lower() in _NON_SOURCE_DIRS for part in p.replace("\\", "/").split("/"))
        ]
        if not source_paths:
            return "unknown", []

        dir_names: set[str] = set()
        for p in source_paths:
            parts = p.replace("\\", "/").split("/")
            for part in parts[:-1]:
                dir_names.add(part.lower())

        # 1. Classical keyword-based pattern matching
        best_pattern = ""
        best_score = 0
        best_priority = -1
        best_matched: dict[str, list[str]] = {}

        for pattern_name, layer_keys in LAYER_PATTERNS.items():
            matched: dict[str, list[str]] = {}
            for layer_key, keywords in layer_keys.items():
                matched_dirs = [d for d in dir_names if d in keywords]
                if matched_dirs:
                    matched[layer_key] = matched_dirs
            score = len(matched)
            priority = _PATTERN_PRIORITY.get(pattern_name, 0)
            if (score, priority) > (best_score, best_priority):
                best_score = score
                best_priority = priority
                best_pattern = pattern_name
                best_matched = matched

        if best_score >= 2:
            layer_confidence: Literal["high", "medium", "low"] = "medium" if best_score >= 3 else "low"
            layers: list[ArchitectureLayer] = []
            for layer_key, matched_dirs in best_matched.items():
                matched_files = [
                    p for p in source_paths
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
            return best_pattern, layers

        # 2. Microservices structural detection (before file-naming heuristics)
        microservices_result = self._detect_microservices(source_paths)
        if microservices_result is not None:
            return microservices_result

        # 3. Functional file-naming heuristic: *_analyzer.py, cli.py, schema.py, …
        func_result = self._detect_layered_functional(source_paths)
        if func_result is not None:
            return func_result

        # 4. Modular sub-package heuristic: ≥2 distinct named sub-packages
        modular_result = self._detect_modular(source_paths)
        if modular_result is not None:
            return modular_result

        # 5. Fallback: flat (shallow) vs truly unknown (deep but unrecognised)
        max_depth = max(
            (len(p.replace("\\", "/").split("/")) - 1 for p in source_paths),
            default=0,
        )
        return ("flat" if max_depth <= 2 else "unknown"), []

    def _detect_microservices(
        self, paths: list[str]
    ) -> Optional[tuple[str, list[ArchitectureLayer]]]:
        """Detect microservices from multiple sibling service directories or services/* pattern."""
        # Pattern 1: explicit services/* subdirectories
        service_subdirs: dict[str, list[str]] = {}
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            if len(parts) >= 3 and parts[0].lower() == "services":
                service_subdirs.setdefault(parts[1], []).append(p)
        if len(service_subdirs) >= 3:
            return "microservices", [
                ArchitectureLayer(name=k, pattern="microservices", files=v, confidence="medium")
                for k, v in list(service_subdirs.items())[:8]
            ]

        # Pattern 2: multiple top-level dirs each containing a canonical entry file
        _ENTRY_FILES = {"main.go", "main.py", "server.js", "server.ts", "main.ts", "app.py"}
        entry_dirs: dict[str, list[str]] = {}
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            if len(parts) >= 2 and parts[-1].lower() in _ENTRY_FILES:
                top = parts[0]
                if top.lower() not in _SRC_TRANSPARENT and top.lower() not in _NON_SOURCE_DIRS:
                    entry_dirs.setdefault(top, []).append(p)
        if len(entry_dirs) >= 4:
            return "microservices", [
                ArchitectureLayer(name=k, pattern="microservices", files=v, confidence="low")
                for k, v in list(entry_dirs.items())[:8]
            ]

        return None

    def _detect_layered_functional(
        self, paths: list[str]
    ) -> Optional[tuple[str, list[ArchitectureLayer]]]:
        """Detect layered architecture from file-naming conventions.

        Recognises three logical layers without requiring classical directory names:
        - orchestration: cli.py, main.py, *_handler.py, *_controller.py
        - processing:    *_analyzer.py, *_processor.py, *_parser.py, *_detector.py, *_scanner.py
        - data:          schema.py, model.py, *_repository.py, *_serializer.py, config.py, …
        """
        layer_files: dict[str, list[str]] = {"orchestration": [], "processing": [], "data": []}
        for p in paths:
            stem = Path(p.replace("\\", "/").split("/")[-1]).stem.lower()
            if stem in _LAYER_STEM_EXACT:
                layer_files[_LAYER_STEM_EXACT[stem]].append(p)
                continue
            for suffix, layer in _LAYER_STEM_SUFFIXES:
                if stem == suffix or stem.endswith("_" + suffix):
                    layer_files[layer].append(p)
                    break

        non_empty = {k: v for k, v in layer_files.items() if v}
        if len(non_empty) >= 2:
            return "layered", [
                ArchitectureLayer(name=k, pattern="layered", files=v, confidence="low")
                for k, v in non_empty.items()
            ]
        return None

    def _detect_modular(
        self, paths: list[str]
    ) -> Optional[tuple[str, list[ArchitectureLayer]]]:
        """Detect modular architecture from ≥2 distinct named sub-packages.

        Each top-level (or first non-transparent) directory that is not a test or
        generic name is treated as a self-contained module.
        """
        module_files: dict[str, list[str]] = {}
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            for part in parts[:-1]:
                if (part not in _SRC_TRANSPARENT
                        and part.lower() not in _NON_SOURCE_DIRS
                        and part.lower() not in _GENERIC_NAMES):
                    module_files.setdefault(part, []).append(p)
                    break

        meaningful = {k: v for k, v in module_files.items() if len(v) >= 3}
        if len(meaningful) >= 2:
            return "modular", [
                ArchitectureLayer(name=k, pattern="modular", files=v, confidence="low")
                for k, v in meaningful.items()
            ]
        return None

    def _has_workspace_config(self, file_paths: list[str]) -> bool:
        for path in file_paths:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 1 and parts[0] in _WORKSPACE_CONFIG_FILES:
                return True
        return False

    def _detect_monorepo_packages(self, paths: list[str]) -> list[ArchitectureLayer]:
        """Find workspace packages (packages/*, apps/*, libs/*) in a monorepo."""
        _WORKSPACE_ROOTS = {"packages", "apps", "libs", "applications"}
        groups: dict[str, list[str]] = {}
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            if len(parts) >= 2 and parts[0].lower() in _WORKSPACE_ROOTS:
                key = f"{parts[0]}/{parts[1]}"
                groups.setdefault(key, []).append(p)
        result = [
            ArchitectureLayer(name=k, pattern="monorepo", files=v, confidence="medium")
            for k, v in groups.items()
            if len(v) >= 2
        ]
        return result[:16]

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
