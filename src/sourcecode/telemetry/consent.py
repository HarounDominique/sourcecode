"""First-run telemetry notice.

Telemetry is enabled by default (opt-out) and stays anonymous. The notice is
shown exactly once, only on interactive TTYs, to inform the user that
telemetry is on and how to turn it off. It does not ask a question — it
informs and respects the user's right to disable.

Notice is written to stderr so it doesn't pollute stdout output.
"""

from __future__ import annotations

import os
import sys

_NOTICE = """\
\033[2m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  sourcecode — anonymous telemetry is ON by default

  Anonymous usage metrics help improve sourcecode.

  Collected: tool version, Python version, OS, commands used,
  flags used, approximate repo size, execution duration, errors.

  Never collected: source code, file paths, file names, secrets,
  tokens, environment variables, or any repository content.

  Disable at any time:
    sourcecode telemetry disable
    export SOURCECODE_TELEMETRY=0   (or DO_NOT_TRACK=1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m
"""


def _is_interactive() -> bool:
    """True when running in an interactive terminal (not CI, not piped)."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    # Common CI environment variables
    ci_vars = {
        "CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "CIRCLECI",
        "TRAVIS", "JENKINS_URL", "BUILDKITE", "GITLAB_CI", "TF_BUILD",
        "TEAMCITY_VERSION", "DRONE", "SEMAPHORE",
    }
    return not any(os.environ.get(v) for v in ci_vars)


def show_first_run_notice() -> None:
    """Print the one-time telemetry notice on interactive terminals.

    Never raises. Does nothing on non-interactive / CI environments.
    Telemetry stays enabled regardless — this only informs the user.
    """
    if not _is_interactive():
        return
    try:
        sys.stderr.write(_NOTICE)
        sys.stderr.flush()
    except Exception:
        pass
