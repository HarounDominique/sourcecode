import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// License validation endpoint hit by the CLI's `sourcecode activate <key>` and
// by the 30-min background revalidation (license.py: _call_get_license).
// Deploy with --no-verify-jwt: the CLI authenticates with the public
// publishable key, which is not a legacy-secret JWT. Protection is the exact
// license_key the caller must present + service-role lookup, not a JWT.
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

// Same format the CLI validates (license.py:72)
const LICENSE_KEY_RE = /^[A-Za-z0-9_\-]{1,200}$/;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return json({ valid: false, error: "method_not_allowed" }, 405);
  }

  let payload: Record<string, unknown>;
  try {
    payload = await req.json();
  } catch {
    return json({ valid: false, error: "invalid_json" }, 400);
  }

  const licenseKey = ((payload.license_key as string) ?? "").trim();
  if (!licenseKey || !LICENSE_KEY_RE.test(licenseKey)) {
    return json({ valid: false, error: "invalid_license_format" });
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  const { data: user, error } = await supabase
    .from("users")
    .select("email, plan, status, features")
    .eq("license_key", licenseKey)
    .maybeSingle();

  if (error) {
    console.error("DB error", error);
    return json({ valid: false, error: "db_error" }, 500);
  }

  if (!user) {
    return json({ valid: false, error: "license_not_found" });
  }

  const active = (user.status ?? "active") === "active";
  const isPro = user.plan === "pro";

  // Revocation: status != active OR plan != pro -> valid:false.
  // The CLI revalidates every 30 min and clears its cache on this response.
  if (!active || !isPro) {
    return json({
      valid: false,
      error: !isPro ? "not_pro" : "inactive",
      plan: user.plan ?? "free",
      status: user.status ?? "inactive",
    });
  }

  // features may arrive as jsonb (array) or as a JSON string — normalize
  let features = user.features as unknown;
  if (typeof features === "string") {
    try { features = JSON.parse(features); } catch { features = []; }
  }
  if (!Array.isArray(features)) features = [];

  return json({
    valid: true,
    plan: user.plan,
    status: user.status ?? "active",
    features,
    email: user.email ?? "",
  });
});
