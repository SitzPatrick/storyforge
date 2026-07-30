# Troubleshooting

## `storyforge doctor` fails

Check the configured paths, provider URL, and FFmpeg installation. The doctor command is intended to surface runtime problems before a long build starts.

## FFmpeg missing

Install FFmpeg before packaging or validating audio outputs. The project can still import and run most tests without FFmpeg.

## Kokoro unreachable

- Confirm the API URL.
- Confirm the provider container or local service is running.
- Confirm the voice name exists in the provider.

## Cache confusion

If a stage seems stale, remove or invalidate the stage outputs and rerun from the first stale boundary. Do not trust a file that lacks a matching sidecar.
