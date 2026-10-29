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
    # Run by freecadcmd as real files: they must exist on disk.
    (str(ROOT / "kicadgen" / "cad3d" / "*.py"), "kicadgen/cad3d"),
    (str(SEED / "data" / "analysis_profiles.sqlite3"), "data"),
    (str(ROOT / ".env.example"), "."),
    # License and third-party notices: shown by the menu «?» > «Licenze» (Licenses).
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    # License files of the bundled components (CPython, Qt, PyQt6, OpenCV's
    # FFmpeg, ...) as _internal/licenses, collected offline by
    # tools/collect_licenses.py.
    (str(LICENSES), "licenses"),
]
# YOLO weights: they have licenses of their own (THIRD_PARTY_NOTICES.md), so
# they enter the package only with scripts/build-windows.ps1 -IncludeYoloModel.
# Then EXACTLY the files of packaging/model-weights.json ship (the four
# detectors in models/yolo, app_paths.YOLO_MODELS_DIR, and the SMD network in
# models/smd, so «YOLO + rete SMD» (YOLO + SMD network) works installed), each
# verified by size and SHA-256 with tools/fetch_model_weights.py (standard
# library only, loaded by path). A missing or different file stops the build;
# any other file in those folders (an unreviewed export, a .pt: the package
# has no PyTorch) is left out. Review 2026-10-29, K1: a glob shipped whatever
# was on disk. Without the switch the program works and the local detector
# asks for an .onnx file.
if os.environ.get("KICADGEN_BUNDLE_YOLO_MODEL") == "1":
    import importlib.util
    _weights_loader = importlib.util.spec_from_file_location(
        "kicadgen_fetch_model_weights", ROOT / "tools" / "fetch_model_weights.py")
    weights_tool = importlib.util.module_from_spec(_weights_loader)
    _weights_loader.loader.exec_module(weights_tool)
    for folder, weights in weights_tool.verified_weights(
            weights_tool.load_manifest(ROOT / "packaging" / "model-weights.json"), ROOT / "models"):
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
    # PyTorch/Ultralytics (.pt models) stay out: size and license still to be
    # decided; in the executable a .pt gives a clear error (photo_yolo).
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
