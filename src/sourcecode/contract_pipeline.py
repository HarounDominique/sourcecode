from __future__ import annotations

"""Contract extraction pipeline.

Orchestrates: scan → extract → rank → compress → emit

Produces a list of FileContracts ranked by semantic importance,
with fan-in/fan-out computed from the import graph.
"""

import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Optional

from sourcecode.ast_extractor import AstExtractor, _LANGUAGE_MAP
from sourcecode.contract_model import ContractSummary, FileContract
from sourcecode.ranking_engine import RankingEngine
from sourcecode.relevance_scorer import RelevanceScorer
from sourcecode.schema import EntryPoint, MonorepoPackageInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FILES = 500      # hard cap on files extracted per run
_SRC_EXTENSIONS: frozenset[str] = frozenset(_LANGUAGE_MAP.keys())


RankStrategy = Literal["relevance", "centrality", "git-churn"]


# ---------------------------------------------------------------------------
# Git changed files helper
# ---------------------------------------------------------------------------

def _get_changed_files(root: Path) -> set[str]:
    """Return set of repo-relative paths that are uncommitted or recently changed."""
    changed: set[str] = set()
    for cmd in [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--cached", "--name-only"],
    ]:
        try:
            result = subprocess.run(
                cmd, cwd=root, capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    changed.add(line.replace("\\", "/"))
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if len(line) > 3:
                changed.add(line[3:].strip().replace("\\", "/"))
    except Exception:
        pass
    return changed


# ---------------------------------------------------------------------------
# Fan-in / fan-out computation
# ---------------------------------------------------------------------------

def _compute_fan_metrics(contracts: list[FileContract]) -> dict[str, tuple[int, int]]:
    """Compute (fan_in, fan_out) for each contract path.

    fan_in:  number of other contracts that import this file's path
    fan_out: number of internal imports from this file
    """
    # Build index: relative_path → contract
    path_set = {c.path for c in contracts}

    fan_in: Counter[str] = Counter()
    fan_out: Counter[str] = Counter()

    for contract in contracts:
        internal_imports = 0
        for imp in contract.imports:
            src = imp.source
            # Relative import — try to resolve to a known path
            if src.startswith("."):
                internal_imports += 1
                # Rough resolution: strip ./ or ../
                base_dir = str(Path(contract.path).parent).replace("\\", "/")
                candidates = _resolve_relative(base_dir, src, path_set)
                for cand in candidates:
                    fan_in[cand] += 1
        fan_out[contract.path] += internal_imports

    return {
        path: (fan_in[path], fan_out[path])
        for path in {c.path for c in contracts}
    }


def _resolve_relative(base_dir: str, src: str, path_set: set[str]) -> list[str]:
    """Approximate resolution of a relative import to known paths."""
    src = src.lstrip("./")
    if not src:
        return []
    # Try common extensions
    for ext in (".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts", "/index.js", "/index.tsx"):
        candidate = f"{base_dir}/{src}{ext}".replace("//", "/")
        if candidate in path_set:
            return [candidate]
    # Try without extension (maybe already has one)
    candidate = f"{base_dir}/{src}".replace("//", "/")
    if candidate in path_set:
        return [candidate]
    return []


# ---------------------------------------------------------------------------
# Git churn scoring
# ---------------------------------------------------------------------------

def _get_git_churn(root: Path, file_paths: list[str]) -> dict[str, int]:
    """Return commit count per file in last 90 days."""
    churn: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--format=", "--since=90.days.ago"],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        path_set = set(file_paths)
        counter: Counter[str] = Counter()
        for line in result.stdout.splitlines():
            line = line.strip().replace("\\", "/")
            if line in path_set:
                counter[line] += 1
        churn = dict(counter)
    except Exception:
        pass
    return churn


# ---------------------------------------------------------------------------
# ContractPipeline
# ---------------------------------------------------------------------------

class ContractPipeline:
    """Extracts, ranks, and filters FileContracts from a repository."""

    def __init__(
        self,
        max_files: int = _MAX_FILES,
    ) -> None:
        self.max_files = max_files
        self._extractor = AstExtractor()

    def run(
        self,
        root: Path,
        file_paths: list[str],
        entry_points: Optional[list[EntryPoint]] = None,
        monorepo_packages: Optional[list[MonorepoPackageInfo]] = None,
        *,
        mode: str = "contract",
        rank_by: RankStrategy = "relevance",
        max_symbols: Optional[int] = None,
        dependency_depth: int = 0,
        entrypoints_only: bool = False,
        changed_only: bool = False,
        symbol: Optional[str] = None,
        compress_types: bool = False,
    ) -> tuple[list[FileContract], ContractSummary]:
        """Run the full extraction pipeline.

        Returns (ranked_contracts, summary).
        """
        entry_paths = {ep.path.replace("\\", "/") for ep in (entry_points or [])}
        scorer = RelevanceScorer(monorepo_packages)
        engine = RankingEngine(monorepo_packages)

        # 1. Changed files (for --changed-only and ranking)
        changed_files: set[str] = set()
        if changed_only or rank_by == "git-churn":
            changed_files = _get_changed_files(root)

        # 2. Select files to extract
        # Exclude test files by default — they dominate by count but add noise
        # for agents focused on navigating/editing production code.
        # Tests are included when: --changed-only, --symbol, or --entrypoints-only=False
        # and the file is explicitly targeted.
        _TEST_MARKERS = frozenset({"/test/", "/tests/", "/spec/", "/specs/", "/__tests__/"})
        _TEST_PATTERNS = ("_test.", ".test.", ".spec.", "test_", "conftest")

        def _is_test(p: str) -> bool:
            pn = p.replace("\\", "/").lower()
            if any(m in f"/{pn}/" for m in _TEST_MARKERS):
                return True
            fname = Path(pn).name
            return any(fname.startswith(pat) or f".{pat.strip('.')}" in fname for pat in _TEST_PATTERNS)

        src_paths = [
            p for p in file_paths
            if Path(p).suffix.lower() in _SRC_EXTENSIONS
            and not scorer.is_noise(p)
            and (symbol is not None or changed_only or not _is_test(p))
        ]

        if changed_only:
            src_paths = [p for p in src_paths if p in changed_files]

        # Apply max_files cap — bypass when symbol search to ensure defining files are found.
        # A symbol query over a large repo needs all files; result set is small after filtering.
        if symbol is None and len(src_paths) > self.max_files:
            src_paths = sorted(
                src_paths,
                key=lambda p: (p in entry_paths, scorer.score(p)),
                reverse=True,
            )[:self.max_files]

        # 3. Extract contracts
        contracts: list[FileContract] = []
        method_counts: Counter[str] = Counter()
        limitations: list[str] = []

        for rel_path in src_paths:
            abs_path = root / rel_path
            contract = self._extractor.extract(abs_path, root)
            if contract is None:
                continue
            contract.is_entrypoint = rel_path in entry_paths
            contract.is_changed = rel_path in changed_files
            contracts.append(contract)
            method_counts[contract.extraction_method] += 1

        if not self._extractor.has_tree_sitter():
            limitations.append(
                "tree_sitter_unavailable: JS/TS extraction uses heuristics. "
                "Install with: pip install 'sourcecode[ast]'"
            )

        # 4. Compute fan-in / fan-out from import graph
        fan_metrics = _compute_fan_metrics(contracts)
        for c in contracts:
            fi, fo = fan_metrics.get(c.path, (0, 0))
            c.fan_in = fi
            c.fan_out = fo

        # 5. Compute git churn scores
        churn: dict[str, int] = {}
        if rank_by == "git-churn":
            churn = _get_git_churn(root, [c.path for c in contracts])

        # 6. Compute relevance scores via unified ranking engine
        max_fan_in = max((c.fan_in for c in contracts), default=1) if contracts else 1
        max_churn_val = max(churn.values(), default=1) if churn else 1
        for c in contracts:
            fs = engine.score(
                c.path,
                fan_in=c.fan_in,
                fan_out=c.fan_out,
                max_fan_in=max_fan_in,
                git_churn=churn.get(c.path, 0),
                max_churn=max_churn_val,
                is_entrypoint=c.is_entrypoint,
                is_changed=c.is_changed,
                export_count=len(c.exports),
                task="default",
            )
            c.relevance_score = fs.display_score
            c.ranking_reasons = fs.reasons

        # 7. Rank
        contracts = self._rank(contracts, rank_by)

        # 8. Symbol filter — keep files that define or import the symbol
        if symbol:
            contracts = _filter_by_symbol(contracts, symbol)
            # When shallow scan missed the defining file (deep monorepo), fall back
            # to a grep-based filesystem search over the full directory tree.
            if not contracts:
                contracts = self._symbol_deep_scan(
                    root, symbol,
                    known_paths=set(src_paths),
                    entry_paths=entry_paths,
                    changed_files=changed_files,
                    engine=engine,
                )

        # 9. Entrypoints-only filter
        if entrypoints_only and not symbol:
            contracts = [c for c in contracts if c.is_entrypoint or c.exports]

        # 10. Compress types if requested
        if compress_types:
            for c in contracts:
                _compress_contract_types(c)

        # 11. Apply max_symbols limit (limits total exports across all contracts)
        if max_symbols is not None and max_symbols > 0:
            contracts = _limit_symbols(contracts, max_symbols)

        summary = ContractSummary(
            mode=mode,
            total_files=len(src_paths),
            extracted_files=len(contracts),
            filtered_files=len(src_paths) - len(contracts),
            method_breakdown=dict(method_counts),
            ranked_by=rank_by,
            limitations=limitations,
        )
        return contracts, summary

    def _rank(self, contracts: list[FileContract], rank_by: RankStrategy) -> list[FileContract]:
        if rank_by == "centrality":
            return sorted(contracts, key=lambda c: (-(c.fan_in + c.fan_out), c.path))
        if rank_by == "git-churn":
            return sorted(contracts, key=lambda c: (-c.is_changed, -c.relevance_score, c.path))
        # Default: relevance — path breaks ties deterministically
        return sorted(contracts, key=lambda c: (-c.is_entrypoint, -c.relevance_score, c.path))

    def _symbol_deep_scan(
        self,
        root: Path,
        symbol: str,
        known_paths: set[str],
        entry_paths: set[str],
        changed_files: set[str],
        engine: RankingEngine,
    ) -> list[FileContract]:
        """Grep-based fallback when the shallow scan missed the defining files.

        Searches the full directory tree for source files containing *symbol*,
        extracts contracts for candidates not already processed, then re-applies
        the symbol filter. Fan-in/fan-out are not computed for these contracts.
        """
        candidates = _find_symbol_files(root, symbol, known_paths, engine)
        if not candidates:
            return []

        extra: list[FileContract] = []
        for rel_path in candidates[:300]:  # cap to prevent excessive extraction
            abs_path = root / rel_path
            contract = self._extractor.extract(abs_path, root)
            if contract is None:
                continue
            contract.is_entrypoint = rel_path in entry_paths
            contract.is_changed = rel_path in changed_files
            fs = engine.score(rel_path, is_entrypoint=contract.is_entrypoint, is_changed=contract.is_changed)
            contract.relevance_score = fs.display_score
            contract.ranking_reasons = fs.reasons
            extra.append(contract)

        return _filter_by_symbol(extra, symbol)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compress_contract_types(c: FileContract) -> None:
    """Abbreviate verbose type strings in-place."""
    # Shorten common verbose TS patterns
    _replacements = [
        (r"Promise<([^>]+)>", r"Promise<\1>"),
        (r"React\.FC<([^>]+)>", r"FC<\1>"),
        (r"React\.ReactNode", "ReactNode"),
        (r"React\.ReactElement", "ReactElement"),
    ]
    for fn in c.functions:
        for pattern, repl in _replacements:
            fn.signature = re.sub(pattern, repl, fn.signature)
            if fn.return_type:
                fn.return_type = re.sub(pattern, repl, fn.return_type)


def _limit_symbols(contracts: list[FileContract], max_symbols: int) -> list[FileContract]:
    """Trim exports/functions so total symbol count ≤ max_symbols, highest-ranked first."""
    total = 0
    result: list[FileContract] = []
    for c in contracts:
        if total >= max_symbols:
            break
        sym_count = len(c.exports) + len(c.functions) + len(c.types)
        if total + sym_count <= max_symbols:
            result.append(c)
            total += sym_count
        else:
            # Partial inclusion: trim to fit
            budget = max_symbols - total
            if budget <= 0:
                break
            from dataclasses import replace as _replace
            trimmed = _replace(
                c,
                exports=c.exports[:max(0, budget // 3)],
                functions=c.functions[:max(0, budget // 3)],
                types=c.types[:max(0, budget // 3)],
                limitations=c.limitations + [f"truncated: max_symbols={max_symbols}"],
            )
            result.append(trimmed)
            total = max_symbols
    return result


# ---------------------------------------------------------------------------
# Symbol-aware filter
# ---------------------------------------------------------------------------

def _filter_by_symbol(contracts: list[FileContract], symbol: str) -> list[FileContract]:
    """Return contracts that define, import, or structurally reference *symbol*.

    Four tiers applied in order:
    1. Exact name match — export/function/type names.
    2. Case-insensitive name match when tier 1 yields nothing.
    3. Import symbol match — name appears in import symbol list.
    4. Type-reference match — symbol in extends clauses, field types, or
       function signatures (word-boundary). Only used when tiers 1-3 fail.

    Defining contracts are ranked first; importers and references follow.
    """
    sym_l = symbol.lower()
    word_re = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )

    def _defines(c: FileContract, case: bool) -> bool:
        cmp = (lambda a, b: a.lower() == b.lower()) if case else (lambda a, b: a == b)
        return (
            any(cmp(e.name, symbol) for e in c.exports)
            or any(cmp(f.name, symbol) for f in c.functions)
            or any(cmp(t.name, symbol) for t in c.types)
        )

    def _imports_sym(c: FileContract, case: bool) -> bool:
        if case:
            return any(sym_l == s.lower() for imp in c.imports for s in imp.symbols)
        return any(symbol in imp.symbols for imp in c.imports)

    def _references_type(c: FileContract) -> bool:
        """Tier 4: symbol appears in extends clauses, field types, or signatures."""
        for t in c.types:
            if any(sym_l in ext.lower() for ext in t.extends):
                return True
            for field in t.fields:
                if sym_l in field.type.lower():
                    return True
        for f in c.functions:
            if word_re.search(f.signature):
                return True
        return False

    # Tier 1: exact name match
    defining = [c for c in contracts if _defines(c, case=False)]
    # Tier 2: case-insensitive name match
    if not defining:
        defining = [c for c in contracts if _defines(c, case=True)]

    defining_paths = {c.path for c in defining}

    # Tier 3: import matching (case-insensitive when no definers found)
    ci_imports = len(defining) == 0
    importer_paths = {c.path for c in contracts if _imports_sym(c, case=ci_imports)}
    importers = [c for c in contracts if c.path in importer_paths and c.path not in defining_paths]

    # Tier 4: type-reference matching (only when tiers 1-3 yield nothing)
    references: list[FileContract] = []
    if not defining and not importers:
        ref_paths = {c.path for c in contracts if _references_type(c)}
        references = [c for c in contracts if c.path in ref_paths]

    # Merge in priority order: defining > importers > type-references
    seen: set[str] = set()
    merged: list[FileContract] = []
    for c in defining + importers + references:
        if c.path not in seen:
            seen.add(c.path)
            merged.append(c)

    return sorted(merged, key=lambda c: (
        c.path not in defining_paths,
        c.path not in importer_paths,
        -c.relevance_score,
    ))


# ---------------------------------------------------------------------------
# Deep symbol scan — grep-based fallback for shallow-scanned repos
# ---------------------------------------------------------------------------

_DEEP_SCAN_NOISE_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".venv", "venv", "target", ".next", ".nuxt", ".turbo", "coverage",
    ".nyc_output", ".mypy_cache", ".pytest_cache",
})


def _find_symbol_files(
    root: Path,
    symbol: str,
    known_paths: set[str],
    engine: RankingEngine,
) -> list[str]:
    """Find source files outside *known_paths* that contain *symbol* as text.

    Uses subprocess grep when available (fast); falls back to os.walk + read.
    Returns repo-relative paths, noise-filtered.
    """
    found: list[str] = []

    # Try grep (fast, available on Linux/Mac)
    try:
        result = subprocess.run(
            [
                "grep", "-rl",
                "--include=*.ts", "--include=*.tsx",
                "--include=*.js", "--include=*.jsx",
                "--include=*.py",
                symbol, ".",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("./"):
                line = line[2:]
            line = line.replace("\\", "/")
            if line and line not in known_paths and not engine.is_noise(line):
                found.append(line)
        return found
    except Exception:
        pass

    # Python fallback — os.walk + text search
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in _DEEP_SCAN_NOISE_DIRS)
        for fname in filenames:
            if Path(fname).suffix.lower() not in _SRC_EXTENSIONS:
                continue
            full = os.path.join(dirpath, fname)
            try:
                rel = Path(full).relative_to(root)
                rel_str = str(rel).replace("\\", "/")
            except ValueError:
                continue
            if rel_str in known_paths or engine.is_noise(rel_str):
                continue
            try:
                content = Path(full).read_text(encoding="utf-8", errors="replace")
                if symbol in content:
                    found.append(rel_str)
            except OSError:
                pass

    return found


# ---------------------------------------------------------------------------
# Dependency graph emission
# ---------------------------------------------------------------------------

def build_dependency_graph(contracts: list[FileContract]) -> dict[str, Any]:
    """Build a compact dependency graph from extracted contracts."""
    nodes = [
        {
            "path": c.path,
            "language": c.language,
            "role": c.role,
            "exports": len(c.exports),
            "fan_in": c.fan_in,
            "fan_out": c.fan_out,
            "is_entrypoint": c.is_entrypoint,
        }
        for c in contracts
    ]
    path_set = {c.path for c in contracts}
    edges: list[dict[str, Any]] = []
    for c in contracts:
        for imp in c.imports:
            if not imp.source.startswith("."):
                continue
            base_dir = str(Path(c.path).parent).replace("\\", "/")
            from sourcecode.contract_pipeline import _resolve_relative
            targets = _resolve_relative(base_dir, imp.source, path_set)
            for target in targets:
                edges.append({
                    "from": c.path,
                    "to": target,
                    "symbols": imp.symbols[:5],  # cap to avoid bloat
                })

    return {
        "nodes": sorted(nodes, key=lambda n: (-n["fan_in"], n["path"])),
        "edges": sorted(edges, key=lambda e: (e["from"], e["to"])),
    }
