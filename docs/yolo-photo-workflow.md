# Local YOLO detector, AI review and KiCad / FreeCAD

The photo inventory of a board starts with a **local detector** that proposes
the mounted bodies on the calibrated top photograph; the paid AI calls come
after it, on what the user kept. This page describes the detector choices,
the weights files, their settings and what the program does with the
proposals. The whole pipeline is in `photo-mechanical-workflow.md` (Italian);
the local SMD network in `CLASSIFICATORE_SMD.md` (Italian).

Launch normally with `python main.py`: when the project's `.venv` Python
exists, the launcher uses it before importing GUI dependencies; no global
packages are installed. `python main.py --check-runtime` checks imports
without opening the GUI, reading `.env` or making AI calls.

## Choosing the detector

In the Project tab, step **Mounting and analysis**, the collapsible box
**AI tools** has the row **Photo detection** (boards only; the two
single-component types never run the inventory):

| Entry | What runs |
|---|---|
| **YOLO + local SMD network (v3)** | default from the sources. YOLO proposes, the local SMD network classifies the kept bodies, only unresolved bodies go to the paid crop reading. Disabled in the installed Windows program (no PyTorch, no checkpoint) with the reason in its tooltip. |
| **Local YOLO + AI verification** | default in the installed program. YOLO proposes, the AI inventory and the crop reading verify. |
| **AI (current)** | no local detector: the AI localizes on the full view and detail grid. |
| **Vision model only (no local detector)** | the vision model alone. |
| **Local YOLO only (pretest, no AI)** | a pretest of the detector: shows how the bodies are found and nothing else. No AI call of any kind. |

The row **YOLO model** gives the weights file; empty, the program uses
`models/yolo/pcb_yolov8s.onnx` (or the `KICADGEN_YOLO_MODEL` variable) for
the ONNX routes and `models/yolo/pcb_component_detector_yolov8m_best.pt` for
«YOLO + local SMD network». The button beside it opens **Choose the YOLO
model**: on the left the weights files of the models folder (plus the one in
use and any file added with «Other file...»), on the right format, size, date,
origin, family, licence and training data when the project knows the file by
SHA-256, the classes it detects and whether the detector can use it. An ONNX
file is checked as soon as it is selected; a `.pt` file is a Python pickle
that runs code when loaded, so it is checked only when you press **Check
compatibility**, never because it sits in a folder. An incompatible or
missing model or runtime is rejected before any paid call.

**YOLO settings** (button next to the threshold) opens the detector's
settings: confidence (default 0.25), overlap / NMS IoU (default 0.5) and the
magnified tiles (640 and/or 1280 px) that make this detector differ from a
single pass. They apply to every YOLO route. Detector and weights choices are
per session; projects remember the detector entry by value.

Starting an analysis always requires an explicit click on **Extract parameters
with AI**. A local detector selection is not an offline-only analysis nor a
cost cap: after it, the routes with AI send the overview and the crops to the
provider and spend credit; the pretest does not.

## What happens after the detection

1. The declared top photo (role **High resolution**) is calibrated on the
   board dimensions (axis by axis); the pretest, which reads no documents,
   calibrates it with the **Body length / Body width** fields and asks for
   them when they are not known.
2. YOLO runs on the full overview and on overlapping magnified tiles. Classes,
   scores, input hash, discarded proposals and recipe are written to
   `<project>/photo-cache/yolo-audit/`.
3. The **review window** opens in its **detector stage** with the proposals:
   remove the rectangles that fall on prints, holes or empty pads, draw the
   missing ones (R), use the lasso (L) for groups, then **OK** sends the kept
   proposals on; **Cancel** stops the analysis before paying. The kept
   proposals are saved in `<project>/photo-review/` and can be reopened for
   free with **Component review…**, or sent later to the paid **Rerun AI
   inventory (paid)…**. In the pretest the window is the end: fields,
   table and 3D are not touched.
4. With the AI routes: one global inventory call on the whole board proposes
   the bodies and complete assemblies where YOLO returned fragments; the
   detector's and the AI's readings are reconciled one-to-one (IoU or
   containment with coincident centres). The detector's own reading — class,
   confidence, box — is kept beside the AI's and never overwritten; bodies
   proposed by the AI alone start excluded until kept. Unresolved bodies are
   re-read twice from their enlarged crops with a visual questionnaire; only
   agreeing readings are adopted.
5. With the pause after the inventory (default on), the same window opens in
   its **inventory stage**: reconciled rows, the detector's box as a dotted
   ghost where it differs, markers for warnings, undetermined types and
   detector/AI disagreements, **Parameters…** with the detector, AI and KiCad
   sections, **Choose KiCad model…**.
