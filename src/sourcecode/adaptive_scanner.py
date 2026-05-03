from __future__ import annotations

"""Adaptive file tree scanner with topology-aware depth budgets.

Replaces pure depth filtering with relevance-oriented traversal:
- Source roots (packages/*/src, apps/*/src) get deep scan budgets.
- Low-signal directories (docs/, benchmarks/) are limited to 2 levels.
- Generated/excluded directories (dist/, node_modules/) are skipped.
- Unclassified directories fall back to the base depth limit.

Drop-in replacement for FileScanner: same scan_tree() and find_manifests()
interface, same output format (None = file, dict = directory).
"""

import os
from pathlib import Path
from typing import Any, Optional, cast

from pathspec import GitIgnoreSpec

from sourcecode.repo_classifier import RepoTopology
from sourcecode.scanner import DEFAULT_EXCLUDES, MANIFEST_NAMES


class AdaptiveScanner:
    """File tree scanner driven by repository topology.

    When *topology* is provided, traversal depth is controlled per-directory:
    directories inside source roots receive a deep budget; low-signal dirs
    are restricted; generated dirs are excluded entirely.

    When *topology* is None, falls back to the base depth limit — identical
    behaviour to FileScanner.
    """

    def __init__(
        self,
        root: Path,
        topology: Optional[RepoTopology] = None,
        base_depth: int = 4,
        extra_excludes: Optional[frozenset[str]] = None,
    ) -> None:
        self.root = root.resolve()
        self.topology = topology
        self.base_depth = base_depth
        self._excludes = DEFAULT_EXCLUDES | (extra_excludes or frozenset())
        self._gitignore_spec: Optional[GitIgnoreSpec] = None

        # Pre-compute lookup tables from topology for O(1) classification
        # during traversal.
        #
        # Each entry is (path_parts_tuple, max_absolute_depth):
        #   source prefix → (src_parts, len(src_parts) + source_depth)
        #   low-signal prefix → (ls_parts, len(ls_parts) + low_signal_depth)
        #
        # "max_absolute_depth" is depth measured from the repo root, not from
        # the classified directory. At depth D, files are visible; at depth
        # >= max we clear dirnames and skip files.
        self._source_prefixes: list[tuple[tuple[str, ...], int]] = []
        self._low_signal_prefixes: list[tuple[tuple[str, ...], int]] = []
        self._extra_exclude_names: frozenset[str] = frozenset()

        if topology is not None:
            budget = topology.scan_budget
            for sr in topology.source_roots:
                parts = tuple(p for p in sr.path.split("/") if p)
                if parts:
                    max_d = len(parts) + budget.source_depth
                    self._source_prefixes.append((parts, max_d))

            for lr in topology.low_signal_roots:
                parts = tuple(p for p in lr.path.split("/") if p)
                if parts:
                    max_d = len(parts) + budget.low_signal_depth
                    self._low_signal_prefixes.append((parts, max_d))

            # Generated roots at depth 1 → add to excludes so os.walk never enters
            top_generated = {
                gr.path.split("/")[0]
                for gr in topology.generated_roots
                if "/" not in gr.path
            }
            self._extra_exclude_names = frozenset(top_generated)

    # ------------------------------------------------------------------
    # Gitignore
    # ------------------------------------------------------------------

    def _load_gitignore_spec(self) -> GitIgnoreSpec:
        if self._gitignore_spec is None:
            gitignore = self.root / ".gitignore"
            lines: list[str] = []
            if gitignore.exists():
                try:
                    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
            self._gitignore_spec = GitIgnoreSpec.from_lines(lines)
        return self._gitignore_spec

    def _is_excluded_by_gitignore(self, rel_path: str, is_dir: bool) -> bool:
        spec = self._load_gitignore_spec()
        path_to_match = rel_path + "/" if is_dir else rel_path
        return spec.match_file(path_to_match)

    # ------------------------------------------------------------------
    # Depth budget computation — the core of adaptive traversal
    # ------------------------------------------------------------------

    def _compute_max_depth(self, rel_parts: tuple[str, ...]) -> int:
        """Return the maximum absolute depth allowed at *rel_parts*.

        Depth is the number of path components from the repo root.  Files
        at depth D are included; the scan stops (dirnames cleared) when
        depth >= returned value.

        Priority order:
        1. Inside a source root → deep budget (source_depth extra levels)
        2. Ancestor of a source root → must allow traversal to reach it
        3. Inside a low-signal root → restricted budget (low_signal_depth)
        4. Default → base_depth
        """
        if not self._source_prefixes and not self._low_signal_prefixes:
            return self.base_depth

        current_depth = len(rel_parts)

        # Track the best depth found via ancestor matching (may have multiple
        # source roots; return the maximum so all are reachable).
        ancestor_best = self.base_depth
        found_ancestor = False

        for src_parts, src_max in self._source_prefixes:
            n = len(src_parts)
            if current_depth >= n:
                # At or inside the source root
                if rel_parts[:n] == src_parts:
                    return src_max  # definite source territory — early exit
            else:
                # Ancestor check: src_parts starts with rel_parts?
                if src_parts[:current_depth] == rel_parts:
                    found_ancestor = True
                    if src_max > ancestor_best:
                        ancestor_best = src_max

        if found_ancestor:
            return ancestor_best

        # Low-signal roots (only if not already committed to a source path)
        for ls_parts, ls_max in self._low_signal_prefixes:
            n = len(ls_parts)
            if current_depth >= n and rel_parts[:n] == ls_parts:
                return ls_max

        return self.base_depth

    # ------------------------------------------------------------------
    # Main traversal
    # ------------------------------------------------------------------

    def scan_tree(self) -> dict[str, Any]:
        """Build the nested file tree dictionary.

        Returns dict where None = file (D-02) and dict = directory (D-01).
        Depth limits are applied per-directory using topology-derived budgets.
        """
        self._load_gitignore_spec()
        root_tree: dict[str, Any] = {}
        all_excludes = self._excludes | self._extra_exclude_names

        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            current = Path(dirpath)
            try:
                rel = current.relative_to(self.root)
            except ValueError:
                continue

            rel_parts = rel.parts
            depth = len(rel_parts)

            effective_max_depth = self._compute_max_depth(rel_parts)

            if depth >= effective_max_depth:
                dirnames.clear()
                continue

            # Filter dirnames in-place (critical: slice assignment)
            dirnames[:] = [
                d for d in dirnames
                if d not in all_excludes
                and not (current / d).is_symlink()
                and not self._is_excluded_by_gitignore(
                    str(rel / d) if rel_parts else d,
                    is_dir=True,
                )
            ]

            node = self._get_or_create_node(root_tree, rel_parts)

            for fname in filenames:
                # Skip flag-shaped names (shell redirect artifacts)
                if fname.startswith("-"):
                    continue
                fpath = current / fname
                if fpath.is_symlink():
                    continue
                rel_file = str(rel / fname) if rel_parts else fname
                if self._is_excluded_by_gitignore(rel_file, is_dir=False):
                    continue
                node[fname] = None  # D-02: None = file

            # Ensure accepted subdirs exist as dict nodes
            for d in dirnames:
                if d not in node:
                    node[d] = {}

        return root_tree

    def _get_or_create_node(
        self, tree: dict[str, Any], parts: tuple[str, ...]
    ) -> dict[str, Any]:
        node = tree
        for part in parts:
            if part not in node or node[part] is None:
                node[part] = {}
            node = cast(dict[str, Any], node[part])
        return node

    # ------------------------------------------------------------------
    # Manifest discovery — same interface as FileScanner
    # ------------------------------------------------------------------

    def find_manifests(self) -> list[str]:
        """Find manifest files at depth 0-1.

        Identical logic to FileScanner.find_manifests() — depth-0 root
        manifests plus depth-1 sub-package manifests, hidden dirs excluded.
        """
        manifests: list[str] = []
        for name in MANIFEST_NAMES:
            candidate = self.root / name
            if candidate.exists() and not candidate.is_symlink():
                manifests.append(str(candidate))
        try:
            for child in self.root.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and child.name not in self._excludes
                    and not child.name.startswith(".")
                ):
                    for name in MANIFEST_NAMES:
                        candidate = child / name
                        if candidate.exists() and not candidate.is_symlink():
                            manifests.append(str(candidate))
        except PermissionError:
            pass
        return manifests
