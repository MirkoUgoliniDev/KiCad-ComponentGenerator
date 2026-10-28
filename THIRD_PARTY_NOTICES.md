# Third-party notices

KiCadGen's own source code is licensed under the GNU Affero General Public
License v3.0 (see `LICENSE`). The components below have their own licenses.
They are listed separately because **their licenses are not necessarily the
same as the project's**, and each one applies only to its own material.

This file records what the project knows about each component and where the
information comes from. It is not a legal review. Before any commercial use or
redistribution of the model weights, check each license yourself.

## YOLO model weights

The repository does **not** contain any weights: `models/*.pt` and
`models/*.onnx` are ignored by Git. The Windows installer includes the ONNX
weights only when the maintainer builds it with `-IncludeYoloModel`, and it
never includes `.pt` checkpoints. The files below are the ones used locally
during development.

The "License" column quotes the license each file declares in its own
metadata (`license` field of the checkpoint, read on 2026-09-25).

### Roboflow YOLO26 Medium

- Local file: `models/yolo/pcb_component_detector_yolo26_roboflow_m.pt`
- SHA-256: `4acc9608fcac382a5c0eabe747aed32af9bd48482ce29117243651d1209ec613`
- Model family: YOLO26 (Ultralytics 8.4.55), trained on Roboflow
- Roboflow model id: `compdetect-dnwth-1-yolo26m-t1`
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- License shown by Roboflow: AGPL-3.0
- Training data: PCB object detection dataset by Arshiasbn (see below). The
  checkpoint's 21 classes match that dataset's class list name for name and
  in order (read from the checkpoint on 2026-10-28); they do not match the
  16 classes of CompDetect. The Roboflow model id above, however, belongs to
  the maintainer's CompDetect project: which data its version 1 held is
  **to be verified** in the maintainer's workspace.
- Classes: the detector's 21 PCB classes, in the detector's order

### Roboflow YOLO26 Large

