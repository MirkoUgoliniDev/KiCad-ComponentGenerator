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