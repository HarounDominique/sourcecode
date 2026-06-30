"""Outgoing-integration detection for the C4/BMB export pipeline.

Scans Java source for *outbound* integration points — the edges a C4 Context
diagram needs to draw arrows from this system to external systems. Detection is
deterministic source-text matching (same approach as the JNDI datasource scan in
``serializer.py``); it never executes code and never resolves runtime values.

Covered clients:

  * HTTP   — ``RestTemplate``, ``WebClient``, ``@FeignClient`` (declarative)
  * LDAP   — ``LdapTemplate``
  * JMS    — ``JmsTemplate``, ActiveMQ connection factories

Each hit is reported with a ``file:line`` evidence anchor and, when a literal URL
or logical name is present on the same construct, a ``target``. URLs assembled at
runtime (concatenated strings, property placeholders) yield a ``null`` target —
honest absence rather than a guess.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# A URL/endpoint literal for any scheme we care about.
_URL_RE = re.compile(r'"((?:https?|ldaps?|tcp|amqp|jms|nio)://[^"]*)"')
# First string literal on a line (fallback target, e.g. WebClient.create("x")).
_STR_RE = re.compile(r'"([^"]+)"')

# Declarative HTTP client. Attrs may span multiple lines, so matched on full text.
_FEIGN_RE = re.compile(r"@FeignClient\s*\(([^)]*)\)", re.DOTALL)
_ATTR_URL_RE = re.compile(r'url\s*=\s*"([^"]*)"')
_ATTR_NAME_RE = re.compile(r'(?:name|value)\s*=\s*"([^"]*)"')
_FIRST_LITERAL_RE = re.compile(r'^\s*"([^"]*)"')

# token -> (kind, client). Matched as whole-word usage outside imports/comments.
# Covers Spring AND plain-Java/Jakarta stacks (Quarkus, Micronaut, Keycloak SPI):
# the detector must not be Spring-centric, or a non-Spring repo with real LDAP /
# SMTP / HTTP integrations falsely reports "0 integrations".
_TOKEN_CLIENTS: "tuple[tuple[str, str, str], ...]" = (
    # Spring HTTP clients
    ("RestTemplate", "http", "resttemplate"),
    ("WebClient", "http", "webclient"),
    # Plain-Java / third-party HTTP clients
    ("HttpClient", "http", "jdk-httpclient"),          # java.net.http.HttpClient
    ("CloseableHttpClient", "http", "apache-httpclient"),
    ("HttpClients", "http", "apache-httpclient"),
    ("OkHttpClient", "http", "okhttp"),
    # LDAP / directory (Spring + JNDI)
    ("LdapTemplate", "ldap", "ldaptemplate"),
    ("InitialLdapContext", "ldap", "jndi-ldap"),
    ("InitialDirContext", "ldap", "jndi-ldap"),
    ("LdapContext", "ldap", "jndi-ldap"),
    # Mail / SMTP (JavaMail / Jakarta Mail)
    ("JavaMailSender", "smtp", "spring-mail"),
    ("MimeMessage", "smtp", "javamail"),
    ("Transport", "smtp", "javamail"),
    # JMS / messaging
    ("JmsTemplate", "jms", "jmstemplate"),
    ("ActiveMQConnectionFactory", "jms", "activemq"),
)
_TOKEN_RES = tuple(
    (re.compile(r"\b" + re.escape(tok) + r"\b"), kind, client)
    for tok, kind, client in _TOKEN_CLIENTS
)


def _line_of(text: str, idx: int) -> int:
    """1-based line number of character offset ``idx`` in ``text``."""
    return text.count("\n", 0, idx) + 1


def _extract_target(line: str) -> Optional[str]:
    """Best-effort literal target on a usage line: a scheme URL, else first string."""
    m = _URL_RE.search(line)
    if m:
        return m.group(1)
    m = _STR_RE.search(line)
    if m:
        return m.group(1)
    return None


def detect_integrations(file_paths: "list[str]", root: Path) -> dict:
    """Detect outbound integrations across ``file_paths`` (relative to ``root``).

    Returns ``{"integrations": [...], "by_kind": {kind: count}, "count": N}`` with
    integrations sorted by ``(kind, client, evidence)`` for deterministic output.
    Each integration is ``{kind, client, target, evidence}`` where ``evidence`` is
    ``relpath:line`` and ``target`` is a literal URL/name or ``None``.
    """
    seen: "set[tuple[str, str, Optional[str], str]]" = set()
    records: "list[dict]" = []

    def _add(kind: str, client: str, target: Optional[str], rel: str, line: int) -> None:
        evidence = f"{rel}:{line}"
        key = (kind, client, target, evidence)
        if key in seen:
            return
        seen.add(key)
        records.append({
            "kind": kind,
            "client": client,
            "target": target,
            "evidence": evidence,
        })

    for rel in file_paths:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # @FeignClient — capture attrs even when spread across lines.
        for m in _FEIGN_RE.finditer(text):
            attrs = m.group(1)
            url_m = _ATTR_URL_RE.search(attrs)
            name_m = _ATTR_NAME_RE.search(attrs)
            first_m = _FIRST_LITERAL_RE.search(attrs)
            target = (
                url_m.group(1) if url_m
                else name_m.group(1) if name_m
                else first_m.group(1) if first_m
                else None
            )
            _add("http", "feign", target, rel, _line_of(text, m.start()))

        # Token clients — per line, skipping imports/package/comment noise.
        # First pass records the declaration site and any variable name bound to
        # the client, so a later call site (where the URL literal usually lives)
        # can be attributed back to the client.
        var_to_client: "dict[str, tuple[str, str]]" = {}
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.lstrip()
            if (
                stripped.startswith("import ")
                or stripped.startswith("package ")
                or stripped.startswith("//")
                or stripped.startswith("*")
                or stripped.startswith("/*")
            ):
                continue
            for token_re, kind, client in _TOKEN_RES:
                m = token_re.search(line)
                if not m:
                    continue
                _add(kind, client, _extract_target(line), rel, lineno)
                tok = m.group(0)
                # `Type name` (field/local decl) and `name = new Type(` forms.
                decl = re.search(re.escape(tok) + r"\s+(\w+)\b", line)
                if decl:
                    var_to_client[decl.group(1)] = (kind, client)
                asgn = re.search(r"(\w+)\s*=\s*new\s+" + re.escape(tok), line)
                if asgn:
                    var_to_client[asgn.group(1)] = (kind, client)

        # Second pass: a call on a tracked client variable carrying a URL literal
        # is reported as a hit at the call site (the URL endpoint C4 wants).
        if var_to_client:
            for lineno, line in enumerate(lines, start=1):
                url = _URL_RE.search(line)
                if not url:
                    continue
                for var, (kind, client) in var_to_client.items():
                    if re.search(r"\b" + re.escape(var) + r"\s*\.", line):
                        _add(kind, client, url.group(1), rel, lineno)
                        break

    records.sort(key=lambda r: (r["kind"], r["client"], r["evidence"]))

    by_kind: "dict[str, int]" = {}
    for r in records:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    # BUG #9: honest confidence. A zero count means "no detectable client
    # construct was found", NOT "this system has no integrations" — runtime-wired
    # clients (DI, config-driven endpoints, JCA connectors) are invisible to static
    # text matching. Report that explicitly instead of an authoritative "0".
    confidence = "observed" if records else "not_analyzed"
    return {
        "integrations": records,
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "count": len(records),
        "confidence": confidence,
        "coverage_note": (
            "Detects HTTP (RestTemplate/WebClient/JDK/Apache/OkHttp), LDAP (Spring "
            "+ JNDI), SMTP (JavaMail), and JMS client constructs by source-text "
            "matching. A count of 0 means no such construct was found, not that the "
            "system has no outbound integrations — runtime/DI-wired clients are not "
            "statically visible."
        ),
    }