- Local file: `models/yolo/pcb_component_detector_yolo26_roboflow_l.pt`
- SHA-256: `e354ea0db881d2e96a630b74591b6356f656c9cdaa0413b3d657e125ccab923a`
- Model family: YOLO26 (Ultralytics 8.4.55), trained on Roboflow
- Roboflow model id: `compdetect-dnwth-2-yolo26l-t1` (read from the
  maintainer's Roboflow model URL)
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- License shown by Roboflow: AGPL-3.0 (model page in the maintainer's private
  workspace, checked on 2026-10-28)
- Training data: CompDetect dataset (see below). The maintainer's project is a
  fork of it: same 583 images and same 16 classes, all images in the single
  batch created by the fork (checked on 2026-10-28)
- Classes: 16 (IC, LED, battery, buzzer, capacitor, clock, connector, diode,
  display, fuse, inductor, potentiometer, relay, resistor, switch,
  transistor); the detector reads them by name

### YOLOv8m trained in Colab

- Local file: `models/yolo/pcb_component_detector_yolov8m_best.pt`
- SHA-256: `27af3b3d1e17251b124c9de860b024833a46f77db04ab8767fc2554a3b3a4607`
- Base model: Ultralytics `yolov8m.pt`; trained with Ultralytics 8.4.37
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- Ultralytics code and YOLOv8 base weights: AGPL-3.0, unless an Ultralytics
  Enterprise license applies
- Training data: `dataset.verified.yaml` in the Colab notebook, built from the
  PCB object detection dataset by Arshiasbn (see below): the checkpoint's 21
  classes match that dataset's class list name for name and in order

### Roboflow YOLO26 Large (ONNX export)

- Local file: `models/yolo/pcb_component_detector_yolo26_roboflow_l.onnx`
- SHA-256: `66528ed90ad53e91b697d58fa1b9dacf45f35363798089107fa2efdb4776b29b`
- Exported on 2026-10-28 from the checkpoint above with
  `tools/export_yolo_onnx.py` (Ultralytics 8.4.163, end-to-end output
  `[1, 300, 6]`, float32, 99.4 MB); the training data path Ultralytics
  writes into the `description` metadata was replaced
- Roboflow model id: `compdetect-dnwth-2-yolo26l-t1`
- License: AGPL-3.0, the checkpoint's; training data: CompDetect (see below)
- Verified: same bodies as the checkpoint through the detector on two photos

### Roboflow YOLO26 Medium (ONNX export)

- Local file: `models/yolo/pcb_component_detector_yolo26_roboflow_m.onnx`
- SHA-256: `c3169e2ba2cbbed794039e983f14a21e4ebc6e217574a132fdd80285e0541988`
- Exported on 2026-10-28 from the checkpoint above with
  `tools/export_yolo_onnx.py` (Ultralytics 8.4.163, end-to-end output
  `[1, 300, 6]`, float32, 81.8 MB); `description` metadata replaced
- Roboflow model id: `compdetect-dnwth-1-yolo26m-t1`
- License: AGPL-3.0, the checkpoint's; training data: as the checkpoint's,
  the dataset by Arshiasbn, with the same point **to be verified**
- Verified: same bodies as the checkpoint through the detector on two photos

### YOLOv8m trained in Colab (ONNX export)

- Local file: `models/yolo/pcb_component_detector_yolov8m_best.onnx`
- SHA-256: `bb040d1f2758bbd1f812362832d80d6de376437ca256d1a30a27c30452bf8752`
- Exported on 2026-10-28 from the checkpoint above with
  `tools/export_yolo_onnx.py` (Ultralytics 8.4.163, raw output
  `[1, 25, 8400]`, float32, 103.7 MB); `description` metadata replaced
- Base model: Ultralytics `yolov8m.pt`
- License: AGPL-3.0, the checkpoint's; training data: the dataset by
  Arshiasbn (MIT, see below)
- Verified: same bodies as the checkpoint through the detector on two photos

### Base ONNX model

- Local file: `models/yolo/pcb_yolov8s.onnx`
- SHA-256: `bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29`
- Source: `Arshia82sbn/pcb-yolov8s-detection`
- License: AGPL-3.0. The author's model card says MIT, but the embedded ONNX
  metadata says AGPL-3.0: the model is a YOLOv8s fine-tuned from Ultralytics
  weights, which are AGPL-3.0, so this project redistributes it under
  AGPL-3.0 (maintainer's decision, 2026-10-28). Details are in
  `docs/yolo-photo-workflow.md`.
- Training data: not named by the author on the model card;
  **to be verified**

## SMD package classifier weights

Not a YOLO detector: a separate network that classifies the bodies the
detector found. Its folder is ignored by Git like the detectors'.

### Local SMD package classifier

- Local file: `models/smd/smd_classifier.pt` (folder `models/smd` since
  2026-10-02, separate from the detectors)
- SHA-256: `3ceeeadc9b03c2cc04e0f59ff04fae6adad9ef11ba021cbfa0dbae89d0fe1a4b`
- Architecture: ResNet18 classifier (torchvision; torchvision's own code is
  BSD-3-Clause), trained by the maintainer outside this repository
- Use: package class and orientation of the bodies the detector found
  (`kicadgen/services/smd_component_classifier.py`, detector «YOLO + rete SMD»)
- Training data: the maintainer's own images (maintainer's statement,
  2026-10-28); no third-party dataset
- License of the weights: AGPL-3.0, like the project's own code
- Distribution: the checkpoint is not published; its ONNX export below is

### Local SMD package classifier (ONNX export)

- Local file: `models/smd/smd_classifier.onnx`
- SHA-256: `d8f329822cfd17a823d861919a40575ade34a93b9df14d87ed15eb4df02baac9`
- Exported on 2026-10-28 from the checkpoint above with
  `tools/export_smd_onnx.py` (PyTorch 2.14 ONNX export, opset 17, input
  `[N, 3, 224, 224]`, outputs class and rotation logits, 44.8 MB); the
  checkpoint's metadata travel in the file as JSON, the training command
  with the maintainer's local path does not
- License: AGPL-3.0, like the checkpoint
- Distribution: asset of the GitHub release «models», shipped by the Windows
  installer (`-IncludeYoloModel`); verified on 2026-10-28 on real boxes of
  two photos: same classes and rotations as the checkpoint

## CompDetect dataset

- Dataset: CompDetect Computer Vision Model
- Source: https://universe.roboflow.com/dataset-lmrsw/compdetect
- License shown on the source page: CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/)
- Use: training data of the Roboflow YOLO26 Large model above
- Distribution: the dataset (images and annotations) is **not** part of this
  repository and is not redistributed by this project
- Changes: exported from Roboflow for training. **Any resize, augmentation,
  conversion, filtering or class change actually applied must be recorded
  here.**

The dataset is credited to its authors at the source above. This project is
not endorsed by the dataset's authors.

## PCB object detection dataset by Arshiasbn

