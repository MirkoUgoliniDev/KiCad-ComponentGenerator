# KiCad Component Generator

Python application for generating complete KiCad component libraries from datasheets, mechanical drawings and product images.

The generator can produce KiCad footprints (`.kicad_mod`), symbols (`.kicad_sym`) and optional parametric 3D models (`.step` and `.wrl`). It includes a GUI for component data entry and previews, reusable geometry/serialization code, datasheet extraction services and automated tests.

## Features

- Parametric THT pin-header and module footprint generation
- KiCad S-expression serialization helpers
- Datasheet and image-based geometry extraction workflow
- FreeCAD-based STEP/VRML model generation
- Pinout and 3D preview support
- Automated validation for pin maps, footprints, annotations and CAD output
- Calibrated photo inventories with simple mechanical envelopes, response caching,
  per-stage logs and a draggable photo-review editor (no component markings)

See [Photo mechanical workflow](docs/photo-mechanical-workflow.md) for operation,
reproducible checks, measured example results and limitations of height estimates.

## Repository layout

```text
main.py                         Application entry point
kicadgen/core/                   KiCad data models and S-expression writers
kicadgen/generators/             Parametric component generators
kicadgen/cad3d/                 FreeCAD and 3D model generation
kicadgen/services/               Datasheet extraction services
kicadgen/gui/                    Desktop GUI and previews
examples/                       Small usage examples
tests/                          Automated tests
generated/                      Local generated output (ignored by Git)
.claude/                        Project-specific skills and review agents
```

## Requirements

- Python 3.11 or newer
- Packages listed in `requirements.txt`
- FreeCAD with `freecadcmd` available when generating 3D models
- KiCad 8 or newer for inspecting generated libraries

Install Python dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run the application

```bash
python main.py
```

The GUI uses local settings and prompts. Keep `.env` and `config.json` local; they are intentionally excluded from version control.

### Component types and AI profiles (SQLite)

Open **Configurazione → Gestione tipologie e profili AI…** to create, edit,
duplicate, deactivate or delete component types. Double-click a row to edit it.
Each type maps to an existing supported geometry family; adding a type does not
add a new generator. Confirmed operations are saved immediately; **Annulla** in
an editor discards its unsaved changes. Deletion requires confirmation and does
not remove generated components.

The three analyses — **Datasheet**, **Localizzazione foto**, **Verifica foto** —
have independent models and base prompts. Per-type fields can inherit the global
default (**Eredita default generale**) or override it. Datasheet type instructions
are added to the editable component instructions in Progetto. Photo instructions
are appended to photographic prompts, including verification; advanced terminal
and lead prompts live under Localizzazione foto. Schema and geometry validation
remain in code. The Info icons open modal explanations without cluttering editors.

Model selections in Progetto override only the current session. Each extraction
receives a resolved snapshot, so later profile edits do not change an ongoing
request. **Default generali AI…** is a shortcut for editing the global models and
the three main prompts; the catalog editor exposes all profile fields.

When no profile database exists, the app imports existing models/prompts and the four original
types into `data/analysis_profiles.sqlite3`. The verification model initially
matches the previous datasheet default, preserving the initial model choice.
Before import, the original JSON is copied to `data/config-before-sqlite-<hash>.json`.
Migration runs only once; an existing versioned database is reused instead of
importing local JSON values. Later JSON saves contain UI preferences, not AI profiles.
The API key remains in `.env`. The profile database `data/analysis_profiles.sqlite3`
is versioned with its configured types, prompts and models. Migration backups and
SQLite journal/WAL files remain git-ignored. Changes made through the profile editor
modify this tracked database; review them before committing or pulling updates.

Use **Backup DB…** to save a consistent SQLite copy to a new filename. To restore,
close the application, preserve the current DB, and replace it with a verified
backup. Unknown/newer or damaged databases are reported rather than silently reset.
`KICADGEN_PROFILE_DB` can select another database path; `KICADGEN_CONFIG_PATH`
selects the preferences/migration source. Tests isolate both paths automatically.

### Standard and advanced workflow

In **Configurazione → Opzioni di generazione**, **Advanced user** controls the
workflow (saved in UI preferences, off by default). Standard mode hides
**Footprint 2D** and **Genera**, keeping their data intact. A successful, validated
AI extraction automatically starts footprint and 3D generation; progress remains
visible, and the app opens the resulting preview. Existing output files require
confirmation before automatic overwriting. Changing mode alone never generates files.

Missing required dimensions or unsupported 3D geometries stop automatic generation
with an explanation. Enable Advanced user and repeat the analysis to review the
parameters and use the existing manual workflow. A CAD failure is reported separately
from an already generated footprint. No new geometry generators are introduced.

## Guided source documents

The Project tab has three document slots: **Pinout**, **Top view**, and **Side view**.
Use **Carica** to assign a file to each slot, or mark it **Non disponibile** when
that view is unavailable. At least one document is required before AI analysis.

Uploads are copied into the component's `docs/` directory as `pinout`, `top`, and
`front`, preserving the actual extension (PNG, JPEG, WEBP, GIF or PDF, up to 20 MB).
Original files are not moved or renamed. Each slot offers a preview, replacement,
and removal; PDF previews show the first page. Replacements and removals require
confirmation and retain recoverable copies in `docs/.history/`.

Role assignments persist in `docs/.document-roles.json` and are sent explicitly
to the AI. Photo localization uses the user-assigned **top** image, not an AI guess
or a different view. This stage requires a PNG/JPEG/WEBP top image; PDFs remain
available to the general extraction stage. `front` is the conventional filename
for a side view, not a guarantee of a particular viewing direction.

Existing documents are preserved: assign them through **Carica** before the next
analysis. Unassigned documents and history copies are not sent to the AI. The Info
button explains the roles and limitations. No additional document roles are enabled.

## Run tests

```bash
pytest
```

Run a focused test file when iterating:

```bash
pytest tests/test_footprint.py
pytest tests/test_cad3d.py
```

## Generated library layout

A generated component normally follows this structure:

```text
ComponentName/
├── ComponentName.kicad_sym
├── ComponentName.pretty/
│   └── ComponentName.kicad_mod
├── ComponentName.3dshapes/
│   ├── ComponentName.step
│   └── ComponentName.wrl
└── docs/
```

Always verify pin numbering, pitch, row spacing, body dimensions, model orientation and mechanical clearances against the manufacturer documentation before using a generated component in a PCB.

## Notes on estimates

If the source does not provide a dimension, the generator may use a documented engineering estimate. Estimates must remain clearly identified and must not be treated as manufacturer-certified geometry.

## License

No project license has been assigned yet. Add an explicit license before redistributing the code or generated libraries.
