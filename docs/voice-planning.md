# Voice Planning

StoryForge voice planning separates assignment logic from speech synthesis.

## Inputs

- normalized story analysis
- voice registry metadata
- series-level bindings
- book-level editable overrides
- scoring, budget, and conflict configuration

## Rules

- preserve narrator continuity where possible
- keep locked assignments intact
- prefer inherited series bindings over new assignments
- avoid same-scene collisions when a better alternative exists
- resolve ties deterministically

## Editable plan behavior

Manual edits are first-class. If a manual choice conflicts with a lower-priority rule, the planner reports the conflict
rather than silently overwriting the edit.

## Registry and budget

The registry stores provider-agnostic capability metadata such as provider voice ID, language support, quality score,
and supported controls. The budget keeps scarce or high-value voices available for important roles.

## Output

The planner emits:

- `voice_plan.json`
- a planning report with warnings, conflicts, and assignment statistics
