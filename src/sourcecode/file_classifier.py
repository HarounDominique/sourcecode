from __future__ import annotations

"""Evidence-based file classification for agent context.

This module intentionally avoids assigning runtime/application roles from a
directory name alone. Runtime roles require execution evidence, imports,
definitions, or manifest/config evidence. Tests/tooling/build classifications
can be structural because their purpose is explicitly encoded by conventional
locations and config filenames.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sourcecode.entrypoint_classifier import is_production_entry_point, normalize_entry_point
from sourcecode.schema import EntryPoint, MonorepoPackageInfo

FileCategory = Literal[
    "runtime_core",
    "application_logic",
    "domain_model",
    "infrastructure",
    "database_layer",
    "api_layer",
    "cli_entrypoint",
    "tests",
    "tooling",
    "build_system",
]


@dataclass
class FileClassification:
    path: str
    category: FileCategory
    confidence: Literal["high", "medium", "low"]
    relevance: float
    reason: str
    evidence: list[str] = field(default_factory=list)


_CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala", ".rb", ".php", ".cs",
}
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e"}
_TOOLING_DIRS = {"scripts", "script", "tools", "tool", "tooling", "ci", ".github", ".vscode"}
_BUILD_FILES = {
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml",
    "build.gradle", "settings.gradle", "Makefile", "Dockerfile",
    "tsconfig.json", "vite.config.ts", "vite.config.js", "webpack.config.js",
    "rollup.config.js", "turbo.json", "nx.json", "pnpm-workspace.yaml",
}
_TOOLING_FILES = {
    ".eslintrc", ".prettierrc", "eslint.config.js", "eslint.config.ts",
    "prettier.config.js", "jest.config.js", "jest.config.ts",
    "vitest.config.ts", "vitest.config.js", ".editorconfig",
}
_API_IMPORTS = {
    "fastapi", "flask", "django", "express", "koa", "fastify", "hono",
    "@nestjs/core", "@apollo/server", "graphql", "springframework",
}
_DB_IMPORTS = {
    "sqlalchemy", "psycopg2", "asyncpg", "pymongo", "mongoose", "prisma",
    "@prisma/client", "typeorm", "sequelize", "pg", "mysql2", "redis",
}
_INFRA_IMPORTS = {
    "boto3", "botocore", "kubernetes", "celery", "dramatiq", "bullmq",
    "kafkajs", "amqplib", "firebase-admin", "@aws-sdk/",
}

_IMPORT_RE = re.compile(
    r"(?:from\s+([A-Za-z0-9_@./-]+)\s+import|import\s+([A-Za-z0-9_@./-]+)|"
    r"require\(['\"]([^'\"]+)['\"]\)|from\s+['\"]([^'\"]+)['\"])",
    re.MULTILINE,
)
_DEF_RE = re.compile(r"\b(class|def|function|const|export\s+class|interface|type)\s+[A-Za-z_]", re.MULTILINE)


class FileClassifier:
    def __init__(
        self,
        root: Path,
        entry_points: list[EntryPoint],
        monorepo_packages: list[MonorepoPackageInfo] | None = None,
    ) -> None:
        self.root = root
        self.entry_points = [normalize_entry_point(ep) for ep in entry_points]
        self.production_entry_paths = {
            ep.path for ep in self.entry_points if is_production_entry_point(ep)
        }
        self.cli_entry_paths = {
            ep.path for ep in self.entry_points
            if is_production_entry_point(ep) and ep.kind == "cli"
        }
        self._pkg_roles = {
            pkg.path.rstrip("/") + "/": pkg.architectural_role
            for pkg in (monorepo_packages or [])
        }

    def classify_paths(self, paths: list[str], *, limit: int = 20) -> list[FileClassification]:
        classified: list[FileClassification] = []
        for path in paths:
            item = self.classify(path)
            if item is not None:
                classified.append(item)
        classified.sort(key=lambda item: (-item.relevance, item.path))
        return classified[:limit]

    def classify(self, path: str) -> FileClassification | None:
        norm = path.replace("\\", "/").lstrip("/")
        parts = norm.split("/")
        filename = Path(norm).name
        suffix = Path(norm).suffix.lower()

        if any(part.lower() in _TEST_DIRS for part in parts[:-1]) or self._is_test_file(norm):
            return FileClassification(norm, "tests", "high", 0.35, "test file by path/suffix convention", [norm])

        if filename in _BUILD_FILES:
            return FileClassification(norm, "build_system", "high", 0.45, "build or package manifest", [filename])

        if filename in _TOOLING_FILES or any(part.lower() in _TOOLING_DIRS for part in parts[:-1]):
            return FileClassification(norm, "tooling", "high", 0.25, "tooling/config path", [norm])

        if suffix not in _CODE_EXTENSIONS:
            return None

        content = self._read(norm)
        imports = self._imports(content)
        has_defs = bool(_DEF_RE.search(content))
        evidence: list[str] = []

        if norm in self.cli_entry_paths:
            return FileClassification(norm, "cli_entrypoint", "high", 1.0, "declared production CLI entrypoint", ["entry_points"])

        if norm in self.production_entry_paths:
            return FileClassification(norm, "runtime_core", "high", 0.95, "declared production runtime entrypoint", ["entry_points"])

        if self._has_any_import(imports, _API_IMPORTS):
            evidence = self._matched_imports(imports, _API_IMPORTS)
            return FileClassification(norm, "api_layer", "high", 0.82, "imports API/server framework", evidence)

        if self._has_any_import(imports, _DB_IMPORTS):
            evidence = self._matched_imports(imports, _DB_IMPORTS)
            return FileClassification(norm, "database_layer", "high", 0.78, "imports database/persistence dependency", evidence)

        if self._has_any_import(imports, _INFRA_IMPORTS):
            evidence = self._matched_imports(imports, _INFRA_IMPORTS)
            return FileClassification(norm, "infrastructure", "high", 0.72, "imports infrastructure dependency", evidence)

        role = self._package_role(norm)
        if role in {"runtime_core", "backend_runtime", "frontend_runtime", "plugin_host"} and has_defs:
            return FileClassification(norm, "application_logic", "medium", 0.65, "code definitions inside runtime package", [f"workspace_role:{role}"])

        if self._looks_like_domain_model(norm, content, has_defs):
            return FileClassification(norm, "domain_model", "medium", 0.58, "model/entity definitions detected", ["class/type definition"])

        if has_defs and imports:
            return FileClassification(norm, "application_logic", "medium", 0.52, "code definitions with imports", self._sample(imports))

        return None

    def _read(self, path: str) -> str:
        try:
            return (self.root / path).read_text(encoding="utf-8", errors="replace")[:12000]
        except OSError:
            return ""

    def _imports(self, content: str) -> list[str]:
        imports: list[str] = []
        for match in _IMPORT_RE.findall(content):
            value = next((part for part in match if part), "")
            if value:
                imports.append(value)
        return imports

    def _has_any_import(self, imports: list[str], needles: set[str]) -> bool:
        return bool(self._matched_imports(imports, needles))

    def _matched_imports(self, imports: list[str], needles: set[str]) -> list[str]:
        matched: list[str] = []
        for imp in imports:
            low = imp.lower()
            if any(low == n or low.startswith(n + "/") or low.startswith(n + ".") for n in needles):
                matched.append(f"import:{imp}")
        return matched[:4]

    def _package_role(self, path: str) -> str:
        for prefix, role in self._pkg_roles.items():
            if path.startswith(prefix):
                return role
        return ""

    def _is_test_file(self, path: str) -> bool:
        name = Path(path).name.lower()
        return (
            name.startswith("test_")
            or ".test." in name
            or ".spec." in name
            or name.endswith("_test.py")
        )

    def _looks_like_domain_model(self, path: str, content: str, has_defs: bool) -> bool:
        if not has_defs:
            return False
        parts = {part.lower() for part in path.split("/")[:-1]}
        if parts & {"domain", "models", "model", "entities", "entity"}:
            return True
        return "@dataclass" in content or "pydantic" in content.lower()

    def _sample(self, imports: list[str]) -> list[str]:
        return [f"import:{imp}" for imp in imports[:4]]

