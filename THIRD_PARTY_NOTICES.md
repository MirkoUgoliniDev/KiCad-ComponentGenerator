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

- Local file: `models/pcb_component_detector_yolo26_roboflow_m.pt`
- SHA-256: `4acc9608fcac382a5c0eabe747aed32af9bd48482ce29117243651d1209ec613`
- Model family: YOLO26 (Ultralytics 8.4.55), trained on Roboflow
- Roboflow model id: `compdetect-dnwth-1-yolo26m-t1`
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- License shown by Roboflow: AGPL-3.0
- Training data: CompDetect dataset (see below)
- Classes: the detector's 21 PCB classes, in the detector's order

### Roboflow YOLO26 Large

- Local file: `models/pcb_component_detector_yolo26_roboflow_l.pt`
- SHA-256: `e354ea0db881d2e96a630b74591b6356f656c9cdaa0413b3d657e125ccab923a`
- Model family: YOLO26 (Ultralytics 8.4.55), trained on Roboflow
- Roboflow model id: **to be recorded from the model page** (it is not the
  Medium model's id)
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- License shown by Roboflow: **to be verified on the model page before
  publishing**
- Classes: 16 (IC, LED, battery, buzzer, capacitor, clock, connector, diode,
  display, fuse, inductor, potentiometer, relay, resistor, switch,
  transistor); the detector reads them by name

### YOLOv8m trained in Colab

- Local file: `models/pcb_component_detector_yolov8m_best.pt`
- SHA-256: `27af3b3d1e17251b124c9de860b024833a46f77db04ab8767fc2554a3b3a4607`
- Base model: Ultralytics `yolov8m.pt`; trained with Ultralytics 8.4.37
- License declared in the checkpoint: AGPL-3.0 (https://ultralytics.com/license)
- Ultralytics code and YOLOv8 base weights: AGPL-3.0, unless an Ultralytics
  Enterprise license applies
- Training data: `dataset.verified.yaml` in the Colab notebook; **the source
  and license of that dataset must be confirmed**

### Base ONNX model

- Local file: `models/pcb_yolov8s.onnx`
- SHA-256: `bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29`
- Source: `Arshia82sbn/pcb-yolov8s-detection`
- License: **unresolved**. The model card says MIT; the embedded ONNX metadata
  says AGPL-3.0. Treat it as AGPL-3.0 until clarified. Details are in
  `docs/yolo-photo-workflow.md`.

## CompDetect dataset

- Dataset: CompDetect Computer Vision Model
- Source: https://universe.roboflow.com/dataset-lmrsw/compdetect
- License shown on the source page: CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/)
- Use: training data of the Roboflow YOLO26 models above
- Distribution: the dataset (images and annotations) is **not** part of this
  repository and is not redistributed by this project
- Changes: exported from Roboflow for training. **Any resize, augmentation,
  conversion, filtering or class change actually applied must be recorded
  here.**

The dataset is credited to its authors at the source above. This project is
not endorsed by the dataset's authors.

## Python dependencies

Installed from PyPI, not included in the repository. Versions are the ones in
the development environment on 2026-09-25; licenses are those declared in each
package's metadata.

| Package | Version | License | Where it is used |
|---|---|---|---|
| PyQt6 | 6.11.0 | GPL-3.0-only | user interface; included in the Windows installer |
| VTK | 9.7.0 | BSD | 3D preview; included in the installer |
| pyvista / pyvistaqt | 0.49.0 / 0.13.1 | MIT | 3D preview; included in the installer |
| OpenCV (opencv-python) | 5.0.0.93 | Apache-2.0 | photo calibration; included in the installer |
| NumPy | 2.5.3 | BSD-3-Clause and others | everywhere; included in the installer |
| Pillow | 12.3.0 | MIT-CMU | images; included in the installer |
| ONNX Runtime | 1.30.0 | MIT | `.onnx` YOLO models; included in the installer |
| python-dotenv | 1.2.3 | BSD-3-Clause | settings; included in the installer |
| Ultralytics | 8.4.163 | AGPL-3.0 | `.pt` YOLO models (optional, `requirements-yolo-pt.txt`); **not** in the installer |
| PyTorch | 2.14.0 | BSD-3-Clause and others | `.pt` YOLO models (optional); **not** in the installer |
| pytest | 9.1.1 | MIT | tests only |
| PyInstaller | 6.x | GPL-2.0 with bootloader exception | building the installer only |

The Windows installer bundles PyQt6, which is GPL-3.0: the installer as a whole
is distributed under terms compatible with GPL-3.0 and AGPL-3.0.

## External programs

FreeCAD (LGPL-2.0-or-later) and KiCad (GPL-3.0-or-later) are separate programs.
KiCadGen detects and runs them when they are installed; it does not include
them.
