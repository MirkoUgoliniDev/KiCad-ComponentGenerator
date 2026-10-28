# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: KiCadGen as a one-folder Windows GUI application.

Run through scripts/build-windows.ps1, which prepares build/seed (the initial
profile database generated from the code by tools/make_seed_db.py),
build/licenses (license files collected by tools/collect_licenses.py) and the
.venv-build environment. Both folders are required: PyInstaller stops when a
data source is missing, so a package can never ship without them.
Read-only resources keep the package layout, because the code finds them
relative to its own modules (kicadgen/gui/assets, kicadgen/cad3d scripts run
by freecadcmd as real files) or through kicadgen.app_paths.resource_path.
"""
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).resolve().parent
SEED = ROOT / "build" / "seed"
LICENSES = ROOT / "build" / "licenses"
VERSION = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"',
                    (ROOT / "kicadgen" / "__init__.py").read_text(encoding="utf-8")).group(1)
NUMBERS = tuple(int(part) for part in VERSION.split(".")) + (0,)

datas = [
    (str(ROOT / "kicadgen" / "gui" / "assets"), "kicadgen/gui/assets"),
    # Eseguiti da freecadcmd come file veri: devono esistere su disco.
    (str(ROOT / "kicadgen" / "cad3d" / "*.py"), "kicadgen/cad3d"),
    (str(SEED / "data" / "analysis_profiles.sqlite3"), "data"),
    (str(ROOT / ".env.example"), "."),
    # Licenza e avvisi di terze parti: li mostra il menu «?» > «Licenze».
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    # License files of the bundled components (CPython, Qt, PyQt6, OpenCV's
    # FFmpeg, ...) as _internal/licenses, collected offline by
    # tools/collect_licenses.py.
    (str(LICENSES), "licenses"),
]
# YOLO weights: they have licenses of their own (THIRD_PARTY_NOTICES.md), so
# they enter the package only with scripts/build-windows.ps1 -IncludeYoloModel.
# Since 2026-10-28 every .onnx of models/yolo ships (the base model and the
# ONNX exports of tools/export_yolo_onnx.py; the user chose all of them), never
# a .pt: the package has no PyTorch. Without weights the program works and the
# local detector asks for an .onnx file.
if os.environ.get("KICADGEN_BUNDLE_YOLO_MODEL") == "1":
    # Since 2026-10-02 the detectors live in models/yolo (app_paths.YOLO_MODELS_DIR)
    # and the SMD network in models/smd; its ONNX export ships too (2026-10-28),
    # so «YOLO + rete SMD» works in the installed program.
    for folder in ("yolo", "smd"):
        for weights in sorted((ROOT / "models" / folder).glob("*.onnx")):
            datas.append((str(weights), f"models/{folder}"))
binaries = []
hiddenimports = []
for package in ("pyvista", "pyvistaqt", "vtkmodules", "onnxruntime"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Mirko Ugolini"),
            StringStruct("FileDescription", "KiCadGen - KiCad component generator"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "KiCadGen"),
            StringStruct("OriginalFilename", "KiCadGen.exe"),
            StringStruct("ProductName", "KiCadGen"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # PyTorch/Ultralytics (modelli .pt) restano fuori: dimensione e licenza da
    # decidere; nell eseguibile un .pt da un errore chiaro (photo_yolo).
    excludes=["tkinter", "pytest", "_pytest", "tests", "torch", "torchvision", "ultralytics"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KiCadGen",
    console=False,
    icon=str(ROOT / "packaging" / "KiCadGen.ico"),
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, name="KiCadGen")
