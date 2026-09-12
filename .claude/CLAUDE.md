# KiCad Component Generator project guidance

This repository generates KiCad component libraries from mechanical and electrical data.

## Important invariants

- Keep symbol pin numbers and names aligned with the source pinout.
- Distinguish pin pitch from row spacing; never reuse one value for the other without evidence.
- Keep generated footprint, symbol and 3D model names consistent.
- Treat estimated mechanical dimensions as estimates and record them in notes.
- Do not commit `.env`, `config.json`, Python caches, generated output or local KiCad state.

## Validation

Run the focused test suite with `pytest`. For changes affecting geometry, run the relevant CAD/footprint tests and inspect generated output under `generated/` locally.

Use the project skills in `.claude/skills/` for generation and verification workflows. Use the reviewer agent for read-only audits before publishing a component.