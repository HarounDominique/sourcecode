from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.detectors.csproj_parser import (
    CsprojProject,
    _classify_project,
    _resolve_ref,
    infer_architecture_pattern,
    parse_csproj,
)
from sourcecode.detectors.dotnet import DotnetDetector
from sourcecode.detectors.project import ProjectDetector
from sourcecode.graph_analyzer import GraphAnalyzer


# ── csproj_parser unit tests ─────────────────────────────────────────────────


def test_parse_csproj_console_project(tmp_path: Path) -> None:
    csproj = tmp_path / "App.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
  </ItemGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "App.csproj")
    assert project is not None
    assert project.name == "App"
    assert project.project_dir == ""
    assert project.target_frameworks == ["net8.0"]
    assert project.output_type == "Exe"
    assert project.sdk == "Microsoft.NET.Sdk"
    assert project.project_type == "console"
    assert project.language == "csharp"
    assert ("Newtonsoft.Json", "13.0.1") in project.package_references


def test_parse_csproj_webapi_project(tmp_path: Path) -> None:
    csproj = tmp_path / "Api.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.AspNetCore.OpenApi" Version="8.0.0" />
  </ItemGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "src/Api/Api.csproj")
    assert project is not None
    assert project.name == "Api"
    assert project.project_dir == "src/Api"
    assert project.sdk == "Microsoft.NET.Sdk.Web"
    assert project.project_type == "webapi"


def test_parse_csproj_test_project_by_package(tmp_path: Path) -> None:
    csproj = tmp_path / "Tests.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="xunit" Version="2.7.0" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.8.0" />
  </ItemGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "tests/UnitTests/Tests.csproj")
    assert project is not None
    assert project.project_type == "test"


def test_parse_csproj_test_project_by_name(tmp_path: Path) -> None:
    csproj = tmp_path / "MyApp.Tests.csproj"
    csproj.write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
    project = parse_csproj(csproj, "MyApp.Tests.csproj")
    assert project is not None
    assert project.project_type == "test"


def test_parse_csproj_multitarget(tmp_path: Path) -> None:
    csproj = tmp_path / "Lib.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFrameworks>net8.0;net6.0;netstandard2.0</TargetFrameworks>
  </PropertyGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "src/Lib/Lib.csproj")
    assert project is not None
    assert project.target_frameworks == ["net8.0", "net6.0", "netstandard2.0"]
    assert project.project_type == "classlib"


def test_parse_csproj_project_references(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Api").mkdir()
    csproj = tmp_path / "src" / "Api" / "Api.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="../Domain/Domain.csproj" />
    <ProjectReference Include="../Infrastructure/Infrastructure.csproj" />
  </ItemGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "src/Api/Api.csproj")
    assert project is not None
    assert "src/Domain/Domain.csproj" in project.project_references
    assert "src/Infrastructure/Infrastructure.csproj" in project.project_references


def test_parse_csproj_windows_backslash_refs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Api").mkdir()
    csproj = tmp_path / "src" / "Api" / "Api.csproj"
    csproj.write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>'
        '<ProjectReference Include="..\\Domain\\Domain.csproj" />'
        '</ItemGroup></Project>'
    )
    project = parse_csproj(csproj, "src/Api/Api.csproj")
    assert project is not None
    assert "src/Domain/Domain.csproj" in project.project_references


def test_parse_csproj_xml_namespace(tmp_path: Path) -> None:
    csproj = tmp_path / "App.csproj"
    csproj.write_text(
        """
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net48</TargetFramework>
  </PropertyGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "App.csproj")
    assert project is not None
    assert project.output_type == "Exe"
    assert project.target_frameworks == ["net48"]


def test_parse_csproj_fsharp_language(tmp_path: Path) -> None:
    csproj = tmp_path / "Lib.fsproj"
    csproj.write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
    project = parse_csproj(csproj, "src/Lib/Lib.fsproj")
    assert project is not None
    assert project.language == "fsharp"


def test_parse_csproj_broken_xml(tmp_path: Path) -> None:
    csproj = tmp_path / "Broken.csproj"
    csproj.write_text("<Project><PropertyGroup><Not closed")
    result = parse_csproj(csproj, "Broken.csproj")
    assert result is None


def test_parse_csproj_missing_file(tmp_path: Path) -> None:
    result = parse_csproj(tmp_path / "nonexistent.csproj", "nonexistent.csproj")
    assert result is None


def test_parse_csproj_assembly_name_overrides_filename(tmp_path: Path) -> None:
    csproj = tmp_path / "MyProject.csproj"
    csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <AssemblyName>CustomName</AssemblyName>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
        """.strip()
    )
    project = parse_csproj(csproj, "MyProject.csproj")
    assert project is not None
    assert project.name == "CustomName"


