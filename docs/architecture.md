# StoryForge Architecture

StoryForge is a deterministic audiobook production pipeline that turns normalized story data into
render units, chapter audio, mastered chapters, and packaged audiobook artifacts.

## Normalized story inputs

The pipeline starts from structured analysis output. The normalized story representation keeps the
raw analysis separate from the cleaned story model so contributors can compare inputs and outputs
without mutating the source data.

## Voice planning

Voice planning assigns a provider voice to the narrator and each character role. The planner uses
registry metadata, series continuity, scene separation constraints, and a deterministic scoring model
to choose the best available voice without generating audio.

## Editable voice plans

Plans are editable. Manual overrides and locked assignments take priority over automatic suggestions,
and the planner records warnings instead of silently discarding user edits.

## Synthesis manifests

The synthesis manifest is the contract between the planner and renderer. It stores canonical render
units, provenance, input hashes, and validation state so rendering can resume without recomputing the
planning stage.

## Provider-neutral rendering

Rendering consumes the manifest and a provider adapter. The renderer does not decide which voice to use;
it only turns the manifest into audio bytes and sidecar metadata.

## Chapter assembly

Chapter assembly concatenates rendered segments into chapter-level WAV files. Cache identity depends on
the canonical input hash, the ordered segment set, and the assembly contract.

## Deterministic mastering

Mastering applies a simple deterministic normalization pass suitable for fixed inputs. The current
Python backend uses an RMS-based loudness proxy rather than true LUFS, and it does not measure true peak.

## M4B packaging

Packaging combines mastered chapter audio, metadata, and chapter markers into an M4B file when FFmpeg is
available. AAC and M4B output may still vary across FFmpeg or encoder versions even when the inputs match.

## Cache identity and sidecars

Stage caches are keyed by canonical input identity plus the backend/version identity required by the stage.
Sidecars record the same identity so the pipeline can tell a true cache hit from a stale artifact.

## Validation boundaries

Each stage validates its own output before handing it downstream. If validation fails, the stage reports
the failure and downstream stages stay blocked until the issue is fixed or the stage is rebuilt.

## Failure propagation and resume behavior

Failures are explicit and stop reuse of dependent stages. Resume behavior only reuses stages whose cache
identities and validation sidecars still match the current inputs.

## Deterministic versus environment-sensitive behavior

Deterministic or canonical:

- ID and hash generation
- manifest serialization
- cache decision logic
- ordered stage planning
- PCM concatenation when inputs are fixed
- deterministic Python mastering for fixed inputs and configuration

Environment-sensitive:

- provider-generated speech bytes
- backend diagnostics and latency
- FFmpeg/AAC output across versions and platforms
- wall-clock runtime and throughput

Kokoro output is not assumed to be bitwise deterministic. The pipeline is reproducible by identity and
validation, not by universal byte-for-byte promises.
