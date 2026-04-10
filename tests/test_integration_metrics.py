"""Tests de integracion para el flag --metrics de la CLI.

Stub placeholder — implementado en el plan 10-04.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="implementado en 10-04: full --metrics flag E2E integration")
def test_full_metrics_flag(tmp_path):
    """MQT-11: Running 'sourcecode analyze --metrics <path>' produces FileMetrics in output.

    This test will verify:
    - CLI flag --metrics is accepted
    - Output JSON contains 'file_metrics' list
    - Output JSON contains 'metrics_summary' dict
    - At least one FileMetrics entry for Python files
    - loc_availability is 'measured' for Python files
    """
    pass