# ── _resolve_ref unit tests ───────────────────────────────────────────────────


def test_resolve_ref_simple() -> None:
    result = _resolve_ref("src/Api/Api.csproj", "../Domain/Domain.csproj")
    assert result == "src/Domain/Domain.csproj"


def test_resolve_ref_deep_relative() -> None:
    result = _resolve_ref("a/b/c/C.csproj", "../../d/D.csproj")
    assert result == "a/d/D.csproj"


def test_resolve_ref_root_level() -> None:
    result = _resolve_ref("Api.csproj", "../Other/Other.csproj")
    assert result == "Other/Other.csproj"


def test_resolve_ref_sibling() -> None:
    result = _resolve_ref("src/Api/Api.csproj", "../Infra/Infra.csproj")
    assert result == "src/Infra/Infra.csproj"


# ── _classify_project unit tests ─────────────────────────────────────────────


def test_classify_console() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk", output_type="Exe", name="App", package_refs=[])
    assert result == "console"


def test_classify_webapi_by_sdk() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk.Web", output_type="", name="Api", package_refs=[])
    assert result == "webapi"


def test_classify_webapi_by_package() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk", output_type="", name="Api", package_refs=["Microsoft.AspNetCore.Mvc"])
    assert result == "webapi"


def test_classify_test_by_package() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk", output_type="", name="MyLib", package_refs=["xunit"])
    assert result == "test"


def test_classify_test_by_name() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk", output_type="", name="MyApp.Tests", package_refs=[])
    assert result == "test"


def test_classify_blazor_by_sdk() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk.Blazor", output_type="", name="Web", package_refs=[])
    assert result == "blazor"


def test_classify_classlib_default() -> None:
    result = _classify_project(sdk="Microsoft.NET.Sdk", output_type="Library", name="MyLib", package_refs=[])
    assert result == "classlib"


# ── infer_architecture_pattern tests ─────────────────────────────────────────


def _make_project(name: str) -> CsprojProject:
    return CsprojProject(name=name, path=f"{name}/{name}.csproj", project_dir=name)


def test_infer_clean_architecture() -> None:
    projects = [
        _make_project("MyApp.Api"),
        _make_project("MyApp.Application"),
        _make_project("MyApp.Domain"),
        _make_project("MyApp.Infrastructure"),
    ]
    assert infer_architecture_pattern(projects) == "Clean Architecture"


def test_infer_onion_architecture() -> None:
    projects = [
        _make_project("MyApp.Api"),
        _make_project("MyApp.Domain"),
        _make_project("MyApp.Infrastructure"),
    ]
    assert infer_architecture_pattern(projects) == "Onion Architecture"


def test_infer_layered_architecture() -> None:
    projects = [
        _make_project("MyApp.Api"),
        _make_project("MyApp.Application"),
        _make_project("MyApp.Infrastructure"),
    ]
    assert infer_architecture_pattern(projects) == "Layered Architecture"


def test_infer_no_pattern_single_project() -> None:
    projects = [_make_project("MyApp")]
    assert infer_architecture_pattern(projects) is None


def test_infer_no_pattern_two_unrelated() -> None:
    projects = [_make_project("Foo"), _make_project("Bar")]
    assert infer_architecture_pattern(projects) is None


# ── DotnetDetector integration tests ─────────────────────────────────────────


