import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Lemon Squeezy webhook. Source of truth for plan/status and the license key.
// Deploy with --no-verify-jwt (Lemon Squeezy does not send a Supabase JWT;
// HMAC signature is the authentication). Keep "Generate license keys" ON on
// every Pro variant: LS emails the key to the customer and we store that same
// native key here, so there is a single key system end to end.
const LEMON_SQUEEZY_WEBHOOK_SECRET = Deno.env.get("LEMON_SQUEEZY_WEBHOOK_SECRET")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const PRO_FEATURES = ["impact", "review-pr", "generate-tests", "mcp"];

// The license key is delivered by this event (LS generates + emails it):
const LICENSE_EVENTS = ["license_key_created"];
// Activate / keep Pro:
const ACTIVATE_EVENTS = [
  "order_created",
  "subscription_created",
  "subscription_updated",
  "subscription_resumed",
  "subscription_unpaused",
  "subscription_payment_success",
];
// Revocation — real end of access. NOT subscription_cancelled: that keeps
// access until period end; LS sends subscription_expired when it actually ends.
const REVOKE_EVENTS = [
  "subscription_expired",
  "subscription_paused",
];
const HANDLED_EVENTS = [...LICENSE_EVENTS, ...ACTIVATE_EVENTS, ...REVOKE_EVENTS];

async function verifySignature(rawBody: string, signature: string): Promise<boolean> {
  if (!signature) return false;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(LEMON_SQUEEZY_WEBHOOK_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const expected = await crypto.subtle.sign("HMAC", key, encoder.encode(rawBody));
  const expectedHex = Array.from(new Uint8Array(expected))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return expectedHex === signature;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405 });

  const rawBody = await req.text();
  const signature =
    req.headers.get("X-Signature") ?? req.headers.get("x-signature") ?? "";

  if (!(await verifySignature(rawBody, signature))) {
    console.error("Invalid webhook signature");
    return new Response("Unauthorized", { status: 401 });
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return new Response("Bad request: invalid JSON", { status: 400 });
  }

  const meta = payload.meta as Record<string, unknown>;
  const data = payload.data as Record<string, unknown>;
  const eventName = meta?.event_name as string;
  const eventId = meta?.event_id as string | undefined;

  if (!HANDLED_EVENTS.includes(eventName)) {
    return json({ received: true, skipped: true });
  }

  const attributes = data?.attributes as Record<string, unknown>;
  const email = ((attributes?.user_email ?? attributes?.customer_email) as string ?? "")
    .toLowerCase();

  if (!email || !email.includes("@")) {
    console.error("No valid email in payload", { eventName });
    return new Response("Bad request: no email", { status: 400 });
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  // Idempotency
  if (eventId) {
    const { data: existing } = await supabase
      .from("license_events").select("id").eq("event_id", eventId).maybeSingle();
    if (existing) return json({ received: true, duplicate: true });
  }

  const { data: existingUser } = await supabase
    .from("users").select("id, license_key").eq("email", email).maybeSingle();

  let userId = existingUser?.id;
  const now = new Date().toISOString();

  // #3  license_key_created -> store the native Lemon Squeezy key
  if (LICENSE_EVENTS.includes(eventName)) {
    const lsKey = attributes?.key as string;
    if (!lsKey) {
      console.error("license_key_created without attributes.key");
      return new Response("Bad request: no key", { status: 400 });
    }
    const { data: up, error } = await supabase.from("users").upsert(
      { email, plan: "pro", status: "active", features: PRO_FEATURES,
        license_key: lsKey, updated_at: now },
      { onConflict: "email", ignoreDuplicates: false },
    ).select("id").single();
    if (error) { console.error("upsert key", error); return json({ error: "DB" }, 500); }
    userId = up?.id ?? userId;
  }

  // #4  Revocation -> status inactive (does NOT touch license_key or plan)
  else if (REVOKE_EVENTS.includes(eventName)) {
    const { error } = await supabase.from("users")
      .update({ status: "inactive", updated_at: now }).eq("email", email);
    if (error) console.error("revoke", error);
    if (userId) {
      await supabase.from("subscriptions").update({ status: "inactive" }).eq("user_id", userId);
    }
  }

  // Activation -> plan pro + active (preserves existing license_key)
  else {
    const { data: up, error } = await supabase.from("users").upsert(
      { email, plan: "pro", status: "active", features: PRO_FEATURES, updated_at: now },
      { onConflict: "email", ignoreDuplicates: false },
    ).select("id").single();
    if (error) { console.error("upsert activate", error); return json({ error: "DB" }, 500); }
    userId = up?.id ?? userId;

    const periodEnd = (attributes?.renews_at ?? attributes?.ends_at ?? null) as string | null;
    await supabase.from("subscriptions").upsert(
      { user_id: userId, provider: "lemonsqueezy", status: "active",
        current_period_end: periodEnd, created_at: now },
      { onConflict: "user_id" },
    );
  }

  // Audit
  const { error: evErr } = await supabase.from("license_events").insert({
    user_id: userId ?? null,
    event_type: eventName,
    event_id: eventId ?? null,
    payload: JSON.parse(JSON.stringify(payload)),
  });
  if (evErr) console.error("license_event insert", evErr);

  console.log(`Processed ${eventName} for ${email}`);
  return json({ received: true, email, event: eventName });
});
