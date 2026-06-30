#!/usr/bin/env python3
"""Generate the JDK unconditional-export allowlist used by migrate-check MIG-011.

MIG-011 flags `sun.*` / `com.sun.*` imports as strongly-encapsulated JDK-internal
APIs. That heuristic is *prefix-based* and produces false positives on packages
that merely share the prefix but are in fact exported **unconditionally** by a
JDK module (no `to` clause) and therefore require NO `--add-exports` /
`--add-opens` on any classpath or module-path scenario. The canonical example is
`com.sun.net.httpserver.*` (module `jdk.httpserver`) — public and documented
since Java 6, the basis of JEP 408.

This script derives the allowlist directly from the running JDK by parsing
`java --describe-module` for every module, so the data tracks the real platform
instead of a hand-maintained list that silently rots across releases.

Two filters are applied:

  1. Only `exports <pkg>` lines WITHOUT a `to <module>` qualifier are kept
     (qualified exports still need `--add-exports` for arbitrary code).

  2. Modules whose whole purpose is internal / tooling / deprecated access are
     DENYLISTED — their `sun.*` / `com.sun.*` exports are genuine migration
     concerns and must keep MIG-011's `high` severity:
        - jdk.unsupported  → sun.misc.Unsafe, sun.reflect (deprecated for removal)
        - jdk.compiler     → com.sun.source.*, com.sun.tools.javac (compiler internals)
        - jdk.attach       → com.sun.tools.attach.* (tooling)
        - jdk.jdi          → com.sun.jdi.* (debugger internals)
        - jdk.jconsole / jdk.jdeps / jdk.javadoc / jdk.jshell → com.sun.tools.*

Run with the OLDEST supported target JDK to stay conservative (a package
exported in JDK 21 but not JDK 17 must not be allowlisted for a JDK 17 target).
Re-run after a JDK bump and commit the regenerated module.

Usage:
    python scripts/generate_jdk_exports.py > src/sourcecode/jdk_exports.py
"""
from __future__ import annotations

import re
import subprocess
import sys

# Modules whose sun.*/com.sun.* exports are genuinely internal/tooling/deprecated.
# Their packages must NOT be allowlisted — MIG-011 keeps firing `high` for them.
_DENYLIST_MODULES: frozenset[str] = frozenset(
    {
        "jdk.unsupported",
        "jdk.compiler",
        "jdk.attach",
        "jdk.jdi",
        "jdk.jconsole",
        "jdk.jdeps",
        "jdk.javadoc",
        "jdk.jshell",
        "jdk.internal.ed",
        "jdk.internal.le",
        "jdk.internal.opt",
        "jdk.internal.vm.ci",
        "jdk.internal.vm.compiler",
    }
)

_EXPORT_RE = re.compile(r"^exports\s+(\S+?)(?:\s+to\s+.*)?$")


def _list_modules() -> list[str]:
    out = subprocess.run(
        ["java", "--list-modules"], capture_output=True, text=True, check=True
    ).stdout
    return [line.split("@", 1)[0].strip() for line in out.splitlines() if line.strip()]


def _module_exports(module: str) -> list[tuple[str, bool]]:
    """Return (package, is_unconditional) for each `exports` line of a module."""
    out = subprocess.run(
        ["java", "--describe-module", module],
        capture_output=True,
        text=True,
    ).stdout
    results: list[tuple[str, bool]] = []
    for line in out.splitlines():
        line = line.strip()
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        pkg = m.group(1)
        unconditional = " to " not in line
        results.append((pkg, unconditional))
    return results


def main() -> int:
    java_version = subprocess.run(
        ["java", "-version"], capture_output=True, text=True
    ).stderr.splitlines()[0]

    allowlist: set[str] = set()
    for module in _list_modules():
        if module in _DENYLIST_MODULES:
            continue
        for pkg, unconditional in _module_exports(module):
            if not unconditional:
                continue
            if pkg.startswith("sun.") or pkg.startswith("com.sun."):
                allowlist.add(pkg)

    packages = sorted(allowlist)

    print('"""GENERATED FILE — do not edit by hand.')
    print()
    print("Allowlist of sun.* / com.sun.* packages exported UNCONDITIONALLY by the")
    print("JDK (no `--add-exports` / `--add-opens` required on classpath or module")
    print("path). Consumed by migrate-check MIG-011 to suppress false positives.")
    print()
    print(f"Regenerate with:  python scripts/generate_jdk_exports.py > "
          "src/sourcecode/jdk_exports.py")
    print(f"Generated from:   {java_version}")
    print('"""')
    print("from __future__ import annotations")
    print()
    print()
    print("JDK_UNCONDITIONAL_EXPORTS: frozenset[str] = frozenset(")
    print("    {")
    for pkg in packages:
        print(f'        "{pkg}",')
    print("    }")
    print(")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
