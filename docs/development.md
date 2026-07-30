# Development

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Verify

```bash
python -m pytest
storyforge validate
python -m compileall app storyforge
ruff check storyforge tests/test_release_readiness.py
black --check storyforge tests/test_release_readiness.py
```

## Synthetic workflow

Use the synthetic fixtures under `tests/fixtures/normalized_analysis_sample/` when you want a small public-safe example.
They are designed to exercise the architecture without depending on private manuscripts or provider credentials.

## Skipping optional integrations

Tests that depend on Kokoro, FFmpeg, or other optional services should skip cleanly when the dependency is not installed.
