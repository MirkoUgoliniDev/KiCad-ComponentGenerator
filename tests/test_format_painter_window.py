"""@brief The «Copia formato» toggle of the 3D toolbar (user, 2026-09-27)."""
from tests.test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa


def _rows():
    return [dict(name=name, kind=kind, x_mm=x, y_mm=0.0, length_mm=length, width_mm=width,
                 height_mm=height, rotation_deg=0.0, color_hex=color, marking="")
            for name, kind, x, length, width, height, color in (
                ("Q1", "ic_sot", -10.0, 2.9, 2.4, 1.1, "#202020"),
                ("Q2", "generic", 0.0, 4.0, 1.0, 0.5, "#FF00FF"),
                ("Q3", "generic", 10.0, 3.5, 3.5, 2.0, "#404040"))]


def test_copy_then_paste_on_two_components_regenerates_once(window, monkeypatch):
    window._set_photo_components(_rows())
    applied = []
    monkeypatch.setattr(window, "_apply_photo_review", lambda rows: applied.append(rows))
    button = window.btn_format_painter_3d
    assert not button.isHidden() and button.isCheckable()
    button.setChecked(True)
    assert window.preview_3d.format_painter_active is True
    for row in (0, 1, 2):
        window._on_format_painter_click(row)
    assert applied == []                                        # no regeneration per click
    assert "Q2, Q3" in window.statusBar().currentMessage()
    # 27/09: a visible, non-modal popup beside the status bar.
    assert window._format_toast.title.text() == "Incollato su Q3"
    assert "Q2, Q3" in window._format_toast.body.text()
    button.setChecked(False)
    assert window.preview_3d.format_painter_active is False
    assert len(applied) == 1
    q1, q2, q3 = applied[0]
    for target, x in ((q2, 0.0), (q3, 10.0)):
        assert (target["length_mm"], target["width_mm"], target["height_mm"]) == (2.9, 2.4, 1.1)
        assert target["kind"] == "ic_sot" and target["x_mm"] == x
    assert q2["color_hex"] == "#FF00FF"                         # the colour stays its own
    assert q1["length_mm"] == 2.9 and q1["x_mm"] == -10.0       # the source is untouched


def test_a_click_with_the_mode_off_changes_nothing(window):
    window._set_photo_components(_rows())
    before = [dict(r) for r in window._photo_rows]
    window._on_format_painter_click(0)
    window._on_format_painter_click(1)
    assert window._photo_rows == before


def test_switching_off_without_pasting_does_not_regenerate(window, monkeypatch):
    window._set_photo_components(_rows())
    applied = []
    monkeypatch.setattr(window, "_apply_photo_review", lambda rows: applied.append(rows))
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)                          # copied only
    assert window._format_toast.title.text() == "Formato di Q1 copiato"
    window.btn_format_painter_3d.setChecked(False)
    assert applied == []


def test_the_command_is_hidden_without_components(window):
    window._set_photo_components([])
    assert window.btn_format_painter_3d.isHidden()
