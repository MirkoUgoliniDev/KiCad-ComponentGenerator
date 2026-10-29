"""@brief Review 2026-10-29, scope GUIO: photo review, model candidates, component check, theme.

Each fix carries a positive test (the defect scenario now behaves) and a
negative one (the case the fix must not change). No FreeCAD and no KiCad:
the long-running work is replaced by fakes that honour (or ignore) the
cancellation channel the production code hands them.
"""
import ast
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage, QShowEvent
from PyQt6.QtWidgets import QDialog

from kicadgen.cad3d import kicad_models
from kicadgen.gui import component_check_dialog as ccd
from kicadgen.gui import model_candidates_dialog as mcd
from kicadgen.gui import photo_review_dialog as prd
from kicadgen.services.component_check import Check

ROOT = Path(__file__).resolve().parents[1]


def _png():
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _components(count=2):
    # Spread across the 40 x 20 mm board, every body inside it.
    pitch = 36.0 / count
    return [dict(name=f"U{i}", kind="ic", x_mm=-18 + pitch * (i + .5), y_mm=0.0,
                 length_mm=pitch * .8, width_mm=2.0, height_mm=1.0, rotation_deg=0.0,
                 confidence="high")
            for i in range(count)]


@pytest.fixture
def review(app):
    """A shown photo review dialog; closed (and its search stopped) at teardown."""
    made = []

    def make(count=2):
        dialog = prd.PhotoReviewDialog(_png(), _components(count), 40, 20)
        dialog.show()
        app.processEvents()
        made.append(dialog)
        return dialog

    yield make
    for dialog in made:
        dialog.close()
    app.processEvents()


