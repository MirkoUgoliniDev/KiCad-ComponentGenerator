"""@brief The footprint preview restyles its own drawn items, never stale wrappers from scene.items().

GitHub «Tests» died twice (2026-11-09 run 42623377393, 2026-11-12 run
43188942383) with a Windows access violation in `_style_drawing_item`: a
wrapper of an item Qt had deleted with `scene.clear()` was still referenced,
and `scene.items()` handed it back for a new item at the same address with
the old item's Python type. The fix restyles from the lists the drawers fill
(`_fp_drawn_drawing_items`, `_fp_drawn_pad_items`), emptied before each clear.
"""
from kicadgen.core.footprint import validate_footprint_drawings
from tests.test_footprint_drawing_kicad import dialogs, fp  # noqa: F401 - fixtures


def test_restyling_survives_wrappers_kept_across_many_redraws(fp, dialogs):  # noqa: F811
    """@brief Positive: the scenario that crashed (300 redraws, every old wrapper kept alive)."""
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [1, 0]]},
        {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[5, 8], [6, 8]], "dtype": "leader",
         "text": "X", "frame": "circle"},
    ])
    fp._draw_preview()
    kept = []
    for round_ in range(300):
        # Stale after the next clear: exactly what a loop variable or a
        # closure holding a scene item does.
        kept.extend(fp.preview_scene.items())
        changed = {"frame": "rect", "text": "Y"} if round_ % 2 else {"frame": "circle", "text": "Z"}
        assert fp.set_drawing_properties(1, changed)
    # The last round (299) is odd: its values are the ones kept.
    assert fp.footprint_drawings[1]["text"] == "Y" and fp.footprint_drawings[1]["frame"] == "rect"


def test_the_drawn_item_lists_hold_only_the_current_scene(fp, dialogs):  # noqa: F811
    """@brief Negative: the lists are emptied at every clear, so they never pin dead items."""
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [1, 0], [1, 1]]},
    ])
    fp._draw_preview()
    first = list(fp._fp_drawn_drawing_items)
    assert first and all(item.scene() is fp.preview_scene for item, _index, _kind in first)
    assert {index for _item, index, _kind in first} == {0}
    fp._draw_preview()
    second = fp._fp_drawn_drawing_items
    assert len(second) == len(first)
    assert not any(new is old for (new, _a, _b) in second for (old, _c, _d) in first)


def test_pad_numbers_are_tagged_by_their_real_type(fp, dialogs):  # noqa: F811
    """@brief Positive and negative (review 2026-11-12): after redraws with old wrappers kept, every
    «numbers» item is really a text and no pad copper is tagged «numbers»."""
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    from kicadgen.gui.footprint_view import LAYER_ROLE, PAD_KEY_ROLE
    from tests.test_package_project_gui import acquire
    acquire(fp)
    kept = []
    for _round in range(50):
        kept.extend(fp.preview_scene.items())
        fp._draw_preview()
    numbers = [item for item in fp.preview_scene.items() if item.data(LAYER_ROLE) == "numbers"]
    # 9 is Qt's QGraphicsSimpleTextItem::Type (PyQt6's attribute on the subclass is QGraphicsItem's 1).
    assert QGraphicsSimpleTextItem().type() == 9
    assert numbers and all(item.type() == 9 for item in numbers)
    copper = [item for item in fp.preview_scene.items() if str(item.data(PAD_KEY_ROLE) or "").startswith("package:")]
    assert copper and all(item.data(LAYER_ROLE) == "pads" for item in copper)


def test_a_restyle_after_the_scene_died_does_nothing(fp, dialogs):  # noqa: F811
    """@brief Negative (review 2026-11-12): a late call with the scene already deleted reads no freed item."""
    from PyQt6 import sip
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [1, 0]]},
    ])
    fp._draw_preview()
    scene = fp.preview_scene
    real_isdeleted = sip.isdeleted
    calls = []

    def dead(obj):
        calls.append(obj)
        return obj is scene or real_isdeleted(obj)
    import kicadgen.gui.main_window as mw
    original = mw.MainWindow._style_drawing_item
    styled = []
    try:
        sip.isdeleted = dead
        mw.MainWindow._style_drawing_item = lambda self, *a, **k: styled.append(a)
        fp._refresh_drawing_selection()
    finally:
        sip.isdeleted = real_isdeleted
        mw.MainWindow._style_drawing_item = original
    assert scene in calls and styled == []
    fp._refresh_drawing_selection()        # alive again: it restyles as before
