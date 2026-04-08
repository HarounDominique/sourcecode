"""Tests del analizador de dependencias para ecosistemas adicionales."""
from __future__ import annotations

import json
from pathlib import Path

from sourcecode.dependency_analyzer import DependencyAnalyzer


def test_php_composer_lock_reports_resolved_and_transitive_dependencies(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^11.0"}, "require-dev": {"phpunit/phpunit": "^11.0"}})
    )
    (tmp_path / "composer.lock").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "laravel/framework", "version": "v11.0.0", "require": {"symfony/http-foundation": "^7.0"}},
                    {"name": "symfony/http-foundation", "version": "v7.1.0"},
                ],
                "packages-dev": [{"name": "phpunit/phpunit", "version": "11.3.0"}],
            }
        )
    )

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    laravel_dep = next(record for record in records if record.name == "laravel/framework")
    symfony_dep = next(record for record in records if record.name == "symfony/http-foundation")
    assert laravel_dep.resolved_version == "v11.0.0"
    assert symfony_dep.scope == "transitive"
    assert symfony_dep.parent == "laravel/framework"
    assert "php" in summary.ecosystems


def test_ruby_gemfile_lock_reports_resolved_versions(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text('gem "rails", "~> 7.1"\n')
    (tmp_path / "Gemfile.lock").write_text(
        """
GEM
  remote: https://rubygems.org/
  specs:
    rails (7.1.0)
      rack (2.2.8)
    rack (2.2.8)

DEPENDENCIES
  rails (~> 7.1)
        """.strip()
    )

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    rails_dep = next(record for record in records if record.name == "rails")
    rack_dep = next(record for record in records if record.name == "rack")
    assert rails_dep.resolved_version == "7.1.0"
    assert rack_dep.scope == "transitive"
    assert rack_dep.parent == "rails"
    assert summary.transitive_count >= 1


def test_rust_go_and_dotnet_report_limitations_or_exact_versions(tmp_path: Path) -> None:
    rust_root = tmp_path / "rust"
    rust_root.mkdir()
    (rust_root / "Cargo.toml").write_text(
        """
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0"
        """.strip()
    )
    (rust_root / "Cargo.lock").write_text(
        """
version = 3

[[package]]
name = "serde"
version = "1.0.210"
        """.strip()
    )

    go_root = tmp_path / "go"
    go_root.mkdir()
    (go_root / "go.mod").write_text(
        """
module example.com/demo

go 1.22

require github.com/gin-gonic/gin v1.10.0
        """.strip()
    )

    dotnet_root = tmp_path / "dotnet"
    dotnet_root.mkdir()
    (dotnet_root / "App.csproj").write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
  </ItemGroup>
</Project>
        """.strip()
    )
    (dotnet_root / "packages.lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "dependencies": {
                    "net8.0": {
                        "Newtonsoft.Json": {"type": "Direct", "resolved": "13.0.3"},
                        "System.Runtime.CompilerServices.Unsafe": {
                            "type": "Transitive",
                            "resolved": "6.0.0",
                            "dependencies": {},
                        },
                    }
                },
            }
        )
    )

    rust_records, rust_summary = DependencyAnalyzer().analyze(rust_root)
    go_records, go_summary = DependencyAnalyzer().analyze(go_root)
    dotnet_records, dotnet_summary = DependencyAnalyzer().analyze(dotnet_root)

    serde_dep = next(record for record in rust_records if record.name == "serde")
    gin_dep = next(record for record in go_records if record.name == "github.com/gin-gonic/gin")
    json_dep = next(record for record in dotnet_records if record.name == "Newtonsoft.Json")
    transitive_dotnet = next(
        record for record in dotnet_records if record.name == "System.Runtime.CompilerServices.Unsafe"
    )

    assert serde_dep.resolved_version == "1.0.210"
    assert gin_dep.resolved_version == "v1.10.0"
    assert "go: go.sum no expone arbol transitivo fiable offline en esta fase" in go_summary.limitations
    assert json_dep.resolved_version == "13.0.3"
    assert transitive_dotnet.scope == "transitive"
    assert "dotnet" in dotnet_summary.ecosystems
