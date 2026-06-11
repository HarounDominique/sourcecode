from __future__ import annotations

import re
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
    "cqrs":               8,
    "clean":              7,
    "onion":              6,
    "hexagonal":          5,
    "monorepo":           4,
    "spring_mvc_layered": 3,
    "mvc":                3,
    "layered":            2,
    "fullstack":          1,
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

        # Step 1b: DDD filesystem detection — runs before the filtered-paths guard
        # because DDD signals live in directory structure, not just file extensions.
        ddd_result = self._detect_ddd(sm.file_paths)
        if ddd_result is not None:
            ddd_pattern, ddd_layers, ddd_contexts, ddd_layer_names = ddd_result
            module_files = self._build_ddd_module_files(sm.file_paths, ddd_contexts)
            # Use DDD bounded context names as domains so --architecture shows each
            # context as a distinct domain instead of collapsing all files under
            # the Maven path segment (e.g. "java").
            domains_for_ddd = [
                ArchitectureDomain(
                    name=n,
                    files=module_files.get(n, []),
                    role="DDD bounded context",
                    confidence="high",
                )
                for n in ddd_contexts
            ]
            bc_list = [
                BoundedContext(name=n, modules=module_files.get(n, []), confidence="high")
                for n in ddd_contexts
            ]
            # Secondary pass: scan Java files for custom annotations and base classes.
            java_paths = [p for p in sm.file_paths if p.endswith(".java")]
            _custom_ann, _base_cls = self._scan_java_patterns(root, java_paths)
            _method = (
                "filesystem_heuristic+annotation_scan"
                if (_custom_ann or _base_cls)
                else "filesystem_inference"
            )
            return ArchitectureAnalysis(
                requested=True,
                pattern=ddd_pattern,
                domains=domains_for_ddd,
                layers=ddd_layers,
                bounded_contexts=bc_list,
                ddd_layers_detected=ddd_layer_names,
                confidence="high",
                method=_method,
                limitations=[],
                evidence=[{
                    "type": "filesystem_naming",
                    "paths": [f"{ddd_contexts[0]}/" if ddd_contexts else ""],
                    "reason": (
                        f"DDD layout detected: {len(ddd_contexts)} modules under common prefix "
                        "each contain application/, domain/, infrastructure/ subdirectories."
                    ),
                    "confidence": "high",
                }],
                tentative=False,
                custom_annotations=_custom_ann,
                base_classes=_base_cls,
            )

        if len(filtered) < 2:
            return ArchitectureAnalysis(
                requested=True,
                pattern="unknown",
                limitations=["Architecture not inferred: insufficient source files in project"],
                evidence=[{"type": "none", "paths": [], "reason": "insufficient source files", "confidence": "high"}],
                tentative=False,
            )

        # Step 2: domain clustering
        domains = self._cluster_domains(filtered)

        # Step 3: layer detection
        pattern, layers = self._detect_layers(filtered)
        if pattern in (None, "flat", "unknown"):
            if pattern == "flat":
                limitations.append("Layer pattern not detected: project has a flat directory structure")
            elif pattern == "unknown":
                limitations.append("Unrecognized layer pattern: directory structure has no clear architectural signals")

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
                    "Workspace config detected — architecture reflects package topology"
                )
                ws_files = [p for p in sm.file_paths if p.split("/")[-1] in _WORKSPACE_CONFIG_FILES]
                evidence.append({
                    "type": "workspace_config",
                    "paths": ws_files[:4],
                    "reason": "Monorepo workspace config file(s) detected — hard evidence for monorepo topology",
                    "confidence": "high",
                })

        # Step 4: bounded context inference
        bounded_contexts = self._infer_bounded_contexts(domains, graph, sm.file_paths)

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
                        "Pattern inferred from directory structure; import graph not available — structural dependency direction unverified"
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

    def _detect_ddd(
        self, paths: list[str]
    ) -> "Optional[tuple[str, list[ArchitectureLayer], list[str], list[str]]]":
        """Detect DDD: ≥5 modules under a common prefix each with application/domain/infrastructure."""
        _DDD_LAYERS = frozenset({"application", "domain", "infrastructure"})
        _DDD_MIN_MODULES = 5

        # Map (prefix, module) → set of DDD layer names found under that module
        prefix_module_layers: dict[tuple[str, str], set[str]] = {}

        for p in paths:
            parts = p.replace("\\", "/").split("/")
            for i, part in enumerate(parts):
                if part in _DDD_LAYERS and i >= 2:
                    module = parts[i - 1]
                    prefix = "/".join(parts[:i - 1])
                    key = (prefix, module)
                    prefix_module_layers.setdefault(key, set()).add(part)
                    break

        # Group by prefix; find prefixes where ≥5 modules have all 3 DDD layers
        prefix_modules: dict[str, list[str]] = {}
        for (prefix, module), layers_found in prefix_module_layers.items():
            if _DDD_LAYERS <= layers_found:  # module has all 3
                prefix_modules.setdefault(prefix, []).append(module)

        best_prefix = max(
            prefix_modules,
            key=lambda p: len(prefix_modules[p]),
            default=None,
        )
        if best_prefix is None or len(prefix_modules[best_prefix]) < _DDD_MIN_MODULES:
            return None

        bounded_context_names = sorted(set(prefix_modules[best_prefix]))
        ddd_layer_names = sorted(_DDD_LAYERS)

        arch_layers: list[ArchitectureLayer] = [
            ArchitectureLayer(
                name=layer,
                pattern="ddd",
                files=[
                    p for p in paths
                    if f"/{layer}/" in p.replace("\\", "/")
                ],
                confidence="high",
            )
            for layer in ddd_layer_names
        ]
        return "ddd", arch_layers, bounded_context_names, ddd_layer_names

    def _build_ddd_module_files(
        self, paths: list[str], bounded_context_names: list[str]
    ) -> "dict[str, list[str]]":
        """Build a mapping of DDD module name → list of file paths."""
        _DDD_LAYERS = frozenset({"application", "domain", "infrastructure"})
        module_files: dict[str, list[str]] = {}
        for p in paths:
            parts = p.replace("\\", "/").split("/")
            for i, part in enumerate(parts):
                if part in _DDD_LAYERS and i >= 2:
                    mod = parts[i - 1]
                    if mod in bounded_context_names:
                        module_files.setdefault(mod, []).append(p)
                    break
        return module_files

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

        # 2. Spring domain-module detection (petclinic-style: deep common prefix + feature dirs)
        spring_result = self._detect_spring_domain_modules(source_paths)
        if spring_result is not None:
            return spring_result

        # 3. Microservices structural detection (before file-naming heuristics)
        microservices_result = self._detect_microservices(source_paths)
        if microservices_result is not None:
            return microservices_result

        # 5. Functional file-naming heuristic: *_analyzer.py, cli.py, schema.py, …
        func_result = self._detect_layered_functional(source_paths)
        if func_result is not None:
            return func_result

        # 6. Modular sub-package heuristic: ≥2 distinct named sub-packages
        modular_result = self._detect_modular(source_paths)
        if modular_result is not None:
            return modular_result

        # 7. Fallback: flat (shallow) vs truly unknown (deep but unrecognised)
        max_depth = max(
            (len(p.replace("\\", "/").split("/")) - 1 for p in source_paths),
            default=0,
        )
        return ("flat" if max_depth <= 2 else "unknown"), []

    def _detect_spring_domain_modules(
        self, paths: list[str]
    ) -> Optional[tuple[str, list[ArchitectureLayer]]]:
        """Detect Spring Boot domain-organized packages (petclinic-style).

        When all source paths share a deep common package prefix
        (e.g. src/main/java/org/springframework/samples/petclinic/),
        strips that prefix and detects feature/domain modules in the remainder.
        Requires ≥3 distinct domain directories to avoid false positives.
        """
        if len(paths) < 6:
            return None

        parts_list = [p.replace("\\", "/").split("/") for p in paths]
        min_depth = min(len(p) for p in parts_list)
        common_depth = 0
        for i in range(min_depth - 1):
            seg = parts_list[0][i]
            if all(pl[i] == seg for pl in parts_list):
                common_depth = i + 1
            else:
                break

        if common_depth < 3:
            return None

        module_files: dict[str, list[str]] = {}
        for orig, pl in zip(paths, parts_list):
            remaining = pl[common_depth:]
            if remaining and remaining[0] not in _GENERIC_NAMES:
                module_files.setdefault(remaining[0], []).append(orig)

        meaningful = {k: v for k, v in module_files.items() if len(v) >= 2}
        if len(meaningful) < 3:
            return None

        return "spring_mvc_layered", [
            ArchitectureLayer(
                name=k, pattern="spring_mvc_layered", files=v, confidence="medium"
            )
            for k, v in meaningful.items()
        ]

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

    @staticmethod
    def _maven_module_bounded_contexts(file_paths: list[str]) -> list[BoundedContext]:
        """Priority 0: extract bounded contexts from Maven module directory names.

        Maven multi-module projects have structure: <module>/src/main/java/...
        The module directory name is a strong bounded context signal
        (e.g. broadleaf-order, keycloak-services → order, services).
        Strips common project-name prefixes (longest common prefix across modules).
        Returns empty list when fewer than 2 distinct modules are found.
        """
        import re as _re
        _MAVEN_SRC = "src/main/java/"
        _MAVEN_TEST = "src/test/java/"
        module_names: dict[str, list[str]] = {}  # module_name → [files]
        for p in file_paths:
            norm = p.replace("\\", "/")
            for marker in (_MAVEN_SRC, _MAVEN_TEST):
                idx = norm.find(marker)
                if idx > 0:
                    # Everything before the marker is the module path
                    module_path = norm[:idx].rstrip("/")
                    # Take the last path segment as module name
                    module_seg = module_path.split("/")[-1] if "/" in module_path else module_path
                    if module_seg:
                        module_names.setdefault(module_seg, []).append(p)
                    break

        if len(module_names) < 2:
            return []

        # Strip common project-name prefix (e.g. "keycloak-", "broadleaf-")
        # by finding longest common prefix across all module names
        all_names = sorted(module_names)
        common = ""
        for i, ch in enumerate(all_names[0]):
            if all(n[i:i+1] == ch for n in all_names[1:]):
                common += ch
            else:
                break
        # Only strip prefix up to last '-' (avoid stripping into meaningful segment)
        prefix_to_strip = common[:common.rfind("-") + 1] if "-" in common else ""

        _GENERIC_EXTENDED = _GENERIC_NAMES | {
            "api", "impl", "base", "test", "tests", "main", "java",
            "integration", "parent", "bom", "platform",
        }
        bc_list: list[BoundedContext] = []
        for raw_name, files in sorted(module_names.items()):
            clean = raw_name[len(prefix_to_strip):] if prefix_to_strip else raw_name
            # Remove trailing -api, -impl, -core suffixes
            clean = _re.sub(r"-(api|impl|core|base|common|parent|test)$", "", clean)
            if not clean or clean in _GENERIC_EXTENDED:
                continue
            bc_list.append(BoundedContext(
                name=clean,
                modules=files[:20],  # cap file list
                confidence="high",
            ))
        return bc_list

    def _infer_bounded_contexts(
        self,
        domains: list[ArchitectureDomain],
        graph: Optional[ModuleGraph],
        file_paths: list[str] | None = None,
    ) -> list[BoundedContext]:
        # Priority 0: Maven module names — strong bounded context signal for Java projects
        if file_paths:
            maven_bcs = self._maven_module_bounded_contexts(file_paths)
            if maven_bcs:
                return maven_bcs

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

    # ------------------------------------------------------------------
    # Java annotation / base-class secondary scanner (C4)
    # ------------------------------------------------------------------

    _ANNOTATION_DECL_RE = re.compile(
        r"@interface\s+([A-Z][A-Za-z0-9_]+)", re.MULTILINE
    )
    _ANNOTATION_USAGE_RE = re.compile(
        r"@([A-Z][A-Za-z0-9_]+)(?:\s*\(([^)]*)\))?", re.MULTILINE
    )
    _EXTENDS_RE = re.compile(
        r"class\s+\w+\s+extends\s+([A-Z][A-Za-z0-9_]+)", re.MULTILINE
    )
    _ANNOTATION_PARAM_RE = re.compile(r"(\w+)\s*=\s*[\"']?([^,\"')]+)[\"']?")
    _MAX_JAVA_SCAN = 2000  # cap files to avoid runaway scanning

    def _scan_java_patterns(
        self,
        root: Path,
        java_paths: list[str],
    ) -> tuple[list[dict], list[dict]]:
        """Scan Java source files for custom @interface annotations and common base classes.

        Returns (custom_annotations, base_classes) lists for ArchitectureAnalysis.
        """
        from sourcecode.tree_utils import safe_read_text

        annotation_decls: dict[str, dict] = {}  # name → {params, files, usage_count}
        annotation_usage: dict[str, int] = {}    # name → total usages across codebase
        extends_counts: dict[str, int] = {}      # base_class_name → subclass count

        for path_str in java_paths[:self._MAX_JAVA_SCAN]:
            abs_path = root / path_str
            try:
                content = safe_read_text(abs_path)
            except OSError:
                continue

            # Custom annotation declarations: `public @interface FooAnnotation`
            for m in self._ANNOTATION_DECL_RE.finditer(content):
                ann_name = m.group(1)
                if ann_name not in annotation_decls:
                    # Extract parameters from the annotation body (next ~500 chars)
                    body_start = m.end()
                    body = content[body_start:body_start + 500]
                    params = re.findall(r"(?:String|int|boolean|Class)\s+(\w+)\s*\(\)", body)
                    annotation_decls[ann_name] = {
                        "name": ann_name,
                        "parameters": params,
                        "usage_count": 0,
                        "purpose": "",
                    }

            # Annotation usage frequency
            for m in self._ANNOTATION_USAGE_RE.finditer(content):
                ann_name = m.group(1)
                # Skip common Java built-ins
                if ann_name in {
                    "Override", "SuppressWarnings", "Deprecated", "FunctionalInterface",
                    "SafeVarargs", "Retention", "Target", "Documented", "Inherited",
                    "RestController", "Service", "Repository", "Component", "Controller",
                    "Autowired", "Value", "Bean", "Configuration", "SpringBootApplication",
                    "EnableAutoConfiguration", "Transactional", "RequestMapping",
                    "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
                    "PathVariable", "RequestBody", "RequestParam", "ResponseBody",
                    "NotNull", "NotBlank", "Size", "Min", "Max", "Valid",
                }:
                    continue
                annotation_usage[ann_name] = annotation_usage.get(ann_name, 0) + 1

            # extends BaseClass — count subclasses per base
            for m in self._EXTENDS_RE.finditer(content):
                base = m.group(1)
                extends_counts[base] = extends_counts.get(base, 0) + 1

        # Merge usage counts into annotation_decls
        for ann_name, info in annotation_decls.items():
            info["usage_count"] = annotation_usage.get(ann_name, 0)
            # Infer purpose from name heuristic
            name_lower = ann_name.lower()
            if "security" in name_lower or "filtro" in name_lower or "auth" in name_lower:
                info["purpose"] = "security filter / access control on REST endpoints"
            elif "valid" in name_lower or "constraint" in name_lower:
                info["purpose"] = "bean validation constraint"
            elif "cache" in name_lower:
                info["purpose"] = "caching hint"

        # Also include heavily-used annotations not declared in this repo (usage ≥ 50)
        # that were NOT already captured as custom declarations
        for ann_name, count in annotation_usage.items():
            if ann_name not in annotation_decls and count >= 50:
                annotation_decls[ann_name] = {
                    "name": ann_name,
                    "parameters": [],
                    "usage_count": count,
                    "purpose": "",
                }

        custom_annotations = sorted(
            annotation_decls.values(),
            key=lambda x: x["usage_count"],
            reverse=True,
        )

        # Base classes with > 10 subclasses
        base_classes = [
            {
                "name": base,
                "subclass_count": count,
                "role": self._infer_base_role(base),
            }
            for base, count in sorted(extends_counts.items(), key=lambda x: -x[1])
            if count > 10
        ]

        return custom_annotations, base_classes

    @staticmethod
    def _infer_base_role(class_name: str) -> str:
        name_lower = class_name.lower()
        if "restcontroller" in name_lower or "controller" in name_lower:
            return "shared exception handler / base REST controller"
        if "service" in name_lower:
            return "base service with common business-logic utilities"
        if "repository" in name_lower or "dao" in name_lower:
            return "base data access object"
        if "entity" in name_lower or "model" in name_lower:
            return "base JPA entity"
        if "dto" in name_lower or "mapper" in name_lower:
            return "base DTO / bean mapper"
        if "test" in name_lower:
            return "base test class"
        return "shared base class"
