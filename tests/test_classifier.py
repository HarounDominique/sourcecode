"""Tests del clasificador de proyecto y scoring de confianza."""
from __future__ import annotations

from sourcecode.classifier import TypeClassifier
from sourcecode.schema import EntryPoint, FrameworkDetection, StackDetection


def test_classifier_marks_nextjs_as_webapp() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"app": {"page.tsx": None}, "package.json": None},
        [
            StackDetection(
                stack="nodejs",
                frameworks=[FrameworkDetection(name="Next.js")],
                manifests=["package.json"],
            )
        ],
        [EntryPoint(path="app/page.tsx", stack="nodejs", kind="web")],
    )

    assert project_type == "webapp"
    assert stacks[0].confidence == "high"
    assert stacks[0].primary is True


def test_classifier_marks_fastapi_as_api() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"src": {"main.py": None}, "pyproject.toml": None},
        [
            StackDetection(
                stack="python",
                frameworks=[FrameworkDetection(name="FastAPI")],
                manifests=["pyproject.toml"],
            )
        ],
        [EntryPoint(path="src/main.py", stack="python", kind="app")],
    )

    assert project_type == "api"
    assert stacks[0].confidence == "high"


def test_classifier_marks_typer_as_cli() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"src": {"main.py": None}, "pyproject.toml": None},
        [
            StackDetection(
                stack="python",
                frameworks=[FrameworkDetection(name="Typer")],
                manifests=["pyproject.toml"],
            )
        ],
        [EntryPoint(path="src/main.py", stack="python", kind="cli")],
    )

    assert project_type == "cli"
    assert stacks[0].primary is True


def test_classifier_marks_rust_lib_as_library() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"Cargo.toml": None, "src": {"lib.rs": None}},
        [StackDetection(stack="rust", manifests=["Cargo.toml"])],
        [],
    )

    assert project_type == "library"
    assert stacks[0].confidence == "medium"


def test_classifier_marks_heuristic_repo_as_unknown() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"src": {"main.py": None}},
        [StackDetection(stack="python", detection_method="heuristic")],
        [EntryPoint(path="src/main.py", stack="python", kind="script", source="heuristic")],
    )

    assert project_type == "unknown"
    assert stacks[0].confidence == "low"


def test_classifier_marks_multistack_repo_as_fullstack() -> None:
    classifier = TypeClassifier()
    stacks, project_type = classifier.enrich(
        {"app": {"page.tsx": None}, "backend": {"main.py": None}},
        [
            StackDetection(
                stack="nodejs",
                frameworks=[FrameworkDetection(name="Next.js")],
                manifests=["package.json"],
            ),
            StackDetection(
                stack="python",
                frameworks=[FrameworkDetection(name="FastAPI")],
                manifests=["pyproject.toml"],
            ),
        ],
        [
            EntryPoint(path="app/page.tsx", stack="nodejs", kind="web"),
            EntryPoint(path="backend/main.py", stack="python", kind="app"),
        ],
    )

    assert project_type == "fullstack"
    assert sum(1 for stack in stacks if stack.primary) == 1
