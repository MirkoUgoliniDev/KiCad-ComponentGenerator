# KiCadGen

KiCadGen is a Windows desktop application that builds a KiCad component —
footprint (`.kicad_mod`), electrical symbol (`.kicad_sym`) and STEP model
(`.step`) — from the manufacturer's documents and from photographs of the
part. It reads the documents with an AI model, turns what the model read into
geometry with deterministic rules, lets you review and correct every result,
and installs the component into a KiCad library.

Italian version: [README.it.md](README.it.md).

> AI reading is a starting point, not a measuring instrument. Check pin
> numbering, dimensions, orientation and clearances against the manufacturer's
> documentation before using an output in a board.

## Purpose and non-goals

- **Single components** (through-hole or SMD): pads, holes and body from the
  datasheet; the electrical symbol from the pinout.
- **Boards with mounted components** (modules, breakout boards, with
  through-hole or castellated contacts): besides the footprint and the symbol,
  KiCadGen reconstructs the **mechanical envelopes** of the bodies visible in a
  top photograph — position, size and orientation — so the STEP model shows what
  takes room on the board.
- It is **not** reverse engineering: the bodies are generic volumes, or models
  taken from the KiCad 3D library because their measures match. Exact part
  names, electrical values and markings are not the goal.
- **Heights read from a single top photograph are estimates**, not measures.
  The program says so wherever a height is estimated; verify heights and
  critical distances with dimensioned drawings or a ruler.
- The AI inventory does not guarantee that every body was found, nor that
  every rectangle is a body: the review window exists for that.

## Installation

### Windows installer

Download `KiCadGen-X.Y.Z.msi` from the GitHub Releases and follow
[docs/INSTALLATION-WINDOWS.md](docs/INSTALLATION-WINDOWS.md) (prerequisites,
API keys, where the files go, update and uninstall). How the executable and
the MSI are built: [docs/PACKAGING-WINDOWS-MSI.md](docs/PACKAGING-WINDOWS-MSI.md).

### From the sources

Requirements:

- Python 3.11 or newer (the release build uses 3.13);
- the packages of `requirements.txt` (PyQt6, Pillow, OpenCV, pyvista,
  pyvistaqt, python-dotenv, pytest);
- optional: **FreeCAD** with `freecadcmd` (STEP models). Without it the
  footprint and the symbol are still produced; the progress window says which
  step cannot run and why;
- optional: **KiCad 10 or newer**: its PCB Editor (`pcbnew`) opens «Test in
  KiCad...»; `kicad-cli` is needed for the global library installation and
  for the real loading check of «Check component for KiCad», which without it
  runs the other checks and skips that one. The generated
  files use the KiCad 10 formats (footprint `20260206`, symbol library
  `20251024`), which KiCad 8 and 9 cannot read;
- optional local detectors: `requirements/yolo.txt` (ONNX Runtime, for `.onnx`
  YOLO weights), `requirements/yolo-pt.txt` (Ultralytics/PyTorch, for `.pt`
  weights), `requirements/smd.txt` (PyTorch/torchvision, for the local SMD
  network). The weights are not in the repository: detectors go in
  `models/yolo/`, the SMD network in `models/smd/`.

**Default detector from the sources.** When run from the sources the photo
detector defaults to *YOLO + local SMD network (v3)*, which needs
`models/smd/smd_classifier.pt` and
`models/yolo/pcb_component_detector_yolov8m_best.pt` plus the packages of
both `requirements/smd.txt` and `requirements/yolo-pt.txt`. These weights are
not distributed with the sources and a fresh checkout has no `models/`
folders: create `models/yolo/` and `models/smd/` when you add the files.
Without them a board analysis stops with a warning before any paid call; to
work without local weights choose another entry in **Photo detection** (for
example *AI (current)*) in the **AI tools** box.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

