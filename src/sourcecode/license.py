"""License activation and enforcement for the sourcecode CLI.

Flow:
  1. Module imported → _init() loads ~/.sourcecode/license.json (if present)
  2. is_pro set globally (True when plan == "pro")
  3. Pro commands call require_pro(feature_name) at entry — exits 1 if not Pro
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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Supabase endpoint config — hardcoded for production; override via env for dev
# ---------------------------------------------------------------------------
_SUPABASE_URL: str = os.environ.get(
    "SOURCECODE_SUPABASE_URL",
    "https://qkndlmyekvujjdgthtmz.supabase.co",
)
_SUPABASE_ANON_KEY: str = os.environ.get(
    "SOURCECODE_SUPABASE_ANON_KEY",
    "",  # Set SOURCECODE_SUPABASE_ANON_KEY to your project anon key
)

_LICENSE_DIR: Path = Path.home() / ".sourcecode"
_LICENSE_FILE: Path = _LICENSE_DIR / "license.json"
_CACHE_TTL_SECONDS: int = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Global license state — loaded once at import time
# ---------------------------------------------------------------------------
_license_data: Optional[dict] = None
is_pro: bool = False


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

    validated_at_str = _license_data.get("validated_at") or _license_data.get("activated_at")
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
        _LICENSE_FILE.write_text(
            json.dumps(_license_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _init() -> None:
    global _license_data, is_pro
    _license_data = _load_license_file()
    is_pro = (
        _license_data is not None
        and _license_data.get("plan") == "pro"
    )


_init()


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def require_pro(feature_name: str) -> None:
    """Exit with structured JSON error when not Pro.

    Re-validates stale cached license before gating (once per 24 h, online).

    Example:
        from sourcecode.license import require_pro
        require_pro("impact")
    """
    if is_pro:
        _maybe_revalidate()

    if not is_pro:
        payload = {
            "error": "pro_required",
            "feature": feature_name,
            "message": "Run: sourcecode activate <license_key>",
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def activate_license(license_key: str) -> None:
    """Validate license_key via Edge Function, write ~/.sourcecode/license.json.

    Outputs JSON to stdout; exits 0 on success, 1 on any failure.
    Never raises — all error paths emit JSON and call sys.exit(1).
    """
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
    _LICENSE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

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
