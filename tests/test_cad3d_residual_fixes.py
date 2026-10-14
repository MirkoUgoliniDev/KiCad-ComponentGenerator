"""@brief Residual fixes of the 3D generation after the 2026-10-14 review.

* E2 warning -- the count-only fallback of a rotated SOIC/QFN sends a
  dedicated Italian template with its parameters, never an English reason
  embedded in a translated sentence; the same holds for the other reasons
  written by the FreeCAD script (invalid geometry, invalid terminal_geometry,
  invalid lead detail, unusable library model);
* D4 -- among FreeCAD installations of equal numeric version the release
  beats a pre-release (-dev, RC, beta, alpha), a higher number still wins and
  a packaging tag such as «-conda-Windows-x86_64» is not a pre-release.

No FreeCAD is needed: freecad_parts is loaded with a box-recording Part stub.
"""
import importlib.util
import json
import string
import sys
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.lead_sides import local_lead_sides
from kicadgen.services.messages import child_warning_text
from tests.test_fixE_parts_orientation import CAD_DIR, parts, flip_y  # noqa: F401 - shared

#: The dedicated E2 template, as the catalog has to know it.
E2_TEMPLATE = ("componente {get} ({kind}) semplificato: lati dei reofori osservati non "
               "riportabili nel riferimento del corpo; reofori distribuiti dal solo conteggio")


def _qfn(rotation, **changes):
    """@brief A QFN payload with leads seen on the image top and bottom."""
    raw = {"top": 4, "bottom": 4}
    component = {"name": "Q", "kind": "ic_qfn", "x": 0.0, "y": 0.0, "length_mm": 5.0,
                 "width_mm": 4.0, "height_mm": 1.0, "rotation_deg": rotation,
                 "pins_by_side": raw, "lead_sides_local": local_lead_sides(raw, rotation)}
    component.update(changes)
    return component


def _capture(monkeypatch, module):
    """@brief Record every (template, values) the script would send to the parent."""
    sent = []
    monkeypatch.setattr(module, "_warn", lambda template, **values: sent.append((template, values)))
    return sent


def _english_free(template):
    """@brief True when no English reason is left in the literal text of a template."""
    literal = "".join(text for text, *_ in string.Formatter().parse(template))
    return not any(word in literal for word in
                   ("skipped", "invalid", "failed", "not mappable", "must", "count only"))


# -- E2: dedicated key and parameters --------------------------------------------

def test_count_only_fallback_sends_the_dedicated_template_and_parameters(parts, monkeypatch):
    """Positive: at 45 degrees the sides are unmappable; the warning is the E2 key."""
    sent = _capture(monkeypatch, parts)
    parts.build_component(_qfn(45.0, lead_sides_local=None), 1.6)
    e2 = [entry for entry in sent if entry[0] == E2_TEMPLATE]
    assert e2 == [(E2_TEMPLATE, {"get": "Q", "kind": "ic_qfn"})]
    assert _english_free(E2_TEMPLATE)


def test_the_e2_warning_line_is_translated_whole_by_the_parent(parts, capsys):
    """Positive: the real `_warn` line carries t + v only, no English value."""
    # The shared fixture silences `_warn`; a second load keeps the real one
    # (the FreeCAD/Part stubs are still in sys.modules).
    spec = importlib.util.spec_from_file_location("residual_parts", CAD_DIR / "freecad_parts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    real.build_component(_qfn(45.0, lead_sides_local=None), 1.6)
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith("KICADGEN_WARN:")]
    payloads = [json.loads(line.split(":", 1)[1]) for line in lines]
    e2 = [p for p in payloads if p["t"] == E2_TEMPLATE]
    assert e2 == [{"t": E2_TEMPLATE, "v": {"get": "Q", "kind": "ic_qfn"}}]
    # Without a registered translator the parent gives the Italian sentence,
    # entirely from the template: no English fragment travels as a value.
    text = child_warning_text(json.dumps(e2[0]))
    assert text.startswith("componente Q (ic_qfn) semplificato: lati dei reofori")


