from __future__ import annotations

"""Classifies workspace packages into architectural roles using weighted inference.

Signal priority (descending):
  scripts.start/serve → runtime_core/backend_runtime
  scripts.cli/bin     → runtime_core (CLI tool)
  runtime dep imports → backend_runtime / frontend_runtime
  name/path patterns  → specialized roles
  fan-in              → weak corroboration only, NEVER sole driver
"""

import json
import re
from pathlib import Path
from typing import Any

from sourcecode.schema import MonorepoPackageInfo

# --------------------------------------------------------------------------
# Noise / negative patterns — packages matching these are NOT runtime_core
# regardless of fan-in.
# --------------------------------------------------------------------------

_NEGATIVE_RUNTIME_CORE: list[re.Pattern[str]] = [
    re.compile(r"(?:^|/)utils?(?:/|$)"),
    re.compile(r"(?:^|/)shared(?:/|$)"),
    re.compile(r"(?:^|/)types?(?:/|$)"),
    re.compile(r"(?:^|/)config(?:s)?(?:/|$)"),
    re.compile(r"(?:^|/)common(?:/|$)"),
    re.compile(r"(?:^|/)helpers?(?:/|$)"),
    re.compile(r"(?:^|/)constants?(?:/|$)"),
    re.compile(r"(?:^|/)fixture[s]?(?:/|$)"),
    re.compile(r"(?:^|/)tooling(?:/|$)"),
    re.compile(r"(?:^|/)preset[s]?(?:/|$)"),
    re.compile(r"(?:^|/)theme[s]?(?:/|$)"),
    re.compile(r"(?:^|/)i18n(?:/|$)"),
    re.compile(r"(?:^|/)locale[s]?(?:/|$)"),
]

_NEGATIVE_NAME_RUNTIME_CORE: list[re.Pattern[str]] = [
    re.compile(r"\butils?\b"),
    re.compile(r"\bshared\b"),
    re.compile(r"\btypes?\b"),
    re.compile(r"\bconfig\b"),
    re.compile(r"\bcommon\b"),
    re.compile(r"\bhelpers?\b"),
    re.compile(r"\bconstants?\b"),
    re.compile(r"\bfixtures?\b"),
    re.compile(r"\btooling\b"),
    re.compile(r"\btheme[s]?\b"),
    re.compile(r"\bi18n\b"),
    re.compile(r"\blocale[s]?\b"),
    re.compile(r"\bpreset[s]?\b"),
    re.compile(r"eslint|prettier|lint|format"),
]

# --------------------------------------------------------------------------
# Role-determining pattern tables
# --------------------------------------------------------------------------

_PATH_SIGNALS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"benchmark|bench"),                                               "benchmark_layer",    4),
    (re.compile(r"(?:^|/)docs?(?:/|$)"),                                          "docs_layer",         4),
    (re.compile(r"(?:^|/)tests?(?:/|$)|(?:^|/)specs?(?:/|$)|(?:^|/)e2e(?:/|$)"), "test_layer",         4),
    (re.compile(r"example|demo|playground|fixture|sandbox"),                       "benchmark_layer",    3),
    (re.compile(r"(?:^|/)plugins?(?:/|$)"),                                        "plugin_package",     3),
    (re.compile(r"(?:^|/)presets?(?:/|$)"),                                        "composition_layer",  3),
    (re.compile(r"infra(?:structure)?"),                                            "infrastructure_layer", 2),
    (re.compile(r"(?:^|/)client(?:/|$)|(?:^|/)web(?:/|$)|(?:^|/)ui(?:/|$)"),     "frontend_runtime",   2),
    (re.compile(r"(?:^|/)server(?:/|$)"),                                          "backend_runtime",    3),
    (re.compile(r"(?:^|/)app(?:/|$)"),                                             "backend_runtime",    2),
]

_NAME_SIGNALS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"benchmark|bench"),                                  "benchmark_layer",    4),
    (re.compile(r"\bplugin"),                                         "plugin_package",     3),
    (re.compile(r"\bpreset"),                                         "composition_layer",  3),
    (re.compile(r"client|web|react|frontend|\bui\b"),                 "frontend_runtime",   2),
    (re.compile(r"\bserver\b|\bapi\b|\bbackend\b"),                   "backend_runtime",    3),
    (re.compile(r"database|db|storage|cache|queue|redis|pg|sql"),     "infrastructure_layer", 2),
    (re.compile(r"\bcore\b|\bkernel\b|\bruntime\b|\bengine\b|\bapp\b"), "runtime_core",     2),
    (re.compile(r"test|spec|e2e"),                                    "test_layer",         3),
    (re.compile(r"lint|format|build|webpack|vite|eslint|prettier|tsconfig"), "tooling_layer", 3),
    (re.compile(r"docs?$"),                                           "docs_layer",         3),
]

