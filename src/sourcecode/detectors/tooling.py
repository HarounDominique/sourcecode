"""Senales universales de tooling y helpers transversales."""
from __future__ import annotations

from sourcecode.tree_utils import flatten_file_tree


def collect_tooling_signals(file_tree: dict[str, object]) -> list[str]:
    """Recolecta senales operativas reutilizables sin crear stacks por si solas."""
    flat_paths = set(flatten_file_tree(file_tree))
    signals: list[str] = []
    for path, label in (
        ("Dockerfile", "tooling:docker"),
        ("docker-compose.yml", "tooling:docker-compose"),
        ("docker-compose.yaml", "tooling:docker-compose"),
        ("Taskfile.yml", "tooling:taskfile"),
        ("Taskfile.yaml", "tooling:taskfile"),
        ("justfile", "tooling:just"),
        ("Procfile", "tooling:procfile"),
        ("Makefile", "tooling:make"),
    ):
        if path in flat_paths:
            signals.append(label)
    return signals


def infer_package_manager(stack: str, file_tree: dict[str, object]) -> str | None:
    """Infiera package manager por lockfiles o tooling conocido."""
    flat_paths = set(flatten_file_tree(file_tree))
    if stack == "nodejs":
        for path, manager in (
            ("bun.lockb", "bun"),
            ("pnpm-lock.yaml", "pnpm"),
            ("package-lock.json", "npm"),
            ("yarn.lock", "yarn"),
        ):
            if path in flat_paths:
                return manager
    if stack == "python":
        for path, manager in (
            ("poetry.lock", "poetry"),
            ("Pipfile.lock", "pipenv"),
            ("uv.lock", "uv"),
            ("requirements.txt", "pip"),
        ):
            if path in flat_paths:
                return manager
    if stack == "php" and "composer.lock" in flat_paths:
        return "composer"
    if stack == "ruby" and "Gemfile.lock" in flat_paths:
        return "bundler"
    if stack == "terraform" and any(path.endswith(".tf") for path in flat_paths):
        return "terraform"
    return None