def test_dotnet_detector_multi_project_solution(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for proj in ("Api", "Application", "Domain", "Infrastructure"):
        proj_dir = tmp_path / "src" / proj
        proj_dir.mkdir()
        sdk = "Microsoft.NET.Sdk.Web" if proj == "Api" else "Microsoft.NET.Sdk"
        (proj_dir / f"{proj}.csproj").write_text(
            f'<Project Sdk="{sdk}"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
        )

    file_tree = {
        "src": {
            "Api": {"Api.csproj": None, "Program.cs": None},
            "Application": {"Application.csproj": None},
            "Domain": {"Domain.csproj": None},
            "Infrastructure": {"Infrastructure.csproj": None},
        }
    }
    detector = ProjectDetector([DotnetDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path, file_tree=file_tree, manifests=[]
    )

    assert stacks[0].stack == "dotnet"
    assert stacks[0].package_manager == "nuget"
    fw_names = {f.name for f in stacks[0].frameworks}
    assert "ASP.NET Core" in fw_names

    signals_joined = " ".join(stacks[0].signals)
    assert "4 projects" in signals_joined
    assert "Clean Architecture" in signals_joined

    assert any(ep.path == "src/Api/Program.cs" for ep in entry_points)
    assert project_type == "api"


def test_dotnet_detector_signals_contain_project_types(tmp_path: Path) -> None:
    (tmp_path / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    )
    (tmp_path / "Tests.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="xunit" Version="2.7.0" /></ItemGroup></Project>'
    )
    file_tree = {"Api.csproj": None, "Tests.csproj": None, "Program.cs": None}
    detector = DotnetDetector()
    from sourcecode.detectors.base import DetectionContext
    context = DetectionContext(root=tmp_path, file_tree=file_tree)
    stacks, _ = detector.detect(context)

    signals_joined = " ".join(stacks[0].signals)
    assert "webapi" in signals_joined
    assert "test" in signals_joined


def test_dotnet_detector_backward_compat_console(tmp_path: Path) -> None:
    (tmp_path / "App.csproj").write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
  </PropertyGroup>
</Project>
        """.strip()
    )
    (tmp_path / "Program.cs").write_text('Console.WriteLine("hi");')
    detector = ProjectDetector([DotnetDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"App.csproj": None, "Program.cs": None},
        manifests=[],
    )
    assert stacks[0].stack == "dotnet"
    assert stacks[0].package_manager == "nuget"
    assert entry_points[0].path == "Program.cs"
    assert project_type == "cli"


# ── GraphAnalyzer dotnet tests ────────────────────────────────────────────────


def test_graph_analyzer_dotnet_project_nodes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for proj in ("Api", "Domain"):
        proj_dir = tmp_path / "src" / proj
        proj_dir.mkdir()
        (proj_dir / f"{proj}.csproj").write_text(
            f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
        )

    file_tree = {
        "src": {
            "Api": {"Api.csproj": None},
            "Domain": {"Domain.csproj": None},
        }
    }
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)

    node_ids = {node.id for node in graph.nodes}
    assert "module:src/Api" in node_ids
    assert "module:src/Domain" in node_ids

    node_map = {node.id: node for node in graph.nodes}
    assert node_map["module:src/Api"].display_name == "Api"
    assert node_map["module:src/Api"].language == "csharp"


def test_graph_analyzer_dotnet_project_edges(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    api_dir = tmp_path / "src" / "Api"
    domain_dir = tmp_path / "src" / "Domain"
    api_dir.mkdir()
    domain_dir.mkdir()

    api_csproj = api_dir / "Api.csproj"
    api_csproj.write_text(
        """
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="../Domain/Domain.csproj" />
  </ItemGroup>
</Project>
        """.strip()
    )
    (domain_dir / "Domain.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    )

    file_tree = {
        "src": {
            "Api": {"Api.csproj": None},
            "Domain": {"Domain.csproj": None},
        }
    }
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)

    edges = [(e.source, e.target, e.kind) for e in graph.edges]
    assert ("module:src/Api", "module:src/Domain", "imports") in edges


def test_graph_analyzer_dotnet_unresolved_ref_logged(tmp_path: Path) -> None:
    api_dir = tmp_path / "Api"
    api_dir.mkdir()
    (api_dir / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup><ProjectReference Include="../Missing/Missing.csproj" /></ItemGroup></Project>'
    )

    file_tree = {"Api": {"Api.csproj": None}}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)

    assert any("dotnet_unresolved_ref" in lim for lim in graph.summary.limitations)


def test_graph_analyzer_dotnet_root_level_project(tmp_path: Path) -> None:
    (tmp_path / "MyApp.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'
    )
    file_tree = {"MyApp.csproj": None}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)

    node_ids = {node.id for node in graph.nodes}
    assert "module:MyApp" in node_ids
