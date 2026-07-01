"""Phase 0 regression gate for the ContextGraph migration.

Two always-on unit tests pin the harness's canonicalization contract. The full
byte-diff gate (`test_no_output_drift_vs_baseline`) re-runs every CLI command across
all field-test repos (~7 min), so it is opt-in via SC_BASELINE_COMPARE=1 — run it at
phase boundaries, not on every `pytest`. It also skips when the oracle or repos are
absent, so it never breaks CI.
Regenerate the oracle with:  python -m tests.contextgraph_baseline.harness capture
"""
from __future__ import annotations

import os

import pytest

from tests.contextgraph_baseline import harness


def test_harness_canonicalization_is_deterministic():
    """Canonicalization must be pure: same input → same bytes, volatiles scrubbed."""
    raw = '{"b": 1, "a": 2, "generated_at": "2026-07-01T20:39:40.328615+00:00"}'
    c1, _ = harness._canonicalize(raw, "/Users/x/repo")
    c2, _ = harness._canonicalize(raw, "/Users/x/repo")
    assert c1 == c2
    assert '"generated_at": "<VOLATILE>"' in c1
    assert c1.index('"a"') < c1.index('"b"')  # keys sorted


def test_repo_path_normalized_in_strings():
    raw = '{"file": "/Users/x/repo/src/Foo.java"}'
    c, _ = harness._canonicalize(raw, "/Users/x/repo")
    assert "<REPO>/src/Foo.java" in c
    assert "/Users/x/repo" not in c


@pytest.mark.skipif(
    os.environ.get("SC_BASELINE_COMPARE") != "1"
    or not harness.INDEX_PATH.exists()
    or not harness._present_repos(),
    reason="set SC_BASELINE_COMPARE=1 (phase gate) with oracle + field-test repos present",
)
def test_no_output_drift_vs_baseline():
    assert harness.compare(write_diffs=True) == 0, (
        "ContextGraph output drift vs baseline oracle — inspect *.NEW.json artifacts "
        "and explain every drifted cell before advancing a phase."
    )