The interpreter is called directly because the default PowerShell execution
policy blocks `.venv\Scripts\Activate.ps1`. `python main.py` also works: it
switches to the project's `.venv` interpreter when it exists.
`python main.py --check-runtime` checks imports and required resources without
opening a window, reading `.env` or calling any AI.

KiCad is found through the `KICAD_CLI_PATH` variable, on PATH, or in
`C:\Program Files` (and `Program Files (x86)`). FreeCAD is **not** looked up on
PATH: it is found through the `FREECAD_CMD_PATH` variable (full path of
`freecadcmd.exe`) or in `C:\Program Files\FreeCAD*\bin\freecadcmd.exe` (and
`Program Files (x86)`).

## AI account and providers

The document analysis is paid and needs an API key. Two providers are
supported, chosen by the model identifier:

- **OpenRouter** (`vendor/model` identifiers, e.g. `anthropic/claude-opus-5`):
  one key for many vendors; the cost of every call is the one OpenRouter
  reports.
- **DeepSeek, direct API** (`deepseek-api/...` identifiers): DeepSeek reports
  no cost, so each call is charged as tokens × DeepSeek's published prices.

The default model depends on how the program is started. **From the
sources** it is `deepseek-api/deepseek-flash` (stored in the profile database
`data/analysis_profiles.sqlite3`), so a DeepSeek key is needed. **Installed
from the MSI** it is `anthropic/claude-opus-5` through OpenRouter (the
installer rebuilds the database from the code). The model is always chosen
from the interface: the default in «Configuration» → «General AI
defaults...», tab «Model», and for the current session in the «AI model»
box; never by editing the database by hand.

**Configuration → AI account...** lists every provider with where its key in
use comes from (environment variable, Windows Credential Manager, `.env` file
or none) and its current balance, read with the providers' free status
endpoints. Paste a key, press **Verify** (no model call, no cost), then
**Save**: the key is stored in the Windows Credential Manager of your user
account (`KiCadGen/OpenRouter`, `KiCadGen/DeepSeek`), never in a file or in
the profile database. **Remove key** deletes that entry only.

The key is looked up in this order: the environment variable
(`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`), the Credential Manager, a `.env`
file (the repository's `.env` from the sources; `%LOCALAPPDATA%\KiCadGen\.env`
in the installed program). `.env.example` is the commented template.

Costs: every paid call is appended to `ai-costs.json` in the project folder,
split by provider; the analysis window shows the cost of the current analysis,
the project total and the account balance («AI credit» in the Project tab).
Answers served from the local cache (`<project>/photo-cache`) cost nothing and
the log says so. Every command that spends says so in its tooltip; review and
3D regeneration commands are free.

## Screens

### Start page and projects

