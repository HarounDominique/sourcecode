"""Tests unitarios del scanner de ficheros."""
from pathlib import Path

import pytest

from sourcecode.scanner import DEFAULT_EXCLUDES, FileScanner


@pytest.fixture
def project_with_excludes(tmp_path: Path) -> Path:
    """Proyecto con directorios que deben excluirse."""
    for excluded in ["node_modules", "__pycache__", ".git", "vendor",
                     "venv", ".venv", "dist", "build", "target"]:
        d = tmp_path / excluded
        d.mkdir()
        (d / "something.txt").write_text("content")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    return tmp_path


@pytest.fixture
def project_with_gitignore(tmp_path: Path) -> Path:
    """Proyecto con .gitignore que excluye *.log y build/."""
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    (tmp_path / "app.py").write_text("# app")
    (tmp_path / "debug.log").write_text("log content")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "output.js").write_text("built")
    return tmp_path


@pytest.fixture
def project_with_symlink(tmp_path: Path) -> Path:
    """Proyecto con symlink a directorio externo."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    # Crear un directorio externo y enlazarlo
    external = tmp_path.parent / "external_dir"
    external.mkdir(exist_ok=True)
    (external / "secret.py").write_text("secret")
    link = tmp_path / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks no disponibles en este entorno")
    return tmp_path


def test_default_excludes(project_with_excludes: Path):
    scanner = FileScanner(project_with_excludes)
    tree = scanner.scan_tree()
    for excluded in DEFAULT_EXCLUDES:
        assert excluded not in tree, f"'{excluded}' no deberia aparecer en el arbol"


def test_gitignore_respected(project_with_gitignore: Path):
    scanner = FileScanner(project_with_gitignore)
    tree = scanner.scan_tree()
    assert "debug.log" not in tree
    assert "build" not in tree


def test_no_symlinks(project_with_symlink: Path):
    scanner = FileScanner(project_with_symlink)
    tree = scanner.scan_tree()
    # El symlink 'linked' puede aparecer como entrada pero sus hijos NO deben estar
    if "linked" in tree:
        assert tree["linked"] is None or tree["linked"] == {}


def test_file_is_null(tmp_path: Path):
    (tmp_path / "foo.py").write_text("# foo")
    scanner = FileScanner(tmp_path)
    tree = scanner.scan_tree()
    assert "foo.py" in tree
    assert tree["foo.py"] is None


def test_dir_is_dict(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    scanner = FileScanner(tmp_path)
    tree = scanner.scan_tree()
    assert "src" in tree
    assert isinstance(tree["src"], dict)
    assert "main.py" in tree["src"]


def test_tree_max_depth(tmp_path: Path):
    # Crear estructura 3 niveles de profundidad
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("deep")
    scanner = FileScanner(tmp_path, max_depth=1)
    tree = scanner.scan_tree()
    assert "a" in tree
    # Con max_depth=1, 'a' deberia estar pero sin sus hijos (o vacio)
    assert tree["a"] == {} or (isinstance(tree["a"], dict) and "b" not in tree["a"])


def test_manifest_depth_limited(tmp_path: Path):
    # Manifiestos en profundidad 0 y 1 se detectan
    (tmp_path / "pyproject.toml").write_text('[project]\nname="root"\n')
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "package.json").write_text('{"name": "sub"}')
    # Manifiesto en profundidad 2 NO se detecta
    deep = tmp_path / "sub" / "nested"
    deep.mkdir()
    (deep / "package.json").write_text('{"name": "nested"}')

    scanner = FileScanner(tmp_path)
    manifests = scanner.find_manifests()
    paths = [str(Path(m).relative_to(tmp_path)) for m in manifests]
    assert any("pyproject.toml" in p for p in paths)
    assert any("package.json" in p and "sub" in p and "nested" not in p for p in paths)
    assert not any("nested" in p for p in paths)


def test_empty_dir_is_empty_dict(tmp_path: Path):
    (tmp_path / "empty_dir").mkdir()
    scanner = FileScanner(tmp_path)
    tree = scanner.scan_tree()
    assert "empty_dir" in tree
    assert tree["empty_dir"] == {}


def test_no_cli_flag_filenames_in_tree(tmp_path: Path):
    """Flag-shaped filenames (e.g. "-o", "--format") must never appear in file_tree.

    These are shell redirect / CLI invocation artifacts and are never legitimate
    source files.  Regression guard for the -o artifact bug.
    """
    (tmp_path / "-o").write_text('{"stale": true}')
    (tmp_path / "--format").write_text("stale")
    (tmp_path / "-v").write_text("stale")
    (tmp_path / "app.py").write_text("# real file")
    scanner = FileScanner(tmp_path)
    tree = scanner.scan_tree()
    assert "-o" not in tree
    assert "--format" not in tree
    assert "-v" not in tree
    assert "app.py" in tree


def test_gitignore_relative_path(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    (tmp_path / "app.py").write_text("# app")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "module.pyc").write_text("bytecode")
    (sub / "module.py").write_text("# module")
    scanner = FileScanner(tmp_path)
    tree = scanner.scan_tree()
    assert "src" in tree
    assert "module.pyc" not in tree.get("src", {})
    assert "module.py" in tree.get("src", {})