- Dataset: Detecting the PCB object, by Arshiasbn
- Source: https://universe.roboflow.com/arshiasbn-4s0xh/detecting-the-pcb-object
  (version 3: 2047 images, split 1788 train / 173 valid / 86 test), also
  published on Hugging Face as `Arshia82sbn/pcb-object-detection-dataset`
- License shown on both source pages: MIT; the author asks to be cited as
  «Arshia, 2026, Hugging Face»
- Use: training data of the YOLO26 Medium and of the YOLOv8m model above
- Distribution: the dataset (images and annotations) is **not** part of this
  repository and is not redistributed by this project

The dataset is credited to its author at the sources above. This project is
not endorsed by the dataset's author.

## Python dependencies

Installed from PyPI, not included in the repository. Versions are the ones
pinned for release builds in `requirements/release-constraints.txt` (read on
2026-09-25 from the build environment, pypdf and the transitive packages on
2026-10-27); licenses are those declared in each package's metadata. The
Windows package also carries every license file itself, see "Licenses shipped
with the Windows installer" below. **The authoritative list of what a given
build bundles is `_internal\licenses\MANIFEST.txt` inside that package**,
written by `tools/collect_licenses.py` from the build environment: this table
is kept by hand and lists the direct dependencies first, then the transitive
packages the installer bundles with them.

| Package | Version | License | Where it is used |
|---|---|---|---|
| PyQt6 | 6.11.0 | GPL-3.0-only | user interface; included in the Windows installer |
| PyQt6-Qt6 (Qt 6 libraries, `PyQt6\Qt6` folder) | 6.11.2 | LGPL-3.0 (metadata: "LGPL v3") | user interface; included in the installer |
| PyQt6-sip | 13.12.0 | BSD-2-Clause (metadata and license file) | PyQt6 runtime; included in the installer |
| flatbuffers | 25.12.19 | Apache-2.0 (metadata; the wheel records no license file) | ONNX Runtime dependency; included in the installer |
| VTK | 9.7.0 | BSD | 3D preview; included in the installer |
| pyvista / pyvistaqt | 0.49.0 / 0.13.1 | MIT | 3D preview; included in the installer |
| OpenCV (opencv-python) | 5.0.0.93 | Apache-2.0 | photo calibration; included in the installer |
| NumPy | 2.5.3 | BSD-3-Clause and others | everywhere; included in the installer |
| Pillow | 13.0.0 | MIT-CMU | images; included in the installer |
| ONNX Runtime | 1.30.0 | MIT | `.onnx` YOLO models; included in the installer |
| python-dotenv | 1.2.3 | BSD-3-Clause | settings; included in the installer |
| pypdf | 6.21.3 | BSD-3-Clause | PDF datasheets rewritten without their metadata before upload; included in the installer |
| Ultralytics | 8.4.163 | AGPL-3.0 | `.pt` YOLO models (optional, `requirements/yolo-pt.txt`); **not** in the installer |
| PyTorch | 2.14.0 | BSD-3-Clause and others | `.pt` YOLO models (optional); **not** in the installer |
| pytest | 9.1.1 | MIT | tests only |
| PyInstaller | 6.22.3 | GPL-2.0 with bootloader exception | builds the installer; its bootloader is `KiCadGen.exe` |

Transitive packages the installer bundles with the ones above (pulled in by
pyvista, OpenCV, ONNX Runtime and the build tools; versions from
`requirements/release-constraints.txt`, licenses from each package's metadata
as read on 2026-10-27):

| Package | Version | License | Pulled in by |
|---|---|---|---|
| matplotlib | 3.11.2 | PSF-based (metadata: "Python Software Foundation License") | pyvista |
| contourpy | 1.4.0 | BSD-3-Clause | matplotlib |
| cycler | 0.12.1 | BSD-3-Clause (metadata classifier: "BSD License") | matplotlib |
| fonttools | 4.66.0 | MIT | matplotlib |
| kiwisolver | 1.5.1 | BSD-3-Clause (metadata classifier: "BSD License") | matplotlib |
| pyparsing | 3.3.3 | MIT | matplotlib |
| python-dateutil | 2.9.0.post0 | Apache-2.0 or BSD-3-Clause (metadata: "Dual License") | matplotlib |
| six | 1.17.0 | MIT | python-dateutil |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | matplotlib, pooch, build tools |
| pooch | 1.9.0 | BSD-3-Clause | pyvista |
| platformdirs | 4.11.13 | MIT | pooch |
| requests | 2.34.2 | Apache-2.0 | pooch |
| certifi | 2026.10.18 | MPL-2.0 | requests |
| charset-normalizer | 3.5.1 | MIT | requests |
| idna | 3.20 | BSD-3-Clause | requests |
| urllib3 | 2.8.0 | MIT | requests |
| scooby | 0.12.0 | MIT | pyvista |
| protobuf | 7.36.2 | BSD-3-Clause (metadata: "3-Clause BSD License") | ONNX Runtime |

