# KiCadGen-Tool

KiCadGen-Tool is a desktop application for creating reusable KiCad component
footprints and optional 3D models from datasheets, mechanical drawings and
calibrated product photographs.

The workflow is engineering-oriented: source documents are assigned explicit
roles, AI extracts candidate geometry, code validates the result, and
KiCad-compatible files are generated locally for human review.

> AI-assisted extraction is a starting point, not a metrology instrument.
> Always verify pin numbering, dimensions, orientation and clearances against
> the manufacturer's documentation before using an output in a PCB design.

## What the tool does

~~~mermaid
flowchart LR
    A[Datasheet / drawings] --> D[Guided document slots]
    B[Top-view photo] --> D
    C[Side-view photo] --> D
    D --> E[AI extraction]
    E --> F[Validated parameters]
    F --> G[KiCad footprint]
    F --> H[Optional 3D model]
    G --> I[2D preview]
    H --> J[3D preview]
    I --> K[Human verification]
    J --> K
~~~

Current geometry families:

- regular THT pin-header matrices;
- castellated modules with independent contact rows on each side;
- individual SMD/THT package geometries with explicit pad coordinates.

The application can generate a KiCad .kicad_mod footprint and, when the required
geometry and FreeCAD installation are available, .step, .wrl and preview
metadata for the 3D model.

## Guided source documents

The Project tab currently has exactly three source-document roles:

| Role | Local filename | Intended use |
| --- | --- | --- |
| Pinout | pinout.extension | Contact numbering, labels and pinout dimensions |
| Dimensioned drawing | top.extension | Mechanical dimensions and calibrated XY localization |
| High-resolution photo | hires.extension | Detection of small mounted components |

Supported uploads are PNG, JPEG, WEBP, GIF and PDF files up to 20 MB. Files are
copied into the component docs folder with the canonical role name while
preserving their real extension. Original files are never moved or renamed.
Each role offers preview, replacement, recoverable removal and an explicit
Not available option.

~~~mermaid
flowchart TD
    S[Select source file] --> V{Valid image or PDF?}
    V -- No --> E[Explain the problem]
    V -- Yes --> C[Copy to docs role filename]
    C --> M[Save role metadata]
    M --> P[Preview / replace / remove]
    P --> A[Send explicit roles to AI]
    N[Mark Not available] --> A
~~~

Assignments are authoritative. Photo localization uses the user-assigned top
file and never substitutes a pinout because an AI response guessed
a different image. Existing unassigned documents remain untouched and must be
assigned manually. Replacement and removal archives are retained under
docs/.history/.

## AI-assisted analysis

The pipeline separates three analysis stages:

1. **Datasheet** — dimensions, contact layout, pin map and mechanical data.
2. **Photo localization** — mounted package envelopes on the assigned top view.
3. **Photo verification** — checks whether proposed rectangles contain real bodies.

Each stage has its own model and prompt. Defaults and per-component-type
overrides are stored in the tracked SQLite catalog at
data/analysis_profiles.sqlite3. The Configuration catalog supports creating,
editing, duplicating, deactivating and deleting component types, with inheritance
from global defaults.

The same SQLite catalog stores the user-interface translation keys for Italian,
English, French and German. Select the active language from the selector in the
top-right corner. Advanced users can edit the catalog through
Configuration -> Manage translations; missing entries fall back to English.

~~~mermaid
flowchart LR
    DB[(SQLite profile and translation catalog)] --> R[Resolve model, prompts and UI text]
    R --> D[Datasheet]
    R --> L[Photo localization]
    R --> V[Photo verification]
    D --> X[Structured extraction]
    L --> X
    V --> X
    X --> Q[Code validation]
    Q --> O[Generation or review]
~~~

The API key is read from the local .env file and is never stored in the profile
database. Cached AI responses and generated output are local data.

## Standard and Advanced user modes

~~~mermaid
stateDiagram-v2
    [*] --> Standard
    Standard --> AIExtraction: Run extraction
    AIExtraction --> AutomaticGeneration: Valid result
    AutomaticGeneration --> Preview: Footprint and 3D complete
    AutomaticGeneration --> ReviewRequired: Missing or unsupported data
    ReviewRequired --> Advanced: Enable Advanced user
    Advanced --> ManualReview: Inspect parameters
    ManualReview --> Preview: Generate explicitly
~~~

Standard mode hides the detailed Footprint 2D, Generate, 3D annotation and
component-editing panels while preserving their data and implementation. A valid
extraction starts the existing footprint and 3D generation flow automatically.
Existing output files require confirmation before replacement.