def test_an_ordinary_warning_path_is_unchanged(parts, monkeypatch):
    """Negative: with mappable local sides no E2 warning, only the usual one."""
    sent = _capture(monkeypatch, parts)
    parts.build_component(_qfn(90.0), 1.6)
    templates = [template for template, _ in sent]
    assert E2_TEMPLATE not in templates
    assert templates == ["componente {get} ({kind}): geometria approssimata, terminal_geometry "
                         "assente"]


def test_a_builder_failure_still_carries_its_runtime_text(parts, monkeypatch):
    """Negative: an error the script did not write (FreeCAD/OCC) still travels as {exc}."""
    sent = _capture(monkeypatch, parts)

    def broken(*args, **kwargs):
        raise RuntimeError("BRep_API: command not done")

    monkeypatch.setattr(parts, "_ic_observed_pins", broken)
    parts.build_component(_qfn(90.0), 1.6)
    template, values = sent[-1]
    assert template == "componente {get} ({kind}) semplificato: {exc}"
    assert "BRep_API" in str(values["exc"])


# -- Other reasons written by the script -----------------------------------------

@pytest.mark.parametrize("changes,template,values", [
    ({"length_mm": None}, "componente {get} ignorato: geometria non valida (valore non numerico "
                          "o fuori scala)", {"get": "Q"}),
    ({"width_mm": True}, "componente {get} ignorato: geometria non valida (valore booleano)",
     {"get": "Q"}),
    ({"x": float("nan")}, "componente {get} ignorato: geometria non valida (valore non finito)",
     {"get": "Q"}),
    ({"height_mm": 0.0}, "componente {get} ignorato: geometria non valida (dimensioni non "
                         "positive)", {"get": "Q"}),
])
def test_invalid_geometry_reasons_have_their_own_templates(parts, monkeypatch, changes,
                                                           template, values):
    """Positive: each reason of an invalid envelope is its own translated sentence."""
    sent = _capture(monkeypatch, parts)
    assert parts.build_component(_qfn(0.0, **changes), 1.6) == []
    assert sent == [(template, values)]
    assert _english_free(template)


def test_a_missing_field_names_the_field_as_a_parameter(parts, monkeypatch):
    """Positive: the field name is a technical value, the sentence is Italian."""
    sent = _capture(monkeypatch, parts)
    component = _qfn(0.0)
    del component["width_mm"]
    assert parts.build_component(component, 1.6) == []
    assert sent == [("componente {get} ignorato: geometria non valida (manca il campo {field})",
                     {"get": "Q", "field": "width_mm"})]


_TG = "componente {get} ({kind}) semplificato: terminal_geometry non valida ("


def _observation(**changes):
    """@brief A valid terminal_geometry observation, with optional changes."""
    geometry = {"confidence": "high", "body_bbox": [0.2, 0.2, 0.8, 0.8],
                "terminals": [{"bbox": [0.0, 0.4, 0.2, 0.6]}]}
    geometry.update(changes)
    return geometry


@pytest.mark.parametrize("geometry,reason", [
    (_observation(body_bbox=[0, 0, 1]), "un rettangolo non ha quattro coordinate"),
    (_observation(body_bbox=[0, 0, 1.5, 1]), "coordinate di un rettangolo non finite o fuori "
                                             "da [0, 1]"),
    (_observation(body_bbox=[0.5, 0, 0.5, 1]), "un rettangolo ha area nulla"),
    ([], "l'osservazione non e' un oggetto"),
    (_observation(confidence="low"), "confidenza diversa da alta o media"),
    (_observation(terminals=None), "i terminali non sono un elenco di al massimo 128 rettangoli"),
    (_observation(terminals=[[0, 0, 1, 1]]), "un terminale non e' un oggetto con bbox"),
])
def test_each_terminal_geometry_reason_has_its_own_template(parts, monkeypatch, geometry, reason):
    """Positive: the user reads which check failed, in a translated sentence naming the part."""
    sent = _capture(monkeypatch, parts)
    result = parts.build_component(_qfn(0.0, terminal_geometry=geometry), 1.6)
    assert len(result) == 1
    assert sent == [(_TG + reason + "); si usa l'ingombro", {"get": "Q", "kind": "ic_qfn"})]
    assert _english_free(sent[0][0].replace("terminal_geometry", ""))


