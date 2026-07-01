"""Outgoing-integration detection for the C4/BMB export pipeline.

Scans Java source for *outbound* integration points — the edges a C4 Context
diagram needs to draw arrows from this system to external systems. Detection is
deterministic source-text matching (same approach as the JNDI datasource scan in
``serializer.py``); it never executes code and never resolves runtime values.

Covered clients:

  * HTTP   — ``RestTemplate``, ``WebClient``, ``@FeignClient`` (declarative),
             JDK/Apache/OkHttp clients
  * LDAP   — ``LdapTemplate``, JNDI ``InitialLdapContext``/``LdapContext``
  * DNS    — JNDI ``DirContext`` configured with ``DnsContextFactory`` (BUG #2:
             ``DirContext`` is protocol-agnostic and is classified by its
             ``INITIAL_CONTEXT_FACTORY``, not assumed to be LDAP)
  * SMTP   — JavaMail / Jakarta Mail (BUG #1: gated on a mail import so the bare
             word "Transport" in a log string is not a false positive)
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

# BUG #1 (v1.68.0): a JavaMail/Jakarta-Mail import is required before an SMTP token
# (Transport, MimeMessage) is trusted. The bare word "Transport" is also a common
# English noun that appears in log strings ( logger.warn("Transport initialization
# failure") in non-mail code), so a mail-import gate plus a string-literal skip
# stops those false positives.
_MAIL_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:(?:javax|jakarta)\.mail|org\.springframework\.mail)\b",
    re.MULTILINE,
)

# BUG #2 (v1.68.0): javax.naming.directory.{DirContext,InitialDirContext} is NOT
# LDAP-specific — the actual protocol is decided by the value bound to
# Context.INITIAL_CONTEXT_FACTORY. com.sun.jndi.dns.DnsContextFactory means DNS
# (SRV/A record lookups), com.sun.jndi.ldap.LdapCtxFactory means LDAP. Classify by
# the factory class present in the file instead of defaulting to LDAP.
_DNS_FACTORY_RE = re.compile(r"jndi\.dns|DnsContextFactory", re.IGNORECASE)
_LDAP_FACTORY_RE = re.compile(r"jndi\.ldap|LdapCtxFactory", re.IGNORECASE)

# Declarative HTTP client. Attrs may span multiple lines, so matched on full text.
_FEIGN_RE = re.compile(r"@FeignClient\s*\(([^)]*)\)", re.DOTALL)
_ATTR_URL_RE = re.compile(r'url\s*=\s*"([^"]*)"')
_ATTR_NAME_RE = re.compile(r'(?:name|value)\s*=\s*"([^"]*)"')
_FIRST_LITERAL_RE = re.compile(r'^\s*"([^"]*)"')

# BUG #5 (Jenkins field test): thresholds for the structured coverage_confidence
# signal. A repo at/above _LARGE_REPO_FILE_THRESHOLD source files with fewer than
# _LOW_COVERAGE_COUNT recognized integration constructs is flagged "low" coverage —
# the count almost certainly under-represents custom-protocol/SPI integrations.
_LARGE_REPO_FILE_THRESHOLD: int = 300
_LOW_COVERAGE_COUNT: int = 10

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


def _in_string_literal(line: str, idx: int) -> bool:
    """True if char offset ``idx`` falls inside a double-quoted string on ``line``.

    BUG #1 (v1.68.0): a Java type token never legitimately appears inside a string
    literal, so a match there (e.g. the word "Transport" in a log message) is noise,
    not a real client construct. Counts unescaped quotes before ``idx``.
    """
    quote_count = 0
    i = 0
    while i < idx and i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            quote_count += 1
        i += 1
    return quote_count % 2 == 1


def _classify_naming_factory(text: str) -> Optional[str]:
    """Classify a javax.naming.directory usage by its INITIAL_CONTEXT_FACTORY.

    Returns ``"dns"``, ``"ldap"``, or ``None`` (factory not statically resolvable).
    See BUG #2 — DirContext alone does not imply LDAP.
    """
    if _DNS_FACTORY_RE.search(text):
        return "dns"
    if _LDAP_FACTORY_RE.search(text):
        return "ldap"
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

    def _add(
        kind: str,
        client: str,
        target: Optional[str],
        rel: str,
        line: int,
        confidence: Optional[str] = None,
    ) -> None:
        evidence = f"{rel}:{line}"
        key = (kind, client, target, evidence)
        if key in seen:
            return
        seen.add(key)
        rec = {
            "kind": kind,
            "client": client,
            "target": target,
            "evidence": evidence,
        }
        # Per-record confidence is emitted only when the classification is uncertain
        # (e.g. a JNDI DirContext whose factory could not be resolved). Confident hits
        # stay schema-clean without the field.
        if confidence is not None:
            rec["confidence"] = confidence
        records.append(rec)

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

        # Per-file context used to disambiguate / gate uncertain tokens (BUG #1/#2).
        has_mail_import = bool(_MAIL_IMPORT_RE.search(text))
        naming_factory = _classify_naming_factory(text)

        # BUG #3 (v1.70.0): "HttpClient" is a simple name that collides with
        # user-defined classes (e.g. org.openmrs.util.HttpClient, a thin wrapper over
        # java.net.HttpURLConnection — a completely different API from the JDK 11+
        # java.net.http.HttpClient). Resolve the JDK client by its FULLY-QUALIFIED
        # import, never by the bare class name. When the file imports/declares a
        # different HttpClient (or none can be resolved), degrade to a low-confidence
        # "custom-http-wrapper" rather than asserting a JDK client that isn't there.
        import_fqns = set(
            re.findall(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", text, re.MULTILINE)
        )
        http_jdk_imported = (
            "java.net.http.HttpClient" in import_fqns or "java.net.http.*" in import_fqns
        )
        declares_own_httpclient = bool(
            re.search(r"\b(?:class|interface|enum)\s+HttpClient\b", text)
        )
        http_other_import = any(
            fqn.endswith(".HttpClient") and not fqn.startswith("java.net.http.")
            for fqn in import_fqns
        )

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
                # BUG #1: a token matched inside a string literal (log message, doc
                # comment text) is never a real client construct — skip it.
                if _in_string_literal(line, m.start()):
                    continue
                confidence: Optional[str] = None
                # BUG #1: SMTP tokens require a JavaMail/Jakarta-Mail import to be
                # trusted — "Transport" / "MimeMessage" are too generic otherwise.
                if kind == "smtp" and not has_mail_import:
                    continue
                # BUG #2: javax.naming.directory.{Dir,Initial}Context is protocol-
                # agnostic. Reclassify by the configured context factory; default to
                # an explicit low-confidence "unknown" rather than assuming LDAP.
                if client == "jndi-ldap" and "Dir" in token_re.pattern:
                    if naming_factory == "dns":
                        kind, client = "dns", "jndi-dns"
                    elif naming_factory == "ldap":
                        kind, client = "ldap", "jndi-ldap"
                    else:
                        kind, client, confidence = (
                            "naming-directory-unknown", "jndi-dircontext", "low",
                        )
                # BUG #3: resolve the ambiguous bare "HttpClient" by its import, and
                # suppress pure type-declaration sites (field / parameter / return
                # type) — only a construction or static call is a real network site.
                if client == "jdk-httpclient":
                    if not (http_jdk_imported and not declares_own_httpclient):
                        # Not the JDK client (own class, third-party, or unresolvable).
                        client, confidence = "custom-http-wrapper", "low"
                        if declares_own_httpclient or http_other_import:
                            confidence = "low"
                    is_construction = bool(
                        re.search(r"\bnew\s+HttpClient\b", line)
                    ) or bool(re.search(r"\bHttpClient\s*\.", line))
                    if not is_construction:
                        # Type-declaration only (e.g. `HttpClient field;`,
                        # `void setX(HttpClient c)`): track the var for the URL
                        # second pass but do NOT emit it as an invocation site.
                        tok = m.group(0)
                        decl = re.search(re.escape(tok) + r"\s+(\w+)\b", line)
                        if decl:
                            var_to_client[decl.group(1)] = (kind, client)
                        continue
                _add(kind, client, _extract_target(line), rel, lineno, confidence)
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

    # BUG #5 (Jenkins field test): the coverage caveat lived only inside the prose
    # `coverage_note`. Propagate it as a STRUCTURED signal so an automated consumer
    # does not read a low `count` as "low external coupling". A large repo with few
    # recognized constructs (Jenkins: custom remoting/SCM/Update-Center SPIs, not
    # RestTemplate/WebClient) is under-counted, not loosely coupled.
    _repo_size = len(file_paths)
    _large_repo = _repo_size >= _LARGE_REPO_FILE_THRESHOLD
    if not records:
        coverage_confidence = "low"
        _cov_reason = (
            "no recognized client-library construct found; custom protocol/SPI-based "
            "or DI-wired integrations are not statically detectable by this analyzer. "
            "A count of 0 does not imply the system has no outbound integrations."
        )
    elif _large_repo and len(records) < _LOW_COVERAGE_COUNT:
        coverage_confidence = "low"
        _cov_reason = (
            f"only {len(records)} recognized construct(s) across {_repo_size} source "
            "files; count reflects only recognized client-library patterns "
            "(RestTemplate/WebClient/JDK/Apache/OkHttp/LDAP/JMS/…). Custom protocol/"
            "SPI-based integrations are likely under-counted — do not read a low count "
            "as low external coupling."
        )
    elif len(records) < _LOW_COVERAGE_COUNT:
        coverage_confidence = "partial"
        _cov_reason = (
            "recognized client-library constructs detected; custom protocol/SPI-based "
            "or DI-wired integrations, if any, are not statically visible."
        )
    else:
        coverage_confidence = "high"
        _cov_reason = (
            "recognized client-library constructs cover the observable integration "
            "surface; runtime/DI-wired clients remain out of static scope."
        )

    return {
        "integrations": records,
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "count": len(records),
        "confidence": confidence,
        "coverage_confidence": coverage_confidence,
        "coverage_confidence_reason": _cov_reason,
        "coverage_note": (
            "Detects HTTP (RestTemplate/WebClient/JDK/Apache/OkHttp), LDAP (Spring "
            "+ JNDI), DNS (JNDI DirContext w/ DnsContextFactory), SMTP (JavaMail, "
            "import-gated), and JMS client constructs by source-text matching. JNDI "
            "DirContext usage is classified by its INITIAL_CONTEXT_FACTORY (dns vs "
            "ldap); when the factory is not statically resolvable the kind is "
            "'naming-directory-unknown' with confidence='low', never assumed LDAP. A "
            "count of 0 means no such construct was found, not that the system has no "
            "outbound integrations — runtime/DI-wired clients are not statically "
            "visible."
        ),
    }
