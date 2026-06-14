"""License activation and enforcement for the sourcecode CLI.

Tier model (hybrid size + collaboration gating — NOT capability gating):
  * Free covers EVERY command on small / mid-size repos, single-repo, local.
  * Pro unlocks enterprise-scale monoliths (repos above the size limit) and
    automation (CI/CD-style repeated delta runs).
  * Enterprise (sold separately) adds multi-repo, hosted server, dashboards, SSO.

  The engine and all commands are identical across tiers. We gate on WHERE and
  HOW MUCH the tool is used (repo size, automation, team), never on WHICH
  command is available. Small repos get the full feature set for free.

Flow:
  1. Module imported → _init() loads ~/.sourcecode/license.json (if present)
  2. is_pro set globally (True when plan == "pro")
  3. Heavy commands call require_repo_or_pro(repo_path, feature) at entry —
     free below the size limit, gated to Pro above it (exit 2). Pure-automation
     features (delta) keep a free quota then gate. require_feature() is the
     low-level hard gate still used where size is irrelevant.
  4. `sourcecode activate <key>` calls activate_license(key) — validates via
     Edge Function, writes ~/.sourcecode/license.json, exits 0 on success
  5. Cached license is re-validated every 24 h (online); network errors keep
     cached state (offline-first). Server-side invalidity clears cache.

Supabase credentials (baked in; override via env vars for testing):
  SOURCECODE_SUPABASE_URL      — project Edge Function base URL
  SOURCECODE_SUPABASE_ANON_KEY — public anon key (not a secret)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Supabase endpoint config — hardcoded for production; override via env for dev
# ---------------------------------------------------------------------------
_DEFAULT_SUPABASE_URL: str = "https://qkndlmyekvujjdgthtmz.supabase.co"
# Public anon/publishable key — safe to ship in client code. RLS plus the
# service-role key (server-only, in the Edge Function secrets) protect the data.
# Paste your project's anon key here so `sourcecode activate` works out of the
# box; env var still overrides for testing against another project.
_DEFAULT_SUPABASE_ANON_KEY: str = "sb_publishable_qiJFLWjbBbTqjg-fb0mAGA_cl8PBOKH"
_SUPABASE_URL: str = os.environ.get("SOURCECODE_SUPABASE_URL", _DEFAULT_SUPABASE_URL)
_SUPABASE_ANON_KEY: str = os.environ.get(
    "SOURCECODE_SUPABASE_ANON_KEY",
    _DEFAULT_SUPABASE_ANON_KEY,
)
if _SUPABASE_URL != _DEFAULT_SUPABASE_URL:
    sys.stderr.write(
        f"[sourcecode] WARNING: SOURCECODE_SUPABASE_URL overridden to {_SUPABASE_URL!r}."
        " License requests will be sent to this server.\n"
    )
    sys.stderr.flush()

_LICENSE_DIR: Path = Path.home() / ".sourcecode"
_LICENSE_FILE: Path = _LICENSE_DIR / "license.json"
_DELTA_RUNS_FILE: Path = _LICENSE_DIR / "delta_runs.json"
_CACHE_TTL_SECONDS: int = 1800  # 30 minutes default; CI env overrides to 24h (see _get_cache_ttl)
_CACHE_TTL_CI_SECONDS: int = 86400  # 24 hours — CI containers must not re-validate mid-run


def _get_cache_ttl() -> int:
    """Return TTL in seconds. CI containers get 24h to avoid mid-run network calls."""
    return _CACHE_TTL_CI_SECONDS if os.environ.get("SOURCECODE_CI") else _CACHE_TTL_SECONDS
_DELTA_FREE_LIMIT: int = 30
# Hybrid model size limit: repos at/under this many Java source files are fully
# free (every command, no caps). Above it = enterprise-scale monolith = Pro.
_FREE_REPO_JAVA_FILE_LIMIT: int = 500
_LICENSE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")

# ---------------------------------------------------------------------------
# Per-feature descriptions for upgrade UX
# ---------------------------------------------------------------------------
_FEATURE_INFO: dict[str, dict[str, str]] = {
    "impact": {
        "display": "impact",
        "description": (
            "Shows blast radius, callers, affected endpoints, and persistence paths in one call."
        ),
        "value": "Answers: what breaks if I touch this? The core risk signal before any change.",
    },
    "modernize": {
        "display": "modernize (full)",
        "description": (
            "Full analysis: dead zones, refactor candidates, dependency tangles, and coupling ranked by git churn."
        ),
        "value": "Prioritizes where to refactor and what is safe to touch.",
    },
    "fix-bug": {
        "display": "fix-bug (full)",
        "description": "Complete risk-ranked file list with all annotation and structural signals.",
        "value": "More results means less time scanning the codebase manually.",
    },
    "review-pr": {
        "display": "review-pr (expanded)",
        "description": "Full PR review: blast radius, all execution paths, security and transaction impact.",
        "value": "CI-grade review — the complete picture before merging.",
    },
    "delta": {
        "display": "prepare-context delta",
        "description": "Incremental context: git-changed files with impact propagation.",
        "value": "Designed for CI/CD pipelines — runs on every PR, flags risk automatically.",
    },
    "generate-tests": {
        "display": "prepare-context generate-tests",
        "description": "Test gap analysis: finds untested files with coverage recommendations.",
        "value": "Reduces test debt systematically across the entire codebase.",
    },
    "--full": {
        "display": "--full flag (large repos)",
        "description": (
            "Removes truncation limits on transactional boundaries, DTO mappers, and large result sets."
            " Free tier may use --full on repositories under 500 Java source files."
        ),
        "value": "Essential for complete analysis of enterprise-scale codebases.",
    },
    "git-history": {
        "display": "git history analysis",
        "description": (
            "Churn ranking, commit frequency per file, volatility signals over 90-day window."
        ),
        "value": "Identifies which files change most — the highest-risk targets in any refactor.",
    },
    "multi-repo": {
        "display": "multi-repo analysis",
        "description": (
            "Cross-repository dependency graphs, shared module impact, and org-level blast radius."
        ),
        "value": "Required for microservices and monorepo architectures.",
    },
    "export-rich": {
        "display": "rich exports (HTML/PDF/CI)",
        "description": "Structured HTML reports, PDF exports, and CI-consumable risk summaries.",
        "value": "Embed analysis into your CI pipeline or share with non-CLI stakeholders.",
    },
    "team-snapshots": {
        "display": "team snapshot sharing",
        "description": "Shared org-level snapshots and multi-user cache access.",
        "value": "Eliminates cold-cache overhead across the entire engineering team.",
    },
}

# ---------------------------------------------------------------------------
# Global license state — loaded once at import time
# ---------------------------------------------------------------------------
_license_data: Optional[dict] = None
is_pro: bool = False


def _write_license_file(data: dict) -> None:
    """Atomically write license data via tmp file + rename."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    tmp = _LICENSE_FILE.with_suffix(".tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(_LICENSE_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _read_delta_runs() -> dict:
    try:
        if _DELTA_RUNS_FILE.exists():
            return json.loads(_DELTA_RUNS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def check_delta_free_tier(repo_path: str) -> "tuple[bool, int, int]":
    """Check and consume one delta free-tier run for repo_path.

    Returns (allowed, runs_used, runs_remaining).
    When allowed=True the run count is incremented atomically.
    When allowed=False the quota is exhausted — caller should gate to Pro.
    """
    import hashlib
    key = hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:16]
    runs = _read_delta_runs()
    used = int(runs.get(key, 0))
    if used >= _DELTA_FREE_LIMIT:
        return False, used, 0
    new_used = used + 1
    runs[key] = new_used
    try:
        _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _DELTA_RUNS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_DELTA_RUNS_FILE)
    except Exception:
        pass
    return True, new_used, max(0, _DELTA_FREE_LIMIT - new_used)