def test_every_script_check_code_has_a_template(parts, monkeypatch):
    """Negative: an unknown code falls back to the reason-less sentence, never to English."""
    sent = _capture(monkeypatch, parts)
    parts._warn_terminal_geometry("unknown", "Q", "ic_qfn")
    assert sent == [("componente {get} ({kind}) semplificato: osservazione terminal_geometry "
                     "non valida; si usa l'ingombro", {"get": "Q", "kind": "ic_qfn"})]


_LEAD = "componente {get}, terminale {terminal}: dettaglio reoforo ignorato ("
_GOOD_LEAD = {"confidence": "high", "source": "photo_separation_v1"}


@pytest.mark.parametrize("detail,reason", [
    ({"confidence": "low"}, "separazione senza evidenza esplicita ad alta confidenza"),
    (dict(_GOOD_LEAD, bbox=[0, 0, 1]), "rettangolo del reoforo non valido"),
    (dict(_GOOD_LEAD, bbox=[0.1, 0.4, 0.1, 0.6]), "rettangolo del reoforo vuoto"),
    (dict(_GOOD_LEAD, bbox=[0.0, 0.3, 0.25, 0.6]), "reoforo oltre l'ingombro del terminale "
                                                   "originale"),
    (dict(_GOOD_LEAD, bbox=[0.0, 0.4, 0.1, 0.6]), "il reoforo deve toccare esattamente una "
                                                  "faccia del corpo"),
])
def test_each_lead_detail_reason_names_component_and_terminal(parts, monkeypatch, detail, reason):
    """Positive: one template per LeadDetailRejected code; the second terminal is named 2."""
    sent = _capture(monkeypatch, parts)
    geometry = _observation(terminals=[{"bbox": [0.8, 0.4, 1.0, 0.6]},
                                       {"bbox": [0.0, 0.4, 0.2, 0.6], "lead_detail": detail}])
    result = parts.build_component(_qfn(0.0, terminal_geometry=geometry), 1.6)
    assert len(result) == 3  # body + two terminals: the observation itself is kept
    assert sent == [(_LEAD + reason + "); terminale originale conservato",
                     {"get": "Q", "terminal": 2})]


def test_a_valid_lead_detail_sends_no_warning(parts, monkeypatch):
    """Negative: an accepted lead detail is not reported."""
    sent = _capture(monkeypatch, parts)
    geometry = _observation(terminals=[{"bbox": [0.0, 0.4, 0.2, 0.6],
                                        "lead_detail": dict(_GOOD_LEAD,
                                                            bbox=[0.0, 0.45, 0.2, 0.55])}])
    body, boxes = parts._validated_terminal_geometry(geometry, "Q")
    assert boxes == [[0.0, 0.45, 0.2, 0.55]]
    assert sent == []


def test_an_unreadable_lead_detail_has_its_own_template(parts, monkeypatch):
    """Positive: a detail without a reason code (TypeError) is still a translated sentence."""
    sent = _capture(monkeypatch, parts)
    parts._warn_lead_detail(None, "Q", 1)
    assert sent == [(_LEAD + "dettaglio malformato); terminale originale conservato",
                     {"get": "Q", "terminal": 1})]


class _FakeShape:
    """@brief Imported shape stub: faces, solids and validity are chosen by the test."""

    def __init__(self, faces, solids):
        self.Faces, self.Solids, self.Placement = [0] * faces, [0] * solids, None

    def copy(self):
        return self

    def isNull(self):
        return False

    def isValid(self):
        return True


