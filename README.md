# StoryForge

> StoryForge is under active development. Interfaces, schemas, and command-line behavior may change before the first stable release.

StoryForge is an open-source work in progress for deterministic audiobook production from EPUB inputs.
It is built around reproducible planning, rendering, assembly, mastering, and packaging stages, with incremental
rebuilds and resumable caches so long conversions can continue from the last known-good artifact.

Current status: public work-in-progress release preparation.

## Core capabilities

- Normalize analysis inputs into stable story and character data
- Plan and persist deterministic voices with editable overrides
- Build synthesis manifests with provider-neutral render units
- Render speech through a thin provider adapter boundary
- Assemble chapter audio into chapter-level WAV artifacts
- Apply deterministic mastering with an RMS-based loudness proxy
- Package chapter audio and metadata into M4B output when FFmpeg is available
- Resume interrupted builds without regenerating reusable stages

## Pipeline overview

1. Normalize or ingest structured story analysis.
2. Generate a voice plan and optional editable overrides.
3. Build a synthesis manifest from the story, plan, and registry.
4. Render segment audio with the selected provider adapter.
5. Assemble chapter WAV files from render outputs.
6. Master the chapter audio to a consistent level.
7. Package the audiobook into an M4B file with chapters and cover art.

See `docs/architecture.md` and `docs/pipeline.md` for the stage-by-stage contract.

## Architecture summary

StoryForge separates planning from rendering. The planner decides which voice should speak each role; the renderer only consumes the approved manifest.
Cache identities are derived from canonical inputs, backend identity, and the stage contract so that unchanged work can be safely reused.

The pipeline is designed for reproducibility, but not every output is bitwise stable:

- Kokoro speech bytes are not assumed to be bitwise deterministic
- the mastering loudness value is an RMS-based proxy, not true LUFS
- sample peak is not true peak
- AAC and M4B output can vary across FFmpeg or encoder versions

## Installation

Requirements:

- Python 3.11 or newer
- `pip`
- FFmpeg for final M4B packaging and some validation commands

Install from a clean checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

If you only need the runtime package:

```bash
pip install -e .
```

## Web Interface / Unraid

StoryForge now includes a lightweight personal web controller for Unraid.
It runs in a single Docker container, uses the existing StoryForge implementation, and stores all persistent state under mapped volumes.

See `docs/unraid-web.md` for deployment, storage mappings, Kokoro setup, uploads, builds, logs, artifacts, cancellation, and update instructions.

## Development setup

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m compileall app storyforge
storyforge --help
storyforge --version
```

A tiny synthetic analysis fixture lives under `tests/fixtures/normalized_analysis_sample/` and is safe to use for local experimentation.

## Minimal usage example

Build a book from a DRM-free EPUB:

```bash
storyforge build --epub ./books/example.epub --config ./config/config.yaml
```

Inspect the environment without touching provider APIs:

```bash
storyforge doctor
storyforge validate
```

`storyforge validate` performs the local release-readiness audit. `storyforge doctor` checks the configured runtime environment.

## Supported and optional dependencies

Runtime dependencies:

- `ebooklib`
- `beautifulsoup4`
- `requests`
- `PyYAML`

Optional development dependencies:

- `pytest`
- `pytest-cov`
- `ruff`
- `black`

Optional system dependency:

- FFmpeg for packaging M4B output and inspecting rendered audio

Standard tests do not require Kokoro, FFmpeg, or external provider credentials.

## Provider notes

- Kokoro is the initial rendering backend, but the planner and manifest stay provider-neutral.
- Provider settings default to local placeholder values and can be overridden with environment variables.
- Keep credentials out of source control; use local configuration or environment variables.

## Output artifacts

Typical stage outputs include:

- normalized analysis JSON files
- `voice_plan.json`
- `voice_assignment_report.json`
- synthesis manifest JSON
- rendered chapter WAVs and sidecars
- mastered chapter WAVs and sidecars
- final M4B package and metadata sidecars

The repository ignores generated audio, temporary workspaces, logs, and package artifacts.

## Incremental-build behavior

Each stage computes an identity from its canonical inputs. If the stage inputs, backend identity, or renderer contract have not changed, the cached result can be reused.
If a stage fails, downstream stages are blocked until the prerequisite stage is repaired or intentionally rebuilt.

Details: `docs/incremental-builds.md`

## Testing

Run the unit and integration-safe suite locally:

```bash
python -m pytest
```

Recommended release checks:

```bash
storyforge validate
python -m compileall app storyforge
ruff check storyforge tests/test_release_readiness.py
black --check storyforge tests/test_release_readiness.py
```

FFmpeg-dependent tests are skipped cleanly when FFmpeg is unavailable.

## Roadmap

See `docs/roadmap.md` for the current release-ready scope, near-term improvements, and future possibilities.

## Contributing

Read `CONTRIBUTING.md` before submitting changes. Please avoid copyrighted manuscript text, provider credentials, and machine-specific paths in commits.

## Security

See `SECURITY.md` for private vulnerability reporting guidance and manuscript/privacy notes.

## License

StoryForge is released under the Apache License 2.0. See `LICENSE`.

## Known limitations

- The project is still a work in progress.
- Schemas and command-line behavior may change before the first stable release.
- Kokoro output is not assumed to be bitwise deterministic.
- The mastering backend uses a simple RMS-based loudness proxy and does not measure true peak.
- AAC and M4B output may differ across FFmpeg or encoder versions.
- FFmpeg is required for real M4B packaging.
- No GUI, cloud workers, hosted publishing, or automatic distribution are included in this milestone.
