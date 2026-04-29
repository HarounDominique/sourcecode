"""First-run consent prompt.

Shown exactly once, only on interactive TTYs, only when the user hasn't been
asked before. Default answer is NO — the user must explicitly type 'y' to opt in.

Prompt is written to stderr so it doesn't pollute stdout output.
"""

from __future__ import annotations

import os
import sys

_PROMPT = """\
\033[2m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  sourcecode — optional anonymous telemetry

  Help improve sourcecode by sharing anonymous usage metrics.

  Collected: tool version, Python version, OS, commands used,
  flags used, approximate repo size, execution duration, errors.

  Never collected: source code, file paths, file names, secrets,
  tokens, environment variables, or any repository content.

  You can change this at any time:
    sourcecode telemetry disable
    export SOURCECODE_TELEMETRY=0
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


def ask_for_consent() -> bool:
    """Show the consent prompt and return the user's choice.

    Returns True if the user opted in, False otherwise.
    Never raises. Default (Enter / non-y input) is False.
    """
    if not _is_interactive():
        return False

    try:
        sys.stderr.write(_PROMPT)
        sys.stderr.write("  Enable anonymous telemetry? [y/N]: ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()
        sys.stderr.write("\n")
        return answer == "y"
    except Exception:
        return False
