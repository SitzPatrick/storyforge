# Incremental Builds

StoryForge uses stage-level cache identities so unchanged work can be reused safely.

## What is hashed

- canonical normalized inputs
- story and plan identity
- renderer/provider backend identity where needed
- assembly/mastering/package configuration
- sidecar metadata that proves the output matches the current inputs

## Reuse rules

A stage may be reused only when its canonical inputs, backend identity, and validation sidecar still match.
If a stage changes, downstream stages should be considered stale until they are rebuilt.

## Resume behavior

Resume is conservative. The pipeline resumes from the last verified artifact rather than assuming a later output is still valid.
If a sidecar is missing or invalid, the pipeline should rebuild that stage.

## Sidecars

Sidecars are part of the cache contract. They record enough metadata to distinguish a genuine cache hit from an artifact that merely exists on disk.
