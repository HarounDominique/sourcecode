# Supabase Edge Functions

Backend for the Pro license flow. The CLI side lives in
`src/sourcecode/license.py`.

## Functions

| Function | Purpose | JWT |
|----------|---------|-----|
| `get-license` | Validates a license key for `sourcecode activate` and the 30-min revalidation. Returns `{valid, plan, status, features, email}`. | `--no-verify-jwt` |
| `lemonsqueezy-webhook` | Lemon Squeezy purchase/subscription webhook. Stores the LS native key, sets plan/status, handles revocation. | `--no-verify-jwt` |
| `telemetry` | Collects opt-in anonymous usage events (no PII). Inserts into `telemetry_events`. CLI side: `src/sourcecode/telemetry/`. | `--no-verify-jwt` |

All deploy with JWT verification OFF: the CLI authenticates with the public
publishable key (not a JWT), the webhook authenticates via HMAC signature, and
telemetry is unauthenticated public ingest.

The `telemetry` table is created from `supabase/sql/telemetry_events.sql`.

## Secrets (Supabase dashboard -> Edge Functions -> Secrets)

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `LEMON_SQUEEZY_WEBHOOK_SECRET` (webhook only)

## Deploy

```bash
supabase functions deploy get-license --no-verify-jwt
supabase functions deploy lemonsqueezy-webhook --no-verify-jwt
```

## Lemon Squeezy config

- Keep **Generate license keys** ON for every Pro variant (LS emails the key;
  the webhook stores that same native key — single key system).
- Subscribe the webhook to: `license_key_created`, `order_created`,
  `subscription_created/updated/resumed/unpaused`,
  `subscription_payment_success`, `subscription_expired`, `subscription_paused`.
