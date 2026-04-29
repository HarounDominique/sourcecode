# Security Policy

## Overview

`sourcecode` analyzes local software repositories and produces structured context. Because it reads source code, configuration files, and potentially sensitive metadata, security is taken seriously.

---

## What sourcecode does with your code

**`sourcecode` does not transmit your code anywhere.**

- Analysis runs entirely locally on your machine
- No source code, file contents, or analysis output leaves your machine (unless you explicitly pipe it somewhere)
- No API keys or authentication required
- Secrets found in source files are redacted by default in output (disable with `--no-redact`)
- Files named `.env`, `*.secret`, and similar patterns are excluded from analysis

---

## Supported versions

We support the latest stable release. Security fixes are backported to the previous minor version when feasible.

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |
| N-1     | Best effort |
| Older   | No        |

---

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Report security issues by emailing: **security@sourcecode.dev**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Your contact information (optional, for follow-up)

**What to expect:**
- Acknowledgement within 48 hours
- Assessment within 7 days
- Fix timeline communicated within 14 days
- Credit in release notes (unless you prefer anonymity)

---

## Threat model

`sourcecode` is a local CLI tool. The primary threat vectors are:

### Secret exposure in output
Secrets appearing in source code (API keys, tokens, credentials) could appear in the structured output.

**Mitigation:** Redaction is enabled by default. Common secret patterns (API keys, tokens, passwords) are replaced with `[REDACTED]`. Use `--no-redact` only if you understand the implications.

### Malicious repository content
A malicious repository could contain content designed to exploit the analysis pipeline (e.g., crafted file names, unusual encodings, deeply nested structures).

**Mitigations:**
- File scanning has configurable depth limits (`--depth`, default 4)
- Parsing is done with safe, non-executing parsers
- No `eval()` or dynamic code execution
- External process execution is limited to `git` commands only (read-only)

### Path traversal
Symlinks or unusual paths could potentially escape the target directory.

**Mitigation:** Analysis is scoped to the provided path. Symlinks are not followed.

### Dependency vulnerabilities
Third-party dependencies (`typer`, `ruamel.yaml`, etc.) could contain vulnerabilities.

**Mitigation:** Dependencies are kept minimal and up to date. Run `pip audit` to check your environment.

---

## Security best practices for users

- Run `sourcecode` on repositories you own or have permission to analyze
- If sharing output with external AI services, be aware of what data is included
- Use `--no-redact` only in controlled environments
- Review output before injecting into shared AI sessions

---

## Telemetry and privacy

Telemetry is **opt-in only** and disabled by default. If enabled, only anonymous usage metadata is collected — never code, paths, or content. See [docs/privacy.md](docs/privacy.md).
