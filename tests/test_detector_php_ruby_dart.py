from __future__ import annotations

import json
from pathlib import Path

from sourcecode.detectors.dart import DartDetector
from sourcecode.detectors.php import PhpDetector
from sourcecode.detectors.project import ProjectDetector
from sourcecode.detectors.ruby import RubyDetector


def test_php_detector_detects_laravel_artisan(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^11.0"}})
    )
    (tmp_path / "artisan").write_text("php artisan")

    detector = ProjectDetector([PhpDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"composer.json": None, "artisan": None},
        manifests=["composer.json"],
    )

    assert stacks[0].frameworks[0].name == "Laravel"
    assert entry_points[0].path == "artisan"
    assert project_type == "api"


def test_ruby_detector_detects_rails_bin_entry(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text('gem "rails"\n')
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rails").write_text("#!/usr/bin/env ruby")

    detector = ProjectDetector([RubyDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"Gemfile": None, "bin": {"rails": None}},
        manifests=["Gemfile"],
    )

    assert stacks[0].frameworks[0].name == "Rails"
    assert entry_points[0].path == "bin/rails"
    assert project_type == "cli"


def test_dart_detector_detects_flutter_main(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n"
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.dart").write_text("void main() {}")

    detector = ProjectDetector([DartDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"pubspec.yaml": None, "lib": {"main.dart": None}},
        manifests=["pubspec.yaml"],
    )

    assert stacks[0].frameworks[0].name == "Flutter"
    assert entry_points[0].path == "lib/main.dart"
    assert project_type == "webapp"
