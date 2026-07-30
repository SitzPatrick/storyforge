# Configuration

The repository uses `config/config.yaml` as the default runtime configuration for source checkouts.
A packaged fallback copy also lives under `storyforge/defaults/config.yaml` so clean installs can load defaults without the repo layout.

## Environment overrides

StoryForge honors the following environment variables when present:

- `STORYFORGE_CONFIG`
- `STORYFORGE_BOOKS_DIR`
- `STORYFORGE_OUTPUT_DIR`
- `STORYFORGE_TEMP_DIR`
- `STORYFORGE_LOG_DIR`
- `STORYFORGE_CHUNK_CHARS`
- `STORYFORGE_KOKORO_TIMEOUT`
- `KOKORO_API_URL`
- `KOKORO_API_KEY`
- `KOKORO_MODEL`
- `KOKORO_VOICE`
- `KOKORO_SPEED`
- `STORYFORGE_OLLAMA_URL`
- `STORYFORGE_OLLAMA_MODEL`
- `STORYFORGE_ANALYSIS_CHUNK_SIZE`

## Example configuration

The checked-in defaults are synthetic and local-safe. They point at repository-relative workspace directories and loopback provider URLs.
Override them for your environment rather than editing them into private machine-specific paths.

## Dependencies

- Runtime analysis and rendering work without Kokoro or FFmpeg at import time.
- FFmpeg is only required for packaging and runtime validation of audio outputs.
- Provider credentials should be supplied locally, not committed.
