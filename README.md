# Storyforge Phase 2

Storyforge Phase 2 is a production-oriented EPUB-to-audiobook conversion engine.

What it does now:

- Reads a full DRM-free EPUB book
- Extracts book metadata and cover art
- Discovers readable chapters in order
- Converts every chapter to a standalone WAV file
- Tracks progress and writes a resumable manifest
- Resumes interrupted jobs without regenerating completed chapters
- Builds a final M4B audiobook with chapter markers and embedded cover art
- Retries Kokoro requests with exponential backoff
- Keeps per-conversion logs plus per-chapter metadata

What Phase 2 does not do:

- Speaker detection
- Multiple character voices
- Emotion analysis
- AI summaries
- Dashboards
- Web interfaces

## Configuration

All configurable values live in:

`config/config.yaml`

This includes:

- default voice
- speed
- chunk size
- retry delays
- temp/output directories
- M4B bitrate
- chapter filename format

## Runtime commands

List Kokoro voices:

```bash
python -m app.kokoro_client --list-voices
```

Show the Kokoro OpenAPI speech schema:

```bash
python -m app.kokoro_client --show-schema
```

Run a full-book conversion:

```bash
python -m app.convert --epub "/books/sample.epub"
```

Resume unfinished jobs and then process a requested EPUB:

```bash
python -m app.convert --epub "/books/sample.epub" --resume-all
```

## Output layout

Each completed book is written under the configured output directory as:

- `manifest.json`
- `metadata.json`
- `chapters.json`
- `cover.jpg`
- `Book Name.m4b`
- `Chapter 001.wav`
- `Chapter 002.wav`
- `logs/conversion.log`

## Validation

The project includes automated tests for:

- EPUB parsing
- chapter ordering
- metadata extraction
- manifest save/load
- resume behavior
- Kokoro retry logic
- M4B creation with chapter markers and cover art