The Windows installer bundles PyQt6, which is GPL-3.0: the installer as a whole
is distributed under terms compatible with GPL-3.0 and AGPL-3.0.

## Components bundled by the Windows installer besides the Python packages

PyInstaller copies into `_internal` the interpreter and the native libraries
the packages above carry. Versions are those of the build environment on
2026-09-25 (CPython 3.13.12 from python.org); the exact versions of each
release are written by the build into `_internal\licenses\MANIFEST.txt`.

| Component | Version | License | Where it comes from |
|---|---|---|---|
| CPython runtime (`python313.dll`, standard library) | 3.13.12 | PSF-2.0 | python.org installer used to create `.venv-build` |
| OpenSSL (`libssl-3.dll`, `libcrypto-3.dll`) | 3.0.18 | Apache-2.0 | shipped with CPython |
| SQLite (`sqlite3.dll`) | 3.50.4 | public domain | shipped with CPython |
| libffi (`libffi-8.dll`) | as shipped with CPython 3.13.12 | MIT | shipped with CPython |
| zlib, expat, bzip2 | 1.3.1, 2.7.3, 1.0.8 | zlib, MIT, bzip2 license | built into CPython |
| Microsoft Visual C++ runtime (`vcruntime140*.dll`) | as shipped with CPython | Microsoft Distributable Code terms (in CPython's `LICENSE.txt`) | shipped with CPython |
| Qt 6 libraries | 6.11.2 | LGPL-3.0 | PyQt6-Qt6 wheel |
| FFmpeg (`cv2\opencv_videoio_ffmpeg*.dll`) | build shipped with opencv-python 5.0.0.93 | LGPL-2.1 (text in `cv2\LICENSE-3RD-PARTY.txt`) | opencv-python wheel |
| PyInstaller bootloader (`KiCadGen.exe`) | 6.22.3 | GPL-2.0 with bootloader exception | PyInstaller |

CPython's `LICENSE.txt` carries the notices of the libraries shipped with the
interpreter (OpenSSL, libffi, bzip2, expat, zlib, the Microsoft runtime and
others).

### Licenses shipped with the Windows installer

`scripts\build-windows.ps1` runs `tools\collect_licenses.py`, which copies,
without any network access, CPython's `LICENSE.txt` and the license, notice,
copying and authors files recorded in every installed distribution's
metadata into `_internal\licenses`, with a `MANIFEST.txt` listing each
distribution's version and declared license. The list is taken from the build
environment, so it is a superset of what is bundled.

### Source code of GPL/LGPL components

The installer includes, unmodified, the following components under the GPL or
the LGPL. Their complete corresponding source code is available from the
upstream projects at the exact versions listed:

- PyQt6 6.11.0 (GPL-3.0): https://pypi.org/project/PyQt6/6.11.0/#files
  (source distribution) and https://www.riverbankcomputing.com/software/pyqt/
- Qt 6.11.2 (LGPL-3.0): https://download.qt.io/official_releases/qt/6.11/6.11.2/
  (the PyQt6-Qt6 wheel contains the Qt libraries of that release, unmodified)
- FFmpeg as bundled by opencv-python 5.0.0.93 (LGPL-2.1): the opencv-python
  project publishes its build scripts and the FFmpeg sources it builds at
  https://github.com/opencv/opencv-python; the exact FFmpeg release used by
  5.0.0.93 must be confirmed there before a release is published.
- PyInstaller 6.22.3 bootloader (GPL-2.0 with bootloader exception):
  https://pypi.org/project/pyinstaller/6.22.3/#files

The LGPL libraries are shipped as separate dynamic libraries (`.dll` files in
`_internal`) and can be replaced by the user with compatible builds. **How the
corresponding source is offered with each published installer (for example
a source archive attached to the GitHub Release next to the MSI, or a written
offer) is still to be decided by the maintainer before a release is
published**; until then, the upstream links above are the reference, at the
versions listed in the installer's `_internal\licenses\MANIFEST.txt`.

## External programs

FreeCAD (LGPL-2.0-or-later) and KiCad (GPL-3.0-or-later) are separate programs.
KiCadGen detects and runs them when they are installed; it does not include
them.