@pytest.mark.parametrize("imported,code", [
    ([], "no_colored_shapes"),
    ([(_FakeShape(3, 0), ["#fff"])], "non_solid_geometry"),
    ([(_FakeShape(3, 1), ["#fff", "#000"])], "face_colour_mismatch"),
])
def test_library_import_checks_carry_distinct_codes(parts, monkeypatch, imported, code):
    """Positive: face-colour mismatch and non-solid geometry are told apart by code."""
    objects = [SimpleNamespace(Shape=shape, getGlobalPlacement=lambda: None)
               for shape, _ in imported]
    monkeypatch.setitem(sys.modules, "Import", SimpleNamespace(
        insert=lambda *a, **k: [(obj, colors) for obj, (_, colors) in zip(objects, imported)]))
    monkeypatch.setattr(parts.FreeCAD, "ActiveDocument", None, raising=False)
    monkeypatch.setattr(parts.FreeCAD, "newDocument", lambda name: SimpleNamespace(Name=name),
                        raising=False)
    monkeypatch.setattr(parts.FreeCAD, "closeDocument", lambda name: None, raising=False)
    with pytest.raises(parts._CheckFailed) as caught:
        parts._read_library_parts("m.step", "#303030")
    assert caught.value.code == code


@pytest.mark.parametrize("code,reason", [
    ("face_colour_mismatch", "il numero dei colori delle facce non corrisponde alla geometria"),
    ("non_solid_geometry", "il modello contiene geometria non valida o non solida"),
    ("no_colored_shapes", "l'importazione non ha restituito forme colorate"),
])
def test_each_native_colour_failure_has_its_own_template(parts, monkeypatch, code, reason):
    """Positive: the colour warning says which check failed, path as the only value."""
    sent = _capture(monkeypatch, parts)

    def refuse(*args, **kwargs):
        raise parts._CheckFailed(code, "english diagnostic")

    def unreadable(path):
        raise RuntimeError("OCC read failure")

    monkeypatch.setattr(parts, "_read_library_parts", refuse)
    monkeypatch.setattr(parts.Part, "read", unreadable, raising=False)
    component = _qfn(0.0, model_path="m.step")
    assert parts._library_model(component, 0.0, 0.0, 1.6, "#303030") is None
    assert sent[0] == ("colori nativi {path} non disponibili (" + reason + "); si conserva la "
                       "geometria con colore fotografico", {"path": "m.step"})
    # The OCC failure that follows is not text of this script: it still travels as {exc}.
    assert sent[1][0] == "modello {path} non caricabile ({exc}); si usa la forma approssimata"


def test_the_script_check_error_is_a_value_error(parts):
    """Negative: callers that catch ValueError keep working; the English text stays a diagnostic."""
    with pytest.raises(ValueError, match="four coordinates") as caught:
        parts._normalized_box([0, 0, 1])
    assert caught.value.code == "bbox_not_four"


# -- D4: release before pre-release at equal version ----------------------------

def _installed(monkeypatch, paths):
    """@brief Pretend these freecadcmd paths are the installed ones."""
    monkeypatch.delenv("FREECAD_CMD_PATH", raising=False)
    monkeypatch.setattr(step_generator.glob, "glob",
                        lambda pattern: list(paths) if "Program Files\\FreeCAD" in pattern
                        and "(x86)" not in pattern else [])


def _path(folder):
    return rf"C:\Program Files\{folder}\bin\freecadcmd.exe"


@pytest.mark.parametrize("order", [("FreeCAD 1.1", "FreeCAD 1.1-dev"),
                                   ("FreeCAD 1.1-dev", "FreeCAD 1.1")])
def test_the_release_beats_its_dev_build_in_either_order(monkeypatch, order):
    """Positive: at equal number the official release wins, whatever the glob order."""
    _installed(monkeypatch, [_path(folder) for folder in order])
    assert step_generator.find_freecadcmd() == _path("FreeCAD 1.1")


@pytest.mark.parametrize("prerelease", ["FreeCAD 1.1 RC", "FreeCAD 1.1 beta", "FreeCAD 1.1alpha2",
                                        "FreeCAD 1.1rc1", "FreeCAD_1.1_b2"])
