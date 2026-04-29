# Contributing to sourcecode

Thank you for your interest in contributing. This document covers how to set up a development environment, run tests, and submit changes.

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.

---

## Getting started

### Prerequisites

- Python 3.9+
- Git

### Local setup

```bash
git clone https://github.com/sourcecode-ai/sourcecode
cd sourcecode
pip install -e ".[dev]"
```

This installs `sourcecode` in editable mode with all development dependencies (`pytest`, `ruff`, `mypy`).

### Run the CLI locally

```bash
sourcecode . --agent
sourcecode . prepare-context explain
```

### Run tests

```bash
pytest tests/
```

Run a single file:
```bash
pytest tests/test_detector_nodejs.py -v
```

---

## Project structure

```
src/sourcecode/
├── cli.py                  # CLI entry point (Typer)
├── scanner.py              # File tree scanning
├── schema.py               # Output schema (SourceMap + all dataclasses)
├── serializer.py           # JSON/YAML output + agent_view/compact_view
├── summarizer.py           # Natural language project summary
├── architecture_summary.py # Architecture description generation
├── prepare_context.py      # Task-specific context compilation
├── relevance_scorer.py     # File operational relevance scoring
├── runtime_classifier.py   # Monorepo package role classification
├── git_analyzer.py         # Git context extraction
├── detectors/              # Stack/framework/entry point detectors
│   ├── python.py
│   ├── nodejs.py
│   ├── go.py
│   └── ...
└── ...
tests/
├── test_detector_nodejs.py
├── test_summarizer.py
└── ...
```

---

## Making changes

### Adding a new language detector

1. Create `src/sourcecode/detectors/<language>.py`
2. Implement the `BaseDetector` interface (see `detectors/base.py`)
3. Register it in `detectors/__init__.py` → `build_default_detectors()`
4. Add tests in `tests/test_detector_<language>.py`

### Modifying the output schema

The schema is defined in `schema.py` as Python dataclasses. All fields must have defaults so that existing consumers are not broken. Add new fields at the end of the relevant dataclass with `Optional[T] = None` or `list[T] = field(default_factory=list)`.

### Modifying relevance scoring

Relevance scoring lives in `relevance_scorer.py` (file-level scoring) and `runtime_classifier.py` (monorepo package-level classification). Changes here affect what agents see first — test with realistic repos.

---

## Code style

This project uses `ruff` for linting and formatting.

```bash
ruff check src/ tests/
ruff format src/ tests/
```

Type checking with `mypy`:
```bash
mypy src/
```

Rules:
- All public functions must have type annotations
- Docstrings are optional but welcome for non-obvious logic
- No inline comments explaining what the code does — names should do that
- Comments only for non-obvious constraints or workarounds

---

## Commits

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add Go module graph support
fix: exclude benchmark entrypoints from production ranking
docs: update agent workflow examples
refactor: simplify relevance scorer baseline calculation
test: add coverage for monorepo runtime classifier
```

Keep commits atomic. One logical change per commit.

---

## Pull requests

1. Fork the repository and create a branch: `git checkout -b feat/my-feature`
2. Make your changes with tests
3. Run the full test suite: `pytest tests/`
4. Run lint: `ruff check src/ tests/`
5. Open a PR against `main`

**PR description should include:**
- What changed and why
- How to test it manually
- Any breaking changes or schema additions

---

## Reporting bugs

Open an issue with:
- `sourcecode --version` output
- Command you ran
- Expected behavior
- Actual behavior / error output
- OS and Python version

---

## Proposing features

Open an issue with the `feature` label. Describe:
- The problem you're solving
- Why it belongs in `sourcecode` (vs. a wrapper or downstream tool)
- What the output or behavior would look like

Features that add flags without clear agent-oriented use cases will be declined. Complexity is a cost.

---

## What we will NOT merge

- LLM API calls inside the analysis pipeline
- Network requests during analysis (except opt-in telemetry)
- New flags that only serve human-readable output, not agent consumption
- Breaking changes to the output schema without a migration path
- Features that compromise the determinism guarantee

---

## License

By submitting a contribution, you agree that it will be licensed under the [Apache 2.0 License](LICENSE).
