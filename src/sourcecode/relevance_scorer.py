from __future__ import annotations

"""Operational relevance scoring for files and directories.

Returns a (relevance, noise) pair per path:
  relevance: 0.0–1.0 — how useful is this for an agent modifying code
  noise: bool       — should this be suppressed unless explicitly requested

Key principle: signal over inventory. Tooling, config, and auxiliary content
score low. Runtime core, entrypoints, and central modules score high.
"""

import re
from pathlib import Path
from typing import Optional

from sourcecode.schema import MonorepoPackageInfo

# --------------------------------------------------------------------------
# Noise path patterns — suppress unless explicitly needed
# --------------------------------------------------------------------------

_NOISE_PREFIXES: frozenset[str] = frozenset({
    "node_modules/",
    ".git/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".pytest_cache/",
    "dist/",
    "build/",
    ".turbo/",
    ".next/",
    ".nuxt/",
    "coverage/",
    ".nyc_output/",
})

_NOISE_DIRS: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".turbo", "coverage", ".nyc_output", ".next", ".nuxt",
    ".venv", "venv", ".mypy_cache", ".pytest_cache",
})

_NOISE_SUFFIXES: frozenset[str] = frozenset({
    ".lock", ".log", ".map", ".min.js", ".min.css",
    ".snap", ".d.ts.map",
})

_TOOLING_FILENAMES: frozenset[str] = frozenset({
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yaml", ".eslintrc.yml",
    ".prettierrc", ".prettierrc.js", ".prettierrc.json",
    "prettier.config.js", "prettier.config.ts",
    "eslint.config.js", "eslint.config.ts",
    ".editorconfig", ".gitignore", ".gitattributes",
    "commitlint.config.js", "stylelint.config.js",
    "jest.config.js", "jest.config.ts", "jest.config.json",
    "vitest.config.ts", "vitest.config.js",
    "webpack.config.js", "webpack.config.ts",
    "vite.config.ts", "vite.config.js",
    "rollup.config.js", "rollup.config.ts",
    "babel.config.js", "babel.config.json",
    ".babelrc", ".babelrc.js",
    "tsconfig.json", "tsconfig.base.json",
    ".dockerignore", "Makefile",
    "lerna.json", "nx.json", "turbo.json",
    "CHANGELOG.md", "LICENSE", "LICENSE.md",
})

_AUXILIARY_DIR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|/)benchmark[s]?(?:/|$)"),
    re.compile(r"(?:^|/)example[s]?(?:/|$)"),
    re.compile(r"(?:^|/)demo[s]?(?:/|$)"),
    re.compile(r"(?:^|/)playground[s]?(?:/|$)"),
    re.compile(r"(?:^|/)fixture[s]?(?:/|$)"),
    re.compile(r"(?:^|/)sandbox(?:/|$)"),
    re.compile(r"(?:^|/)docs?(?:/|$)"),
    re.compile(r"(?:^|/)\.github(?:/|$)"),
    re.compile(r"(?:^|/)\.claude(?:/|$)"),
    re.compile(r"(?:^|/)\.vscode(?:/|$)"),
    re.compile(r"(?:^|/)scripts?(?:/|$)"),
    re.compile(r"(?:^|/)tools?(?:/|$)"),
    re.compile(r"(?:^|/)ci(?:/|$)"),
    re.compile(r"(?:^|/)migrations?(?:/|$)"),
    re.compile(r"(?:^|/)generated?(?:/|$)"),
    re.compile(r"(?:^|/)storybook(?:/|$)"),
    re.compile(r"(?:^|/)stories(?:/|$)"),
]

# Test file patterns — scored low, excluded from default contract output
_TEST_FILE_PATTERNS: tuple[str, ...] = (
    "_test.", ".test.", ".spec.", "test_", "conftest", "_spec.",
)
_TEST_DIR_MARKERS: frozenset[str] = frozenset({
    "/test/", "/tests/", "/spec/", "/specs/", "/__tests__/", "/__mocks__/",
})

