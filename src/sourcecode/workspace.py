from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sourcecode.scanner import DEFAULT_EXCLUDES


@dataclass
class WorkspaceCandidate:
    """Subdirectorio candidato a workspace."""

    path: str
    reason: str
    depth: int


@dataclass
class WorkspaceAnalysis:
    """Resultado del análisis de workspaces."""

    is_monorepo: bool
    markers: list[str] = field(default_factory=list)
    workspaces: list[WorkspaceCandidate] = field(default_factory=list)


class WorkspaceAnalyzer:
    """Detecta workspaces y señales de monorepo con recursión limitada."""

    def __init__(self, max_depth: int = 3) -> None:
        self.max_depth = max_depth

    def analyze(self, root: Path, manifests: list[str]) -> WorkspaceAnalysis:
        markers = self._detect_markers(root)
        candidates: dict[str, WorkspaceCandidate] = {}

        for candidate in self._workspace_candidates_from_manifests(root, manifests):
            candidates[candidate.path] = candidate

        for candidate in self._workspace_candidates_from_markers(root, markers):
            candidates.setdefault(candidate.path, candidate)

        workspaces = sorted(candidates.values(), key=lambda item: item.path)
        return WorkspaceAnalysis(is_monorepo=bool(markers), markers=markers, workspaces=workspaces)

    def _detect_markers(self, root: Path) -> list[str]:
        markers: list[str] = []
        for filename in ("pnpm-workspace.yaml", "go.work", "turbo.json", "lerna.json"):
            if (root / filename).exists():
                markers.append(filename)

        cargo = root / "Cargo.toml"
        if cargo.exists():
            content = cargo.read_text(encoding="utf-8", errors="replace")
            if "[workspace]" in content:
                markers.append("Cargo.toml[workspace]")
        return markers

    def _workspace_candidates_from_manifests(
        self, root: Path, manifests: list[str]
    ) -> list[WorkspaceCandidate]:
        candidates: list[WorkspaceCandidate] = []
        for manifest in manifests:
            try:
                relative = Path(manifest).resolve().relative_to(root.resolve())
            except ValueError:
                continue
            if len(relative.parts) <= 1:
                continue
            workspace = relative.parts[0]
            if self._is_allowed_workspace(workspace):
                candidates.append(
                    WorkspaceCandidate(path=workspace, reason=f"manifest:{relative.name}", depth=1)
                )
        return candidates

    def _workspace_candidates_from_markers(
        self, root: Path, markers: list[str]
    ) -> list[WorkspaceCandidate]:
        candidates: list[WorkspaceCandidate] = []
        if "pnpm-workspace.yaml" in markers:
            candidates.extend(self._from_pnpm_workspace(root))
        if "go.work" in markers:
            candidates.extend(self._from_go_work(root))
        if "Cargo.toml[workspace]" in markers:
            candidates.extend(self._from_cargo_workspace(root))
        return [candidate for candidate in candidates if self._is_allowed_workspace(candidate.path)]

    def _from_pnpm_workspace(self, root: Path) -> list[WorkspaceCandidate]:
        content = (root / "pnpm-workspace.yaml").read_text(encoding="utf-8", errors="replace")
        patterns = []
        for line in content.splitlines():
            stripped = line.strip().strip("'\"")
            if stripped.startswith("- "):
                patterns.append(stripped[2:].strip("'\""))

        candidates: list[WorkspaceCandidate] = []
        for pattern in patterns:
            for path in self._resolve_pattern(root, pattern):
                candidates.append(
                    WorkspaceCandidate(path=path, reason="marker:pnpm-workspace.yaml", depth=len(Path(path).parts))
                )
        return candidates

    def _from_go_work(self, root: Path) -> list[WorkspaceCandidate]:
        content = (root / "go.work").read_text(encoding="utf-8", errors="replace")
        candidates: list[WorkspaceCandidate] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("use "):
                target = stripped.replace("use ", "", 1).strip().strip("()")
                if target and target != ".":
                    rel = target.removeprefix("./").rstrip("/")
                    candidates.append(
                        WorkspaceCandidate(path=rel, reason="marker:go.work", depth=len(Path(rel).parts))
                    )
            elif stripped.startswith("./"):
                rel = stripped.removeprefix("./").rstrip()
                candidates.append(
                    WorkspaceCandidate(path=rel, reason="marker:go.work", depth=len(Path(rel).parts))
                )
        return candidates

    def _from_cargo_workspace(self, root: Path) -> list[WorkspaceCandidate]:
        content = (root / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
        candidates: list[WorkspaceCandidate] = []
        in_members = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("members"):
                in_members = True
            if in_members:
                for quote in ('"', "'"):
                    if quote in stripped:
                        parts = [segment for segment in stripped.split(quote) if segment and segment not in {"[", "]", ",", "members = "}]
                        for part in parts:
                            if "/" in part or "*" in part:
                                for path in self._resolve_pattern(root, part):
                                    candidates.append(
                                        WorkspaceCandidate(path=path, reason="marker:Cargo.toml[workspace]", depth=len(Path(path).parts))
                                    )
                if "]" in stripped:
                    in_members = False
        return candidates

    def _resolve_pattern(self, root: Path, pattern: str) -> list[str]:
        matches: list[str] = []
        for candidate in root.glob(pattern):
            if not candidate.is_dir():
                continue
            try:
                relative = candidate.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            if 1 <= len(relative.parts) <= self.max_depth:
                rel = str(relative)
                if self._is_allowed_workspace(rel):
                    matches.append(rel)
        return sorted(set(matches))

    def _is_allowed_workspace(self, relative_path: str) -> bool:
        parts = Path(relative_path).parts
        if not parts or len(parts) > self.max_depth:
            return False
        return all(part not in DEFAULT_EXCLUDES for part in parts)
