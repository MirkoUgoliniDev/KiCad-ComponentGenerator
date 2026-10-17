"""@brief No invented 3D body for a pin matrix without documented dimensions (audit 2026-10-15, item 4).

A pin-header/matrix project whose analysis gave no body dimensions built,
on «Rigenera 3D» (and in the footprint's F.Fab/silkscreen), the placeholder
10 x 10 x 5 mm block anchored at pin 1, with no message and the button
enabled. Now, like the package path, no body is built without documented
dimensions: the button is disabled with the reason, a direct call refuses
with the same message, and the footprint draws no invented outline.
"""
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction


class _CapturedCad3D(QThread):
    """Stands for Cad3DWorker: records its body, never runs FreeCAD."""
    done = pyqtSignal(str)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    warning = pyqtSignal(str)
    cancellable = pyqtSignal(bool)
    phase = pyqtSignal(str, str, int, int)
    created = []

    def __init__(self, params, body, project_dir, fillets, face_texts, single_component=False):
        super().__init__()
        self.body = body
        _CapturedCad3D.created.append(self)

    def start(self, *_args):  # noqa: D401 - QThread override in the fake
        self.finished.emit()


def _analysed(window, monkeypatch, tmp_path, **body):
    prepare(window, monkeypatch, tmp_path, "HDR")
    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    monkeypatch.setattr(gui, "Cad3DWorker", _CapturedCad3D)
    _CapturedCad3D.created = []
    run_extraction(window, header_extraction(**body))
    window.group_3d.setChecked(True)            # FreeCAD present: the 3D group is on
    window._refresh_output_actions()
    return infos


def test_without_documented_body_rigenera_3d_is_off_and_refuses(window, monkeypatch, tmp_path):  # noqa: F811
    infos = _analysed(window, monkeypatch, tmp_path, body_length_mm=None, body_width_mm=None,
                      body_height_mm=None)
    assert not window.btn_regen_3d.isEnabled()
    assert "mancano le quote documentate del corpo" in window.btn_regen_3d.toolTip()
    window._regenerate_3d()                     # direct call: refused on its own
    assert _CapturedCad3D.created == []
    assert infos and "mancano le quote documentate del corpo" in infos[-1]
    assert window._current_body_params() is None
    # No invented outline in the footprint either.
    params = window._current_params()
    assert params.body_length is None and params.body_width is None


def test_documented_body_builds_exactly_that_body(window, monkeypatch, tmp_path):  # noqa: F811
    """Positive: the analysis gave 20 x 10 x 3 mm; that is what FreeCAD gets."""
    _analysed(window, monkeypatch, tmp_path)
    assert window.btn_regen_3d.isEnabled()
    window._regenerate_3d()
    assert len(_CapturedCad3D.created) == 1
    body = _CapturedCad3D.created[0].body
    assert (body.length_mm, body.width_mm, body.height_mm) == (20, 10, 3)
    assert window._current_params().body_length == 20


def test_a_new_analysis_without_body_withdraws_the_previous_body(window, monkeypatch, tmp_path):  # noqa: F811
    """The fields keep the previous analysis's values; they are not this
    analysis's body and must not build one."""
    _analysed(window, monkeypatch, tmp_path)
    assert window._body_size_declared
    run_extraction(window, header_extraction(body_length_mm=None, body_width_mm=None,
                                             body_height_mm=None))
    window._refresh_output_actions()
    assert not window._body_size_declared and not window.btn_regen_3d.isEnabled()


def test_the_flag_is_saved_and_older_projects_are_judged_on_their_values(window, monkeypatch, tmp_path):  # noqa: F811
    """Saved with the project; a project saved before the flag counts as
    declared only when its fields differ from the placeholder (declared
    heuristic, see `_apply_project_state`)."""
    _analysed(window, monkeypatch, tmp_path)
    state = window._project_state()
    assert state["body_size_declared"] is True
    legacy = dict(state)
    legacy.pop("body_size_declared")
    window._apply_project_state(legacy)
    assert window._body_size_declared is True             # 20 x 10 x 3: not the placeholder
    widgets = dict(legacy.get("widgets") or {})
    widgets.update(field_body_length=10.0, field_body_width=10.0, field_body_height=5.0)
    legacy["widgets"] = widgets
    window._apply_project_state(legacy)
    assert window._body_size_declared is False            # placeholder values


def test_a_package_without_body_sizes_says_the_same(window, monkeypatch, tmp_path):  # noqa: F811
    """The package path already refused; it now names the missing sizes too."""
    from tests.ui_flow_helpers import package_extraction, soic_geometry
    prepare(window, monkeypatch, tmp_path, "SOIC")
    geometry = soic_geometry()
    geometry["body_height_mm"] = None
    run_extraction(window, package_extraction(geometry))
    window.group_3d.setChecked(True)
    window._refresh_output_actions()
    assert not window.btn_regen_3d.isEnabled()
    assert "mancano le quote documentate del corpo" in window.btn_regen_3d.toolTip()
    # The direct call names exactly the missing size.
    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    window._regenerate_3d()
    assert "(altezza)" in infos[-1]
