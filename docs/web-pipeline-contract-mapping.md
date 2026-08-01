# StoryForge web-to-pipeline contract mapping

This mapping records the existing typed contracts inspected before implementation. Canonical metadata stores only project-relative paths; adapters resolve them under the project workspace at execution time.

## Planning and analysis

- web project `work/normalized/{normalized_story,normalized_entities,normalized_dialogue,normalized_scenes,normalization_report}.json`
  -> `load_character_profiles(normalized_dir)`
  -> `CharacterProfileBundle` (`profiles`, `normalization_report`, `statistics`, source hashes)
  -> `AssignmentContext` (`book_id`, `series_id`, source analysis identity, profiles, registry, bindings, candidate scores, narrator candidates, `VoiceBudget`, `ConflictReport`, planner config)
  -> `assign_voices(context)`
  -> `AssignmentResult.voice_plan` + `AssignmentResult.assignment_report`
  -> `voice_plan.generated.json` and `voice_assignment_report.json`

- project `work/voice_plan.generated.json` + optional existing editable `work/voice_plan.json`
  -> `merge_voice_plans(previous, generated_plan, registry=registry)`
  -> `EditableVoicePlan`
  -> atomic `work/voice_plan.json`

## Manifest

- normalized `normalized_story.json` (including `book_id`, `series_id`, `source_analysis_hash`, `source_analysis_path`, characters, scenes, and ordered segments)
  + editable voice plan
  + validated live registry
  + planner/manifest config and relative source-artifact map
  -> `build_synthesis_manifest(normalized_story, editable_plan, registry, planner_config, source_artifacts=...)`
  -> `ManifestBuildResult.manifest`
  -> atomic `work/synthesis_manifest.json`; report is persisted in the canonical artifact map.

## Render

- `work/synthesis_manifest.json` + renderer config + provider adapter mapping
  -> `load_synthesis_manifest(...)`, `RenderContext`
  -> `SegmentRenderer.render(manifest, adapters=providers)`
  -> `work/render/segments/<scene>/<render_unit>.audio` WAV plus adjacent JSON sidecars, and `work/render/render.report.json`
  -> assembly consumes the same manifest and segment root; each request uses `RenderUnit.assigned_provider_voice_id`.

## Assembly

- synthesis manifest + `work/render/segments` + canonical chapter structure from normalized chapter metadata (`chapter_id`, `chapter_order`, `chapter_title`, scene IDs, render-unit IDs)
  -> `ChapterAssembler(manifest, chapter_structure_source=chapter_structure, config=ChapterAssemblyConfig(...))`
  -> `work/assemble/chapters/<chapter>/<assembly_id>.wav`, chapter sidecars, and `chapter_assembly_report.json`
  -> assembly report chapter results become mastering records.

## Mastering

- assembly report `chapter_results` mapped to typed engine-neutral chapter records
  -> `MasteringConfig(source_root=work/assemble, mastering_root=work/master)`
  -> `MasteringEngine(config).master_chapters(records)`
  -> `work/master/mastered/<chapter>/<mastered_id>.wav`, mastering sidecars, and a mastering report
  -> mastered chapter results become packaging `MasteredChapterInput` records.

## Packaging

- mastered chapter results + canonical chapter order + `BookMetadata` + optional `CoverArtInput` + `PackagingConfig(package_root=work/package, mastered_root=work/master, backend=FFmpegPackagingBackend())`
  -> `package_audiobook(chapters, metadata=..., config=..., backend=..., cover_art=...)`
  -> `work/package/<book>/<package_id>.m4b`, package sidecar/report
  -> final artifact path returned to project metadata and build report.

## Orchestrator boundary

- web `ProjectRecord` metadata and settings
  -> typed `BuildRequest` fields: project/book/series identity, normalized story input, planner/editable/manifest/renderer/assembler/mastering/packaging configs, chapter structure, cover art, target, rebuild policy, dry-run, failure policy, workspace root, contract/orchestrator versions
  -> `PipelineOrchestrator.build_storyforge_project(request)`
  -> each `StageContext` supplies build ID, workspace/project/stage/report roots and upstream `StageResult`s
  -> each `StageResult.artifact_refs` and report reference update the authoritative artifact map and downstream input references.

## Registry source

The authoritative existing mechanism is `load_settings().voice_planner.registry_path` (normally `voices/registry.json`), resolved from the configured project/config workspace. It is validated and normalized by `load_voice_registry`; no voice IDs are hard-coded. A live provider discovery result may only be used after normalization into this schema; a failed discovery does not silently replace the configured validated registry.

## Required artifact map

`analysis`, `normalized_analysis`, `generated_voice_plan`, `editable_voice_plan`, `assignment_report`, `synthesis_manifest`, `render_report`, `segment_root`, `assembly_report`, `chapter_root`, `mastering_report`, `mastered_root`, `packaging_report`, `final_m4b`, and `build_report` are relative project-root references. Each is persisted atomically with migration-safe empty/default values and validated before downstream use.