def _wait_until(app, condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return condition()


class _CancellableSearch:
    """Fake `model_candidates`: runs until cancelled (like a freecadcmd measure) or released."""

    def __init__(self):
        self.release = threading.Event()
        self.cancels = []

    def __call__(self, component, *, limit=5, cancel=None, **_):
        self.cancels.append(cancel)
        while not self.release.is_set():
            if cancel is not None and cancel.is_set():
                raise kicad_models.MatchingCancelled(measured={})
            time.sleep(0.01)
        return {"status": "unresolved", "candidates": [], "selected_path": None}


@pytest.fixture
def search(monkeypatch):
    fake = _CancellableSearch()
    monkeypatch.setattr(kicad_models, "model_candidates", fake)
    yield fake
    fake.release.set()


# --- U1: the candidate search is cancelled with its dialog and at exit --------------------

def test_u1_rejecting_the_review_cancels_its_running_search(review, search, app):
    dialog = review()
    dialog._select_row(0)
    dialog.choose_model()
    worker = dialog._match_worker
    assert worker is not None and _wait_until(app, lambda: search.cancels)
    assert search.cancels[0] is worker.cancel, "the search must receive the cancel channel"
    shown = []
    dialog._show_model_candidates = lambda *args: shown.append(args)
    dialog.reject()
    assert worker.cancel.is_set()
    assert worker.wait(3000), "a cancelled search must end promptly"
    app.processEvents()
    assert shown == []


def test_u1_quit_hook_cancels_then_waits_a_bounded_time(review, search, app):
    dialog = review()
    dialog._select_row(0)
    dialog.choose_model()
    worker = dialog._match_worker
    assert _wait_until(app, lambda: search.cancels)
    started = time.monotonic()
    worker._stop_for_quit()
    assert worker.isFinished()
    assert time.monotonic() - started < prd.MATCH_QUIT_WAIT_MS / 1000


def test_u1_quit_wait_is_bounded_even_if_the_search_ignores_the_cancel(monkeypatch, app):
    """@brief Negative of the cancel path: a search deaf to the cancel still cannot hold the exit."""
    release = threading.Event()
    monkeypatch.setattr(kicad_models, "model_candidates",
                        lambda component, **_: (release.wait(10), {})[1])
    monkeypatch.setattr(prd, "MATCH_QUIT_WAIT_MS", 200)
    worker = prd._MatchWorker({"name": "U1"})
    worker.start()
    started = time.monotonic()
    worker._stop_for_quit()
    assert time.monotonic() - started < 2.0
    release.set()
    assert worker.wait(3000)


def test_u1_a_search_left_running_delivers_its_result(review, search, app):
    """@brief Negative: nobody cancelled, so the candidates reach the dialog as before."""
    dialog = review()
    dialog._select_row(0)
    shown = []
    dialog._show_model_candidates = lambda item, snapshot, report: shown.append(report)
    dialog.choose_model()
    worker = dialog._match_worker
    search.release.set()
    assert _wait_until(app, lambda: dialog._match_worker is None)
    assert not worker.cancel.is_set()
    assert shown and shown[0]["status"] == "unresolved"


def test_u1_a_result_ready_after_the_cancel_is_dropped(monkeypatch, app):
    """@brief A search that ignored the cancel and finished anyway emits nothing."""
    release = threading.Event()
    monkeypatch.setattr(kicad_models, "model_candidates",
                        lambda component, **_: (release.wait(5), {"status": "late"})[1])
    worker = prd._MatchWorker({"name": "U1"})
    received = []
    worker.done.connect(received.append)
    worker.failed.connect(received.append)
    worker.start()
    worker.stop()
    release.set()
    assert worker.wait(3000)
    app.processEvents()
    assert received == []


def test_u1_each_dialog_cancels_only_its_own_search(review, search, app):
    first, second = review(), review()
    for dialog in (first, second):
        dialog._select_row(0)
        dialog.choose_model()
    worker_a, worker_b = first._match_worker, second._match_worker
    assert worker_a is not worker_b
    first.reject()
    assert worker_a.cancel.is_set() and not worker_b.cancel.is_set()
    assert worker_a.wait(3000)
    second.accept()
    assert worker_b.cancel.is_set() and worker_b.wait(3000)


# --- U3: the status line never stays on «in corso» --------------------------------------

def test_u3_a_stale_result_says_it_was_discarded(review, search, app):
    dialog = review()
    dialog._select_row(0)
    dialog.choose_model()
    dialog.text_fields["name"].setText("U0-edited")
    dialog._text_edited("name")
    search.release.set()
    assert _wait_until(app, lambda: dialog._match_worker is None)
    assert dialog.model_status.text() == prd.tx(
        "Ricerca candidati scartata: il componente è cambiato durante la ricerca. "
        "Doppio clic per ripeterla.")
    assert dialog.model_button.isEnabled()


def test_u3_a_fresh_result_still_reports_candidates_ready(review, search, app, monkeypatch):
    """@brief Negative: the body was not touched, so the candidates are offered as before."""

    class _Chooser:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mcd, "ModelCandidatesDialog", _Chooser)
    dialog = review()
    dialog._select_row(0)
    dialog.choose_model()
    search.release.set()
    assert _wait_until(app, lambda: dialog._match_worker is None)
    assert dialog.model_status.text() == prd.tx(
        "Candidati pronti. Premi Applica correzioni per salvare le scelte.")


def test_u3_a_search_stopped_without_closing_clears_the_running_status(review, search, app):
    dialog = review()
    dialog._select_row(0)
    dialog.choose_model()
    dialog._match_worker.stop()
    assert _wait_until(app, lambda: dialog._match_worker is None)
    assert dialog.model_status.text() == prd.tx("Ricerca candidati interrotta senza risultato.")


# --- U2: a selection redraws only what it changed ---------------------------------------

def _count_refreshes(dialog, monkeypatch):
    refreshed = []
    original = dialog._refresh_item
    monkeypatch.setattr(dialog, "_refresh_item",
                        lambda item: (refreshed.append(dialog._items.index(item)),
                                      original(item))[1])
    return refreshed


def test_u2_a_selection_click_redraws_only_the_old_and_new_selected(review, monkeypatch):
    dialog = review(20)
    dialog._select_row(3, center=False)
    refreshed = _count_refreshes(dialog, monkeypatch)
    dialog._select_row(11, center=False)
    assert sorted(set(refreshed)) == [3, 11]
    assert dialog._items[11].halo.isVisible() and not dialog._items[3].halo.isVisible()