def test_every_prerelease_marker_ranks_below_the_release(monkeypatch, prerelease):
    """Positive: RC, beta, alpha and the short b<n> form are pre-releases."""
    _installed(monkeypatch, [_path(prerelease), _path("FreeCAD 1.1")])
    assert step_generator.freecad_folder_is_prerelease(_path(prerelease))
    assert step_generator.find_freecadcmd() == _path("FreeCAD 1.1")


def test_a_higher_number_still_beats_a_release(monkeypatch):
    """Positive: 1.2-dev is newer than 1.1, pre-release or not."""
    _installed(monkeypatch, [_path("FreeCAD 1.1"), _path("FreeCAD 1.2-dev")])
    assert step_generator.find_freecadcmd() == _path("FreeCAD 1.2-dev")


def test_a_conda_packaging_tag_is_not_a_prerelease(monkeypatch):
    """Negative: «-conda-Windows-x86_64» names the packaging, not a pre-release."""
    conda = _path("FreeCAD_1.0.2-conda-Windows-x86_64")
    assert not step_generator.freecad_folder_is_prerelease(conda)
    _installed(monkeypatch, [_path("FreeCAD 1.0.2-dev"), conda])
    assert step_generator.find_freecadcmd() == conda


@pytest.mark.parametrize("folder", ["FreeCAD 1.1", "FreeCAD", "FreeCAD dev", "FreeCAD 1.1 portable",
                                    "FreeCAD 1.1 x64"])
def test_folders_without_a_prerelease_word_are_releases(folder):
    """Negative: no version, or an unknown suffix, is never a pre-release."""
    assert not step_generator.freecad_folder_is_prerelease(_path(folder))


def test_two_prereleases_of_one_version_keep_the_text_order(monkeypatch):
    """Documented tie: RC and -dev rank equal; the reverse path text decides (-dev first)."""
    for order in (("FreeCAD 1.1 RC", "FreeCAD 1.1-dev"), ("FreeCAD 1.1-dev", "FreeCAD 1.1 RC")):
        _installed(monkeypatch, [_path(folder) for folder in order])
        assert step_generator.find_freecadcmd() == _path("FreeCAD 1.1-dev")


def test_unversioned_installations_still_come_last(monkeypatch):
    """Negative: the release rank never lifts an unversioned folder above a versioned one."""
    _installed(monkeypatch, [_path("FreeCAD"), _path("FreeCAD 0.19-dev")])
    assert step_generator.find_freecadcmd() == _path("FreeCAD 0.19-dev")


@pytest.mark.parametrize("word", ["weekly", "nightly", "snapshot"])
def test_development_builds_rank_below_the_release_of_the_same_version(word):
    """FreeCAD's weekly/nightly builds are pre-releases too (D4 list, 2026-10-14)."""
    from kicadgen.cad3d.step_generator import freecad_folder_is_prerelease
    assert freecad_folder_is_prerelease(f"C:/Program Files/FreeCAD 1.1-{word}/bin/freecadcmd.exe")
    assert not freecad_folder_is_prerelease("C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe")


@pytest.mark.parametrize("order", [("FreeCAD 1.1", "FreeCAD 1.1.0-dev"),
                                   ("FreeCAD 1.1.0-dev", "FreeCAD 1.1")])
def test_a_trailing_zero_does_not_make_a_dev_build_newer(monkeypatch, order):
    """Positive: 1.1.0 and 1.1 are the same version, so the release wins in either order."""
    _installed(monkeypatch, [_path(folder) for folder in order])
    assert step_generator.find_freecadcmd() == _path("FreeCAD 1.1")


def test_a_real_patch_number_still_beats_the_release(monkeypatch):
    """Negative: 1.1.1 is newer than 1.1; only trailing zeros are dropped."""
    _installed(monkeypatch, [_path("FreeCAD 1.1"), _path("FreeCAD 1.1.1-dev")])
    assert step_generator.find_freecadcmd() == _path("FreeCAD 1.1.1-dev")
