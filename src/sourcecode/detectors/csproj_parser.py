from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


@dataclass
class CsprojProject:
    name: str
    path: str                                         # repo-relative path to .csproj
    project_dir: str                                  # repo-relative folder (empty if root)
    target_frameworks: list[str] = field(default_factory=list)
    output_type: str = ""
    sdk: str = ""
    project_references: list[str] = field(default_factory=list)  # resolved repo-relative csproj paths
    package_references: list[tuple[str, str]] = field(default_factory=list)
    project_type: str = "classlib"                    # webapi|classlib|console|test|blazor|worker
    language: str = "csharp"                          # csharp|fsharp|vbnet


_TEST_PACKAGES: frozenset[str] = frozenset({
    "xunit", "nunit", "mstest.testframework", "microsoft.net.test.sdk",
    "nunit3testadapter", "xunit.runner.visualstudio", "mstest.testadapter",
    "coverlet.collector", "moq", "nsubstitute", "fluentassertions",
})

_LAYER_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("api", "Api"),
    ("web", "Api"),
    ("endpoint", "Api"),
    ("controller", "Api"),
    ("application", "Application"),
    ("usecase", "Application"),
    ("usecases", "Application"),
    ("domain", "Domain"),
    ("core", "Domain"),
    ("infrastructure", "Infrastructure"),
    ("infra", "Infrastructure"),
    ("persistence", "Infrastructure"),
    ("repository", "Infrastructure"),
    ("shared", "Shared"),
    ("common", "Shared"),
    ("contracts", "Shared"),
    ("abstractions", "Shared"),
    ("test", "Tests"),
    ("tests", "Tests"),
    ("spec", "Tests"),
    ("specs", "Tests"),
)


def parse_csproj(absolute_path: Path, relative_path: str) -> CsprojProject | None:
    """Parse a .csproj/.fsproj/.vbproj and return structured project info. Returns None on error."""
    try:
        content = absolute_path.read_text(encoding="utf-8", errors="replace")
        root_elem = ET.fromstring(content)
    except (OSError, ET.ParseError):
        return None

    suffix = Path(relative_path).suffix.lower()
    language = {"fsproj": "fsharp", "vbproj": "vbnet"}.get(suffix.lstrip("."), "csharp")

    project_dir = str(PurePosixPath(relative_path).parent)
    if project_dir == ".":
        project_dir = ""

    name = Path(relative_path).stem
    sdk = root_elem.get("Sdk", "") or ""

    frameworks: list[str] = []
    output_type = ""
    package_refs: list[tuple[str, str]] = []
    project_refs: list[str] = []

    for elem in root_elem.iter():
        tag = _strip_ns(elem.tag)
        text = (elem.text or "").strip()

        if tag == "TargetFramework":
            if not frameworks and text:
                frameworks = [text]
        elif tag == "TargetFrameworks":
            if text:
                frameworks = [f.strip() for f in text.split(";") if f.strip()]
        elif tag == "OutputType":
            if not output_type:
                output_type = text
        elif tag == "AssemblyName":
            if text:
                name = text
        elif tag == "PackageReference":
            pkg = elem.get("Include", "") or ""
            ver = elem.get("Version", "") or (elem.findtext("Version") or "").strip()
            if pkg:
                package_refs.append((pkg, ver))
        elif tag == "ProjectReference":
            include = (elem.get("Include", "") or "").replace("\\", "/")
            if include:
                project_refs.append(include)

    if not sdk:
        sdk = _detect_sdk_from_imports(root_elem)

    resolved_refs = [
        r for r in (_resolve_ref(relative_path, ref) for ref in project_refs) if r
    ]

    project_type = _classify_project(
        sdk=sdk,
        output_type=output_type,
        name=name,
        package_refs=[p[0] for p in package_refs],
    )

    return CsprojProject(
        name=name,
        path=relative_path,
        project_dir=project_dir,
        target_frameworks=frameworks,
        output_type=output_type,
        sdk=sdk,
        project_references=resolved_refs,
        package_references=package_refs,
        project_type=project_type,
        language=language,
    )


def infer_architecture_pattern(projects: list[CsprojProject]) -> str | None:
    """Infer Clean Architecture / Onion / Layered from project names."""
    layers: set[str] = set()
    for project in projects:
        name_lower = project.name.lower()
        for keyword, layer in _LAYER_KEYWORDS:
            if keyword in name_lower:
                layers.add(layer)
                break

    if {"Application", "Domain", "Infrastructure"} <= layers:
        return "Clean Architecture"
    if {"Api", "Domain", "Infrastructure"} <= layers:
        return "Onion Architecture"
    if {"Api", "Application", "Infrastructure"} <= layers:
        return "Layered Architecture"
    if len(layers) >= 3:
        return "Layered Architecture"
    return None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _resolve_ref(source_csproj: str, ref: str) -> str | None:
    """Resolve a ProjectReference path (relative to source .csproj) to a repo-relative path."""
    source_dir = str(PurePosixPath(source_csproj).parent)
    base = "" if source_dir == "." else source_dir
    combined = f"{base}/{ref}" if base else ref
    parts: list[str] = []
    for part in combined.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts) if parts else None


def _detect_sdk_from_imports(root_elem: ET.Element) -> str:
    for elem in root_elem.iter():
        if _strip_ns(elem.tag) == "Import":
            project = elem.get("Project", "")
            if "Microsoft.NET" in project:
                return project
    return ""


def _classify_project(
    *,
    sdk: str,
    output_type: str,
    name: str,
    package_refs: list[str],
) -> str:
    sdk_l = sdk.lower()
    name_l = name.lower()
    pkgs_l = {p.lower() for p in package_refs}
    out_l = output_type.lower()

    if pkgs_l & _TEST_PACKAGES or any(kw in name_l for kw in ("test", "tests", "spec", "specs")):
        return "test"
    if "microsoft.net.sdk.blazor" in sdk_l or any("blazor" in p for p in pkgs_l):
        return "blazor"
    if "microsoft.net.sdk.web" in sdk_l or any(
        p.startswith("microsoft.aspnetcore") for p in pkgs_l
    ):
        return "webapi"
    if "microsoft.net.sdk.worker" in sdk_l or "microsoft.extensions.hosting" in pkgs_l:
        return "worker"
    if out_l in ("exe", "winexe"):
        return "console"
    return "classlib"