def _load_license_file() -> Optional[dict]:
    """Read ~/.sourcecode/license.json. Returns parsed dict or None."""
    try:
        if _LICENSE_FILE.exists():
            raw = _LICENSE_FILE.read_text(encoding="utf-8")
            return json.loads(raw)
    except Exception:
        pass
    return None


def _call_get_license(license_key: str) -> Optional[dict]:
    """POST to /get-license edge function. Returns parsed dict or None on network error."""
    import urllib.error
    import urllib.request

    if not _SUPABASE_ANON_KEY:
        return None

    url = f"{_SUPABASE_URL}/functions/v1/get-license"
    body = json.dumps({"license_key": license_key}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("apikey", _SUPABASE_ANON_KEY)
    req.add_header("Authorization", f"Bearer {_SUPABASE_ANON_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            return {"valid": False, "error": f"HTTP {exc.code}"}
    except Exception:
        return None  # Network error — caller decides what to do


def _maybe_revalidate() -> None:
    """Re-validate cached license if stale. Mutates globals; never raises."""
    global _license_data, is_pro

    if not _license_data:
        return

    validated_at_str = (
        _license_data.get("validated_at")
        or _license_data.get("activated_at")
        or _license_data.get("authenticated_at")
    )
    if validated_at_str:
        try:
            validated_at = datetime.fromisoformat(validated_at_str)
            if validated_at.tzinfo is None:
                validated_at = validated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - validated_at).total_seconds()
            if age < _get_cache_ttl():
                return
        except Exception:
            pass

    # Key-based auth
    key = _license_data.get("license_key")
    if not key:
        return

    result = _call_get_license(key)
    if result is None:
        return  # Network error — keep cached data (offline-first)

    if not result.get("valid"):
        _license_data = None
        is_pro = False
        try:
            if _LICENSE_FILE.exists():
                _LICENSE_FILE.unlink()
        except Exception:
            pass
        return

    _license_data["plan"] = result.get("plan", "pro")
    _license_data["features"] = result.get("features", [])
    _license_data["validated_at"] = datetime.now(timezone.utc).isoformat()
    is_pro = _license_data.get("plan") == "pro"
    try:
        _write_license_file(_license_data)
    except Exception:
        pass


def _init() -> None:
    global _license_data, is_pro
    _license_data = _load_license_file()
    is_pro = (
        _license_data is not None
        and _license_data.get("plan") == "pro"
        and _license_data.get("status", "active") != "inactive"
    )


_init()


# ---------------------------------------------------------------------------
# Entitlement helpers
# ---------------------------------------------------------------------------

def _emit_telemetry(event: str, **kw: object) -> None:
    """Best-effort telemetry emit. Respects opt-in; never raises or blocks."""
    try:
        from sourcecode import telemetry as _tel
        _tel.record(event, **kw)  # type: ignore[arg-type]
    except Exception:
        pass


def can_use(feature_name: str) -> bool:
    """Return True if the current plan has access to feature_name.

    Does not trigger revalidation — use require_feature() at command entry
    points where you want revalidation + gating in one call.
    """
    return is_pro


def _emit_upgrade_and_exit(headline: str, body_lines: list[str], payload: dict) -> None:
    """Write human-readable prompt to stderr + JSON error to stdout, then exit 2.

    Shared by require_feature() and require_repo_or_pro() so terminal UX and the
    machine-readable contract stay identical regardless of which gate fired.
    """
    lines = [f"\n  {headline}"]
    for body in body_lines:
        if body:
            lines.append(f"  {body}")
    lines.append("")
    lines.append("  Upgrade:  sourcecode activate <license_key>")
    lines.append("")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()

    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(2)  # exit 2 = Pro required (0=ok, 1=runtime error, 2=license required)


def count_java_files(repo_path: str, ceiling: Optional[int] = None) -> int:
    """Count *.java files under repo_path, skipping .git.

    When ceiling is given, counting stops at ceiling+1 (bounded — cheap on huge
    monorepos where we only need to know "is this over the limit?").
    """
    from itertools import islice
    try:
        root = Path(repo_path).resolve()
        gen = (p for p in root.rglob("*.java") if ".git" not in p.parts)
        if ceiling is not None:
            return sum(1 for _ in islice(gen, ceiling + 1))
        return sum(1 for _ in gen)
    except Exception:
        return 0


def is_large_repo(repo_path: str) -> bool:
    """True if repo exceeds the free-tier size limit (enterprise-scale monolith).

    Sizing counts ONLY Java source files — by design. sourcecode monetises
    enterprise Java monoliths; non-Java repos (Python/Go/TS/…) never gate to Pro
    and are intentionally free at any size. Do not "fix" this to count other
    languages without a product decision: it would start charging users we've
    chosen to leave on Free.
    """
    return count_java_files(repo_path, ceiling=_FREE_REPO_JAVA_FILE_LIMIT) > _FREE_REPO_JAVA_FILE_LIMIT


def require_feature(
    feature_name: str,
    extra_fields: Optional[dict] = None,
) -> None:
    """Hard gate: exit with a clean upgrade prompt unless Pro.

    Used where repo size is irrelevant (e.g. delta automation quota exhausted).
    Most heavy commands should use require_repo_or_pro() instead, which keeps
    small repos free. Re-validates stale cached license before gating.

    Writes human-readable context to stderr (terminal UX) and a JSON error
    to stdout (backward-compatible machine-readable format).

    Args:
        extra_fields: Optional extra keys merged into the JSON error payload
                      (e.g. ``{"free_tier_alternative": "..."}``)
    """
    _maybe_revalidate()

    if is_pro:
        return

    info = _FEATURE_INFO.get(feature_name, {})
    display = info.get("display", feature_name)
    payload: dict = {
        "error": "pro_required",
        "feature": feature_name,
        "message": (
            f"'{display}' requires a Pro license. "
            "Run: sourcecode activate <license_key>"
        ),
        "upgrade_hint": "sourcecode activate <license_key>",
    }
    if extra_fields:
        payload.update(extra_fields)
    _emit_telemetry("gate_blocked", feature=feature_name, success=False)
    _emit_upgrade_and_exit(
        f"'{display}' is a Pro feature.",
        [info.get("description", ""), info.get("value", "")],
        payload,
    )


def require_repo_or_pro(
    repo_path: str,
    feature_name: str,
    extra_fields: Optional[dict] = None,
) -> None:
    """Hybrid size gate: free on small/mid repos, Pro on enterprise monoliths.

    The core of the hybrid model. A free user gets the FULL feature on any repo
    at or under the size limit. Only when the repo exceeds the limit (an
    enterprise-scale monolith — exactly who Pro is for) does this gate to Pro.

    No-op when already Pro or when the repo is within the free size limit.
    Exits 2 with size-framed messaging otherwise.
    """
    _maybe_revalidate()

    if is_pro:
        return
    if not is_large_repo(repo_path):
        return  # small/mid repo → fully free

    info = _FEATURE_INFO.get(feature_name, {})
    display = info.get("display", feature_name)
    headline = f"This repository exceeds the free-tier size limit ({_FREE_REPO_JAVA_FILE_LIMIT}+ Java files)."
    body = [
        f"'{display}' is free on repos up to {_FREE_REPO_JAVA_FILE_LIMIT} Java source files.",
        "Pro unlocks analysis of enterprise-scale monoliths.",
    ]
    payload: dict = {
        "error": "pro_required",
        "reason": "repo_too_large",
        "feature": feature_name,
        "free_repo_java_file_limit": _FREE_REPO_JAVA_FILE_LIMIT,
        "message": (
            f"This repository exceeds the free-tier limit of "
            f"{_FREE_REPO_JAVA_FILE_LIMIT} Java source files. "
            "Pro unlocks enterprise-scale monoliths. "
            "Run: sourcecode activate <license_key>"
        ),
        "upgrade_hint": "sourcecode activate <license_key>",
    }
    if extra_fields:
        payload.update(extra_fields)
    _emit_telemetry("gate_blocked", feature=feature_name, repo_size="large", success=False)
    _emit_upgrade_and_exit(headline, body, payload)


def require_pro(feature_name: str) -> None:
    """Backward-compatible alias for require_feature.

    Example:
        from sourcecode.license import require_pro
        require_pro("impact")
    """
    require_feature(feature_name)


# ---------------------------------------------------------------------------
# Activation (key-based — direct key entry)
# ---------------------------------------------------------------------------

def activate_license(license_key: str) -> None:
    """Validate license_key via Edge Function, write ~/.sourcecode/license.json.

    Outputs JSON to stdout; exits 0 on success, 1 on any failure.
    Never raises — all error paths emit JSON and call sys.exit(1).
    """
    if not _LICENSE_KEY_RE.match(license_key):
        _fail("invalid_license", "License key format is invalid.")

    if not _SUPABASE_ANON_KEY:
        _fail("configuration_error", "SOURCECODE_SUPABASE_ANON_KEY not set. Contact support.")

    result = _call_get_license(license_key)

    if result is None:
        _fail("network_error", "Could not reach license server. Check your internet connection.")

    if not result.get("valid"):
        _emit_telemetry("activation", feature="key", success=False, error_kind="InvalidLicense")
        _fail("invalid_license", result.get("error", "License key is not valid or subscription is inactive."))

    if result.get("plan") != "pro":
        _emit_telemetry("activation", feature="key", success=False, error_kind="NotPro")
        _fail("not_pro", "This license is not a Pro license.")

    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "license_key": license_key,
        "plan": result["plan"],
        "features": result.get("features", []),
        "email": result.get("email", ""),
        "activated_at": now,
        "validated_at": now,
    }
    _write_license_file(data)
    _emit_telemetry("activation", feature="key", success=True)

    output = {"status": "activated", "plan": "pro", "features": data["features"]}
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _fail(error: str, message: str) -> None:
    """Emit JSON error to stdout and exit 1. Never returns."""
    payload = {"error": error, "message": message}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(1)
