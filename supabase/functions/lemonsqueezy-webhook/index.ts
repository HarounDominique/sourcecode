import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Lemon Squeezy webhook. Source of truth for plan/status and the license key.
// Deploy with --no-verify-jwt (Lemon Squeezy does not send a Supabase JWT;
// HMAC signature is the authentication). Keep "Generate license keys" ON on
// every Pro variant: LS emails the key to the customer and we store that same
// native key here, so there is a single key system end to end.
//
// Ordering safety: LS delivers events out of order and Edge Functions run
// concurrently. We never blind-upsert status. Instead apply_license_event()
// (see supabase/sql/license_event_ordering.sql) locks the row and applies a
// status change only when the event is not older than the last one applied,
// using each event's own LS timestamp. A stale `subscription_paused`/`expired`
// can therefore never clobber a newer paid state.
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

// Map a Lemon Squeezy subscription status to our access status. This is the
// authoritative current state LS attaches to every subscription_* event, so we
// prefer it over inferring from the event name (an out-of-order event still
// carries the status that was true at its own event time, and the recency
// guard in apply_license_event decides whether it wins).
//   cancelled keeps access until period end (LS sends subscription_expired at
//   the real end); past_due is a payment-retry grace window — keep access.
function mapLsStatus(lsStatus: string | undefined): "active" | "inactive" | null {
  switch ((lsStatus ?? "").toLowerCase()) {
    case "on_trial":
    case "active":
    case "cancelled":
    case "past_due":
      return "active";
    case "paused":
    case "unpaid":
    case "expired":
      return "inactive";
    default:
      return null; // unknown -> do not infer a status
  }
}

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

  const attributes = (data?.attributes ?? {}) as Record<string, unknown>;
  const email = ((attributes?.user_email ?? attributes?.customer_email) as string ?? "")
    .toLowerCase();

  if (!email || !email.includes("@")) {
    console.error("No valid email in payload", { eventName });
    return new Response("Bad request: no email", { status: 400 });
  }

  // The license key only ever arrives on license_key_created.
  const licenseKey = LICENSE_EVENTS.includes(eventName)
    ? ((attributes?.key as string) ?? null)
    : null;
  if (LICENSE_EVENTS.includes(eventName) && !licenseKey) {
    console.error("license_key_created without attributes.key");
    return new Response("Bad request: no key", { status: 400 });
  }

  // Desired status: prefer LS's authoritative subscription status, fall back to
  // event-name semantics. Non-subscription events (order/license) only activate.
  let desiredStatus: "active" | "inactive" | null;
  if (eventName.startsWith("subscription_")) {
    desiredStatus = mapLsStatus(attributes?.status as string | undefined);
    if (desiredStatus === null) {
      desiredStatus = REVOKE_EVENTS.includes(eventName)
        ? "inactive"
        : ACTIVATE_EVENTS.includes(eventName)
        ? "active"
        : null;
    }
  } else if (LICENSE_EVENTS.includes(eventName) || ACTIVATE_EVENTS.includes(eventName)) {
    desiredStatus = "active";
  } else if (REVOKE_EVENTS.includes(eventName)) {
    desiredStatus = "inactive";
  } else {
    desiredStatus = null;
  }

  // Recency key: the event's own LS timestamp, NOT our receipt time.
  const eventAt =
    (attributes?.updated_at as string) ??
    (attributes?.created_at as string) ??
    new Date().toISOString();

  // Only grant features when we are activating; a revoke leaves them untouched.
  const features = desiredStatus === "active" ? PRO_FEATURES : null;

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  // Idempotency: skip if we already logged this exact event.
  if (eventId) {
    const { data: existing } = await supabase
      .from("license_events").select("id").eq("event_id", eventId).maybeSingle();
    if (existing) return json({ received: true, duplicate: true });
  }

  // Atomic, recency-guarded apply. A stale event cannot downgrade newer state.
  const { data: rpcData, error: rpcErr } = await supabase.rpc("apply_license_event", {
    p_email: email,
    p_desired_status: desiredStatus,
    p_event_at: eventAt,
    p_features: features,
    p_license_key: licenseKey,
    p_plan: "pro",
  });

  if (rpcErr) {
    console.error("apply_license_event", rpcErr);
    return json({ error: "DB" }, 500); // LS retries; the apply is idempotent.
  }
  // PostgREST may return the composite as an object or a single-element array.
  const row = (Array.isArray(rpcData) ? rpcData[0] : rpcData) as
    | Record<string, unknown>
    | null;
  const userId = (row?.id as string | undefined) ?? null;

  // Mirror period end into the subscriptions table on activation (informational;
  // get-license reads users.status, not this row).
  if (ACTIVATE_EVENTS.includes(eventName) && userId) {
    const now = new Date().toISOString();
    const periodEnd = (attributes?.renews_at ?? attributes?.ends_at ?? null) as string | null;
    await supabase.from("subscriptions").upsert(
      { user_id: userId, provider: "lemonsqueezy", status: "active",
        current_period_end: periodEnd, created_at: now },
      { onConflict: "user_id" },
    );
  }

  // Reliable audit: a failed log returns 500 so LS retries. Because the apply
  // above is idempotent (same event_at re-applies the same state) and the
  // idempotency guard keys on this not-yet-inserted event_id, the retry safely
  // reprocesses and re-attempts the log. No silent, unlogged state changes.
  const { error: evErr } = await supabase.from("license_events").insert({
    user_id: userId,
    event_type: eventName,
    event_id: eventId ?? null,
    payload: JSON.parse(JSON.stringify(payload)),
  });
  if (evErr) {
    console.error("license_event insert", evErr);
    return json({ error: "audit_failed" }, 500);
  }

  console.log(`Processed ${eventName} for ${email} -> status=${desiredStatus}`);
  return json({ received: true, email, event: eventName, status: desiredStatus });
});