# Config/tooling filenames that are low runtime-relevance
_LOW_RUNTIME_STEMS: frozenset[str] = frozenset({
    "setup", "setup.cfg", "pyproject", "package", "package-lock",
    "yarn.lock", "pnpm-lock", "composer", "gemfile", "podfile",
    "dockerfile", "docker-compose", "makefile", "rakefile",
    "gruntfile", "gulpfile", "webpack.config", "vite.config",
    "rollup.config", "babel.config", "jest.config", "vitest.config",
    "tsconfig", "jsconfig", ".eslintrc", ".prettierrc", ".editorconfig",
    # doc-site tooling
    "rspress", "rspress.config", "docusaurus.config", "docusaurus",
    "vuepress.config", "vuepress", "nextra.config",
    "astro.config", "gatsby.config", "gatsby-config",
    # build/workspace orchestration
    "turbo", "turbo.config", "nx", "nx.config", "lerna",
    "esbuild.config", "swc.config", "postcss.config",
    "tailwind.config", "tailwind",
    # storybook
    "main.storybook", "preview.storybook",
    # playwright / cypress / e2e
    "playwright.config", "cypress.config",
})

_HIGH_VALUE_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".go", ".java", ".kt", ".rs", ".rb", ".cs",
})

_ENTRYPOINT_STEMS: frozenset[str] = frozenset({
    "main", "cli", "app", "server", "index", "__main__",
    "application", "bootstrap", "entry",
})


class RelevanceScorer:
    """Scores file paths by operational relevance for AI agents."""

    def __init__(self, monorepo_packages: Optional[list[MonorepoPackageInfo]] = None) -> None:
        self._pkg_roles: dict[str, str] = {}  # path_prefix → role
        if monorepo_packages:
            for pkg in monorepo_packages:
                prefix = pkg.path.rstrip("/") + "/"
                self._pkg_roles[prefix] = pkg.architectural_role

    def score(self, path: str) -> float:
        """Return operational relevance 0.0–1.0. Higher = more useful for agents."""
        norm = path.replace("\\", "/").lstrip("/")

        if self.is_noise(norm):
            return 0.0

        base = 0.3

        # Package role boost — runtime code scores high, tooling/docs low
        role = self._package_role(norm)
        role_boost = {
            "runtime_core": 0.4,
            "plugin_host": 0.35,
            "backend_runtime": 0.3,
            "frontend_runtime": 0.25,
            "composition_layer": 0.2,
            "plugin_package": 0.15,
            "infrastructure_layer": 0.15,
            "tooling_layer": -0.15,
            "docs_layer": -0.25,
            "test_layer": -0.1,
            "benchmark_layer": -0.25,
        }.get(role, 0.0)
        base += role_boost

        # Source file boost
        suffix = Path(norm).suffix.lower()
        if suffix in _HIGH_VALUE_SUFFIXES:
            base += 0.1

        # Entrypoint stem boost
        stem = Path(norm).stem.lower()
        if stem in _ENTRYPOINT_STEMS:
            base += 0.15

        # Test file penalty — tests are useful for coverage but not for
        # understanding architecture or editing production code
        fname = Path(norm).name.lower()
        if (any(m in f"/{norm}/" for m in _TEST_DIR_MARKERS)
                or any(fname.startswith(p.strip(".")) or p in fname
                       for p in _TEST_FILE_PATTERNS)):
            base -= 0.30

        # Config/tooling filename penalty — stronger than before
        if stem.lower() in _LOW_RUNTIME_STEMS:
            base -= 0.30

        # Auxiliary dir penalty (docs, examples, demos, fixtures, scripts…)
        # Aggressive: these almost never belong in top-ranked agent context
        if self._is_auxiliary(norm):
            base -= 0.40

        return max(0.0, min(1.0, base))

    def is_noise(self, path: str) -> bool:
        """True if this file should be suppressed from default agent output."""
        norm = path.replace("\\", "/").lstrip("/")

        if any(norm.startswith(p) for p in _NOISE_PREFIXES):
            return True

        parts = norm.split("/")
        if any(p in _NOISE_DIRS for p in parts):
            return True

        filename = Path(norm).name
        if filename in _TOOLING_FILENAMES:
            return True

        for suffix in _NOISE_SUFFIXES:
            if norm.endswith(suffix):
                return True

        return False

    def is_auxiliary(self, path: str) -> bool:
        """True if file is in a benchmark/example/demo/docs directory."""
        return self._is_auxiliary(path.replace("\\", "/"))

    def _is_auxiliary(self, norm: str) -> bool:
        return any(p.search(norm) for p in _AUXILIARY_DIR_PATTERNS)

    def _package_role(self, norm: str) -> str:
        for prefix, role in self._pkg_roles.items():
            if norm.startswith(prefix):
                return role
        return "unknown"
