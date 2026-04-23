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
