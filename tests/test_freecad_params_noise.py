"""@brief FreeCAD's complaint about OUR params file stays in freecad.log, not on screen.

User screenshot (2026-09-26): «FreeCAD: File format not supported:
.../tmpXXXX.kicadgen_params» on the progress window looked like an error.
freecadcmd tries to open every extra argument as a document after the script;
the params file is ours, so the line is expected and harmless.
"""
import pytest

from kicadgen.cad3d import process_log
from kicadgen.cad3d import step_generator as step
from kicadgen.cad3d.body_params import Body3DParams


def test_only_the_line_about_our_params_file_is_held_back(monkeypatch, tmp_path):
    forwarded = []

    def fake_run_logged(command, log_path, progress=None, **kwargs):
        params_file = command[-1]
        progress("FreeCAD: KICADGEN_COMPONENT_START:1/1:U1")
        progress(f"FreeCAD: File format not supported: {params_file.replace(chr(92), '/')}")
        # Negativo: la stessa frase su un ALTRO file resta visibile.
        progress("FreeCAD: File format not supported: C:/altro/modello.xyz")
        raise process_log.ProcessCancelled("stop")

    monkeypatch.setattr(process_log, "run_logged", fake_run_logged)
    monkeypatch.setattr(step, "find_freecadcmd", lambda *a, **k: "freecadcmd")
    with pytest.raises(step.Cad3DError):
        step._run_freecad("P", [], Body3DParams(length_mm=5, width_mm=5, height_mm=2),
                          tmp_path, freecadcmd_path="freecadcmd", progress=forwarded.append)
    assert forwarded == ["FreeCAD: KICADGEN_COMPONENT_START:1/1:U1",
                         "FreeCAD: File format not supported: C:/altro/modello.xyz"]
