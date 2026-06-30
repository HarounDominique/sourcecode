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


def test_classifier_marks_single_primary_for_duplicate_ecosystem_stacks() -> None:
    classifier = TypeClassifier()
    stacks, _ = classifier.enrich(
        {
            "package.json": None,
            "packages": {"app": {"package.json": None}},
        },
        [
            StackDetection(stack="nodejs", manifests=["package.json"], root="."),
            StackDetection(stack="nodejs", manifests=["package.json"], root="packages/app"),
        ],
        [],
    )

    assert sum(1 for stack in stacks if stack.primary) == 1


# ── v1.69.0 regression: BUG #4 framework-locality (JobRunr field test) ────────
# A framework confined to a small optional adapter submodule must not label the
# whole multi-module repo as that framework's app type.

def _tree_from_paths(paths):
    tree: dict = {}
    for p in paths:
        cur = tree
        segs = p.split("/")
        for seg in segs[:-1]:
            cur = cur.setdefault(seg, {})
        cur[segs[-1]] = None
    return tree


def test_framework_in_adapter_submodule_is_library() -> None:
    classifier = TypeClassifier()
    core = [f"core/src/main/java/org/jr/C{i}.java" for i in range(20)]
    adapter = [f"framework-support/jr-quarkus/src/main/java/org/jr/q/Q{i}.java" for i in range(3)]
    file_tree = _tree_from_paths(core + adapter + ["build.gradle", "settings.gradle"])
    quarkus = FrameworkDetection(
        name="Quarkus", source="imports", confidence="medium",
        detected_via=[
            "import io.quarkus (framework-support/jr-quarkus/src/main/java/org/jr/q/Q0.java)",
            "import io.quarkus (framework-support/jr-quarkus/src/main/java/org/jr/q/Q1.java)",
        ],
    )
    stacks, project_type = classifier.enrich(
        file_tree, [StackDetection(stack="java", frameworks=[quarkus])], [],
    )
    assert project_type == "library", project_type
    assert project_type != "api"


def test_monolithic_framework_app_still_api() -> None:
    # Control: framework lives in the dominant module → real app, stays "api".
    classifier = TypeClassifier()
    paths = [f"src/main/java/org/app/C{i}.java" for i in range(20)]
    file_tree = _tree_from_paths(paths + ["build.gradle"])
    spring = FrameworkDetection(
        name="Spring Boot", source="imports", confidence="high",
        detected_via=["import org.springframework (src/main/java/org/app/C0.java)"],
    )
    _, project_type = classifier.enrich(
        file_tree, [StackDetection(stack="java", frameworks=[spring])], [],
    )
    assert project_type == "api"
