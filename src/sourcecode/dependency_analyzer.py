from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML

from sourcecode.detectors.parsers import load_json_file, load_toml_file, read_text_lines
from sourcecode.schema import DependencyRecord, DependencySummary


class DependencyAnalyzer:
    """Resuelve dependencias desde manifests y lockfiles sin ejecutar toolchains."""

    def analyze(
        self,
        root: Path,
        *,
        workspace: str | None = None,
    ) -> tuple[list[DependencyRecord], DependencySummary]:
        records: list[DependencyRecord] = []
        limitations: list[str] = []

        for handler in (
            self._analyze_node,
            self._analyze_python,
            self._analyze_php,
            self._analyze_ruby,
            self._analyze_rust,
            self._analyze_go,
            self._analyze_dotnet,
            self._analyze_java,
        ):
            handler_records, handler_limitations = handler(root)
            records.extend(replace(record, workspace=workspace) for record in handler_records)
            limitations.extend(handler_limitations)

        deduped = self._dedupe(records)
        return deduped, self._build_summary(deduped, limitations)

    def merge_summaries(self, summaries: Iterable[DependencySummary]) -> DependencySummary:
        result = DependencySummary(requested=True)
        ecosystems: set[str] = set()
        sources: set[str] = set()
        limitations: list[str] = []
        for summary in summaries:
            result.total_count += summary.total_count
            result.direct_count += summary.direct_count
            result.transitive_count += summary.transitive_count
            ecosystems.update(summary.ecosystems)
            sources.update(summary.sources)
            for limitation in summary.limitations:
                if limitation not in limitations:
                    limitations.append(limitation)
        result.ecosystems = sorted(ecosystems)
        result.sources = sorted(sources)
        result.limitations = limitations
        return result

    def _build_summary(
        self, records: list[DependencyRecord], limitations: list[str]
    ) -> DependencySummary:
        sources = sorted({record.source for record in records})
        ecosystems = sorted({record.ecosystem for record in records})
        direct_count = sum(1 for record in records if record.scope != "transitive")
        transitive_count = sum(1 for record in records if record.scope == "transitive")
        unique_limitations: list[str] = []
        for limitation in limitations:
            if limitation not in unique_limitations:
                unique_limitations.append(limitation)
        return DependencySummary(
            requested=True,
            total_count=len(records),
            direct_count=direct_count,
            transitive_count=transitive_count,
            ecosystems=ecosystems,
            sources=sources,
            limitations=unique_limitations,
        )

    def _dedupe(self, records: Iterable[DependencyRecord]) -> list[DependencyRecord]:
        seen: set[tuple[Any, ...]] = set()
        deduped: list[DependencyRecord] = []
        for record in records:
            key = (
                record.workspace,
                record.ecosystem,
                record.name,
                record.scope,
                record.declared_version,
                record.resolved_version,
                record.source,
                record.parent,
                record.manifest_path,
            )
            if key not in seen:
                seen.add(key)
                deduped.append(record)
        return deduped

    def _analyze_node(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        package_json = load_json_file(root / "package.json")
        if package_json is None:
            return [], []

        direct = self._parse_node_direct_dependencies(package_json)
        direct_names = {record.name for record in direct}
        records = list(direct)

        package_lock = load_json_file(root / "package-lock.json")
        if package_lock is not None:
            records = self._merge_resolved(records, self._parse_package_lock(package_lock, direct_names))
            return records, []

        pnpm_lock = self._load_yaml_file(root / "pnpm-lock.yaml")
        if isinstance(pnpm_lock, dict):
            records = self._merge_resolved(records, self._parse_pnpm_lock(pnpm_lock, direct))
            return records, []

        yarn_lock = root / "yarn.lock"
        if yarn_lock.exists():
            return records, ["nodejs: yarn.lock detectado; resolucion transitiva no implementada"]

        return records, []

    def _parse_node_direct_dependencies(self, package_json: dict[str, Any]) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        field_scope = {
            "dependencies": "direct",
            "devDependencies": "dev",
            "peerDependencies": "peer",
            "optionalDependencies": "optional",
        }
        for field, scope in field_scope.items():
            raw = package_json.get(field, {})
            if not isinstance(raw, dict):
                continue
            for name, version in raw.items():
                records.append(
                    DependencyRecord(
                        name=str(name),
                        ecosystem="nodejs",
                        scope=scope,
                        declared_version=str(version),
                        source="manifest",
                        manifest_path="package.json",
                    )
                )
        return records

    def _parse_package_lock(
        self, package_lock: dict[str, Any], direct_names: set[str]
    ) -> list[DependencyRecord]:
        packages = package_lock.get("packages")
        if isinstance(packages, dict) and packages:
            parent_map = self._node_parent_map_from_packages(packages)
            records: list[DependencyRecord] = []
            for package_path, info in packages.items():
                if not package_path:
                    continue
                if not isinstance(info, dict):
                    continue
                name = self._node_name_from_package_path(package_path)
                if not name:
                    continue
                version = info.get("version")
                scope = "direct" if name in direct_names else "transitive"
                records.append(
                    DependencyRecord(
                        name=name,
                        ecosystem="nodejs",
                        scope=scope,
                        resolved_version=str(version) if version is not None else None,
                        source="lockfile",
                        parent=parent_map.get(name),
                        manifest_path="package-lock.json",
                    )
                )
            return records

        dependencies = package_lock.get("dependencies")
        if isinstance(dependencies, dict):
            return self._walk_npm_dependency_tree(dependencies, direct_names)
        return []

    def _walk_npm_dependency_tree(
        self,
        dependencies: dict[str, Any],
        direct_names: set[str],
        *,
        parent: str | None = None,
    ) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        for name, info in dependencies.items():
            if not isinstance(info, dict):
                continue
            scope = "direct" if parent is None and name in direct_names else "transitive"
            records.append(
                DependencyRecord(
                    name=str(name),
                    ecosystem="nodejs",
                    scope=scope,
                    resolved_version=str(info.get("version")) if info.get("version") is not None else None,
                    source="lockfile",
                    parent=parent,
                    manifest_path="package-lock.json",
                )
            )
            child_dependencies = info.get("dependencies")
            if isinstance(child_dependencies, dict):
                records.extend(
                    self._walk_npm_dependency_tree(child_dependencies, direct_names, parent=str(name))
                )
        return records

    def _node_parent_map_from_packages(self, packages: dict[str, Any]) -> dict[str, str]:
        parent_map: dict[str, str] = {}
        for package_path, info in packages.items():
            parent_name = self._node_name_from_package_path(package_path)
            if not parent_name or not isinstance(info, dict):
                continue
            dependencies = info.get("dependencies")
            if not isinstance(dependencies, dict):
                continue
            for dependency_name in dependencies:
                parent_map.setdefault(str(dependency_name), parent_name)
        return parent_map

    def _node_name_from_package_path(self, package_path: str) -> str:
        marker = "node_modules/"
        if marker not in package_path:
            return package_path.strip("/")
        return package_path.rsplit(marker, 1)[-1]

    def _parse_pnpm_lock(
        self,
        lock_data: dict[str, Any],
        direct_records: list[DependencyRecord],
    ) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        direct_by_name = {record.name: record for record in direct_records}

        importers = lock_data.get("importers", {})
        importer_data = importers.get(".") if isinstance(importers, dict) else None
        if isinstance(importer_data, dict):
            for field, scope in (
                ("dependencies", "direct"),
                ("devDependencies", "dev"),
                ("peerDependencies", "peer"),
                ("optionalDependencies", "optional"),
            ):
                raw = importer_data.get(field, {})
                if not isinstance(raw, dict):
                    continue
                for name, details in raw.items():
                    resolved_version = self._extract_pnpm_version(details)
                    record = direct_by_name.get(str(name))
                    records.append(
                        DependencyRecord(
                            name=str(name),
                            ecosystem="nodejs",
                            scope=scope,
                            declared_version=record.declared_version if record else None,
                            resolved_version=resolved_version,
                            source="lockfile",
                            manifest_path="pnpm-lock.yaml",
                        )
                    )

        packages = lock_data.get("packages", {})
        if isinstance(packages, dict):
            parent_map = self._pnpm_parent_map(packages)
            for key, info in packages.items():
                name, version = self._pnpm_package_key(key)
                if not name or name in direct_by_name:
                    continue
                if not isinstance(info, dict):
                    info = {}
                records.append(
                    DependencyRecord(
                        name=name,
                        ecosystem="nodejs",
                        scope="transitive",
                        resolved_version=version,
                        source="lockfile",
                        parent=parent_map.get(name),
                        manifest_path="pnpm-lock.yaml",
                    )
                )
        return records

    def _pnpm_parent_map(self, packages: dict[str, Any]) -> dict[str, str]:
        parent_map: dict[str, str] = {}
        for key, info in packages.items():
            parent_name, _version = self._pnpm_package_key(key)
            if not parent_name or not isinstance(info, dict):
                continue
            dependencies = info.get("dependencies", {})
            if not isinstance(dependencies, dict):
                continue
            for dependency_name in dependencies:
                parent_map.setdefault(str(dependency_name), parent_name)
        return parent_map

    def _pnpm_package_key(self, key: str) -> tuple[str, Optional[str]]:
        cleaned = key.lstrip("/")
        if "@" not in cleaned:
            return cleaned, None
        if cleaned.startswith("@"):
            parts = cleaned.rsplit("@", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        name, version = cleaned.split("@", 1)
        return name, version

    def _extract_pnpm_version(self, details: Any) -> Optional[str]:
        if isinstance(details, str):
            return details
        if isinstance(details, dict):
            version = details.get("version")
            return str(version) if version is not None else None
        return None

    def _analyze_python(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        direct = self._parse_python_direct_dependencies(root)
        if not direct:
            return [], []

        direct_names = {record.name for record in direct}
        records = list(direct)
        limitations: list[str] = []

        poetry_lock = load_toml_file(root / "poetry.lock")
        if poetry_lock is not None:
            records = self._merge_resolved(records, self._parse_python_lock_packages(poetry_lock, direct_names, "poetry.lock"))

        uv_lock = load_toml_file(root / "uv.lock")
        if uv_lock is not None:
            records = self._merge_resolved(records, self._parse_python_lock_packages(uv_lock, direct_names, "uv.lock"))

        pipfile_lock = load_json_file(root / "Pipfile.lock")
        if pipfile_lock is not None:
            records = self._merge_resolved(records, self._parse_pipfile_lock(pipfile_lock, direct_names))

        if not any(record.source == "lockfile" for record in records):
            requirements_lock = root / "requirements.lock"
            if requirements_lock.exists():
                limitations.append("python: requirements.lock detectado pero no parseado en esta fase")

        return records, limitations

    def _parse_python_direct_dependencies(self, root: Path) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        pyproject = load_toml_file(root / "pyproject.toml")
        if pyproject:
            project = pyproject.get("project", {})
            if isinstance(project, dict):
                for dependency in project.get("dependencies", []):
                    parsed = self._split_python_requirement(dependency)
                    if parsed is not None:
                        name, version = parsed
                        records.append(
                            DependencyRecord(
                                name=name,
                                ecosystem="python",
                                declared_version=version,
                                source="manifest",
                                manifest_path="pyproject.toml",
                            )
                        )
                optional = project.get("optional-dependencies", {})
                if isinstance(optional, dict):
                    for group_name, group in optional.items():
                        if not isinstance(group, list):
                            continue
                        for dependency in group:
                            parsed = self._split_python_requirement(dependency)
                            if parsed is not None:
                                name, version = parsed
                                records.append(
                                    DependencyRecord(
                                        name=name,
                                        ecosystem="python",
                                        scope=f"optional:{group_name}",
                                        declared_version=version,
                                        source="manifest",
                                        manifest_path="pyproject.toml",
                                    )
                                )
            tool = pyproject.get("tool", {})
            if isinstance(tool, dict):
                poetry = tool.get("poetry", {})
                if isinstance(poetry, dict):
                    for name, raw in poetry.get("dependencies", {}).items():
                        if str(name).lower() == "python":
                            continue
                        records.append(
                            DependencyRecord(
                                name=str(name).lower(),
                                ecosystem="python",
                                declared_version=self._normalize_declared_version(raw),
                                source="manifest",
                                manifest_path="pyproject.toml",
                            )
                        )
                    groups = poetry.get("group", {})
                    if isinstance(groups, dict):
                        for group_name, group in groups.items():
                            if not isinstance(group, dict):
                                continue
                            dependencies = group.get("dependencies", {})
                            if not isinstance(dependencies, dict):
                                continue
                            for name, raw in dependencies.items():
                                records.append(
                                    DependencyRecord(
                                        name=str(name).lower(),
                                        ecosystem="python",
                                        scope=f"group:{group_name}",
                                        declared_version=self._normalize_declared_version(raw),
                                        source="manifest",
                                        manifest_path="pyproject.toml",
                                    )
                                )

        for path in ("requirements.txt", "requirements-dev.txt"):
            file_path = root / path
            if not file_path.exists():
                continue
            scope = "dev" if "dev" in path else "direct"
            for line in read_text_lines(file_path):
                parsed = self._split_python_requirement(line)
                if parsed is None:
                    continue
                name, version = parsed
                records.append(
                    DependencyRecord(
                        name=name,
                        ecosystem="python",
                        scope=scope,
                        declared_version=version,
                        source="manifest",
                        manifest_path=path,
                    )
                )

        return self._dedupe(records)

    def _split_python_requirement(self, raw: Any) -> Optional[tuple[str, Optional[str]]]:
        if not isinstance(raw, str):
            return None
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "[")):
            return None
        cleaned = stripped.split(";", 1)[0].strip()
        cleaned = cleaned.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", cleaned)
        if match is None:
            return None
        name = match.group(1).lower().replace("_", "-")
        version = match.group(2).strip() or None
        return name, version

    def _normalize_declared_version(self, raw: Any) -> Optional[str]:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            version = raw.get("version")
            return str(version) if version is not None else None
        return None

    def _parse_python_lock_packages(
        self,
        lock_data: dict[str, Any],
        direct_names: set[str],
        manifest_path: str,
    ) -> list[DependencyRecord]:
        raw_packages = lock_data.get("package", [])
        if not isinstance(raw_packages, list):
            return []
        parent_map: dict[str, str] = {}
        records: list[DependencyRecord] = []
        for package in raw_packages:
            if not isinstance(package, dict):
                continue
            name = str(package.get("name", "")).lower()
            if not name:
                continue
            dependencies = package.get("dependencies", {})
            for dependency_name in self._iter_dependency_names(dependencies):
                parent_map.setdefault(dependency_name, name)
            records.append(
                DependencyRecord(
                    name=name,
                    ecosystem="python",
                    scope="direct" if name in direct_names else "transitive",
                    resolved_version=str(package.get("version")) if package.get("version") is not None else None,
                    source="lockfile",
                    parent=parent_map.get(name),
                    manifest_path=manifest_path,
                )
            )
        return records

    def _parse_pipfile_lock(
        self, lock_data: dict[str, Any], direct_names: set[str]
    ) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        for field, scope in (("default", "direct"), ("develop", "dev")):
            raw = lock_data.get(field, {})
            if not isinstance(raw, dict):
                continue
            for name, info in raw.items():
                resolved = None
                dependencies = {}
                if isinstance(info, dict):
                    version = info.get("version")
                    resolved = str(version).lstrip("=") if version is not None else None
                    dependencies = info.get("dependencies", {})
                records.append(
                    DependencyRecord(
                        name=str(name).lower(),
                        ecosystem="python",
                        scope=scope,
                        declared_version=None,
                        resolved_version=resolved,
                        source="lockfile",
                        manifest_path="Pipfile.lock",
                    )
                )
                for dependency_name in self._iter_dependency_names(dependencies):
                    if dependency_name in direct_names:
                        continue
                    records.append(
                        DependencyRecord(
                            name=dependency_name,
                            ecosystem="python",
                            scope="transitive",
                            source="lockfile",
                            parent=str(name).lower(),
                            manifest_path="Pipfile.lock",
                        )
                    )
        return records

    def _iter_dependency_names(self, raw: Any) -> list[str]:
        if isinstance(raw, dict):
            return [str(name).lower() for name in raw]
        if isinstance(raw, list):
            result: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    result.append(item.lower())
                elif isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        result.append(name.lower())
            return result
        return []

    def _analyze_php(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        composer_json = load_json_file(root / "composer.json")
        if composer_json is None:
            return [], []
        records: list[DependencyRecord] = []
        for field, scope in (("require", "direct"), ("require-dev", "dev")):
            raw = composer_json.get(field, {})
            if not isinstance(raw, dict):
                continue
            for name, version in raw.items():
                records.append(
                    DependencyRecord(
                        name=str(name),
                        ecosystem="php",
                        scope=scope,
                        declared_version=str(version),
                        source="manifest",
                        manifest_path="composer.json",
                    )
                )
        composer_lock = load_json_file(root / "composer.lock")
        if composer_lock is not None:
            records = self._merge_resolved(records, self._parse_composer_lock(composer_lock, records))
        return records, []

    def _parse_composer_lock(
        self, lock_data: dict[str, Any], direct_records: list[DependencyRecord]
    ) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        packages = []
        for field in ("packages", "packages-dev"):
            raw = lock_data.get(field, [])
            if isinstance(raw, list):
                packages.extend(raw)
        parent_map: dict[str, str] = {}
        direct_names = {record.name for record in direct_records}
        for package in packages:
            if not isinstance(package, dict):
                continue
            name = str(package.get("name", ""))
            if not name:
                continue
            requires = package.get("require", {})
            if isinstance(requires, dict):
                for dependency_name in requires:
                    parent_map.setdefault(str(dependency_name), name)
            records.append(
                DependencyRecord(
                    name=name,
                    ecosystem="php",
                    scope="direct" if name in direct_names else "transitive",
                    resolved_version=str(package.get("version")) if package.get("version") is not None else None,
                    source="lockfile",
                    parent=parent_map.get(name),
                    manifest_path="composer.lock",
                )
            )
        return records

    def _analyze_ruby(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        gemfile = root / "Gemfile"
        if not gemfile.exists():
            return [], []
        records = self._parse_gemfile(gemfile)
        gemfile_lock = root / "Gemfile.lock"
        if gemfile_lock.exists():
            records = self._merge_resolved(records, self._parse_gemfile_lock(gemfile_lock, records))
            return records, []
        return records, ["ruby: Gemfile sin Gemfile.lock; transitivas no disponibles"]

    def _parse_gemfile(self, gemfile: Path) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        for line in read_text_lines(gemfile):
            match = re.search(r"""gem\s+["']([^"']+)["'](?:\s*,\s*["']([^"']+)["'])?""", line)
            if match is None:
                continue
            records.append(
                DependencyRecord(
                    name=match.group(1),
                    ecosystem="ruby",
                    declared_version=match.group(2),
                    source="manifest",
                    manifest_path="Gemfile",
                )
            )
        return records

    def _parse_gemfile_lock(
        self, gemfile_lock: Path, direct_records: list[DependencyRecord]
    ) -> list[DependencyRecord]:
        direct_names = {record.name for record in direct_records}
        records: list[DependencyRecord] = []
        current_parent: str | None = None
        in_specs = False
        for line in read_text_lines(gemfile_lock):
            if line.strip() == "specs:":
                in_specs = True
                continue
            if not in_specs:
                continue
            if line.strip() == "DEPENDENCIES":
                break
            if line.startswith("    ") and not line.startswith("      "):
                match = re.match(r"\s{4}([^\s(]+)\s+\(([^)]+)\)", line)
                if match is None:
                    continue
                current_parent = match.group(1)
                records.append(
                    DependencyRecord(
                        name=current_parent,
                        ecosystem="ruby",
                        scope="direct" if current_parent in direct_names else "transitive",
                        resolved_version=match.group(2),
                        source="lockfile",
                        manifest_path="Gemfile.lock",
                    )
                )
            elif line.startswith("      ") and current_parent is not None:
                dep_match = re.match(r"\s{6}([^\s(]+)", line)
                if dep_match is None:
                    continue
                dependency_name = dep_match.group(1)
                if dependency_name in direct_names:
                    continue
                records.append(
                    DependencyRecord(
                        name=dependency_name,
                        ecosystem="ruby",
                        scope="transitive",
                        source="lockfile",
                        parent=current_parent,
                        manifest_path="Gemfile.lock",
                    )
                )
        return records

    def _analyze_rust(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        cargo_toml = load_toml_file(root / "Cargo.toml")
        if cargo_toml is None:
            return [], []
        direct = self._parse_cargo_direct(cargo_toml)
        cargo_lock = load_toml_file(root / "Cargo.lock")
        limitations: list[str] = []
        if cargo_lock is not None:
            direct = self._merge_resolved(direct, self._parse_cargo_lock(cargo_lock, direct))
        else:
            limitations.append("rust: Cargo.lock ausente; transitivas no disponibles")
        return direct, limitations

    def _parse_cargo_direct(self, cargo_toml: dict[str, Any]) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        dependencies = cargo_toml.get("dependencies", {})
        if not isinstance(dependencies, dict):
            return []
        for name, raw in dependencies.items():
            declared_version = self._normalize_declared_version(raw)
            records.append(
                DependencyRecord(
                    name=str(name),
                    ecosystem="rust",
                    declared_version=declared_version,
                    source="manifest",
                    manifest_path="Cargo.toml",
                )
            )
        return records

    def _parse_cargo_lock(
        self, cargo_lock: dict[str, Any], direct_records: list[DependencyRecord]
    ) -> list[DependencyRecord]:
        direct_names = {record.name for record in direct_records}
        raw_packages = cargo_lock.get("package", [])
        if not isinstance(raw_packages, list):
            return []
        parent_map: dict[str, str] = {}
        records: list[DependencyRecord] = []
        for package in raw_packages:
            if not isinstance(package, dict):
                continue
            name = str(package.get("name", ""))
            if not name:
                continue
            dependencies = package.get("dependencies", [])
            if isinstance(dependencies, list):
                for dependency in dependencies:
                    if isinstance(dependency, str):
                        dep_name = dependency.split(" ", 1)[0]
                        parent_map.setdefault(dep_name, name)
            records.append(
                DependencyRecord(
                    name=name,
                    ecosystem="rust",
                    scope="direct" if name in direct_names else "transitive",
                    resolved_version=str(package.get("version")) if package.get("version") is not None else None,
                    source="lockfile",
                    parent=parent_map.get(name),
                    manifest_path="Cargo.lock",
                )
            )
        return records

    def _analyze_go(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        go_mod = root / "go.mod"
        if not go_mod.exists():
            return [], []
        records = self._parse_go_mod(go_mod)
        return records, ["go: go.sum no expone arbol transitivo fiable offline en esta fase"]

    def _parse_go_mod(self, go_mod: Path) -> list[DependencyRecord]:
        records: list[DependencyRecord] = []
        in_require_block = False
        for line in read_text_lines(go_mod):
            stripped = line.strip()
            if stripped.startswith("require ("):
                in_require_block = True
                continue
            if in_require_block and stripped == ")":
                in_require_block = False
                continue
            if stripped.startswith("require "):
                stripped = stripped.removeprefix("require ").strip()
            elif not in_require_block:
                continue

            parts = stripped.split()
            if len(parts) < 2:
                continue
            name, version = parts[0], parts[1]
            scope = "indirect" if "// indirect" in stripped else "direct"
            records.append(
                DependencyRecord(
                    name=name,
                    ecosystem="go",
                    scope=scope,
                    declared_version=version,
                    resolved_version=version,
                    source="manifest",
                    manifest_path="go.mod",
                )
            )
        return records

    def _analyze_dotnet(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        csproj_files = sorted(root.glob("*.csproj"))
        if not csproj_files:
            return [], []
        records: list[DependencyRecord] = []
        for csproj in csproj_files:
            records.extend(self._parse_csproj_dependencies(csproj))

        packages_lock = load_json_file(root / "packages.lock.json")
        if packages_lock is not None:
            records = self._merge_resolved(records, self._parse_packages_lock(packages_lock, records))
            return records, []
        return records, ["dotnet: packages.lock.json ausente; transitivas no disponibles"]

    def _parse_csproj_dependencies(self, csproj: Path) -> list[DependencyRecord]:
        try:
            tree = ET.parse(csproj)
        except (ET.ParseError, OSError):
            return []
        records: list[DependencyRecord] = []
        for elem in tree.findall(".//PackageReference"):
            include = elem.attrib.get("Include") or elem.attrib.get("Update")
            if not include:
                continue
            version = elem.attrib.get("Version")
            if version is None:
                version_elem = elem.find("Version")
                version = version_elem.text if version_elem is not None else None
            records.append(
                DependencyRecord(
                    name=include,
                    ecosystem="dotnet",
                    declared_version=version,
                    source="manifest",
                    manifest_path=csproj.name,
                )
            )
        return records

    def _parse_packages_lock(
        self, lock_data: dict[str, Any], direct_records: list[DependencyRecord]
    ) -> list[DependencyRecord]:
        direct_names = {record.name for record in direct_records}
        records: list[DependencyRecord] = []
        dependencies = lock_data.get("dependencies", {})
        if not isinstance(dependencies, dict):
            return []
        for target_data in dependencies.values():
            if not isinstance(target_data, dict):
                continue
            parent_map: dict[str, str] = {}
            for package_name, info in target_data.items():
                if not isinstance(info, dict):
                    continue
                nested = info.get("dependencies", {})
                if isinstance(nested, dict):
                    for dependency_name in nested:
                        parent_map.setdefault(str(dependency_name), str(package_name))
            for package_name, info in target_data.items():
                if not isinstance(info, dict):
                    continue
                records.append(
                    DependencyRecord(
                        name=str(package_name),
                        ecosystem="dotnet",
                        scope="direct" if package_name in direct_names else "transitive",
                        resolved_version=str(info.get("resolved")) if info.get("resolved") is not None else None,
                        source="lockfile",
                        parent=parent_map.get(str(package_name)),
                        manifest_path="packages.lock.json",
                    )
                )
        return records

    def _merge_resolved(
        self,
        direct_records: list[DependencyRecord],
        resolved_records: list[DependencyRecord],
    ) -> list[DependencyRecord]:
        if not resolved_records:
            return direct_records
        result: list[DependencyRecord] = []
        matched: set[tuple[str, str, str]] = set()
        for direct in direct_records:
            replacement = next(
                (
                    resolved
                    for resolved in resolved_records
                    if resolved.ecosystem == direct.ecosystem
                    and resolved.name == direct.name
                    and resolved.scope == direct.scope
                ),
                None,
            )
            if replacement is None:
                result.append(direct)
                continue
            matched.add((replacement.ecosystem, replacement.name, replacement.scope))
            result.append(
                DependencyRecord(
                    name=direct.name,
                    ecosystem=direct.ecosystem,
                    scope=direct.scope,
                    declared_version=direct.declared_version,
                    resolved_version=replacement.resolved_version,
                    source=replacement.source,
                    parent=replacement.parent,
                    manifest_path=replacement.manifest_path or direct.manifest_path,
                    workspace=direct.workspace,
                )
            )
        for resolved in resolved_records:
            key = (resolved.ecosystem, resolved.name, resolved.scope)
            if key not in matched:
                result.append(resolved)
        return self._dedupe(result)

    def _analyze_java(self, root: Path) -> tuple[list[DependencyRecord], list[str]]:
        pom = root / "pom.xml"
        if not pom.exists():
            return [], []
        try:
            tree = ET.parse(pom)
        except (ET.ParseError, OSError):
            return [], ["java: error al parsear pom.xml"]

        root_elem = tree.getroot()
        ns_match = re.match(r"\{[^}]+\}", root_elem.tag)
        ns = ns_match.group(0) if ns_match else ""

        records: list[DependencyRecord] = []
        deps_elem = root_elem.find(f"{ns}dependencies")
        if deps_elem is None:
            return [], ["java: pom.xml sin bloque <dependencies>"]

        for dep in deps_elem.findall(f"{ns}dependency"):
            group_id = (dep.findtext(f"{ns}groupId") or "").strip()
            artifact_id = (dep.findtext(f"{ns}artifactId") or "").strip()
            if not group_id or not artifact_id:
                continue
            version_raw = (dep.findtext(f"{ns}version") or "").strip() or None
            # Versiones interpoladas con propiedades Maven (${...}) no son resolvibles estáticamente
            declared = version_raw if version_raw and not version_raw.startswith("${") else None
            scope_text = (dep.findtext(f"{ns}scope") or "compile").strip().lower()
            scope = "dev" if scope_text == "test" else "direct"
            records.append(
                DependencyRecord(
                    name=f"{group_id}:{artifact_id}",
                    ecosystem="java",
                    scope=scope,
                    declared_version=declared,
                    source="manifest",
                    manifest_path="pom.xml",
                )
            )

        limitations: list[str] = []
        if not records:
            limitations.append("java: pom.xml sin dependencias parseables (puede usar BOM o propiedades)")
        return records, limitations

    def _load_yaml_file(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        yaml = YAML(typ="safe")
        try:
            data = yaml.load(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None