6. Real library STEP solids are imported without rescaling; unmatched entries
   use generic volumes. A dimensional match is not a verified electrical
   identity.

Top photographs do not establish height, hidden contacts, pin 1, polarity or
SOT-versus-TSOT variants. The photographic long-axis orientation is not a
certified native-model rotation: reviewed rotation and Z overrides are kept
through the table and the CAD payload. An observed edge-mating connector may
overhang the PCB and is not shifted to force its envelope inside the board.

## Local runtime

```powershell
.venv/Scripts/python.exe -m pip install -r requirements/yolo.txt      # .onnx (onnxruntime)
.venv/Scripts/python.exe -m pip install -r requirements/yolo-pt.txt   # .pt (Ultralytics / PyTorch)
```

ONNX Runtime runs on CPU. The ONNX contract is a fixed float32 input
`[1,3,640,640]` and a raw output `[1, 4 + classes, 8400]`; an arbitrary YOLO
ONNX export is not necessarily compatible. No model is downloaded
automatically.

The locally tested ONNX weight originated from
`Arshia82sbn/pcb-yolov8s-detection`, SHA-256
`bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29`. It is
ignored by Git and **not shipped** in the Windows installer by default: its
model-card MIT licence and its embedded AGPL metadata conflict, and the
`build-windows.ps1 -IncludeYoloModel` switch must be used only once
redistribution is allowed (see `PACKAGING-WINDOWS-MSI.md`). Without weights
the program works and asks for a file in the «YOLO model» field.

## ONNX and PyTorch models

The detector loads both formats, chosen by the file extension:

| Extension | Runtime | Output read by the detector |
|---|---|---|
| `.onnx` | onnxruntime (CPU) | raw `[1, 4 + classes, 8400]` |
| `.pt` | PyTorch through Ultralytics (`requirements/yolo-pt.txt`) | standard head (YOLOv8): raw `[1, 4 + classes, 8400]`; end-to-end head (YOLO26, YOLOv10): the official one-to-one output `[1, 4 + classes, 300]` |

Every model goes through the same letterbox, thresholds, NMS, audit and
review code; only the object running the network differs
(`photo_yolo._TorchSession` for `.pt`). PyTorch is not part of the Windows
installer (size; licence still to be decided): there a `.pt` model gives a
clear error and an `.onnx` export of the same model is the way to go.

### Class tables

The detector reads the classes a model declares **by name**, not by position
(`photo_yolo.class_mapping`):

- names match ignoring case (`IC` is `ic`);
- the scores are reordered into the detector's order; classes the model does
  not have stay at zero and are listed in the log and in the audit
  (`missing_classes`, `class_order`);
- a model is refused if it declares a class the detector does not know, the
  same class twice, or indexes that do not start at 0: reading an unknown
  class as another one would label bodies wrongly without any error;
- a model with the detector's own table keeps the plain onnxruntime session.

Limit: without the `pads` and `pins` classes, the recovery of bodies that the
model confused with pads or pins has nothing to compare and stays inactive;
the log says so. End-to-end heads always use the official one-to-one output
(forced explicitly, because Ultralytics changed its default between 8.4.37
and 8.4.163); this head gives only the best class of each detection, so the
same recovery stays inactive there too.

## Logs and reproducibility

The project's `analisi-ai.log` receives the progress stream and is flushed
while work continues. `<project>/photo-cache/yolo-audit/` holds the detector,
coverage and review JSON files: exact prompt, model, per-crop coordinate
transforms, parsed responses and resulting hypotheses, without API keys or
encoded source images. Cached responses are content-addressed by model,
prompt, schema and images; malformed or incomplete responses are not reused
as complete successes. Audit write failures are reported in the progress log.

## Validation notes and limits

- On the ESP32 sample photo (2026-09-19) the detector gave 63 local
  proposals from 30 views in under 7 seconds; the whole RF module scored
  below 0.25 while fragments and false positives remained. This is why the AI
  coverage call and the review stage exist; it is not evidence that YOLO
  finds every component.
- On `AltaRisoluzione2.jpg` the one-to-one head of a YOLO26 `.pt` model
  matched 46/46 bodies against 39/46 for the one-to-many branch with this
  module's NMS: the reason the one-to-one output is forced.
- The software tests replay cached answers: they do not certify that every
  body of a new photograph is found. Verify the rectangles on the photo
  before accepting the CAD.
