# Contributing to StoryForge

Thanks for helping improve StoryForge.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Test commands

```bash
python -m pytest
storyforge validate
python -m compileall app storyforge
ruff check storyforge tests/test_release_readiness.py
black --check storyforge tests/test_release_readiness.py
```

## Branch and commit expectations

- Keep changes focused on one milestone or one bug at a time.
- Do not stage unrelated files.
- Use clear commit messages that describe the behavior change.
- Re-run the relevant tests after every code edit.

## Coding conventions

- Preserve deterministic ordering and canonical serialization.
- Do not change cache keys, hashes, or invalidation rules without targeted tests.
- Keep public imports lightweight; optional dependencies should be imported lazily.
- Prefer small, reviewable edits over broad refactors.

## Adding tests

- Add a regression test for every non-trivial behavior change.
- Prefer synthetic fixtures over real manuscripts.
- Optional integrations should skip cleanly when their dependencies are absent.
- If you change a stage boundary, add an assertion for the emitted artifact or sidecar.

## Documentation expectations

- Document user-facing behavior, especially anything that changes file names, cache semantics, or command-line output.
- Update the architecture or troubleshooting docs when release behavior changes.

## Safety rules

- Do not commit provider keys, private keys, bearer tokens, or connection strings.
- Do not commit copyrighted manuscript text.
- Do not commit generated audio, package outputs, caches, or local workspace files.
- Avoid machine-specific absolute paths in documentation or fixtures.

## Pull-request expectations

- Include the StoryForge version and Python version in bug reports.
- Mention whether optional dependencies were involved.
- Summarize the tests you ran and the output you observed.
- If a change touches deterministic hashes, cache semantics, or artifact naming, include focused compatibility tests.
