from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Optional

_MAX_FILES_PER_COMMIT = 10
_MAX_HOTSPOTS = 20
_MAX_CONTRIBUTORS = 20

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _run_git(args: list[str], cwd: Path, timeout: int = 15) -> tuple[str, int]:
    result = subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout, result.returncode


class GitAnalyzer:
    """Extrae contexto temporal del repositorio git."""

    def analyze(self, path: Path, depth: int = 20, days: int = 90) -> "GitContext":
        from sourcecode.schema import (
            ChangeHotspot,
            CommitRecord,
            GitContext,
            UncommittedChanges,
        )

        limitations: list[str] = []
        branch: Optional[str] = None
        recent_commits: list[CommitRecord] = []
        change_hotspots: list[ChangeHotspot] = []
        uncommitted: Optional[UncommittedChanges] = None
        contributors: list[str] = []

        try:
            stdout, rc = _run_git(["rev-parse", "--git-dir"], path, timeout=5)
            if rc != 0 or not stdout.strip():
                return GitContext(
                    requested=True,
                    limitations=["no_git_repo"],
                    git_summary="No es un repositorio git.",
                )
        except FileNotFoundError:
            return GitContext(
                requested=True,
                limitations=["git_not_found"],
                git_summary="Git no está disponible en el sistema.",
            )
        except subprocess.TimeoutExpired:
            return GitContext(requested=True, limitations=["git_timeout"])

        try:
            stdout, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path, timeout=5)
            branch = stdout.strip() or None
        except Exception:
            limitations.append("branch_unavailable")

        try:
            stdout, _ = _run_git(
                [
                    "log",
                    f"-n{depth}",
                    "--name-only",
                    "--pretty=format:__COMMIT__|%H|%s|%an|%aI",
                ],
                path,
                timeout=15,
            )
            recent_commits = _parse_commits(stdout)
        except subprocess.TimeoutExpired:
            limitations.append("commits_timeout")
        except Exception as exc:
            limitations.append(f"commits_error:{exc}")

        try:
            stdout, _ = _run_git(
                [
                    "log",
                    f"--since={days} days ago",
                    "--name-only",
                    "--pretty=format:%aI",
                ],
                path,
                timeout=30,
            )
            change_hotspots = _parse_hotspots(stdout)
        except subprocess.TimeoutExpired:
            limitations.append("hotspots_timeout")
        except Exception as exc:
            limitations.append(f"hotspots_error:{exc}")

        try:
            stdout, _ = _run_git(["status", "--porcelain"], path, timeout=10)
            uncommitted = _parse_uncommitted(stdout)
        except subprocess.TimeoutExpired:
            limitations.append("status_timeout")
        except Exception as exc:
            limitations.append(f"status_error:{exc}")

        try:
            stdout, _ = _run_git(
                ["log", f"--since={days} days ago", "--format=%an"],
                path,
                timeout=10,
            )
            names = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
            contributors = sorted(set(names))[:_MAX_CONTRIBUTORS]
        except Exception as exc:
            limitations.append(f"contributors_error:{exc}")

        git_summary = _build_summary(branch, recent_commits, change_hotspots, uncommitted)

        return GitContext(
            requested=True,
            branch=branch,
            recent_commits=recent_commits,
            change_hotspots=change_hotspots,
            uncommitted_changes=uncommitted,
            contributors=contributors,
            git_summary=git_summary,
            limitations=limitations,
        )


def _parse_commits(output: str) -> list:
    from sourcecode.schema import CommitRecord

    commits = []
    blocks = re.split(r"(?m)^__COMMIT__\|", output)
    for block in blocks:
        if not block.strip():
            continue
        lines = block.split("\n")
        header = lines[0].strip()
        parts = header.split("|", 3)
        if len(parts) < 4:
            continue
        hash_val, message, author, date_str = parts
        files = [
            ln.strip()
            for ln in lines[1:]
            if ln.strip() and not ln.startswith("__COMMIT__")
        ][:_MAX_FILES_PER_COMMIT]
        commits.append(
            CommitRecord(
                hash=hash_val[:8],
                message=message,
                author=author,
                date=date_str[:10] if date_str else "",
                files_changed=files,
            )
        )
    return commits


def _parse_hotspots(output: str) -> list:
    from sourcecode.schema import ChangeHotspot

    file_counts: Counter = Counter()
    file_last_date: dict[str, str] = {}
    current_date = ""

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if _DATE_PATTERN.match(line):
            current_date = line[:10]
        else:
            file_counts[line] += 1
            if line not in file_last_date and current_date:
                file_last_date[line] = current_date

    return [
        ChangeHotspot(
            file=f,
            commit_count=count,
            last_changed=file_last_date.get(f, ""),
        )
        for f, count in file_counts.most_common(_MAX_HOTSPOTS)
    ]


def _parse_uncommitted(output: str) -> "UncommittedChanges":
    from sourcecode.schema import UncommittedChanges

    staged, unstaged, untracked = [], [], []
    for line in output.splitlines():
        if len(line) < 3:
            continue
        x, y = line[0], line[1]
        filepath = line[3:].strip()
        if x == "?" and y == "?":
            untracked.append(filepath)
        else:
            if x != " ":
                staged.append(filepath)
            if y != " ":
                unstaged.append(filepath)
    return UncommittedChanges(staged=staged, unstaged=unstaged, untracked=untracked)


def _build_summary(
    branch: Optional[str],
    commits: list,
    hotspots: list,
    uncommitted: Optional[object],
) -> str:
    parts = []
    if branch:
        parts.append(f"Rama {branch}.")
    if uncommitted is not None:
        total = len(uncommitted.staged) + len(uncommitted.unstaged) + len(uncommitted.untracked)
        if total > 0:
            parts.append(
                f"{total} cambios pendientes"
                f" (staged: {len(uncommitted.staged)},"
                f" unstaged: {len(uncommitted.unstaged)},"
                f" untracked: {len(uncommitted.untracked)})."
            )
        else:
            parts.append("Working tree limpio.")
    if hotspots:
        top = hotspots[:3]
        hotspot_str = ", ".join(f"{h.file} ({h.commit_count} commits)" for h in top)
        parts.append(f"Archivos más activos: {hotspot_str}.")
    if commits:
        last = commits[0]
        msg = last.message[:80]
        parts.append(f"Último commit: {last.date} — {msg}.")
    return " ".join(parts) if parts else "Sin historial git disponible."
