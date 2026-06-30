from __future__ import annotations

from pathlib import Path

from sourcecode.architecture_analyzer import ArchitectureAnalyzer
from sourcecode.schema import SourceMap


def _sm_with_paths(*paths: str) -> SourceMap:
    sm = SourceMap()
    sm.file_paths = list(paths)
    return sm


ROOT = Path(".")


def test_domain_clustering_from_paths() -> None:
    sm = _sm_with_paths(
        "controllers/auth.py",
        "controllers/users.py",
        "services/auth.py",
        "services/orders.py",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    domain_names = {d.name for d in analysis.domains}
    assert "controllers" in domain_names
    assert "services" in domain_names
    ctrl = next(d for d in analysis.domains if d.name == "controllers")
    assert len(ctrl.files) == 2
    svc = next(d for d in analysis.domains if d.name == "services")
    assert len(svc.files) == 2


def test_layer_detection_mvc() -> None:
    sm = _sm_with_paths(
        "controllers/home.py",
        "controllers/users.py",
        "models/user.py",
        "models/order.py",
        "views/home.html",
        "views/user.html",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern == "mvc"
    layer_patterns = {la.pattern for la in analysis.layers}
    assert "mvc" in layer_patterns


def test_layer_detection_layered() -> None:
    sm = _sm_with_paths(
        "controllers/api.py",
        "controllers/web.py",
        "services/orders.py",
        "services/users.py",
        "repositories/user_repo.py",
        "repositories/order_repo.py",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern == "layered"


def test_layer_detection_fullstack() -> None:
    sm = _sm_with_paths(
        "frontend/app.tsx",
        "frontend/index.ts",
        "backend/server.py",
        "backend/api.py",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern == "fullstack"


def test_bounded_context_from_directories() -> None:
    sm = _sm_with_paths(
        "orders/model.py",
        "orders/service.py",
        "payments/model.py",
        "payments/service.py",
        "users/model.py",
        "users/service.py",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert len(analysis.bounded_contexts) >= 3
    bc_names = {bc.name for bc in analysis.bounded_contexts}
    assert "orders" in bc_names
    assert "payments" in bc_names
    assert "users" in bc_names


def test_graceful_degradation_flat_project() -> None:
    sm = _sm_with_paths("main.py", "utils.py", "helpers.py")
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern in ("flat", "unknown")
    assert len(analysis.limitations) > 0


def test_no_architecture_flag_omits_field() -> None:
    sm = SourceMap()
    assert sm.architecture is None


def test_atlas_cli_structure_detects_layered() -> None:
    """atlas-cli repo structure must classify as layered, not unknown.

    The project has no classical directory names (controllers/, services/) but
    its file-naming conventions clearly signal three layers:
      - orchestration: cli.py
      - processing:    *_analyzer.py, scanner.py, coverage_parser.py
      - data:          schema.py, serializer.py
    Tests must not be counted as an architecture domain.
    """
    sm = _sm_with_paths(
        # orchestration layer
        "src/sourcecode/cli.py",
        "src/sourcecode/prepare_context.py",
        # processing layer
        "src/sourcecode/scanner.py",
        "src/sourcecode/architecture_analyzer.py",
        "src/sourcecode/dependency_analyzer.py",
        "src/sourcecode/graph_analyzer.py",
        "src/sourcecode/semantic_analyzer.py",
        "src/sourcecode/doc_analyzer.py",
        "src/sourcecode/metrics_analyzer.py",
        "src/sourcecode/coverage_parser.py",
        # detectors sub-package
        "src/sourcecode/detectors/__init__.py",
        "src/sourcecode/detectors/base.py",
        "src/sourcecode/detectors/python.py",
        "src/sourcecode/detectors/nodejs.py",
        # data/schema layer
        "src/sourcecode/schema.py",
        "src/sourcecode/serializer.py",
        # support modules
        "src/sourcecode/classifier.py",
        "src/sourcecode/redactor.py",
        "src/sourcecode/workspace.py",
        "src/sourcecode/tree_utils.py",
        # tests — must NOT appear as a domain
        "tests/test_cli.py",
        "tests/test_scanner.py",
        "tests/test_architecture_analyzer.py",
        "tests/test_schema.py",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)

    assert analysis.pattern == "layered", (
        f"Expected 'layered', got '{analysis.pattern}'. "
        f"Layers detected: {[la.name for la in analysis.layers]}"
    )
    assert analysis.pattern != "unknown"

    domain_names = {d.name for d in analysis.domains}
    assert "tests" not in domain_names, "tests must not be an architecture domain"

    layer_names = {la.name for la in analysis.layers}
    assert "processing" in layer_names, "processing layer expected (*_analyzer.py, scanner.py)"
    assert "data" in layer_names, "data layer expected (schema.py, serializer.py)"

    assert analysis.confidence == "low", (
        f"Filename-only architecture inference must stay low confidence, got '{analysis.confidence}'"
    )
    assert any("filename" in item.lower() for item in analysis.limitations)

    # detectors sub-package must surface as its own domain, not collapse into 'sourcecode'
    assert "detectors" in domain_names, (
        "src/sourcecode/detectors/ should be its own domain, not merged into 'sourcecode'"
    )


# ── v1.69.0 regression: BUG #4 phantom MVC on a library (JobRunr field test) ──

def test_no_mvc_without_view_layer() -> None:
    # controller-ish + model dirs but NO view layer is NOT MVC (it's layered/other).
    sm = _sm_with_paths(
        "dashboard/server/http/handlers/JobHandler.java",
        "dashboard/server/http/handlers/ServerHandler.java",
        "dashboard/ui/model/JobModel.java",
        "dashboard/ui/model/ServerModel.java",
        "storage/StorageProvider.java",
        "storage/sql/SqlStorage.java",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern != "mvc", analysis.pattern


def test_bundled_frontend_under_resources_not_a_view_layer() -> None:
    # A React SPA bundled under src/main/resources/.../frontend must not create an
    # MVC "view" layer for a Java backend (JobRunr dashboard).
    sm = _sm_with_paths(
        "core/src/main/java/org/jr/dashboard/server/http/handlers/H.java",
        "core/src/main/java/org/jr/dashboard/server/http/handlers/H2.java",
        "core/src/main/java/org/jr/dashboard/ui/model/M.java",
        "core/src/main/java/org/jr/dashboard/ui/model/M2.java",
        "core/src/main/resources/org/jr/dashboard/frontend/src/components/App.tsx",
        "core/src/main/resources/org/jr/dashboard/frontend/src/components/Job.tsx",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern != "mvc", analysis.pattern
    layer_names = {l.name for l in analysis.layers}
    assert "view" not in layer_names


def test_real_mvc_with_template_views_still_detected() -> None:
    # Control: a genuine MVC app whose views are .html templates is still MVC,
    # even though templates are non-code files.
    sm = _sm_with_paths(
        "controllers/home.py", "controllers/users.py",
        "models/user.py", "models/order.py",
        "views/home.html", "views/user.html",
    )
    analysis = ArchitectureAnalyzer().analyze(ROOT, sm)
    assert analysis.pattern == "mvc", analysis.pattern