# Scripts that provide hard runtime evidence
_RUNTIME_SCRIPTS: frozenset[str] = frozenset({"start", "serve", "server"})
_DEV_SCRIPTS: frozenset[str] = frozenset({"dev", "develop", "watch"})
_CLI_SCRIPTS: frozenset[str] = frozenset({"cli", "bin", "run"})

_SCRIPT_SIGNALS: dict[str, tuple[str, int]] = {
    "start":     ("backend_runtime", 4),
    "serve":     ("backend_runtime", 4),
    "server":    ("backend_runtime", 4),
    "cli":       ("backend_runtime", 3),
    "dev":       ("backend_runtime", 2),
    "develop":   ("backend_runtime", 2),
    "benchmark": ("benchmark_layer", 4),
    "bench":     ("benchmark_layer", 4),
    "test":      ("test_layer",      3),
    "lint":      ("tooling_layer",   2),
    "format":    ("tooling_layer",   2),
}

_DEP_SIGNALS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"^react$|^vue$|^@angular/core$|^svelte$"),               "frontend_runtime",    3),
    (re.compile(r"^express$|^fastify$|^koa$|^@nestjs/core$|^hono$|^elysia$"), "backend_runtime", 3),
    (re.compile(r"^sequelize$|^prisma$|^typeorm$|^mongoose$|^knex$|^pg$"),"infrastructure_layer",2),
    (re.compile(r"^jest$|^mocha$|^vitest$|^cypress$|^playwright$"),       "test_layer",          3),
    (re.compile(r"^eslint$|^prettier$"),                                   "tooling_layer",       2),
]

_ROLE_PRIORITY: dict[str, int] = {
    "runtime_core":        10,
    "plugin_host":          9,
    "backend_runtime":      8,
    "composition_layer":    7,
    "frontend_runtime":     6,
    "plugin_package":       5,
    "infrastructure_layer": 4,
    "test_layer":           3,
    "docs_layer":           3,
    "tooling_layer":        3,
    "benchmark_layer":      3,
    "unknown":              0,
}

_NOISE_ROLES = frozenset({"benchmark_layer", "test_layer", "docs_layer", "tooling_layer"})