With no project open the window shows a start page: **New project...**
(Ctrl+N), **Open project...** (Ctrl+O) and the list of recent projects (one
click reopens one; the right-click menu removes it from the list). A project
is a folder in the **workspace** (`Documents\KiCadGen` in the installed
program, `generated\` from the sources; the folder button beside the field
chooses another). Its name is also the name of the footprint, the symbol and
the STEP model; **File → Rename project...** (F2) renames the folder and the
files together.

### Menus

- **File**: new, open, rename, recent projects, exit.
- **Configuration**: «Manage AI types and profiles...» (component types and
  their prompts, in the SQLite catalogue `data/analysis_profiles.sqlite3`),
  «General AI defaults...» (the single AI model with its source, OpenRouter or
  DeepSeek, and the base prompts in the tabs Model, Datasheet,
  Photo localization, Photo verification), «AI account...», «Generation
  options...» (NC pins, engraved pin labels, pause after the AI inventory),
  «Accepted matches archive...».
- **Tools**: open the component folder, the three KiCad commands (also in
  the KiCad tab), open the program's data folder and `config.json`.
- **Help** (shown as **?** in Italian): «User guide» (the HTML guide shipped
  with the program, in Italian), «Open log folder», «Report a problem...»,
  «Check for updates...», «Licenses», «About KiCadGen», «Keyboard
  shortcuts...».

### Workflow bar and tabs

Above the tabs, a bar shows the phases of the work — **Documents › AI analysis
› Body review › STEP model › KiCad** — with the completed ones ticked, the
current one highlighted and the ones that do not apply dimmed (the body review
for a single component). Clicking a phase goes to its tab. On the right, the
language selector: Italian, English, French, German (the texts come from the
same SQLite catalogue; missing entries fall back to English).

The tabs are **Project** (Ctrl+1), **Footprint preview** (Ctrl+2),
**Electrical symbol** (Ctrl+3), **STEP model** (Ctrl+4), **KiCad** (Ctrl+5)
and **Exports** (Ctrl+6).

## Inputs

The Project tab guides the preparation in two steps, **Documents** and
**Mounting and analysis**, with **Continue** / **Back** (**Back to
projects** on the first step saves and returns to the start page). The
collapsible panel **Project details** holds name and workspace.

### Documents and roles

**Add…** copies a file (PDF, PNG, JPG, WEBP, GIF) into the project's
`docs` folder; the original is not touched. Each file gets a card where you
tick the roles it carries:

| Role | Content |
|---|---|
| Pinout | numbering and position of the contacts; the model says from which side the drawing is seen and the code mirrors it |
| Dimensions | the dimensioned top view: body length and width, holes, row spacing |
| High resolution | a real top photograph: the only role sent to the component detector and to the calibration; images only |
| Land pattern | the recommended land pattern |
| Side view | heights: body, components, board thickness |
| Datasheet | the complete manufacturer datasheet; a PDF always takes this role alone, and the extraction looks inside it for pinout, dimensions, land pattern and side view |

A file without roles blocks the analysis until you tick one or remove it. The
card's buttons replace the file (keeping its roles), open a zoomable preview
or remove the local copy (recoverable from `docs/.history`). A role nobody
provides is «not provided»: the model knows it and does not substitute
another file.

Top photographs are calibrated on the board dimensions, axis by axis. They
must be taken from above, nearly orthogonal, with the board's long side
horizontal and the outline distinguishable from the background; perspective
is not corrected, a tilted photo is refused. Every image is re-encoded
**without metadata** (GPS, device) before it reaches a provider; an image
whose metadata cannot be removed is refused with the request to save it as
JPEG or PNG.

### Component type and manual parameters

**Component type** tells the model what it is reading:

- *Single through-hole component (THT)* — pads with holes; the group
  «THT component pads» (pad Ø, hole Ø, «Regenerate footprint»);
- *Single SMD component* — no holes, pads from the land pattern;
- *Board with components · through-hole connectors* and *Board with
  components · castellated connectors (half-holes)* — in addition to the footprint, the
  photo inventory of the mounted bodies runs.

The analysis is blocked without a component type. For boards, **Mounting
on the host board** chooses how the module sits on the host board
(direct soldering / module pads; strip header with round or square pads, with
pad and hole diameters); **Show headers in the STEP model** adds the male
headers under the board in the STEP model only (on castellated boards, only on
contacts that have a through hole). **PCB thickness** shows the board
thickness with its provenance (from the documents, estimated by the AI, or set
by you; «Reset» goes back).

The pin-header parameters (rows, columns, pitch, row spacing, pad and hole
diameters) and the castellated parameters (body, pad size, overhang, half-hole
diameter, board thickness) are kept as project data with validated ranges;
the analysis fills them and the footprint preview edits the host pads. A pin
header entered by hand without an analysis gets a correct footprint but **no
3D body** (user decision of 2026-10-15); it still asks for the keywords before
installation.

The collapsible **AI tools** box shows the detector and the model in use:

- **Photo detection** (boards only): *YOLO + local SMD network (v3)* (default
  from the sources: the local network classifies the kept bodies and only the
  ones it does not resolve go to the AI), *Local YOLO + AI verification*
  (default in the installed program, where PyTorch is not shipped), *AI
  (current)*, *Vision model only (no local detector)*, *Local YOLO only
  (pretest, no AI)*. The *YOLO model* row opens a picker that shows, for every
  weights file, format, size, origin, licence, classes and compatibility; a
  `.pt` YOLO checkpoint runs code when loaded, so it is checked only on «Check
  compatibility». The *SMD network model* row opens a separate picker that
  shows size, date, SHA-256 and what the checkpoint declares about itself
  (architecture, training, classes, rotation kinds, threshold, metrics); it
  reads the checkpoint with `weights_only=True`, so no code from the file runs,
  and has no «Check compatibility» button. See
  [docs/yolo-photo-workflow.md](docs/yolo-photo-workflow.md) and
  [docs/CLASSIFICATORE_SMD.md](docs/CLASSIFICATORE_SMD.md).
- **AI model**: the single model used for documents, photos and
  verification; the button beside it changes it for this session only
  (filter by cost, source OpenRouter or DeepSeek).

## Analysis

**Extract parameters with AI** starts the paid analysis. A progress window lists
the phases and who works in each one — *AI model*, *Crop reading*,
*Marking reading* (remote, paid), *YOLO*, *Local SMD network*, *OpenCV*,
*KiCadGen (2D footprint)*, *KiCad STEP model library*, *Accepted matches*,
*FreeCAD* (local) — with the cost of the analysis; **Stop** stops it
(a request already sent must finish; nothing more is attempted). The AI log
(Ctrl+L) is written to `analisi-ai.log` in the project folder.

What happens, in order:

1. **Documents**: dimensions, contacts, pin map, board thickness and their
   provenance. A new analysis that would discard pin names you typed asks
   first. Doubts about the package orientation (body covering the pads,
   mirrored bottom view) are asked, never resolved silently. Pin header pitch
   and row spacing are kept distinct; a row spacing that was not read does not
   place holes, and a pad width equal to the pitch is not taken as a measure.
2. **Castellated contacts**: the AI counts the half-holes in its first call;
   the photo then counts the notches of the outline on every edge. A side
   declared on an edge without notches is dropped, a disagreeing count is
   recounted by the model on the enlarged edge (only the contradicted sides),
   and on confirmed sides the lands are **measured** on the photo (recorded as
   «measured», never overriding a dimension read in the documents).
3. **Local detector** (boards): YOLO proposes the bodies on the calibrated
   photo. The **review window** opens in its first stage with the detector's
   proposals: remove rectangles that fall on prints, holes or empty pads, draw
   missing ones, then OK sends the kept proposals on; Cancel stops before
   paying. The proposals are saved in `photo-review/` and can be reopened for
   free.
4. **AI inventory**: one global call on the whole board, then — for the
   bodies the local SMD network did not resolve — each body is re-read twice
   from its own enlarged crop with a visual questionnaire; the class is
   derived by the program from the observations, and only agreeing readings
   are adopted. Contradictions with the inventory are flagged for review.
   Bodies proposed by the AI alone are checked against the local detector and
   start excluded until you keep them. Estimated sizes are never used as
   measures.
5. **Pause after the inventory** (on by default, Configuration → Generation
   options...): the review window opens in its second stage with the
   reconciled bodies, the detector's reading kept beside the AI's (never
   overwritten), the detector's original box as a dotted ghost, markers for
   rows with warnings or an undetermined type.
6. **Generation**: footprint, candidate STEP models from the KiCad library
   (FreeCAD measures the ones not cached yet), the STEP solid with FreeCAD.

### The review window

One window, two stages. Toolbar: **Add component** (R), **Delete
selected** (Del), **Lasso** (L), **Keep or leave** the bodies added by the
model alone (T), **Copy format** (copy sizes, height, type, package and
KiCad model onto other bodies), zoom, **Fit photo** (F), photo visibility
(P: full, attenuated, hidden), details, model doubts. Centre and sides are
corrected by dragging the rectangle and its handles; the rotation only through
the numeric «Rotation» field of **Parameters…**, which opens a
modal with three read-only sections (local detector, AI, KiCad model) and the
editable corrections, plus **Choose KiCad model…**. **Apply corrections**
saves; **Cancel** leaves everything as it was. Later, from the Project tab,
**Component review…** reopens the saved stage for free and **Rerun AI
inventory (paid)…** repeats only the AI inventory on the saved
proposals.

## Outputs

### Footprint preview

The 2D drawing in scale with dimensions and pin numbers, a grid (1 mm or
0.1 in) with a fine dotted sub-grid for drawings, KiCad layer colours and a
kept camera. The preview is a **draft** like the symbol: the Reference and
Value texts can be dragged, drawings added from the palette, host pads edited
(«Host-board footprint»: direct soldering or strip header, pad and
hole diameters), with undo/redo; **Apply** writes the `.kicad_mod`,
**Cancel** discards, **Recover draft** restores an unapplied draft after a
crash (`.footprint-draft.json` in the project). «Update footprint only»
rewrites the file from the mounting choice without calling the AI or
rebuilding the 3D.

### Electrical symbol

The pin numbers are the footprint's pads and cannot be changed in the table;
names and electrical types can. The editor moves pins freely (connection point
on the 0.1 in grid), rotates them, resizes the rectangle, selects groups,
aligns and distributes, adds wires and glyphs from the catalogue, with
undo/redo, a draft recovered after a crash, and **Apply** / **Cancel**.
**Check symbol** reports missing names, overlapping texts and unverified
passive types; the same check precedes the export. **Suggest pin types with
AI** proposes electrical types from the pinout or datasheet documents (paid;
the proposals carry source and reason and are selected one by one).
**Export KiCad symbol** writes the `.kicad_sym` with the footprint link
`KiCadGen:<name>`. The initial types are *passive*: verify them on the
datasheet before relying on KiCad's ERC.

### STEP model

Rotate with the mouse, the navigation cube for the standard views. Toolbar:
pin numbers (visible from the pin side, a practical check of mirroring),
pinout table, fit, open the `3dshapes` folder, **Regenerate STEP model with
these annotations** (F5, free), **I like it — remember these models…** (saves the accepted
body/model matches for future boards), **Copy format**. **Edit
components…** (Ctrl+E, or right click on a body) edits name, type, position,
sizes, rotation, colour and marking by hand, also without the photo; the
right-click menu also offers *Remove from the STEP model (false positive)*,
*Put back into the STEP model*, *Rotate the solid*, *Change KiCad model...*.

**STEP model choice** shows up to five candidates of the KiCad library
with their measures beside the photo crop; the preview is oriented as mounted,
with manual or automatic rotation. A dimensional match is not a verified
electrical identity: the report distinguishes `auto_matched` from
`confirmed`. Details: [docs/interactive-kicad-models.md](docs/interactive-kicad-models.md).

Mounted body heights, hidden contacts, pin 1 and polarity are not established
by a top photograph: they stay to be verified.

### Project folder

```text
<Name>/
├── <Name>.pretty/<Name>.kicad_mod
├── <Name>.3dshapes/<Name>.step, .wrl, .preview.json
├── <Name>.kicad_sym                (after «Export KiCad symbol»)
├── project_config.json             (the project file: settings, tables, annotations)
├── docs/                           (your documents, .document-roles.json, .history/)
├── photo-cache/                    (AI answers, keyed by model, prompt and pixels; no keys)
├── photo-review/                   (saved detector proposals and reviews)
├── yolo-pretest/                   (photo-review/ and photo-cache/ of «Local YOLO only»)
├── kicad-model-candidates.json, kicad-model-choices.json
├── consolidated-matches.json       (comparison with the accepted matches)
├── .header-model-reuse.json        (reused pin-header STEP model, boards with headers)
├── ai-costs.json, analisi-ai.log, freecad.log
└── .kicad-preview/                 (the isolated test project)
```

Generated files stay local until you install them explicitly.

## The KiCad tab

**Files ready for KiCad** lists footprint, symbol and STEP with their state.
Installation and the isolated test are **blocked until the files on disk
match the parameters** and every block names its remedy, enabled while the
block stands: *Apply to files* (metadata, footprint) and *Update the symbol
file* in this tab; for a STEP that no longer matches (also after a change of
colours or 3D annotations only) the regenerate button of the STEP model tab
toolbar («Regenerate STEP model with these annotations»), or *Remove the
obsolete STEP model* here (a stale STEP that cannot be rebuilt).
Projects saved by older versions may need one «Apply to files». The commands
are also blocked while a background job runs. They use the **applied** files:
an unapplied footprint or symbol draft is not installed, is not a block, and
is reported by «Check component for KiCad».

**Component metadata** (collapsed by default): reference designator
(letters only; the grey proposal counts as filled), description, keywords,
datasheet (URL or file), footprint filters. Designator, description, keywords
and datasheet are **required** before installation. The list **KiCad checks
(KLC)** updates while you type (⛔ error, ⚠ warning) and «Apply to files»
writes the values into the footprint and the symbol.

**Review before installation**:

- **Test in KiCad...** creates a small temporary project in the component's
  `.kicad-preview` folder, with the footprint embedded and a local copy of the
  STEP referenced through `${KIPRJMOD}`, and opens it in the PCB Editor
  (Alt+3 for the 3D view). **No library table is touched.**
- **Check component for KiCad** runs read-only checks in a worker and
  shows a copyable report: footprint on disk, pin–pad correspondence,
  original 3D link, STEP integrity, the exported symbol compared with the
  applied one, and — when `kicad-cli` is available — the real opening of
  footprint, symbol and 3D import in a temporary folder. It does not certify
  pinout, measures or visual alignment; ERC and DRC stay on the final design.
- **Quick guide to using the component in KiCad** opens a five-page wizard on using the component in KiCad.

**Library installation**:

| Command | What it writes |
|---|---|
| **Install in KiCad project...** (recommended) | creates or updates `KiCadGen.pretty` and `KiCadGen.3dshapes` in the chosen KiCad project and registers the footprint library in that project's `fp-lib-table` with `${KIPRJMOD}` paths, so the project stays portable |
| **Install as global library...** | copies the same files to a stable folder you choose and registers `KiCadGen.pretty` in the per-user global `fp-lib-table` of the installed KiCad (found through `kicad-cli version`), with absolute paths. It is refused while that table does not exist yet: start KiCad once, complete its initial library setup, then retry |

Only the current component's files are replaced, after explicit confirmation;
other components and other library rows are preserved. Before changing a
table the program keeps `<table>.kicadgen-backup` (rotated), writes through a
scratch file and rolls back everything it touched if a step fails. A table
that is not UTF-8 or a write refused by the operating system is reported with
the real path.

The **symbol** is not registered in any `sym-lib-table`: export the
`.kicad_sym` and add it in KiCad with *Manage Symbol Libraries*, then set
the symbol's Footprint to `KiCadGen:<name>`.

The **Exports** tab keeps the history of every installation and symbol
export made from this computer (component, kind, destination, date).

## Logs, problems, updates

| File | Where | Content |
|---|---|---|
| `analisi-ai.log` | project folder | the analysis diary: steps, models, costs, warnings |
| `freecad.log` | project folder | FreeCAD's output while building the 3D |
| `crash-reports/` | `%LOCALAPPDATA%\KiCadGen\logs` (installed) | one file per unhandled error: version, system, traceback; only the newest 20 are kept |

**Help → Report a problem...** opens a new GitHub issue prefilled with versions
only and opens the log folder: you attach what you want after reading it.
**Help → Check for updates...** compares this version with the latest GitHub
release, only when you ask. The only network call made at start-up reads the
balance of the AI provider (OpenRouter or DeepSeek) whose API key is
configured; without a key nothing is sent. No other call is made at start-up.

## Limits and known heuristics

- The AI can misread: always check the previews before using a result.
- Heights from a single top photograph are estimates; so is a board
  thickness not quoted in the documents (the standard value is used and
  logged).
- The photo inventory can miss bodies or keep non-bodies; the review window
  and «Edit components…» exist for that. The number of bodies found is
  not a measure of accuracy.
- The notch counting of castellated contacts counts open half-holes only; a
  run touching an end of the edge gives a lower bound, not a count.
- The orientation doubt for packages relies on the IPC assumption «pad width
  ≈ lead width»; a pad read exactly on the body edge can raise a false doubt
  and a transposed TSSOP is not detectable.
- The KiCad library matcher compares measures (30 % tolerance, at most 64
  candidates measured per search): a dimensional match is not an identity.
- Documents, photographs, generated models and third-party libraries may have
  their own copyright or usage restrictions.

## Repository layout

```text
main.py                          GUI entry point (uses .venv when present)
kicadgen/core/                   KiCad data models and S-expression writers
kicadgen/generators/             footprint and symbol generators
kicadgen/cad3d/                  FreeCAD orchestration, KiCad model matcher
kicadgen/services/               AI providers, profiles, documents, photo pipeline, KiCad library
kicadgen/gui/                    Qt interface, previews, help/guida.html
data/analysis_profiles.sqlite3   profile and translation catalogue (tracked)
docs/                            user and maintainer documentation
examples/, tools/                command-line examples and maintenance tools
packaging/, scripts/             PyInstaller spec, WiX source, build and release scripts
tests/                           regression and GUI tests
```

## Testing

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-run
```

The suite is large (about 8 500 tests) and is run in two halves in CI
(`tools/run_test_halves.py`) because a single process can die with a native
Qt/VTK crash. Use a fresh temporary directory for every run.

## Licensing

KiCadGen's source code and documentation are licensed under the
**GNU Affero General Public License v3.0** (`LICENSE`).
Copyright (c) 2026 Mirko Ugolini.

The project previously used the MIT License; it moved to AGPL-3.0 on
2026-09-25 because it uses Ultralytics (AGPL-3.0) for `.pt` YOLO models and the
YOLO weights it works with declare AGPL-3.0.

The YOLO model weights and the training dataset **have their own licenses,
which may differ from the code's**. They are listed one by one in
`THIRD_PARTY_NOTICES.md`, together with the Python dependencies. In short:

- No weights are stored in this repository (`.pt` and `.onnx` files under
  `models/` are ignored by Git; detectors go in `models/yolo/`, the SMD
  network in `models/smd/`). The checkpoints used during development declare
  AGPL-3.0 in their metadata. The license of the base ONNX model is still
  unresolved (MIT model card, AGPL-3.0 metadata).
- The Roboflow YOLO26 models were trained on the
  [CompDetect dataset](https://universe.roboflow.com/dataset-lmrsw/compdetect),
  licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  The dataset was exported from Roboflow for training; the preprocessing
  actually applied (resize, augmentation, conversion, filtering, class
  changes) **must be recorded** in `THIRD_PARTY_NOTICES.md` and is still
  pending. The dataset is not redistributed here, and its authors do not
  endorse this project.
- FreeCAD and KiCad are separate programs with their own licenses and are not
  included.
- The Windows installer also bundles the CPython runtime, the Qt 6 libraries,
  PyQt6 and the FFmpeg library shipped with OpenCV, each under its own license
  (PSF-2.0, LGPL-3.0, GPL-3.0, LGPL-2.1); their license files are copied into
  the installed program's `_internal\licenses` folder, and
  `THIRD_PARTY_NOTICES.md` lists them with the upstream sources.

Generated footprints, STEP models, and the datasheets and photos you provide may
have their own licenses or usage restrictions: check them yourself. This
section does not state that any use, commercial or otherwise, is allowed
beyond what the individual licenses say.
