"""License activation and enforcement for the sourcecode CLI.

Flow:
  1. Module imported → _init() loads ~/.sourcecode/license.json (if present)
  2. is_pro set globally (True when plan == "pro")
  3. Pro commands call require_pro(feature_name) at entry — exits 1 if not Pro
  4. `sourcecode activate <key>` calls activate_license(key) — validates via
     Supabase REST, writes ~/.sourcecode/license.json, exits 0 on success

Supabase credentials:
  SOURCECODE_SUPABASE_URL      — project REST endpoint
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
# Supabase endpoint config — override via env vars
# ---------------------------------------------------------------------------
_SUPABASE_URL: str = os.environ.get(
    "SOURCECODE_SUPABASE_URL",
    "https://YOUR_PROJECT.supabase.co",
)
_SUPABASE_ANON_KEY: str = os.environ.get(
    "SOURCECODE_SUPABASE_ANON_KEY",
    "",
)

_LICENSE_DIR: Path = Path.home() / ".sourcecode"
_LICENSE_FILE: Path = _LICENSE_DIR / "license.json"

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

    Call at the very top of every Pro-gated command, before any work.

    Example:
        from sourcecode.license import require_pro
        require_pro("impact")
    """
    if not is_pro:
        payload = {
            "error": "pro_required",
            "feature": feature_name,
            "message": "Run sourcecode activate <license_key>",
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def activate_license(license_key: str) -> None:
    """Validate license_key via Supabase, write ~/.sourcecode/license.json.

    Outputs JSON to stdout; exits 0 on success, 1 on any failure.
    Never raises — all error paths emit JSON and call sys.exit(1).
    """
    import urllib.error
    import urllib.request

    # Bail early when Supabase isn't configured yet
    if not _SUPABASE_ANON_KEY or _SUPABASE_URL == "https://YOUR_PROJECT.supabase.co":
        _fail("configuration_error", "SOURCECODE_SUPABASE_URL / SOURCECODE_SUPABASE_ANON_KEY not configured.")

    url = (
        f"{_SUPABASE_URL}/rest/v1/users"
        f"?license_key=eq.{license_key}"
        f"&select=license_key,plan,email"
    )
    req = urllib.request.Request(url)
    req.add_header("apikey", _SUPABASE_ANON_KEY)
    req.add_header("Authorization", f"Bearer {_SUPABASE_ANON_KEY}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        _fail("network_error", f"Supabase returned HTTP {exc.code}")
    except Exception as exc:
        _fail("network_error", str(exc))

    try:
        rows = json.loads(body)
    except Exception:
        _fail("network_error", "Invalid JSON response from Supabase")

    if not rows:
        _fail("invalid_license", "License key not found")

    user = rows[0]
    if user.get("plan") != "pro":
        _fail("not_pro", "This license is not Pro")

    # Write license file
    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "license_key": license_key,
        "plan": "pro",
        "email": user.get("email", ""),
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }
    _LICENSE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    result = {"status": "activated", "plan": "pro"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
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
