"""Tests del detector Node.js."""
from __future__ import annotations

import json
from pathlib import Path

from sourcecode.detectors.nodejs import NodejsDetector
from sourcecode.detectors.project import ProjectDetector


def test_nodejs_detector_detects_nextjs_and_pnpm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "next-app",
                "dependencies": {"next": "15.0.0", "react": "19.0.0"},
            }
        )
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page() {}")

    detector = ProjectDetector([NodejsDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={
            "package.json": None,
            "pnpm-lock.yaml": None,
            "app": {"page.tsx": None},
        },
        manifests=["package.json"],
    )

    assert stacks[0].stack == "nodejs"
    assert stacks[0].package_manager == "pnpm"
    assert {framework.name for framework in stacks[0].frameworks} == {"Next.js", "React"}
    assert [entry.path for entry in entry_points] == ["app/page.tsx"]
    assert project_type == "webapp"


def test_nodejs_detector_detects_express_server_entry(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "5.0.0"}, "main": "server.js"})
    )
    (tmp_path / "server.js").write_text("console.log('hello')")

    detector = ProjectDetector([NodejsDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"package.json": None, "server.js": None},
        manifests=["package.json"],
    )

    assert stacks[0].frameworks[0].name == "Express"
    assert entry_points[0].path == "server.js"
    assert entry_points[0].source == "package.json"
    assert entry_points[0].confidence == "high"
    assert project_type == "api"


def test_nodejs_detector_detects_vite_typescript_entry(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}, "devDependencies": {"vite": "6.0.0"}})
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.tsx").write_text("console.log('vite')")

    detector = ProjectDetector([NodejsDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"package.json": None, "src": {"main.tsx": None}},
        manifests=["package.json"],
    )

    assert {framework.name for framework in stacks[0].frameworks} == {"React", "Vite"}
    assert entry_points[0].path == "src/main.tsx"
    assert entry_points[0].source == "convention"
    assert entry_points[0].confidence == "medium"
    assert project_type == "webapp"


def test_nodejs_detector_uses_bin_as_convention_entry(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "cli", "bin": {"sourcecode": "bin/sourcecode.js"}})
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "sourcecode.js").write_text("console.log('cli')")

    detector = ProjectDetector([NodejsDetector()])
    stacks, entry_points, _project_type = detector.detect(
        root=tmp_path,
        file_tree={"package.json": None, "bin": {"sourcecode.js": None}},
        manifests=["package.json"],
    )

    assert stacks[0].stack == "nodejs"
    assert entry_points[0].path == "bin/sourcecode.js"
    assert entry_points[0].source == "convention"
    assert entry_points[0].confidence == "medium"