Advanced user mode exposes the detailed editors and the Review photo detection
action. This action inspects and corrects detected component envelopes; it is not
a generation command. The AI Log is available in both modes and includes copy
buttons for analysis and detected elements.

## Installation

Requirements:

- Python 3.11 or newer;
- packages listed in requirements.txt;
- FreeCAD with freecadcmd on PATH for STEP/VRML generation;
- KiCad 8 or newer for inspecting the generated footprint.

~~~bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
~~~

Create a local .env file with an OpenRouter key:

~~~text
OPENROUTER_API_KEY=your-key-here
~~~

Do not commit .env, API keys, generated output or migration backups.

## Run the application

~~~bash
python main.py
~~~

The standard path keeps the first-run experience focused on documents,
extraction and verification. Advanced user mode exposes detailed geometry
editors and manual generation.

## Output structure

~~~text
ComponentName/
├── ComponentName.pretty/
│   └── ComponentName.kicad_mod
├── ComponentName.3dshapes/
│   ├── ComponentName.step
│   ├── ComponentName.wrl
│   └── ComponentName.preview.json
└── docs/
    ├── pinout.extension
    ├── top.extension
    ├── front.extension
    ├── .document-roles.json
    └── .history/
~~~

Generated libraries remain local until the user explicitly exports them to a
KiCad library, keeping unverified results separate from production libraries.

## Review and install in KiCad

After both the footprint and STEP model have been generated, open the **KiCad**
tab. The workflow deliberately separates review from installation:

1. **Test in KiCad** creates an isolated preview project under the generated
   component directory and opens it in the KiCad PCB Editor. It does not edit
   any footprint library table. Press `Alt+3` in KiCad to inspect the real STEP
   model.
2. **Install in KiCad project** creates or updates `KiCadGen.pretty` and
   `KiCadGen.3dshapes` in the selected project and registers the footprint
   library in that project's `fp-lib-table`. The generated 3D reference uses
   `${KIPRJMOD}`, so the project remains portable.
3. **Install as global library** copies the same artifacts to a stable folder
   selected by the user and registers `KiCadGen.pretty` in the detected KiCad
   global library table. Global model references use an absolute path on that
   computer.

Only the current component files are replaced after explicit confirmation;
other components in the library are preserved. Before changing an existing
`fp-lib-table`, the application keeps a one-time `.kicadgen-backup` copy.

The installed library has this structure:

~~~text
KiCad project or global library root/
├── KiCadGen.pretty/
│   └── ComponentName.kicad_mod
├── KiCadGen.3dshapes/
│   ├── ComponentName.step
│   └── ComponentName.wrl
└── fp-lib-table                 # project mode; global table lives in KiCad config
~~~

This is a footprint and 3D-model library. A schematic symbol library
(`.kicad_sym`) is not generated yet; assign the installed footprint to an
existing or custom symbol in the Schematic Editor.

## Repository layout

~~~text
main.py                         GUI entry point
kicadgen/core/                   KiCad data models and S-expression writers
kicadgen/generators/             Parametric footprint generators
kicadgen/cad3d/                 FreeCAD orchestration and 3D builders
kicadgen/services/               AI, profiles, document roles and photo analysis
kicadgen/gui/                    Qt desktop interface and previews
data/analysis_profiles.sqlite3  Versioned AI profile catalog
examples/                       Small command-line examples
tests/                          Automated regression and GUI tests
~~~

## Testing

~~~bash
pytest
~~~

The tests cover serialization, geometry validation, AI response parsing, SQLite
profile CRUD, guided document roles, photo review, automatic generation, 2D/3D
preview behavior and worker lifecycle handling.

## Engineering limitations

- AI output can contain uncertain classifications, estimated package heights or
  incomplete mechanical information.
- A photograph can localize visible bodies but cannot reliably establish hidden
  geometry, electrical identity or certified dimensions.
- FreeCAD is required for the real STEP/VRML generation path.
- Generated footprints and models must be checked against the source datasheet,
  especially pin numbering, pitch, body orientation, solder clearances and 3D
  alignment.
- Datasheets, photographs, product markings, generated models and third-party
  libraries may have separate copyright or usage restrictions.

## Roadmap

The current three-role document workflow is deliberately small and explicit.
Future extensions can add more source roles, richer review tools, additional
geometry families and project-level reproducibility metadata without changing
the existing role contract.

## License

This project is licensed under the MIT License in LICENSE.

Copyright (c) 2026 Mirko Ugolini.

The license applies to this project's source code and documentation. Generated
footprints, 3D models, datasheets, images and other third-party materials may
have separate licenses or usage restrictions and must be checked independently.
