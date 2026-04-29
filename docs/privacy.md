# Privacy & Telemetry

`sourcecode` includes an **opt-in** anonymous telemetry system. It is **disabled by default**. Nothing is collected unless you explicitly enable it.

---

## Quick reference

```bash
sourcecode telemetry status   # check current setting
sourcecode telemetry enable   # opt in
sourcecode telemetry disable  # opt out

export SOURCECODE_TELEMETRY=0  # disable via environment variable
export SOURCECODE_TELEMETRY=1  # enable via environment variable
```

The environment variable always takes precedence over the config file.

---

## Why telemetry exists

`sourcecode` is an open source tool with no usage visibility by default. Anonymous telemetry helps answer questions like:

- Which commands do developers actually use?
- What flag combinations are common?
- Are there performance regressions between versions?
- What error types occur most frequently?
- Which Python versions and operating systems need support?

This data guides development priorities without requiring surveys, support tickets, or guesswork.

**The goal is product improvement, not user tracking.**

---

## What is collected

When telemetry is enabled, these fields are sent per command invocation:

| Field | Example | Notes |
|-------|---------|-------|
| `v` | `0.26.0` | sourcecode version |
| `py` | `3.11` | Python major.minor only |
| `os` | `macos` | OS family: linux / macos / windows / other |
| `arch` | `x64` | CPU family: x64 / arm64 / other |
| `cmd` | `analyze` | Command run: analyze / prepare-context |
| `flags` | `["--agent"]` | Flag names only (allowlist), no values |
| `output_fmt` | `json` | Output format: json or yaml |
| `repo_size` | `small` | File count range bucket (see below) |
| `duration` | `<5s` | Execution time range bucket |
| `success` | `true` | Whether the command succeeded |
| `error_kind` | `FileNotFoundError` | Exception class name only, no message |
| `session` | `a3f2c1b0` | 8-char random hex, ephemeral, never persisted |

### Repo size buckets

File counts are converted to anonymous ranges before sending:

| Bucket | File count |
|--------|-----------|
| `tiny` | < 50 |
| `small` | 50 – 499 |
| `medium` | 500 – 1,999 |
| `large` | 2,000 – 9,999 |
| `huge` | 10,000+ |

### Session identifier

The `session` field is an 8-character random hex string generated fresh each time `sourcecode` runs. It is:
- **Never persisted** to disk
- **Never reused** across invocations
- Only useful for correlating multiple events within a single run (e.g., start + completion)
- Not a user identifier, device fingerprint, or persistent tracker

---

## What is NEVER collected

The following are explicitly prohibited by the telemetry system and verified in automated tests:

- Source code or file contents
- File names or directory names
- Absolute paths or relative paths
- Repository names or project names
- Environment variable names or values
- Secrets, tokens, API keys, or credentials
- Command output or analysis results
- IP addresses (requests go through a privacy proxy layer)
- User names or identities
- Exact file counts (only bucketed ranges)
- Exact durations (only bucketed ranges)
- Error messages (only exception class names)

---

## Privacy filter

Every event passes through a mandatory sanitization layer before transmission (`src/sourcecode/telemetry/filters.py`). This layer:

- Validates all fields against explicit allowlists
- Rejects any string containing path separators (`/` or `\`)
- Truncates all strings to a maximum of 64 characters
- Strips exception messages, keeping only the class name
- Validates the session ID against a strict hex pattern
- Drops any unexpected or unknown fields

This filter is tested in `tests/test_telemetry.py`. Tests explicitly verify that paths, long strings, and sensitive data cannot escape the filter.

---

## Data transmission

- Events are sent to `https://t.sourcecode.dev/v1/event` (configurable via `SOURCECODE_TELEMETRY_ENDPOINT`)
- Transmission happens in a background daemon thread — it never blocks the CLI
- Timeout is 3 seconds — if the request fails or times out, it is silently dropped
- No retries — a missed event is acceptable
- No queuing or batching to disk

---

## Configuration file

When you run `sourcecode telemetry enable` or `disable`, the setting is saved to:

```
~/.config/sourcecode/config.json
```

The file looks like:
```json
{
  "telemetry": {
    "enabled": false,
    "asked": true
  }
}
```

You can edit or delete this file directly at any time.

---

## First-run consent

The first time you run `sourcecode` interactively (not in CI, not piped), you will be shown a brief prompt explaining what telemetry is and asking if you want to enable it.

- Default answer is **No** (press Enter to decline)
- The prompt is shown **once only**
- In CI environments (GitHub Actions, CircleCI, etc.) the prompt is skipped and telemetry stays disabled
- When piped or run non-interactively, the prompt is skipped

---

## CI environments

Telemetry is always disabled in CI environments, regardless of config file settings. The following environment variables trigger CI detection:

`CI`, `CONTINUOUS_INTEGRATION`, `GITHUB_ACTIONS`, `CIRCLECI`, `TRAVIS`, `JENKINS_URL`, `BUILDKITE`, `GITLAB_CI`, `TF_BUILD`, `TEAMCITY_VERSION`, `DRONE`, `SEMAPHORE`

---

## Open source auditability

The entire telemetry implementation is in `src/sourcecode/telemetry/`. You can read it, audit it, and verify it does what this document claims.

Key files:
- `config.py` — configuration read/write
- `consent.py` — first-run consent prompt
- `events.py` — event schema
- `filters.py` — privacy sanitization (the critical safety layer)
- `transport.py` — HTTP transmission
- `__init__.py` — public API

Tests in `tests/test_telemetry.py` verify that sensitive data cannot escape the system.

---

## Contact

Questions or concerns about privacy: open an issue on GitHub or email security@sourcecode.dev.