class RuntimeClassifier:
    """Classifies workspace packages by architectural role."""

    def classify(self, root: Path, workspace_paths: list[str]) -> list[MonorepoPackageInfo]:
        raw: list[tuple[str, dict[str, Any]]] = []
        for ws_path in workspace_paths:
            pkg_root = root / ws_path
            if pkg_root.is_dir():
                raw.append((ws_path, self._load_pkg_json(pkg_root)))

        fan_in = self._compute_fan_in(raw)
        results: list[MonorepoPackageInfo] = []

        for ws_path, pkg_json in raw:
            name = str(pkg_json.get("name", ws_path)) if pkg_json else ws_path
            role, signals, conf = self._score_package(ws_path, name, pkg_json, fan_in.get(ws_path, 0))

            # plugin_host detection (independent of runtime_core path)
            if role not in _NOISE_ROLES and self._detect_plugin_host(pkg_json, ws_path, fan_in.get(ws_path, 0)):
                role = "plugin_host"
                signals.append("plugin_host:inferred")

            criticality = self._criticality(role, fan_in.get(ws_path, 0))
            results.append(MonorepoPackageInfo(
                path=ws_path,
                name=name,
                architectural_role=role,
                criticality=criticality,
                confidence=conf,
                role_signals=signals,
            ))

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_pkg_json(self, pkg_root: Path) -> dict[str, Any]:
        try:
            content = (pkg_root / "package.json").read_text(encoding="utf-8", errors="replace")
            return json.loads(content)  # type: ignore[no-any-return]
        except Exception:
            return {}

    def _compute_fan_in(self, raw: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
        name_to_path: dict[str, str] = {}
        for ws_path, pkg_json in raw:
            name = str(pkg_json.get("name", "")) if pkg_json else ""
            if name:
                name_to_path[name] = ws_path

        fan_in: dict[str, int] = {}
        for _, pkg_json in raw:
            if not pkg_json:
                continue
            for field in ("dependencies", "devDependencies", "peerDependencies"):
                for dep_name in pkg_json.get(field, {}):
                    target_path = name_to_path.get(str(dep_name))
                    if target_path:
                        fan_in[target_path] = fan_in.get(target_path, 0) + 1
        return fan_in

    def _has_runtime_evidence(self, pkg_json: dict[str, Any], ws_path: str) -> bool:
        """True only when package has explicit evidence of being a running process."""
        if not pkg_json:
            return False
        # bin field = CLI/server executable
        if pkg_json.get("bin"):
            return True
        # start/serve/server script = runtime entry point
        scripts = pkg_json.get("scripts") or {}
        if isinstance(scripts, dict):
            if _RUNTIME_SCRIPTS & set(scripts.keys()):
                return True
        return False

    def _is_utility_package(self, ws_path: str, name: str) -> bool:
        """True for packages that are libraries/utilities, NOT runtime processes."""
        path_lower = ws_path.lower().replace("\\", "/")
        name_lower = (name or "").lower()
        return (
            any(p.search(path_lower) for p in _NEGATIVE_RUNTIME_CORE)
            or any(p.search(name_lower) for p in _NEGATIVE_NAME_RUNTIME_CORE)
        )

    def _score_package(
        self, ws_path: str, name: str, pkg_json: dict[str, Any], fan_in: int
    ) -> tuple[str, list[str], str]:
        scores: dict[str, float] = {}
        signals: list[str] = []

        path_lower = ws_path.lower().replace("\\", "/")
        name_lower = name.lower() if name else ""

        for pattern, role, weight in _PATH_SIGNALS:
            if pattern.search(path_lower):
                scores[role] = scores.get(role, 0) + weight
                signals.append(f"path:{role}")
                break  # one path signal per package

        for pattern, role, weight in _NAME_SIGNALS:
            if pattern.search(name_lower):
                scores[role] = scores.get(role, 0) + weight
                signals.append(f"name:{role}")
                break

        if pkg_json:
            scripts = pkg_json.get("scripts", {}) or {}
            if isinstance(scripts, dict):
                for script_name, (role, weight) in _SCRIPT_SIGNALS.items():
                    if script_name in scripts:
                        scores[role] = scores.get(role, 0) + weight
                        signals.append(f"script:{script_name}")

            # bin = CLI tool or server executable; strong runtime signal
            if pkg_json.get("bin"):
                scores["backend_runtime"] = scores.get("backend_runtime", 0) + 3
                signals.append("bin:declared")

            # exports/main: weak lib signal only — do NOT boost runtime_core
            if pkg_json.get("exports") or pkg_json.get("main"):
                signals.append("exports:declared")

            prod_deps: set[str] = set()
            for field in ("dependencies", "peerDependencies"):
                dep_map = pkg_json.get(field) or {}
                if isinstance(dep_map, dict):
                    prod_deps.update(str(k) for k in dep_map)

            for dep_name in prod_deps:
                for pattern, role, weight in _DEP_SIGNALS:
                    if pattern.search(dep_name.lower()):
                        scores[role] = scores.get(role, 0) + weight
                        break

        # Fan-in: weak corroboration only (+1 per point, capped at 2)
        # Never sole driver — it boosts the CURRENT leading role, not runtime_core
        if fan_in >= 2:
            signals.append(f"fan_in:{fan_in}")
            fan_boost = min(fan_in, 2)
            if scores:
                # Boost whatever role is currently winning
                current_best = max(scores, key=lambda r: (scores[r], _ROLE_PRIORITY.get(r, 0)))
                scores[current_best] = scores.get(current_best, 0) + fan_boost
            # No scores yet (purely utility pkg): high fan-in → infrastructure_layer
            else:
                scores["infrastructure_layer"] = scores.get("infrastructure_layer", 0) + fan_boost

        if not scores:
            return "unknown", signals, "low"

        best_role = max(scores, key=lambda r: (scores[r], _ROLE_PRIORITY.get(r, 0)))
        best_score = scores[best_role]

        # Promote backend_runtime → runtime_core only with explicit runtime evidence
        # AND not a utility/shared package
        if best_role == "backend_runtime" and self._has_runtime_evidence(pkg_json, ws_path):
            if not self._is_utility_package(ws_path, name):
                best_role = "runtime_core"
                signals.append("promoted:has_runtime_evidence")

        # Hard guard: runtime_core NEVER assigned to utility packages
        if best_role == "runtime_core" and self._is_utility_package(ws_path, name):
            # Downgrade: utility with high fan-in → infrastructure_layer
            best_role = "infrastructure_layer"
            signals.append("demoted:utility_package")

        conf = "high" if best_score >= 6 else "medium" if best_score >= 3 else "low"
        return best_role, signals, conf

    def _detect_plugin_host(self, pkg_json: dict[str, Any], ws_path: str, fan_in: int) -> bool:
        if fan_in < 2:
            return False
        path_lower = ws_path.lower()
        if "plugin" in path_lower or "benchmark" in path_lower:
            return False
        if not pkg_json:
            return False
        name = str(pkg_json.get("name", "")).lower()
        if "plugin" in name:
            return False
        scripts = pkg_json.get("scripts", {}) or {}
        if isinstance(scripts, dict):
            if any("plugin" in str(k).lower() or "plugin" in str(v).lower()
                   for k, v in scripts.items()):
                return True
        exports = pkg_json.get("exports") or {}
        if isinstance(exports, dict):
            if any("plugin" in str(k).lower() for k in exports):
                return True
        return False

    def _criticality(self, role: str, fan_in: int) -> str:
        if role in {"runtime_core", "plugin_host"}:
            return "high"
        if role in {"backend_runtime", "frontend_runtime", "composition_layer"} or fan_in >= 3:
            return "medium"
        return "low"
