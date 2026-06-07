"""License activation and enforcement for the sourcecode CLI.

Flow:
  1. Module imported → _init() loads ~/.sourcecode/license.json (if present)
  2. is_pro set globally (True when plan == "pro")
  3. Pro commands call require_feature(feature_name) at entry — exits 1 if not Pro
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
_SUPABASE_URL: str = os.environ.get("SOURCECODE_SUPABASE_URL", _DEFAULT_SUPABASE_URL)
_SUPABASE_ANON_KEY: str = os.environ.get(
    "SOURCECODE_SUPABASE_ANON_KEY",
    "",  # Set SOURCECODE_SUPABASE_ANON_KEY to your project anon key
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
_CACHE_TTL_SECONDS: int = 1800  # 30 minutes — Supabase is source of truth; cache is perf only
_DELTA_FREE_LIMIT: int = 30
_DEVICE_POLL_INTERVAL_S: float = 2.5
_DEVICE_POLL_TIMEOUT_S: float = 300.0  # 5-minute window for user to complete browser auth
_AUTH_BASE_URL: str = "https://sourcecode.dev"
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


def _generate_device_code() -> str:
    """Generate a human-readable device code: XXXX-XXXX-XXXX."""
    import uuid
    raw = uuid.uuid4().hex.upper()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


def _call_device_check(device_code: str) -> Optional[dict]:
    """Poll /device-check edge function. Returns dict or None on network error.

    Expected responses:
      {"status": "pending"}
      {"status": "complete", "device_token": "...", "email": "...", "plan": "pro", ...}
      {"status": "error", "message": "..."}
    """
    import urllib.error
    import urllib.request

    if not _SUPABASE_ANON_KEY:
        return None

    url = f"{_SUPABASE_URL}/functions/v1/device-check"
    body = json.dumps({"device_code": device_code}).encode("utf-8")
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
            return {"status": "error", "message": f"HTTP {exc.code}"}
    except Exception:
        return None


def _call_get_user_plan(device_token: str) -> Optional[dict]:
    """Fetch current plan/status for an authenticated device token.

    Expected response:
      {"valid": true, "plan": "pro", "status": "active", "features": [...], "email": "..."}
      {"valid": false, "error": "token_revoked"}
    """
    import urllib.error
    import urllib.request

    if not _SUPABASE_ANON_KEY:
        return None

    url = f"{_SUPABASE_URL}/functions/v1/get-user-plan"
    body = json.dumps({"device_token": device_token}).encode("utf-8")
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
        return None


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
            if age < _CACHE_TTL_SECONDS:
                return
        except Exception:
            pass

    auth_method = _license_data.get("auth_method")

    if auth_method == "device_flow":
        device_token = _license_data.get("device_token")
        if not device_token:
            return
        result = _call_get_user_plan(device_token)
        if result is None:
            return  # Network error — keep cached (offline-first)
        if not result.get("valid", True):
            _license_data = None
            is_pro = False
            try:
                if _LICENSE_FILE.exists():
                    _LICENSE_FILE.unlink()
            except Exception:
                pass
            return
        _license_data["plan"] = result.get("plan", "free")
        _license_data["status"] = result.get("status", "active")
        _license_data["features"] = result.get("features", [])
        _license_data["validated_at"] = datetime.now(timezone.utc).isoformat()
        is_pro = (
            _license_data.get("plan") == "pro"
            and _license_data.get("status", "active") != "inactive"
        )
        try:
            _write_license_file(_license_data)
        except Exception:
            pass
        return

    # Key-based auth (existing flow / legacy)
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

def can_use(feature_name: str) -> bool:
    """Return True if the current plan has access to feature_name.

    Does not trigger revalidation — use require_feature() at command entry
    points where you want revalidation + gating in one call.
    """
    return is_pro


def require_feature(
    feature_name: str,
    extra_fields: Optional[dict] = None,
) -> None:
    """Exit with a clean upgrade prompt when feature_name requires Pro.

    Re-validates stale cached license before gating (once per 24 h, online).

    Writes human-readable context to stderr (terminal UX) and a JSON error
    to stdout (backward-compatible machine-readable format).

    Args:
        extra_fields: Optional extra keys merged into the JSON error payload
                      (e.g. ``{"free_tier_alternative": "..."}``)

    Example:
        from sourcecode.license import require_feature
        require_feature("impact")
    """
    _maybe_revalidate()

    if is_pro:
        return

    info = _FEATURE_INFO.get(feature_name, {})
    display = info.get("display", feature_name)
    description = info.get("description", "")
    value = info.get("value", "")

    # Human-readable upgrade prompt on stderr
    lines = [f"\n  '{display}' is a Pro feature."]
    if description:
        lines.append(f"  {description}")
    if value:
        lines.append(f"  {value}")
    lines.append("")
    lines.append("  Upgrade:  sourcecode activate <license_key>")
    lines.append("")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()

    # JSON on stdout — backward-compatible for CI / MCP consumers
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
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(2)  # exit 2 = Pro feature required (0=ok, 1=runtime error, 2=license required)


def require_pro(feature_name: str) -> None:
    """Backward-compatible alias for require_feature.

    Example:
        from sourcecode.license import require_pro
        require_pro("impact")
    """
    require_feature(feature_name)


# ---------------------------------------------------------------------------
# Device-flow authentication
# ---------------------------------------------------------------------------

def _finish_device_auth(result: dict) -> None:
    """Persist device-flow credentials and emit success JSON. Exits on error."""
    global _license_data, is_pro

    device_token = result.get("device_token") or result.get("access_token") or ""
    email = result.get("email", "")
    plan = result.get("plan", "free")
    plan_status = (
        result.get("status_detail")
        or result.get("user_status")
        or result.get("status", "active")
    )
    features = result.get("features") or []

    if not device_token:
        sys.stderr.write("\n")
        _fail("auth_error", "Authentication completed but no session token received. Contact support.")

    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    data: dict = {
        "auth_method": "device_flow",
        "device_token": device_token,
        "email": email,
        "plan": plan,
        "status": plan_status,
        "features": features,
        "authenticated_at": now,
        "validated_at": now,
    }
    _write_license_file(data)
    _license_data = data
    is_pro = plan == "pro" and plan_status != "inactive"

    sys.stderr.write(f"\n  Authenticated as {email}.  Plan: {plan}\n\n")
    sys.stderr.flush()

    output: dict = {"status": "authenticated", "email": email, "plan": plan, "pro": is_pro}
    if not is_pro:
        output["upgrade_hint"] = "https://sourcecode.dev/pricing"
    else:
        output["features"] = features
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def auth_login() -> None:
    """Device code authentication flow.

    Shows a browser URL; polls the backend every 2.5 s until the user
    completes authentication or the 5-minute window expires.
    Writes credentials to ~/.sourcecode/license.json on success.
    Exits 0 on success, 1 on any failure.
    """
    import time

    device_code = _generate_device_code()
    activate_url = f"{_AUTH_BASE_URL}/activate?code={device_code}"

    sys.stderr.write(f"\n  Open this URL to authenticate:\n  {activate_url}\n\n  Waiting")
    sys.stderr.flush()

    deadline = time.monotonic() + _DEVICE_POLL_TIMEOUT_S
    _tick = 0

    while time.monotonic() < deadline:
        time.sleep(_DEVICE_POLL_INTERVAL_S)
        _tick += 1
        if _tick % 4 == 0:
            sys.stderr.write(".")
            sys.stderr.flush()

        result = _call_device_check(device_code)
        if result is None:
            continue  # network blip — keep polling

        status = result.get("status")
        if status == "pending":
            continue

        if status == "complete":
            _finish_device_auth(result)
            return

        if status == "error" or result.get("error"):
            sys.stderr.write("\n")
            _fail("auth_error", result.get("message") or result.get("error") or "Authentication failed.")

        # Unknown status — keep polling

    sys.stderr.write("\n")
    _fail("auth_timeout", "Authentication timed out after 5 minutes. Please try again.")


# ---------------------------------------------------------------------------
# Activation (key-based — legacy / direct key entry)
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
        _fail("invalid_license", result.get("error", "License key is not valid or subscription is inactive."))

    if result.get("plan") != "pro":
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
