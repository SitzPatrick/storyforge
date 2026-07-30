# StoryForge Pipeline

The public pipeline is organized as a sequence of deterministic stages.

## Stages

1. Normalize story inputs.
2. Plan voices.
3. Build the synthesis manifest.
4. Render speech through a provider adapter.
5. Assemble chapter audio.
6. Master chapter audio.
7. Package the audiobook.

## Typical artifacts

- `voice_plan.json`
- `voice_assignment_report.json`
- synthesis manifest JSON
- rendered segment WAV files and sidecars
- assembled chapter WAV files and sidecars
- mastered chapter WAV files and sidecars
- final M4B output and package sidecars

## Reproducibility notes

The pipeline is deterministic in its ordering, hashing, and cache decisions.
Provider audio, FFmpeg encoding, and some runtime diagnostics remain environment-sensitive.

## Validation and failure behavior

Each stage emits its own validation result. A failure in an upstream stage blocks downstream work until the
failed stage is corrected or intentionally rebuilt.
