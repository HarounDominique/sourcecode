"""Shared path classification helpers used across all tools.

Centralises test-path and vendor-path detection so each tool does not
duplicate — and diverge — these heuristics.
"""
from __future__ import annotations

_TEST_SEGMENTS = frozenset({
    "test", "tests", "spec", "specs",
    "test-helpers", "test_helpers", "testfixtures",
    "it",          # integration-tests short name
    "integrationtest", "integrationtests",
})

# Whole-module directories that are test harness / fixtures even though their
# code lives under src/main (e.g. a Maven module that ships a test framework).
# A finding under one of these modules is test infrastructure, not the product.
_TEST_MODULE_SEGMENTS = frozenset({
    "testsuite", "test-framework", "testframework",
    "integration-arquillian", "arquillian",
    "test-utils", "test-util", "testutils",
    "test-support", "testsupport",
})

_VENDOR_SEGMENTS = frozenset({
    "vendor", "vendors",
    "third_party", "thirdparty",
    "node_modules",
    "external", "externals",
    "contrib",
})

# lib/libs are vendor only for web-asset extensions.
# Java/Kotlin/Python source in a package named "lib" is NOT vendor.
_LIB_SEGMENTS = frozenset({"lib", "libs"})
_WEB_ASSET_EXTS = frozenset({
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".css", ".less", ".scss", ".sass",
    ".json", ".map",
})

_VENDOR_PATH_FRAGMENTS = (
    "/vendor/", "/vendors/",
    "/third_party/", "/thirdparty/",
    "/node_modules/",
    "/external/", "/externals/",
    "/contrib/",
)

_JAVA_TEST_ROOTS = (
    "/src/test/",
    "\\src\\test\\",
)


def is_test_path(path: str) -> bool:
    """Return True when *path* is part of a test tree, not production code.

    Handles:
      - Standard Maven/Gradle layout  (src/test/java/…)
      - Common naming conventions     (/tests/, /spec/, /it/)
      - Java file name conventions    (FooTest.java, TestFoo.java)
      - Python conventions            (test_foo.py, foo_test.py)
      - JS/TS conventions             (foo.test.ts, foo.spec.ts)
    """
    norm = path.replace("\\", "/").lower()

    # Maven/Gradle standard test root (fast path)
    if "/src/test/" in norm:
        return True

    # Segment-based check – any directory component is a test segment
    parts = norm.split("/")
    for part in parts[:-1]:  # skip filename itself
        bare = part.rstrip("/")
        if bare in _TEST_SEGMENTS:
            return True

    # File-name conventions
    name = parts[-1]
    if (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.ts")
        or name.endswith(".test.js")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.js")
        or (name.endswith("test.java") and name != "test.java")
        or name.endswith("tests.java")
        or (name.startswith("test") and name.endswith(".java") and len(name) > 9
            and "/src/main/" not in norm)
    ):
        return True

    return False


def is_test_or_fixture_path(path: str) -> bool:
    """Return True when *path* is test code OR test-harness/fixture infrastructure.

    Broader than is_test_path: also catches whole modules that are test frameworks
    or integration-test harnesses (testsuite/, test-framework/, integration-arquillian/,
    *-test*, *-it modules) even when their sources sit under src/main. Migration
    tooling uses this to keep test fixtures (deprecated web.xml deployment
    descriptors, test-only sun.* HTTP servers) out of the product's blocking count.
    """
    if is_test_path(path):
        return True
    norm = path.replace("\\", "/").lower()
    parts = norm.split("/")
    for part in parts[:-1]:  # skip the filename
        bare = part.rstrip("/")
        if bare in _TEST_MODULE_SEGMENTS:
            return True
        # module dirs like "adapter-test", "foo-tests", "bar-itests"
        if bare.endswith(("-test", "-tests", "-it", "-itest", "-itests")):
            return True
    return False


def is_vendor_path(path: str) -> bool:
    """Return True when *path* is inside a vendored / third-party directory.

    Handles:
      - /vendor/, /vendors/, /third_party/, /node_modules/
      - /lib/, /libs/ containing web assets (NOT JVM/Python source — those may
        legitimately use "lib" as a package name)
      - Minified JS/CSS files anywhere (*.min.js, *.min.css)
    """
    norm = path.replace("\\", "/").lower()

    # Minified files are always vendor regardless of directory
    if norm.endswith(".min.js") or norm.endswith(".min.css"):
        return True

    # Fast fragment check for unambiguous vendor directories
    for frag in _VENDOR_PATH_FRAGMENTS:
        if frag in norm:
            return True

    parts = norm.split("/")
    dir_parts = parts[:-1]  # exclude filename

    # Unambiguous vendor directory names
    for part in dir_parts:
        if part in _VENDOR_SEGMENTS:
            return True

    # lib/libs: vendor only for web-asset file types, not JVM/Python source
    filename = parts[-1]
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext in _WEB_ASSET_EXTS:
        for part in dir_parts:
            if part in _LIB_SEGMENTS:
                return True

    return False