def test_u2_a_deletion_still_renumbers_every_following_row(review, monkeypatch):
    """@brief Negative: renumbering changes the look of rows the selection never touched."""
    dialog = review(6)
    dialog._select_row(1, center=False)
    dialog.delete_selected()
    texts = [dialog.component_list.item(row).text() for row in range(len(dialog._items))]
    assert [text.split(".")[0] for text in texts] == [str(n) for n in range(1, 6)]
    assert texts[1].startswith("2. U2")


def test_u2_a_lasso_redraws_its_members_once(review, monkeypatch):
    dialog = review(10)
    refreshed = _count_refreshes(dialog, monkeypatch)
    dialog._lasso_drawn(dialog.photo_rect)
    assert len(dialog._lasso_items) == 10
    # Every member once (they joined the group) and no second full pass.
    assert len(refreshed) == len(set(refreshed)) == 10
    assert all(item.halo.isVisible() for item in dialog._items)


def test_u2_the_step_probe_is_cached_by_path_but_fresh_for_the_selected(review, monkeypatch,
                                                                        tmp_path):
    step = tmp_path / "m.step"
    step.write_text("x", encoding="utf-8")
    dialog = review(3)
    for item in dialog._items:
        item.component["model_selection"] = dict(status="confirmed", path=str(step))
        dialog._refresh_item(item)
    missing = dialog._tr("Il file STEP scelto non è disponibile: scegliere nuovamente.")
    step.unlink()
    dialog._select_row(2, center=False)
    # The selected body is probed again and says the file is gone...
    assert missing in dialog.warning_label.text()
    # ...and another path is a new key, never an old answer.
    other = tmp_path / "n.step"
    other.write_text("x", encoding="utf-8")
    dialog._items[0].component["model_selection"] = dict(status="confirmed", path=str(other))
    assert missing not in dialog._warnings(dialog._items[0])


# --- U6: minimise/restore keeps the zoom ------------------------------------------------

class _SpontaneousShow(QShowEvent):
    def spontaneous(self):
        return True


def test_u6_a_spontaneous_show_keeps_the_user_zoom(review):
    dialog = review()
    dialog.view.zoom(3)
    scale = dialog.view.transform().m11()
    dialog.showEvent(_SpontaneousShow())
    assert dialog.view.transform().m11() == pytest.approx(scale)


def test_u6_a_program_show_with_auto_fit_still_fits(review):
    """@brief Negative: the first show (auto-fit on, not spontaneous) fits the photo."""
    dialog = review()
    dialog.view.resetTransform()
    assert dialog.view.auto_fit()
    dialog.showEvent(QShowEvent())
    assert dialog.view.transform().m11() == pytest.approx(dialog.view._fit_scale)


# --- U4: the preview child is killed with its dialog ------------------------------------

def test_u4_a_cancelled_preview_child_is_killed_promptly():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(mcd.PreviewCancelled):
        mcd._run_preview_child([sys.executable, "-c", "import time; time.sleep(30)"], cancel)
    assert time.monotonic() - started < 5.0


def test_u4_an_uncancelled_child_returns_its_output_and_exit_status():
    """@brief Negative: with the channel unset the child runs to the end as with run(check=True)."""
    cancel = threading.Event()
    done = mcd._run_preview_child([sys.executable, "-c", "print('KICADGEN_OK')"], cancel)
    assert "KICADGEN_OK" in done.stdout
    with pytest.raises(subprocess.CalledProcessError):
        mcd._run_preview_child([sys.executable, "-c", "raise SystemExit(3)"], cancel)


def test_u4_closing_the_chooser_sets_its_preview_cancel(app, monkeypatch):
    monkeypatch.setattr(mcd, "_thumbnail", lambda *args, **kwargs: None)
    dialog = mcd.ModelCandidatesDialog(
        dict(name="U1", length_mm=3, width_mm=2), {"candidates": []})
    assert not dialog._preview_cancel.is_set()
    dialog.reject()
    assert dialog._preview_cancel.is_set()


