---
name: verify-kicad-component
description: Audit generated KiCad component libraries for pin-map, footprint, 3D-model, file-layout and regression-test correctness without modifying source files.
---

# Verify KiCad component

Use this skill for read-only review of generated symbols, footprints and 3D models.

## Checks

- Confirm the expected `.kicad_sym`, `.kicad_mod`, STEP and VRML outputs exist and use consistent names.
- Compare pin numbers, names, positions and electrical roles with the source pin map.
- Check footprint pad count, pad numbering, pitch, row spacing, courtyard and reference/value fields.
- Check 3D model placement, scale, orientation, body dimensions and visible pin geometry when a model is present.
- Verify that output paths are portable and do not contain local secrets or machine-specific configuration.
- Run the relevant pytest tests and report failures with file paths and line numbers.

## Reporting

Separate confirmed facts, estimates and missing evidence. Report each issue with severity and a concrete suggested correction. Do not edit files, regenerate output or silently repair a failing component during an audit.