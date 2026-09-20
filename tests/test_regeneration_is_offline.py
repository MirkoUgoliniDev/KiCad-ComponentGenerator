# -*- coding: utf-8 -*-
"""@brief "Rigenera modello 3D" deve restare FreeCAD e basta.

La rigenerazione riparte dalle scelte salvate nel JSON, non da una nuova
lettura delle foto: ricontattare il modello AI costerebbe soldi a ogni
correzione manuale e potrebbe ribaltare la correzione appena fatta.

Il controllo e' strutturale, non sul comportamento di una singola corsa:
verifica che il pacchetto che costruisce il solido non abbia proprio modo
di raggiungere un servizio AI. Cosi' fallisce quando qualcuno aggiunge
l'import, non mesi dopo quando arriva la bolletta.
"""

import ast
from pathlib import Path

import pytest

CAD3D = Path(__file__).resolve().parent.parent / "kicadgen" / "cad3d"

#: Moduli che parlano con il modello o ne custodiscono le credenziali.
AI_MODULES = ("openrouter", "datasheet_extractor", "photo_grounding",
              "photo_global_inventory", "photo_inventory", "photo_terminals",
              "photo_yolo", "ai_diagnostics")


def _imported_names(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"


@pytest.mark.parametrize("source", sorted(CAD3D.glob("*.py")), ids=lambda p: p.name)
def test_the_cad_package_cannot_reach_an_ai_service(source):
    offending = [name for name in _imported_names(source)
                 if any(module in name for module in AI_MODULES)]
    assert not offending, (
        f"{source.name} importa {offending}: la rigenerazione 3D tornerebbe "
        "a dipendere dal modello AI")


def test_the_check_would_notice_an_ai_import(tmp_path):
    """Il negativo: senza questo, il test sopra passerebbe anche a vuoto."""
    intruder = tmp_path / "intruso.py"
    intruder.write_text("from ..services.openrouter import call_tool\n", encoding="utf-8")
    assert any("openrouter" in name for name in _imported_names(intruder))


def test_no_credentials_are_read_while_building_the_solid():
    """Nessun file del pacchetto deve nemmeno leggere la chiave API."""
    for source in CAD3D.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for secret in ("OPENROUTER_API_KEY", "api_key"):
            assert secret not in text, f"{source.name} tocca {secret}"
