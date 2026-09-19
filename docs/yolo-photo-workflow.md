# Local YOLO + AI review + KiCad / FreeCAD

The default photo detector **YOLO locale + verifica AI** is available in the
analysis panel. Restart the application after updating Python sources.
YOLO plus AI review is selected on every new app window, as requested by the
user. The previous AI-only detector remains selectable. Alternative detector
and ONNX file choices apply only to the current session; saved LLM model
settings are not changed. Starting analysis still requires an explicit click.

Launch normally with `python main.py`: when the project's `.venv` Python exists,
the launcher uses it automatically before importing GUI dependencies. No global
packages are installed. With no local environment it keeps the current Python.
`python main.py --check-runtime` checks imports without opening the GUI, reading
`.env` or making AI calls.

## Running it

1. Select the populated-board component type and provide mechanical dimensions,
   pinout and a high-resolution top-side photograph in their appropriate roles.
2. Select **YOLO locale + verifica AI**. The optional ONNX picker overrides the
   default `models/pcb_yolov8s.onnx` or `KICADGEN_YOLO_MODEL` environment setting.
3. Check **Verifica foto**: this configured visual AI model reviews coverage
   and the crops. The initial document analysis still uses its configured model.
4. Start extraction, inspect the photographic overlay and uncertain bodies,
   correct measurements where necessary, then generate and inspect the 3D model.

An incompatible/missing local model or runtime is rejected before paid calls;
previous displayed results remain available. After local inference, the overview
and component crops are sent to OpenRouter. This hybrid flow was explicitly
authorized by the user. AI calls consume provider credit; local inference does
not. Existing API credit/error checks remain in use. Do not interpret a local
YOLO selection as an offline-only analysis or a cost cap.

## Local runtime

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-yolo.txt
```

The tested environment uses ONNX Runtime 1.30.0 on CPU (four threads). The model
contract is a fixed float32 input `[1,3,640,640]`, output `[1,25,8400]`, and the
21 PCB classes validated by `photo_yolo.py`. An arbitrary YOLO ONNX export is
not necessarily compatible. No automatic model download is performed.

The locally tested weight originated from `Arshia82sbn/pcb-yolov8s-detection`:
SHA-256 `bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29`.
It is ignored by Git. Model-card MIT and embedded AGPL metadata conflict;
resolve licensing before redistribution. The binary is not part of this change.

## Evidence flow

- The existing photo preparation checks/calibration convert image coordinates
  to millimetres. Production does not use the manually rectified trial photo.
- Local YOLO uses the full overview and overlapping 640/1280-pixel windows;
  generic thresholds are confidence 0.25 and class-agnostic NMS IoU 0.5.
  Classes, scores, input hash, discarded proposals and recipe are audited.
- One overview AI call proposes missing bodies or complete assemblies where
  YOLO returned fragments. These remain labelled AI proposals. Similar envelopes
  are deduplicated; contained boxes are not blindly deleted.
- Batches of eight padded crops are reviewed for mounted-body presence, physical
  family, visible markings, body-versus-lead bounds, contact arrangement and
  package evidence. Invalid/missing answers retain uncertain candidates. Only
  explicit high-confidence non-component votes delete a proposal.
- Optional crop-relative boundary refinement is checked against the original
  target and converted deterministically back to board coordinates. It cannot
  manufacture nominal dimensions from a desired package name.
- The existing terminal observation and KiCad resolver stages remain in use.
  Real library STEP solids are imported without rescaling; unmatched entries use
  approximations. A dimensional match is not a verified electrical identity.

Top photographs do not establish height, hidden contacts, pin 1, polarity or
SOT-versus-TSOT variants. Photographic long-axis orientation is not a certified
native-model rotation. Reviewed native rotation/Z overrides are retained through
the table and CAD payload; absent rotation overrides keep existing axis inference.
An observed edge-mating connector may overhang the PCB and is not shifted merely
to force its whole envelope inside the substrate.

## Logs and reproducibility

The component's `analisi-ai.log` receives the existing GUI progress stream and
is flushed for reading while work continues. `generated/photo-cache/yolo-audit/`
(or the supplied cache directory) contains unique detector, coverage and review
JSON files. Review audits preserve the exact prompt, model, individual crop
coordinate transforms, parsed responses and resulting hypotheses. They do not
contain API keys or encoded source images. Cached responses are content-addressed
by model, prompt, schema and images; malformed or incomplete responses are not
reused as complete successes. Audit write failures are reported in progress logs.

## Actual validation and limits (2026-09-19)

The app-calibrated ESP32 photo produced 63 local **proposals** from 30 views in
6.827 seconds. The whole RF module scored below 0.25, while fragments and false
positives remained. This motivated the explicit AI coverage check; it is not
evidence that YOLO found all components. The new AI calls have been exercised
with offline mocks, not a fresh paid end-to-end analysis.

The separate FreeCAD demonstration replays 43 reviewed observations: 41 real
STEP dimensional candidates from 14 distinct local files, and two explicit
placeholders because exact WROOM-1U and RGB2020 STEP files are absent locally.
It does not substitute WROOM-1 for WROOM-1U. Actual STEP/FCStd reopening, native
volumes and selected lead orientations were checked. Positions, package choice
and rotation in that demonstration include manual review; automatic production
output is not guaranteed to reproduce it. No global KiCad installation occurred.
