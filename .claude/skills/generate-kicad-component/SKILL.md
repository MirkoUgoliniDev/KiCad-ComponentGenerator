---
name: generate-kicad-component
description: Generate or update KiCad symbols, footprints and optional parametric 3D models from datasheet and mechanical data in this repository.
---

# Generate KiCad component

Use this skill when implementing or adapting a component generator in this repository.

## Workflow

1. Inspect the existing generator, data model and tests before changing code.
2. Extract only evidenced dimensions and pin information from the source material. Keep pitch, row spacing, body dimensions and pin length as separate values.
3. Reuse the repository serializers and generator APIs instead of writing KiCad S-expressions ad hoc.
4. Generate the symbol/footprint and, when requested and supported, the STEP/VRML model using the existing FreeCAD integration.
5. Keep output names and folder layout KiCad-compatible: symbol file, `.pretty` footprint directory, `.3dshapes` model directory, and optional docs/verification files.
6. Record estimates and missing source data in component notes or documentation; do not present estimates as manufacturer-certified dimensions.
7. Run the smallest relevant tests, then the full test suite when the shared core or CAD code changes.
8. When delivery is requested, use the KiCad tab workflow: review in an
   isolated preview first, then install into a project or global library only
   after explicit confirmation.

## Geometry rules

- `pitch` is the spacing between adjacent pins in one row.
- `row_spacing` is the perpendicular distance between pin rows.
- `body_height` is the vertical case thickness and must not be confused with `row_spacing`.
- Use populated-pin lists when a matrix has physically missing positions.
- Add fillets, body colors and text colors only when supported by a product photo or explicit source data.

## Safety

Do not overwrite source documents or unrelated generated components. Do not read or commit secrets from `.env` or `config.json`.

For library delivery, use `kicadgen.services.kicad_library` rather than hand-
writing KiCad library tables. Preserve unrelated `fp-lib-table` entries, use
`${KIPRJMOD}` for project model references, and keep staging rollback-safe.
Do not change LLM prompts as part of packaging work.