def test_u4_the_preview_worker_publishes_its_cancel_to_the_renderer(monkeypatch):
    seen = []
    monkeypatch.setattr(mcd, "_thumbnail",
                        lambda *args, **kwargs: seen.append(mcd._PREVIEW_CANCEL.get()))
    cancel = threading.Event()
    mcd._Preview(1, "x.step", 0.0, cancel=cancel).run()
    mcd._Preview(2, "x.step", 0.0).run()
    assert seen == [cancel, None]


# --- U5: the mesh cache is bounded by size ----------------------------------------------

@pytest.fixture
def mesh_cache():
    mcd._MESH_CACHE.clear()
    yield mcd._MESH_CACHE
    mcd._MESH_CACHE.clear()


def test_u5_large_meshes_are_bounded_by_triangles(mesh_cache):
    big = {"vertices": [(0, 0, 0)], "triangles": [(0, 0, 0)] * 100_000}
    for index in range(4):
        mcd._store_mesh(("big", index), big, None)
    assert sum(len(entry[0]["triangles"]) for entry in mesh_cache.values()) \
        <= mcd._MESH_CACHE_TRIANGLES
    assert ("big", 3) in mesh_cache, "the newest mesh is never the one dropped"


def test_u5_small_meshes_still_fill_every_entry(mesh_cache):
    """@brief Negative: the entry bound still decides for small packages."""
    small = {"vertices": [(0, 0, 0)], "triangles": [(0, 0, 0)] * 500}
    for index in range(mcd._MESH_CACHE_SIZE):
        mcd._store_mesh(("small", index), small, None)
    assert len(mesh_cache) == mcd._MESH_CACHE_SIZE


# --- U7: the KiCad verification can be stopped ------------------------------------------

def _fake_verification(monkeypatch, gate):
    def verify(*, emit, **_):
        for index in range(50):
            emit(Check("OK", f"Controllo {index}", "ok"))
            if index == 0:
                gate.wait(5)
    monkeypatch.setattr(ccd, "verify_component", verify)


def test_u7_interrompi_stops_after_the_running_command(app, monkeypatch):
    gate = threading.Event()
    _fake_verification(monkeypatch, gate)
    dialog = ccd.ComponentCheckDialog({}, tx=lambda text: text)
    assert _wait_until(app, lambda: dialog.results)
    dialog.stop_button.click()
    assert not dialog.stop_button.isEnabled()
    gate.set()
    assert _wait_until(app, lambda: not dialog._running)
    assert dialog.verdict.text() == "VERIFICA INTERROTTA"
    # The check in flight when the stop came is kept; no further one started.
    assert [check.title for check in dialog.results] == ["Controllo 0", "Controllo 1"]
    dialog.close()


def test_u7_an_uninterrupted_verification_reaches_its_normal_verdict(app, monkeypatch):
    """@brief Negative: nobody pressed «Interrompi», every check runs and the verdict is the usual."""
    gate = threading.Event()
    gate.set()
    _fake_verification(monkeypatch, gate)
    dialog = ccd.ComponentCheckDialog({}, tx=lambda text: text)
    assert _wait_until(app, lambda: not dialog._running)
    assert len(dialog.results) == 50
    assert dialog.verdict.text() == "PRONTO PER L’USO"
    dialog.close()


# --- U8: English QSS comments, no dead duplicates ---------------------------------------

def test_u8_qss_comments_are_english():
    import re
    from kicadgen.gui import theme
    from tests.test_documentation import _looks_italian
    comments = re.findall(r"/\*(.*?)\*/", theme.APP_QSS + theme.TAB_QSS, re.S)
    assert comments, "the style sheets carry comments: the check must see them"
    assert [c for c in comments if _looks_italian(c)] == []


def test_u8_start_page_defines_each_tooltip_once():
    tree = ast.parse((ROOT / "kicadgen" / "gui" / "start_page.py").read_text(encoding="utf-8"))
    module_level = [target.id for node in tree.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)]
    assert module_level.count("NO_PROJECTS_TOOLTIP") == 1
    attributes = [target.attr for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  for target in node.targets if isinstance(target, ast.Attribute)]
    assert attributes.count("_open_tooltip") == 1
