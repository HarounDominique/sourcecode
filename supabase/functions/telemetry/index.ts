import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Opt-in anonymous usage telemetry collector. The CLI sends privacy-filtered
// events (no PII, no paths) fire-and-forget — see src/sourcecode/telemetry/.
// Deploy with --no-verify-jwt: events are public, low-value, and the client
// sends no auth header. This endpoint only INSERTS into telemetry_events.
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

// Server-side defense in depth — mirror the client allowlists. Anything
// unexpected is coerced to a safe default so a tampered payload cannot inject.
const EVENTS = new Set([
  "command_executed", "execution_completed", "execution_failed",
  "telemetry_enabled", "telemetry_disabled", "gate_blocked", "activation",
]);
const SIZES = new Set(["tiny", "small", "medium", "large", "huge", "unknown"]);
const DURATIONS = new Set(["<1s", "<5s", "<15s", "<60s", "60s+", "unknown"]);
const OSES = new Set(["linux", "macos", "windows", "other"]);
const ARCHES = new Set(["x64", "arm64", "other"]);
const FMTS = new Set(["json", "yaml"]);

const pick = (v: unknown, allowed: Set<string>, fb: string) =>
  typeof v === "string" && allowed.has(v) ? v : fb;
const short = (v: unknown, n = 32) =>
  typeof v === "string" && v.length <= n && !/[/\\\s]/.test(v) ? v : null;

serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("ok", { status: 405 });

  let p: Record<string, unknown>;
  try {
    p = await req.json();
  } catch {
    return new Response("ok", { status: 200 }); // never error loudly on telemetry
  }

  const row = {
    event: pick(p.event, EVENTS, "command_executed"),
    client_ts: short(p.ts, 20),
    v: short(p.v, 16),
    py: short(p.py, 8),
    os: pick(p.os, OSES, "other"),
    arch: pick(p.arch, ARCHES, "other"),
    cmd: short(p.cmd, 24),
    flags: Array.isArray(p.flags) ? p.flags.filter((f) => short(f, 32)).slice(0, 40) : [],
    output_fmt: pick(p.output_fmt, FMTS, "json"),
    repo_size: pick(p.repo_size, SIZES, "unknown"),
    duration: pick(p.duration, DURATIONS, "unknown"),
    success: typeof p.success === "boolean" ? p.success : null,
    error_kind: short(p.error_kind, 64),
    feature: short(p.feature, 32),
    session: short(p.session, 16),
  };

  try {
    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
    await supabase.from("telemetry_events").insert(row);
  } catch (e) {
    console.error("telemetry insert failed", e);
    // still return 200 — never penalize the CLI for our DB hiccup
  }

  return new Response(JSON.stringify({ received: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
