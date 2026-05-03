from __future__ import annotations

"""Repository topology classifier for adaptive traversal.

Detects monorepo vs single-package structure, identifies source roots,
low-signal directories, and generated content. Feeds AdaptiveScanner
with per-path depth budgets so traversal is relevance-oriented, not
purely structural.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Signal tables
# ---------------------------------------------------------------------------

# Top-level dirs that almost always contain actual source code
_SOURCE_DIRS: frozenset[str] = frozenset({
    "src", "lib", "source", "sources", "core",
    "app", "server", "client", "backend", "frontend",
    "cmd", "pkg",          # Go conventions
    "main",                # Java src/main
    "kotlin", "java", "scala",  # JVM source dirs
})

# First-level dirs that act as workspace containers in monorepos
_WORKSPACE_CONTAINERS: frozenset[str] = frozenset({
    "packages", "apps", "libs", "services", "internal",
    "plugins", "modules", "components", "crates",
    "workspaces", "projects",
})

# Directories with low signal value for AI code understanding
_LOW_SIGNAL_DIRS: frozenset[str] = frozenset({
    "docs", "doc", "documentation", "docsrc", "website", "site",
    "benchmark", "benchmarks", "bench", "perf", "perfs",
    "examples", "example", "demo", "demos", "sample", "samples",
    "fixtures", "fixture", "__fixtures__",
    "scripts", "script", "tools", "tool",
    "ci", ".ci",
    "storybook", "stories", "__stories__",
    "sandbox", "playground", "playgrounds",
    "migrations", "migration",
    ".github", ".vscode", ".claude", ".cursor", ".idea",
    "themes", "theme",
    "static", "public", "assets",
})

# Directories to skip entirely — generated content and dependency stores
_GENERATED_DIRS: frozenset[str] = frozenset({
    "dist", "build", "out", "output", "release", "releases",
    "target", "coverage", ".next", ".nuxt", ".svelte-kit",
    ".turbo", "node_modules", "__pycache__",
    ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".nyc_output", ".tox",
    "generated", ".generated", "gen", "_gen",
    ".cache", "cache",
    "vendor",
    ".git",
})

# Manifest file names that mark a directory as a source package
_PACKAGE_MANIFESTS: frozenset[str] = frozenset({
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "Gemfile", "pubspec.yaml",
})

# Source file extensions — presence signals a directory has real code
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rs", ".java", ".kt", ".rb",
    ".cs", ".swift", ".scala", ".cpp", ".c", ".h",
})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SourceRoot:
    """A classified directory with traversal priority and depth budget."""
    path: str       # repo-relative path, forward slashes
    signal: str     # "high" | "medium" | "low" | "excluded"
    reason: str     # human-readable explanation
    priority: float  # 0.0–1.0 traversal priority


@dataclass
class ScanBudget:
    """Per-topology traversal budget constraints."""
    max_files: int = 2000
    base_depth: int = 4      # depth cap for unclassified paths
    source_depth: int = 8    # additional levels allowed inside source roots
    low_signal_depth: int = 2  # additional levels allowed inside low-signal roots


@dataclass
class RepoTopology:
    """Classified repository topology for adaptive traversal.

    Produced by RepoClassifier.classify() and consumed by AdaptiveScanner.
    The three root lists partition the repository's first-level directories
    into source code, low-value content, and generated/excluded content.
    """
    workspace_type: str = "unknown"  # "monorepo" | "single-package" | "unknown"
    source_roots: list[SourceRoot] = field(default_factory=list)
    low_signal_roots: list[SourceRoot] = field(default_factory=list)
    generated_roots: list[SourceRoot] = field(default_factory=list)
    package_manager: str = "unknown"
    confidence: float = 0.0
    scan_budget: ScanBudget = field(default_factory=ScanBudget)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_type": self.workspace_type,
            "source_roots": [
                {"path": r.path, "reason": r.reason, "priority": round(r.priority, 2)}
                for r in self.source_roots
            ],
            "low_signal_roots": [r.path for r in self.low_signal_roots],
            "generated_roots": [r.path for r in self.generated_roots],
            "package_manager": self.package_manager,
            "confidence": round(self.confidence, 2),
            "scan_budget": {
                "base_depth": self.scan_budget.base_depth,
                "source_depth": self.scan_budget.source_depth,
                "low_signal_depth": self.scan_budget.low_signal_depth,
            },
        }


# ---------------------------------------------------------------------------
# RepoClassifier
# ---------------------------------------------------------------------------

class RepoClassifier:
    """Detects repository topology and classifies directories for adaptive traversal.

    Reads workspace config files (pnpm-workspace.yaml, package.json workspaces,
    turbo.json, nx.json, lerna.json, go.work, Cargo.toml), resolves package
    glob patterns, and identifies which directories contain real source code
    vs. docs, benchmarks, or generated content.

    Classification is fast: only depth-0 and depth-1 filesystem reads.
    """

    def classify(self, root: Path) -> RepoTopology:
        """Classify the repository at *root* and return its topology."""
        topology = RepoTopology()
        topology.package_manager = self._detect_package_manager(root)

        markers = self._detect_markers(root)
        workspace_patterns = self._read_workspace_patterns(root, markers)

        try:
            root_children = [
                d for d in sorted(root.iterdir())
                if d.is_dir() and not d.is_symlink()
            ]
        except PermissionError:
            root_children = []

        source_roots = self._find_source_roots(
            root, root_children, workspace_patterns, bool(markers) or bool(workspace_patterns)
        )
        low_signal_roots = self._find_low_signal_roots(root, root_children, source_roots)
        generated_roots = self._find_generated_roots(root, root_children)

        # Monorepo heuristic: explicit markers OR multiple packages found via
        # workspace containers (packages/*, apps/*, etc.) without top-level src/
        container_sourced = [
            r for r in source_roots
            if "container:" in r.reason or "workspace:" in r.reason
        ]
        has_top_level_src = any(
            r.reason == "top_level_source" for r in source_roots
        )
        is_monorepo = (
            bool(markers)
            or bool(workspace_patterns)
            or (len(container_sourced) >= 2 and not has_top_level_src)
        )
        topology.workspace_type = "monorepo" if is_monorepo else "single-package"

        topology.source_roots = sorted(source_roots, key=lambda r: -r.priority)
        topology.low_signal_roots = low_signal_roots
        topology.generated_roots = generated_roots
        topology.confidence = self._compute_confidence(topology, is_monorepo)
        topology.scan_budget = self._compute_budget(topology)

        return topology

    # ------------------------------------------------------------------
    # Package manager detection
    # ------------------------------------------------------------------

    def _detect_package_manager(self, root: Path) -> str:
        if (root / "pnpm-lock.yaml").exists() or (root / "pnpm-workspace.yaml").exists():
            return "pnpm"
        if (root / "yarn.lock").exists():
            return "yarn"
        if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            return "bun"
        if (root / "package-lock.json").exists():
            return "npm"
        if (root / "go.work").exists():
            return "go-workspace"
        if (root / "go.mod").exists():
            return "go-modules"
        if (root / "Cargo.toml").exists():
            return "cargo"
        if (root / "uv.lock").exists():
            return "uv"
        if (root / "Pipfile").exists():
            return "pipenv"
        if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            return "python"
        return "unknown"

    # ------------------------------------------------------------------
    # Workspace marker detection
    # ------------------------------------------------------------------

    def _detect_markers(self, root: Path) -> list[str]:
        """Return list of workspace marker file names present at root."""
        markers: list[str] = []
        for name in ("pnpm-workspace.yaml", "go.work", "turbo.json", "lerna.json", "nx.json"):
            if (root / name).exists():
                markers.append(name)

        cargo = root / "Cargo.toml"
        if cargo.exists():
            try:
                content = cargo.read_text(encoding="utf-8", errors="replace")
                if "[workspace]" in content:
                    markers.append("Cargo.toml[workspace]")
            except OSError:
                pass

        pkg = root / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
                if "workspaces" in data:
                    markers.append("package.json[workspaces]")
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        return markers

    # ------------------------------------------------------------------
    # Workspace pattern extraction from config files
    # ------------------------------------------------------------------

    def _read_workspace_patterns(self, root: Path, markers: list[str]) -> list[str]:
        """Extract glob patterns from workspace config files."""
        patterns: list[str] = []

        if "pnpm-workspace.yaml" in markers:
            patterns.extend(self._patterns_from_pnpm(root))

        if "package.json[workspaces]" in markers:
            patterns.extend(self._patterns_from_npm_workspaces(root))

        if "nx.json" in markers:
            patterns.extend(self._patterns_from_nx(root))

        if "lerna.json" in markers:
            patterns.extend(self._patterns_from_lerna(root))

        if "Cargo.toml[workspace]" in markers:
            patterns.extend(self._patterns_from_cargo_workspace(root))

        if "go.work" in markers:
            patterns.extend(self._patterns_from_go_work(root))

        return list(dict.fromkeys(patterns))  # deduplicate, preserve order

    def _patterns_from_pnpm(self, root: Path) -> list[str]:
        try:
            content = (root / "pnpm-workspace.yaml").read_text(encoding="utf-8", errors="replace")
            result = []
            for line in content.splitlines():
                stripped = line.strip().lstrip("- ").strip("'\"")
                if stripped and not stripped.startswith("#"):
                    result.append(stripped)
            return result
        except OSError:
            return []

    def _patterns_from_npm_workspaces(self, root: Path) -> list[str]:
        try:
            data = json.loads((root / "package.json").read_text(encoding="utf-8", errors="replace"))
            ws = data.get("workspaces", [])
            if isinstance(ws, list):
                return [str(p) for p in ws]
            if isinstance(ws, dict):
                return [str(p) for p in ws.get("packages", [])]
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return []

    def _patterns_from_nx(self, root: Path) -> list[str]:
        try:
            data = json.loads((root / "nx.json").read_text(encoding="utf-8", errors="replace"))
            patterns = []
            wl = data.get("workspaceLayout", {})
            if "appsDir" in wl:
                patterns.append(f"{wl['appsDir']}/*")
            if "libsDir" in wl:
                patterns.append(f"{wl['libsDir']}/*")
            return patterns
        except (json.JSONDecodeError, OSError, ValueError):
            return []

    def _patterns_from_lerna(self, root: Path) -> list[str]:
        try:
            data = json.loads((root / "lerna.json").read_text(encoding="utf-8", errors="replace"))
            pkgs = data.get("packages", ["packages/*"])
            return [str(p) for p in pkgs] if isinstance(pkgs, list) else []
        except (json.JSONDecodeError, OSError, ValueError):
            return []

    def _patterns_from_cargo_workspace(self, root: Path) -> list[str]:
        try:
            content = (root / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
            in_members = False
            patterns = []
            for line in content.splitlines():
                stripped = line.strip()
                if "members" in stripped and "=" in stripped:
                    in_members = True
                if in_members:
                    for quote in ('"', "'"):
                        if quote in stripped:
                            for segment in stripped.split(quote):
                                segment = segment.strip(" [],")
                                if segment and "/" in segment:
                                    patterns.append(segment)
                    if "]" in stripped:
                        in_members = False
            return patterns
        except OSError:
            return []

    def _patterns_from_go_work(self, root: Path) -> list[str]:
        try:
            content = (root / "go.work").read_text(encoding="utf-8", errors="replace")
            patterns = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("use "):
                    target = stripped[4:].strip().strip("()")
                    if target and target != ".":
                        patterns.append(target.removeprefix("./").rstrip("/"))
                elif stripped.startswith("./") and not stripped.startswith("//"):
                    patterns.append(stripped.removeprefix("./").rstrip())
            return patterns
        except OSError:
            return []

    # ------------------------------------------------------------------
    # Source root discovery
    # ------------------------------------------------------------------

    def _find_source_roots(
        self,
        root: Path,
        root_children: list[Path],
        workspace_patterns: list[str],
        is_monorepo: bool,
    ) -> list[SourceRoot]:
        """Identify directories that contain actual source code."""
        result: list[SourceRoot] = []
        seen: set[str] = set()

        def _add(path_str: str, reason: str, priority: float) -> None:
            if path_str not in seen:
                seen.add(path_str)
                result.append(SourceRoot(
                    path=path_str, signal="high", reason=reason, priority=priority
                ))

        # 1. Resolve workspace glob patterns → packages → src/
        for pattern in workspace_patterns:
            try:
                for pkg_dir in sorted(root.glob(pattern)):
                    if not pkg_dir.is_dir() or pkg_dir.is_symlink():
                        continue
                    try:
                        rel = pkg_dir.relative_to(root)
                    except ValueError:
                        continue
                    rel_str = str(rel).replace("\\", "/")
                    if not self._is_allowed_path(rel_str):
                        continue

                    found_src = False
                    for src_name in ("src", "lib", "source"):
                        src_dir = pkg_dir / src_name
                        if src_dir.is_dir() and not src_dir.is_symlink():
                            _add(f"{rel_str}/{src_name}", f"workspace:{pattern}", 0.92)
                            found_src = True

                    if not found_src and self._has_source_signal(pkg_dir):
                        _add(rel_str, f"workspace_flat:{pattern}", 0.72)
            except Exception:
                continue

        # 2. Check known workspace container dirs even without explicit patterns
        for child in root_children:
            name = child.name
            if name not in _WORKSPACE_CONTAINERS:
                continue
            try:
                for pkg_dir in sorted(child.iterdir()):
                    if not pkg_dir.is_dir() or pkg_dir.is_symlink():
                        continue
                    try:
                        rel = pkg_dir.relative_to(root)
                    except ValueError:
                        continue
                    rel_str = str(rel).replace("\\", "/")
                    if not self._is_allowed_path(rel_str):
                        continue

                    found_src = False
                    for src_name in ("src", "lib", "source"):
                        src_dir = pkg_dir / src_name
                        if src_dir.is_dir() and not src_dir.is_symlink():
                            _add(f"{rel_str}/{src_name}", f"container:{name}", 0.88)
                            found_src = True

                    if not found_src and self._has_source_signal(pkg_dir):
                        _add(rel_str, f"container_flat:{name}", 0.68)
            except PermissionError:
                continue

        # 3. Top-level source dirs (single-package repos or workspace containers)
        for child in root_children:
            name = child.name
            if name in _SOURCE_DIRS and name not in _GENERATED_DIRS:
                try:
                    rel_str = str(child.relative_to(root)).replace("\\", "/")
                    _add(rel_str, "top_level_source", 0.95)
                except ValueError:
                    pass

        # 4. Workspace containers themselves if they contain source files at root
        for child in root_children:
            name = child.name
            if name in _WORKSPACE_CONTAINERS and name not in _GENERATED_DIRS:
                try:
                    rel_str = str(child.relative_to(root)).replace("\\", "/")
                except ValueError:
                    continue
                if rel_str not in seen and self._has_source_signal(child):
                    _add(rel_str, f"workspace_container_source:{name}", 0.55)

        return result

    def _has_source_signal(self, directory: Path) -> bool:
        """Return True if directory has a manifest or source files."""
        for name in _PACKAGE_MANIFESTS:
            if (directory / name).exists():
                return True
        try:
            for entry in directory.iterdir():
                if entry.is_file() and entry.suffix.lower() in _SOURCE_EXTENSIONS:
                    return True
                if entry.name in _PACKAGE_MANIFESTS:
                    return True
        except PermissionError:
            pass
        return False

    def _is_allowed_path(self, rel_str: str) -> bool:
        parts = rel_str.split("/")
        return all(p not in _GENERATED_DIRS for p in parts)

    # ------------------------------------------------------------------
    # Low-signal root discovery
    # ------------------------------------------------------------------

    def _find_low_signal_roots(
        self,
        root: Path,
        root_children: list[Path],
        source_roots: list[SourceRoot],
    ) -> list[SourceRoot]:
        """Identify root-level directories with low signal value."""
        top_source_names = {sr.path.split("/")[0] for sr in source_roots}
        low_signal: list[SourceRoot] = []

        for child in root_children:
            name = child.name
            if name in top_source_names or name in _GENERATED_DIRS:
                continue
            try:
                rel_str = str(child.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue

            if name in _LOW_SIGNAL_DIRS:
                low_signal.append(SourceRoot(
                    path=rel_str, signal="low",
                    reason=f"low_signal:{name}", priority=0.15,
                ))
            elif name.startswith("."):
                low_signal.append(SourceRoot(
                    path=rel_str, signal="low",
                    reason="hidden_dir", priority=0.05,
                ))

        return low_signal

    # ------------------------------------------------------------------
    # Generated root discovery
    # ------------------------------------------------------------------

    def _find_generated_roots(
        self,
        root: Path,
        root_children: list[Path],
    ) -> list[SourceRoot]:
        """Identify root-level generated/excluded directories."""
        generated: list[SourceRoot] = []
        for child in root_children:
            name = child.name
            if name in _GENERATED_DIRS:
                generated.append(SourceRoot(
                    path=name, signal="excluded",
                    reason=f"generated:{name}", priority=0.0,
                ))
        return generated

    # ------------------------------------------------------------------
    # Budget and confidence
    # ------------------------------------------------------------------

    def _compute_confidence(self, topology: RepoTopology, is_monorepo: bool) -> float:
        sc = len(topology.source_roots)
        if sc >= 5:
            return 0.95
        if sc >= 2:
            return 0.85
        if sc >= 1:
            return 0.75 if is_monorepo else 0.80
        return 0.30

    def _compute_budget(self, topology: RepoTopology) -> ScanBudget:
        if topology.workspace_type == "monorepo":
            return ScanBudget(
                max_files=2000,
                base_depth=4,
                source_depth=8,
                low_signal_depth=2,
            )
        return ScanBudget(
            max_files=2000,
            base_depth=6,
            source_depth=8,
            low_signal_depth=2,
        )
