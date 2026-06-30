"""GENERATED FILE — do not edit by hand.

Allowlist of sun.* / com.sun.* packages exported UNCONDITIONALLY by the
JDK (no `--add-exports` / `--add-opens` required on classpath or module
path). Consumed by migrate-check MIG-011 to suppress false positives.

Regenerate with:  python scripts/generate_jdk_exports.py > src/sourcecode/jdk_exports.py
Generated from:   java version "21.0.10" 2026-01-20 LTS
"""
from __future__ import annotations


JDK_UNCONDITIONAL_EXPORTS: frozenset[str] = frozenset(
    {
        "com.sun.java.accessibility.util",
        "com.sun.management",
        "com.sun.net.httpserver",
        "com.sun.net.httpserver.spi",
        "com.sun.nio.sctp",
        "com.sun.security.auth",
        "com.sun.security.auth.callback",
        "com.sun.security.auth.login",
        "com.sun.security.auth.module",
        "com.sun.security.jgss",
    }
)
