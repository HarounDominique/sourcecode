"""ContextGraph migration — Phase 0 baseline validation harness.

Pure validation infrastructure. Imports nothing from the engine; drives the CLI
as a black box via subprocess so it can prove "zero functional change" across the
ContextGraph migration (see .planning/DESIGN-context-graph.md).
"""
