"""@brief Two open items of the 2026-11-16 review, closed on 2026-11-17 (fixer B).

- R1-2, service side: the review callback of `photo_inventory.extract_inventory`
  says whether a person really reviewed the proposals. Only then is a body
  stamped «reviewed by the user»; a callback that raises, or that answers
  «not reviewed», leaves the proposals as without a reviewer. A plain list
  (the contract before) still counts as reviewed.
- R1-8: the detector pre-flight (`validate_yolo_setup` / `validate_smd_setup`,
  a model load) runs in `DetectorPreflightWorker`, not on the GUI thread; a
  failure gives the same message and the idle window, «Interrompi» abandons it.

Every rule has a positive and a negative case that differ for a structural
reason (who answered the review; which thread ran the check; whether the
check failed, passed or was abandoned). No model weights, no network.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QThread, QTimer  # noqa: E402
from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.gui.workers import DetectorPreflightWorker, ExtractWorker, YoloPretestWorker  # noqa: E402
from kicadgen.services import messages, photo_inventory, photo_yolo  # noqa: E402
from kicadgen.services import smd_component_classifier  # noqa: E402
from kicadgen.services.ai_diagnostics import CallCancelled  # noqa: E402
from kicadgen.services.photo_inventory import ProposalReview, ReviewCancelled, review_outcome  # noqa: E402


# ── R1-2: the service is told whether a person reviewed ─────────────────────


def _proposal(identity, x_mm, **extra):
    """One detector proposal on a 20 x 10 mm board; `extra` adds detector flags."""
    left = 0.1 + 0.3 * (identity - 1)
    return dict(_id=identity, _box=(left, 0.1, left + 0.2, 0.3), name=f"x{identity}",
                kind="generic", x_mm=x_mm, y_mm=-3.0, length_mm=4.0, width_mm=2.0,
                height_mm=1.0, rotation_deg=0, color_hex="#303030", _uncertain=True, **extra)


def _detector_run(monkeypatch, tmp_path, review, detector="yolo_only", proposals=None):
    """@brief Run one local-detector branch on a fake calibration and a fake detector.

    @param review The `review_proposals` callback under test, or None.
    @param detector "yolo_only" (free) or "yolo" (paid reconciliation, faked
        by `_paid_route`).
    @param proposals What the detector proposes; by default one strong body
        and one weak one (`_yolo_review_only`), which only a person may keep.
    @return (report, log lines).
    """
    photo = np.zeros((100, 200, 3), np.uint8)
    monkeypatch.setattr(photo_inventory, "prepare_photo", lambda *a, **k: photo.copy())
    found = proposals if proposals is not None else [
        _proposal(1, -6.0), _proposal(2, 0.0, _yolo_review_only=True)]
    monkeypatch.setattr(photo_yolo, "detect_yolo_components",
                        lambda *a, **k: [dict(row) for row in found])
    tmp_path.joinpath("top.png").write_bytes(b"")
    lines = []
    report = photo_inventory.extract_inventory(
        [tmp_path / "top.png"], "", 20.0, 10.0, [], lambda **kw: pytest.fail("paid call"),
        progress=lines.append, top_photo_index=0, detector=detector,
        review_proposals=review)
    return report, lines


def _paid_route(monkeypatch):
    """@brief Fake the paid tail of the 'yolo' route; return the list of proposal sets sent to it.

    The reconciliation records what it was asked to pay for and answers the
    same rows; the per-crop reading and the edge-contact flagging do nothing.
    """
    from kicadgen.services import photo_global_inventory
    class Sent(list):
        """The proposal sets sent; `kwargs` are the keywords of the last request."""

        kwargs = None
    sent = Sent()

    def reconcile(photo, proposals, *args, **kwargs):
        sent.append([row["_id"] for row in proposals])
        sent.kwargs = kwargs
        return [dict(row) for row in proposals]
    monkeypatch.setattr(photo_global_inventory, "review_global_inventory", reconcile)
    monkeypatch.setattr(photo_inventory, "read_retained_bodies", lambda *a, **k: None)
    monkeypatch.setattr(photo_inventory, "flag_ai_bodies_on_edge_contacts", lambda *a, **k: None)
    return sent


def _warning_lines(lines):
    """The warnings of the proposal review among the log lines (the photo has warnings of its own)."""
    return [line for line in lines if messages.kind_of(line) == messages.WARNING
            and "Revisione dei corpi" in str(line)]


def _reviewed_flags(report):
    """The «reviewed by the user» stamp of every body of the report."""
    return [row.get("_presence_reviewed_by_user") for row in report.components]


def test_r1_2_a_review_that_raises_marks_nothing_reviewed(monkeypatch, tmp_path):
    """@brief Positive: the dialog raised -> no body reviewed, the weak one out, a warning logged."""
    def broken(photo_png, proposals, length, width):
        raise RuntimeError("vista rotta")
    report, lines = _detector_run(monkeypatch, tmp_path, broken)
    assert _reviewed_flags(report) == [False], "only the strong body, never as reviewed"
    assert "reviewed_proposal_count" not in report.calibration
    # ONE warning for one failed review, naming the failure and what stayed out.
    failed = _warning_lines(lines)
    assert len(failed) == 1 and "vista rotta" in str(failed[0])
    text = str(failed[0])
    assert "1 proposta prosegue come NON rivista" in text
    assert "1 proposta debole" in text and "da zero" not in text
    # The same channel as the sibling notices: it reaches the report too.
    assert [str(line) for line in report.warnings if "Revisione dei corpi" in str(line)] == [text]


def test_r1_2_an_answered_review_marks_the_kept_bodies_reviewed(monkeypatch, tmp_path):
    """@brief Negative: the same proposals, answered by a person, are reviewed -- the weak one too."""
    report, lines = _detector_run(monkeypatch, tmp_path,
                                  lambda png, proposals, length, width: ProposalReview(proposals, True))
    assert _reviewed_flags(report) == [True, True]
    assert report.calibration["reviewed_proposal_count"] == 2
    assert not any(messages.kind_of(line) == messages.WARNING and "NON riviste" in str(line)
                   for line in lines)


def test_r1_2_a_plain_list_still_counts_as_reviewed(monkeypatch, tmp_path):
    """@brief Legacy contract: a callback returning the kept list is a review, as before."""
    report, _ = _detector_run(monkeypatch, tmp_path,
                              lambda png, proposals, length, width: proposals[1:])
    assert _reviewed_flags(report) == [True]
    assert report.calibration["reviewed_proposal_count"] == 1


@pytest.mark.parametrize("answer", [
    lambda proposals: ProposalReview(list(proposals), False),
    lambda proposals: (list(proposals), False),
])
def test_r1_2_a_declined_review_is_like_no_reviewer(monkeypatch, tmp_path, answer):
    """@brief «Not reviewed» from the callback gives exactly what no callback gives."""
    declined, lines = _detector_run(monkeypatch, tmp_path,
                                    lambda png, proposals, length, width: answer(proposals))
    without, _ = _detector_run(monkeypatch, tmp_path, None)
    assert _reviewed_flags(declined) == _reviewed_flags(without) == [False]
    assert [row["x_mm"] for row in declined.components] == [row["x_mm"] for row in without.components]
    assert "reviewed_proposal_count" not in declined.calibration
    assert len(_warning_lines(lines)) == 1


# The three weak flavours: the detector's pair of flags, the review-only flag
# alone (saved or hand-built rows), the low-confidence flag alone.
_ROUTE_PROPOSALS = [
    _proposal(1, -6.0),
    dict(_proposal(2, -2.0, _yolo_review_only=True), _yolo_low_confidence=True),
    _proposal(3, 2.0, _yolo_review_only=True),
    _proposal(4, 6.0, _yolo_low_confidence=True),
]
for _index, _row in enumerate(_ROUTE_PROPOSALS):
    _row["_box"] = (0.05 + 0.22 * _index, 0.1, 0.2 + 0.22 * _index, 0.3)


@pytest.mark.parametrize("review", [
    lambda png, proposals, length, width: (_ for _ in ()).throw(RuntimeError("vista rotta")),
    lambda png, proposals, length, width: ProposalReview(list(proposals), False),
    lambda png, proposals, length, width: 7,       # not an answer at all
])
def test_r1_2_paid_route_an_unreviewed_set_never_pays_for_weak_proposals(monkeypatch, tmp_path, review):
    """@brief Positive, 'yolo': the reviewer failed -> only the strong proposal reaches the paid call."""
    sent = _paid_route(monkeypatch)
    report, lines = _detector_run(monkeypatch, tmp_path, review, "yolo", _ROUTE_PROPOSALS)
    assert sent == [[1]]
    assert "reviewed_proposal_count" not in report.calibration
    assert not any(row.get("_presence_reviewed_by_user") for row in report.components)
    warnings = _warning_lines(lines)
    assert len(warnings) == 1 and "3 proposte deboli" in str(warnings[0])
    assert "1 proposta prosegue come NON rivista" in str(warnings[0])
    assert str(warnings[0]) in [str(line) for line in report.warnings]
    # Something was still sent: the paid step is not told of an empty detector.
    assert sent.kwargs.get("unapproved_weak") == 3 and "da zero" not in str(warnings[0])


def test_r1_2_paid_route_an_answered_review_sends_what_the_user_kept(monkeypatch, tmp_path):
    """@brief Negative, 'yolo': the user kept a weak proposal -> it is what is sent, and counted reviewed."""
    sent = _paid_route(monkeypatch)
    report, lines = _detector_run(
        monkeypatch, tmp_path, lambda png, proposals, length, width: [proposals[0], proposals[2]],
        "yolo", _ROUTE_PROPOSALS)
    assert sent == [[1, 3]]
    assert report.calibration["reviewed_proposal_count"] == 2
    assert _warning_lines(lines) == []


def test_r1_2_paid_route_without_a_reviewer_is_unchanged(monkeypatch, tmp_path):
    """@brief No review callback: the service hands every proposal to the paid step, as before.

    @note The paid step's own filter (`split_ai_candidates`, faked away here)
        is what withholds the low-confidence ones there; nothing is dropped
        or warned about by the review block, which does not run.
    """
    sent = _paid_route(monkeypatch)
    report, lines = _detector_run(monkeypatch, tmp_path, None, "yolo", _ROUTE_PROPOSALS)
    assert sent == [[1, 2, 3, 4]]
    assert "reviewed_proposal_count" not in report.calibration
    assert not any("NON riviste" in str(line) for line in lines)


def test_r1_2_an_unreviewed_empty_set_is_not_a_reviewer_who_removed_everything(monkeypatch, tmp_path):
    """@brief Unreviewed and empty: ONE paid enumeration, announced truthfully; reviewed and empty: stop.

    @note User decision 2026-11-17 («Prosegui con l'AI»): with nothing left
        after a review that was not done, the paid route still lets the model
        list the bodies from scratch. What is asserted is that the user is
        told so BEFORE the call, and is never told that the detector found
        nothing or that a person removed everything.
    """
    weak_only = [_proposal(1, -6.0, _yolo_review_only=True)]
    sent = _paid_route(monkeypatch)
    unreviewed, lines = _detector_run(
        monkeypatch, tmp_path, lambda png, proposals, length, width: (list(proposals), False),
        "yolo", weak_only)
    assert unreviewed.calibration.get("inventory_strategy") != "review-kept-none"
    assert not any("sono state tolte" in str(line) for line in unreviewed.warnings)
    assert sent == [[]], "one paid request, with no proposal"
    told = [str(line) for line in unreviewed.warnings if "Revisione dei corpi" in str(line)]
    assert len(told) == 1 and len(_warning_lines(lines)) == 1
    assert "non eseguita" in told[0] and "Nessuna proposta prosegue" in told[0]
    assert "1 proposta debole" in told[0] and "nessuno l'ha approvata" in told[0]
    assert "da zero con una richiesta a pagamento" in told[0]
    # The paid step is told why it enumerates, so it does not say «found nothing».
    assert sent.kwargs.get("unapproved_weak") == 1
    sent.clear()
    removed, _ = _detector_run(
        monkeypatch, tmp_path, lambda png, proposals, length, width: [], "yolo", weak_only)
    assert removed.calibration["inventory_strategy"] == "review-kept-none" and sent == []
    assert any("sono state tolte" in str(line) for line in removed.warnings)
    assert not any("da zero" in str(line) for line in removed.warnings)


@pytest.mark.parametrize("answer", [
    lambda proposals: ([], False),
    lambda proposals: ProposalReview([], False),
    lambda proposals: ([proposals[2]], False),      # a subset nobody reviewed: only a weak row
])
def test_r1_2_an_unreviewed_answer_is_counted_on_the_detector_set(monkeypatch, tmp_path, answer):
    """@brief Positive: whatever rows an unreviewed answer carries, strong go on and weak are «left out».

    Three proposals, strong and weak mixed. The answer is not a selection:
    the two strong ones are sent, the weak one is named as left out and the
    paid step is told, so it never says the detector found nothing.
    """
    mixed = [_proposal(1, -6.0), _proposal(2, 0.0), _proposal(3, 6.0, _yolo_review_only=True)]
    sent = _paid_route(monkeypatch)
    report, lines = _detector_run(
        monkeypatch, tmp_path, lambda png, proposals, length, width: answer(proposals),
        "yolo", mixed)
    assert sent == [[1, 2]] and sent.kwargs.get("unapproved_weak") == 1
    told = _warning_lines(lines)
    assert len(told) == 1
    text = str(told[0])
    assert "2 proposte proseguono come NON riviste" in text and "1 proposta debole" in text
    assert "Nessuna proposta prosegue" not in text and "da zero" not in text
    assert "reviewed_proposal_count" not in report.calibration


def test_r1_2_a_reviewed_empty_answer_is_still_a_persons_choice(monkeypatch, tmp_path):
    """@brief Negative: the SAME empty set, reviewed, is a person removing everything -- no call."""
    mixed = [_proposal(1, -6.0), _proposal(2, 0.0), _proposal(3, 6.0, _yolo_review_only=True)]
    sent = _paid_route(monkeypatch)
    report, lines = _detector_run(
        monkeypatch, tmp_path, lambda png, proposals, length, width: ([], True), "yolo", mixed)
    assert sent == [] and report.calibration["inventory_strategy"] == "review-kept-none"
    assert not any("NON rivist" in str(line) for line in lines)


def test_r1_2_the_paid_request_line_never_says_found_nothing_for_unapproved_proposals():
    """@brief The request line: «found nothing» only when the detector really proposed nothing."""
    from kicadgen.services.photo_global_inventory import paid_request_message
    left_out = paid_request_message(2, 0, True, True, unapproved=3)
    assert "non ha trovato corpi" not in left_out
    assert "nessuno ha approvato: 3" in left_out and "da zero" in left_out
    empty = paid_request_message(2, 0, True, True)
    assert "non ha trovato corpi" in empty and "approvato" not in empty
    # With proposals to reconcile the count changes nothing: that line is already true.
    assert paid_request_message(2, 4, False, True, unapproved=3) == paid_request_message(2, 4, False, True)


def test_r1_2_proposals_withheld_by_the_paid_step_are_named_too(monkeypatch, tmp_path):
    """@brief The real paid step, all proposals low-confidence and unkept: the line names them.

    @note Negative: an empty detector output keeps «non ha trovato corpi».
    """
    from kicadgen.services import photo_global_inventory as global_inventory
    photo = np.zeros((100, 200, 3), np.uint8)

    def announced(components):
        lines = []

        def call(**kwargs):
            raise RuntimeError("stop after the announcement")
        try:
            global_inventory.review_global_inventory(
                photo, components, 20.0, 10.0, "m", call, lines.append, None, detector_ran=True)
        except Exception:  # noqa: BLE001 - only the lines before the request matter here
            pass
        return [str(line) for line in lines if "una sola richiesta a pagamento" in str(line)]
    weak = announced([_proposal(1, -6.0, _yolo_low_confidence=True)])
    assert len(weak) == 1 and "non ha trovato corpi" not in weak[0]
    assert "nessuno ha approvato: 1" in weak[0]
    none = announced([])
    assert len(none) == 1 and "non ha trovato corpi" in none[0]


@pytest.mark.parametrize("stop, expected", [
    (lambda: None, ReviewCancelled),
    (lambda: ProposalReview(None, True), ReviewCancelled),
    (lambda: (_ for _ in ()).throw(ReviewCancelled("no")), ReviewCancelled),
    (lambda: (_ for _ in ()).throw(CallCancelled("stop")), CallCancelled),
])
def test_r1_2_a_cancellation_is_never_taken_for_a_failed_review(monkeypatch, tmp_path, stop, expected):
    """@brief Cancelling (None, or the cancellation types) stops the run; it does not go on unreviewed."""
    with pytest.raises(expected):
        _detector_run(monkeypatch, tmp_path, lambda png, proposals, length, width: stop())


def test_r1_2_review_outcome_tells_a_pair_from_two_proposals():
    """@brief A (kept, bool) pair is an outcome; a tuple of two proposals is a kept list."""
    first, second = _proposal(1, 0.0), _proposal(2, 1.0)
    assert review_outcome(([first], False)) == ProposalReview([first], False)
    assert review_outcome(([first], True)) == ProposalReview([first], True)
    assert review_outcome((first, second)) == ProposalReview([first, second], True)
    assert review_outcome([first]) == ProposalReview([first], True)
    assert review_outcome(None).kept is None
    assert review_outcome(ProposalReview(None, False)).kept is None
    assert review_outcome((None, False)).kept is None


@pytest.mark.parametrize("flag, expected", [
    (np.bool_(False), False), (np.bool_(True), True), (0, False), (1, True)])
def test_r1_2_review_outcome_reads_the_flag_by_truth_not_by_type(flag, expected):
    """@brief A non-bool flag (numpy.bool_, 0/1) is still the flag of a pair, never a second proposal."""
    first = _proposal(1, 0.0)
    outcome = review_outcome(([first], flag))
    assert outcome == ProposalReview([first], expected) and type(outcome.reviewed) is bool
    assert review_outcome(ProposalReview([first], flag)).reviewed is expected


def _answering_worker(qapp, answer):
    """An ExtractWorker whose review request is answered at once by `answer(worker, proposals)`."""
    worker = ExtractWorker([], model="m")
    worker.review_requested.connect(lambda png, proposals, length, width: answer(worker, proposals))
    return worker


def test_r1_2_the_worker_forwards_an_unreviewed_delivery(qapp):
    """@brief Positive: `deliver_review(kept, reviewed=False)` reaches the service as not reviewed."""
    worker = _answering_worker(qapp, lambda w, proposals: w.deliver_review(proposals, reviewed=False))
    try:
        answer = worker._review_proposals(b"png", [{"name": "U1"}], 20.0, 10.0)
        assert answer == ProposalReview([{"name": "U1"}], False)
    finally:
        worker.deleteLater()


def test_r1_2_the_worker_keeps_the_plain_list_for_an_answered_review(qapp):
    """@brief Negative: the default delivery is the kept list, and a later one is not tainted."""
    worker = _answering_worker(qapp, lambda w, proposals: w.deliver_review(proposals, reviewed=False))
    try:
        worker._review_proposals(b"png", [{"name": "U1"}], 20.0, 10.0)
        worker.review_requested.disconnect()
        worker.review_requested.connect(lambda png, proposals, length, width:
                                        worker.deliver_review(proposals[:1]))
        assert worker._review_proposals(b"png", [{"name": "U1"}, {"name": "C1"}], 20.0, 10.0) \
            == [{"name": "U1"}]
        worker.review_requested.disconnect()
        worker.review_requested.connect(lambda png, proposals, length, width:
                                        worker.deliver_review(None, reviewed=False))
        assert worker._review_proposals(b"png", [{"name": "U1"}], 20.0, 10.0) is None
    finally:
        worker.deleteLater()


def test_r1_2_a_pretest_whose_review_failed_reports_no_reviewed_body(qapp, monkeypatch, tmp_path):
    """@brief End to end, worker + service: an unreviewed delivery, then an answered one."""
    def run(deliver):
        photo = np.zeros((100, 200, 3), np.uint8)
        monkeypatch.setattr(photo_inventory, "prepare_photo", lambda *a, **k: photo.copy())
        monkeypatch.setattr(photo_yolo, "detect_yolo_components",
                            lambda *a, **k: [_proposal(1, -6.0),
                                             _proposal(2, 0.0, _yolo_review_only=True)])
        tmp_path.joinpath("top.png").write_bytes(b"")
        worker = YoloPretestWorker([tmp_path / "top.png"], None, 20.0, 10.0, None, None)
        reports, errors = [], []
        worker.done.connect(reports.append)
        worker.error.connect(errors.append)
        worker.review_requested.connect(lambda png, proposals, length, width:
                                        deliver(worker, proposals))
        worker.run()
        worker.deleteLater()
        assert errors == [] and len(reports) == 1
        return reports[0]
    failed = run(lambda worker, proposals: worker.deliver_review(proposals, reviewed=False))
    assert _reviewed_flags(failed) == [False]
    answered = run(lambda worker, proposals: worker.deliver_review(proposals))
    assert _reviewed_flags(answered) == [True, True]


class _ReviewWorker:
    """@brief A worker waiting for the detector review; records what the window delivers."""

    photo_detector = "yolo"

    def __init__(self, revision):
        self._analysis_revision = revision
        self.delivered = "nothing"
        self.reviewed = "nothing"

    def deliver_review(self, kept, reviewed=True):
        """@brief Record the delivery."""
        self.delivered, self.reviewed = kept, reviewed


WINDOW_PROPOSALS = [{"name": "U1", "x_mm": 0.0},
                    {"name": "C1", "x_mm": 1.0, "_yolo_review_only": True}]


def _review_setup(window, monkeypatch):
    """A pending review request: worker in the slot, bundle writes recorded."""
    worker = _ReviewWorker(window._analysis_revision)
    window._extract_worker = worker
    window._reviewed_this_extraction = False
    bundles = []
    monkeypatch.setattr(window, "_write_review_bundle",
                        lambda rows, *a, **k: bundles.append((rows, k.get("reviewed"))) or None)
    monkeypatch.setattr(window.i18n, "retranslate_widgets", lambda widget: None)
    return worker, bundles


def test_r1_2_the_window_delivers_a_failed_view_as_not_reviewed(window, monkeypatch):
    """@brief Positive: the dialog raises -> every proposal delivered with reviewed=False, bundle unreviewed."""
    from kicadgen.gui import photo_review_dialog
    worker, bundles = _review_setup(window, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("vista rotta")
    monkeypatch.setattr(photo_review_dialog, "PhotoReviewDialog", broken)
    try:
        window._on_review_requested(b"png", WINDOW_PROPOSALS, 10.0, 10.0)
        assert worker.reviewed is False
        assert [p["name"] for p in worker.delivered] == ["U1", "C1"]
        assert bundles == [(WINDOW_PROPOSALS, False)]
        assert window._reviewed_this_extraction is False
        assert not hasattr(window, "_unmark_user_reviewed_presence"), \
            "the window-side correction is gone: the service does it"
    finally:
        window._extract_worker = None


def test_r1_2_the_window_delivers_an_accepted_view_as_reviewed(window, monkeypatch):
    """@brief Negative: the dialog was accepted -> the user's choice, reviewed, bundle reviewed."""
    from kicadgen.gui import photo_review_dialog
    worker, bundles = _review_setup(window, monkeypatch)

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def kept_components(self):
            return [dict(WINDOW_PROPOSALS[1])]
    monkeypatch.setattr(photo_review_dialog, "PhotoReviewDialog", Dialog)
    monkeypatch.setattr(window, "_run_modal", lambda dialog: QDialog.DialogCode.Accepted)
    try:
        window._on_review_requested(b"png", WINDOW_PROPOSALS, 10.0, 10.0)
        assert worker.reviewed is True
        assert [p["name"] for p in worker.delivered] == ["C1"]
        assert bundles[0][1] is True and window._reviewed_this_extraction is True
    finally:
        window._extract_worker = None


# ── R1-8: the detector pre-flight runs off the GUI thread ───────────────────


class _Preflight:
    """@brief Fake validation functions installed on the real service modules.

    Records the thread of every check; `gate` (when given) holds the YOLO
    check until the test releases it, as a slow model load would.
    """

    def __init__(self, monkeypatch, *, error=None, gate=None):
        self.yolo_threads, self.smd_paths = [], []
        self.started = threading.Event()
        self.error, self.gate = error, gate
        monkeypatch.setattr(photo_yolo, "validate_yolo_setup", self._yolo)
        monkeypatch.setattr(smd_component_classifier, "validate_smd_setup", self._smd)

    def _yolo(self, path=None):
        self.yolo_threads.append(QThread.currentThread())
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(20), "the test never released the fake model load"
        if self.error is not None:
            raise self.error

    def _smd(self, path=None):
        self.smd_paths.append(path)


def _ready(window, monkeypatch, detector="yolo"):
    """A window that may start an analysis with `detector`; returns the list of continuations.

    The step right after the pre-flight (`_selected_documents`) records that
    it was reached, with the window state it found, and stops the start
    there: these tests are about the pre-flight, not about the run.
    """
    window.field_name.setText("TEST")
    monkeypatch.setattr(window, "_extract_block_reason", lambda: "")
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData(detector))
    continued = []

    def documents():
        continued.append(dict(tabs=window.tabs.isEnabled(),
                              overlay_hidden=window.analysis_overlay.isHidden(),
                              worker=window._preflight_worker))
        raise ValueError("nessun documento")
    monkeypatch.setattr(window, "_selected_documents", documents)
    return continued


def _while_validating(preflight, action):
    """Run `action` on the GUI thread once the fake model load is in flight."""
    def poll():
        if preflight.started.is_set():
            action()
        else:
            QTimer.singleShot(5, poll)
    QTimer.singleShot(0, poll)


def _warnings(monkeypatch):
    """Record QMessageBox.warning calls as (title, text)."""
    shown = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *a: shown.append((title, text)))
    return shown


def _settle(window, qapp, worker):
    """Let an abandoned pre-flight end and its `finished` reach the window."""
    assert worker.wait(20000)
    for _ in range(20):
        qapp.processEvents()
        if window._preflight_worker is None:
            break


def test_r1_8_the_checks_run_off_the_gui_thread_and_the_start_goes_on(window, qapp, monkeypatch):
    """@brief Positive: the check runs in another thread while the GUI thread serves events."""
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    during = {}

    def gui_is_alive():
        # This timer firing at all is the proof: the GUI thread is not inside the check.
        try:
            overlay = window.analysis_overlay
            during.update(thread=QThread.currentThread(), tabs=window.tabs.isEnabled(),
                          overlay_hidden=overlay.isHidden(), abortable=overlay.is_abortable(),
                          tool=overlay.tool.text(), busy=window._background_work_running(),
                          button=window.btn_extract_ai.text(),
                          enabled=window.btn_extract_ai.isEnabled(),
                          menus=window.menuBar().isEnabled())
            window._extract_with_ai()            # a second click is ignored
            close = QCloseEvent()
            window.closeEvent(close)             # the window does not close under the load
            during["close_accepted"] = close.isAccepted()
        finally:
            gate.set()
    _while_validating(preflight, gui_is_alive)
    warnings = _warnings(monkeypatch)
    window._extract_with_ai()
    gui_thread = qapp.thread()
    assert len(preflight.yolo_threads) == 1 and preflight.yolo_threads[0] is not gui_thread
    assert during["thread"] is gui_thread
    assert during == dict(thread=gui_thread, tabs=False, overlay_hidden=False, abortable=True,
                          tool=during["tool"], busy=True,
                          button=window.i18n.text("Preparazione…"), enabled=False,
                          menus=False, close_accepted=False)
    assert "YOLO" in during["tool"] and window.menuBar().isEnabled()
    # The start went on exactly once, with the window given back first.
    assert continued == [dict(tabs=True, overlay_hidden=True, worker=None)]
    assert warnings == [] and preflight.smd_paths == []
    assert window._preflight_worker is None and not window._background_work_running()
    assert not window._extract_preparing


def test_r1_8_a_failed_check_gives_the_same_message_and_the_idle_window(window, qapp, monkeypatch):
    """@brief Negative: the check fails -> same warning as before, nothing started, window at rest."""
    preflight = _Preflight(monkeypatch, error=ValueError("pesi mancanti"))
    continued = _ready(window, monkeypatch)
    warnings = _warnings(monkeypatch)
    revealed = []
    monkeypatch.setattr(window, "_reveal_ai_tools", lambda: revealed.append(1))
    idle_text = window.btn_extract_ai.text()
    tabs_before = window.tabs.isEnabled()
    window._extract_with_ai()
    assert preflight.yolo_threads[0] is not qapp.thread()
    assert warnings == [(window._photo_detector_text("error_title"),
                         window._photo_detector_text("error", error="pesi mancanti"))]
    assert continued == []
    assert window.tabs.isEnabled() is tabs_before and window.analysis_overlay.isHidden()
    assert not window.analysis_overlay.is_abortable()
    assert window._preflight_worker is None and not window._background_work_running()
    assert not window._extract_preparing and not window._extraction_busy
    assert window.btn_extract_ai.text() == idle_text
    assert revealed == [1], "the AI tools group is shown as a hint, as before"
    assert window.menuBar().isEnabled()
    # Re-enabled a moment later, as after every early exit.
    assert not window.btn_extract_ai.isEnabled()
    window._reenable_extract_button()
    assert window.btn_extract_ai.isEnabled()


def test_r1_8_interrompi_during_the_checks_cancels_cleanly(window, qapp, monkeypatch):
    """@brief Positive: «Interrompi» confirmed -> window back at once, nothing started, no message."""
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch, detector="yolo_smd")
    monkeypatch.setattr(gui, "smd_route_available", lambda: True)
    warnings = _warnings(monkeypatch)
    asked = []
    monkeypatch.setattr(window, "_confirm_abort",
                        lambda kind, phase: asked.append((kind, phase)) or True)
    revision = window._analysis_revision
    idle_text = window.btn_extract_ai.text()
    # The state of the previous analysis: no run started, so none of it may be reset.
    params = object()
    run_state = dict(_auto_after_extraction=True, _auto_generation_params=params,
                     _extract_finish_pending=True, _inventory_pause_pending=True)
    for name, value in run_state.items():
        setattr(window, name, value)
    logged = []
    monkeypatch.setattr(window, "_append_log", logged.append)
    _while_validating(preflight, window._request_abort)
    worker = None
    try:
        window._extract_with_ai()
        worker = window._preflight_worker
        # Returned while the fake load is still held: the wait ended on the stop.
        assert not gate.is_set() and worker is not None and worker.isRunning()
        assert [kind for kind, _ in asked] == ["local"]
        assert "Verifica del rilevatore locale" in asked[0][1]
        assert continued == [] and warnings == []
        assert worker.stop_confirmed and worker.cancel_event.is_set()
        # Nothing of a run was touched: revision and run state as they were.
        assert window._analysis_revision == revision
        assert {name: getattr(window, name) for name in run_state} == run_state
        assert any("Verifica del rilevatore locale" in str(line) and "INTERRUZIONE" in str(line)
                   for line in logged)
        assert window.tabs.isEnabled() and window.analysis_overlay.isHidden()
        assert window.menuBar().isEnabled()
        assert not window._extract_preparing and not window._extraction_busy
        assert window.btn_extract_ai.text() == idle_text
        # The load cannot be interrupted: it still counts, so nothing else starts on it.
        assert window._background_work_running() and not window.btn_extract_ai.isEnabled()
        window._extract_with_ai()
        assert len(preflight.yolo_threads) == 1
    finally:
        gate.set()
        if worker is not None:
            _settle(window, qapp, worker)
    # Its late end frees the slot and the commands; the SMD check was skipped.
    assert window._preflight_worker is None and not window._background_work_running()
    assert window._abandoned_workers == [] and window.btn_extract_ai.isEnabled()
    assert preflight.smd_paths == [] and continued == [] and warnings == []
    for name in ("_auto_after_extraction", "_auto_generation_params",
                 "_extract_finish_pending", "_inventory_pause_pending"):
        setattr(window, name, False if name != "_auto_generation_params" else None)


def test_r1_8_a_stop_confirmed_after_the_checks_ended_still_starts_nothing(window, qapp, monkeypatch):
    """@brief «Sì» given when the checks have already ended: the analysis must not start.

    The confirmation box runs a nested event loop inside the pre-flight's
    wait. Here the fake box lets the fake load end, waits for the thread and
    serves the events (so `finished` is delivered) before answering yes --
    the case that used to log «niente da fermare» and go on to the paid run.
    @note Negative: `test_r1_8_the_checks_run_off_the_gui_thread_and_the_start_goes_on`
        (no stop: the start goes on) and
        `test_r1_8_a_refused_interrompi_lets_the_start_go_on` («No»).
    """
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    warnings = _warnings(monkeypatch)
    logged = []
    monkeypatch.setattr(window, "_append_log", logged.append)
    seen = {}

    def confirm_late(kind, phase):
        worker = window._preflight_worker
        gate.set()
        assert worker.wait(20000)
        for _ in range(5):
            qapp.processEvents()
        seen.update(running=worker.isRunning(), failed=worker.failed)
        return True
    monkeypatch.setattr(window, "_confirm_abort", confirm_late)
    idle_text = window.btn_extract_ai.text()
    _while_validating(preflight, window._request_abort)
    window._extract_with_ai()
    assert seen == dict(running=False, failed=False), "the checks had ended, and well"
    assert continued == [] and warnings == []
    assert any("INTERRUZIONE" in str(line) and "Verifica del rilevatore locale" in str(line)
               for line in logged)
    assert not any("niente da fermare" in str(line) for line in logged)
    # Nothing is left behind: no worker, no abandoned work, window at rest.
    assert window._preflight_worker is None and window._abandoned_workers == []
    assert not window._background_work_running() and not window._extract_preparing
    assert window.tabs.isEnabled() and window.analysis_overlay.isHidden()
    assert window.menuBar().isEnabled() and window.btn_extract_ai.text() == idle_text
    window._reenable_extract_button()
    assert window.btn_extract_ai.isEnabled()


def test_r1_8_the_menu_bar_was_already_off_stays_off(window, qapp, monkeypatch):
    """@brief The wait restores the menu bar and each shortcut to what it WAS, not to «on»."""
    _Preflight(monkeypatch)
    continued = _ready(window, monkeypatch)
    window.menuBar().setEnabled(False)
    off, on = window._shortcuts[0], window._shortcuts[1]
    off.setEnabled(False)
    try:
        window._extract_with_ai()
        assert len(continued) == 1 and not window.menuBar().isEnabled()
        assert not off.isEnabled() and on.isEnabled()
    finally:
        window.menuBar().setEnabled(True)
        off.setEnabled(True)


def test_r1_8_ctrl_e_opens_nothing_during_the_wait_and_works_after(window, qapp, monkeypatch):
    """@brief A real Ctrl+E key click: no body editor over the pre-flight; the editor again after.

    The editor is modal: opened during the wait it would hold the
    continuation, and the paid analysis would start when it closed.
    @note The click after the wait is the control: the same key, the same
        window, and the editor opens -- so the silence during the wait is the
        guard, not a key that never arrives.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    opened = []
    monkeypatch.setattr(window, "_edit_components", lambda: opened.append(1))
    monkeypatch.setattr(window, "_photo_rows", [{"name": "U1"}])
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)
    during = {}

    def press():
        try:
            QTest.keyClick(window, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
            during["by_key"] = list(opened)
            # The handler itself refuses too, whoever calls it.
            window._shortcut_edit_components()
            during["by_call"] = list(opened)
            during["shortcuts"] = [shortcut.isEnabled() for shortcut in window._shortcuts]
        finally:
            gate.set()
    _while_validating(preflight, press)
    window._extract_with_ai()
    assert during["by_key"] == [] and during["by_call"] == []
    assert during["shortcuts"] and not any(during["shortcuts"])
    assert len(continued) == 1 and all(shortcut.isEnabled() for shortcut in window._shortcuts)
    QTest.keyClick(window, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
    assert opened == [1]


def _shown(window, qapp):
    """Show and activate the window, so keys and modal windows behave as on screen."""
    from PyQt6.QtTest import QTest
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)


def test_r1_8_the_cards_log_button_opens_nothing_during_the_wait(window, qapp, monkeypatch):
    """@brief A real Space on the focused «Apri log AI» button: nothing during the wait, the log after.

    The log is modal: opened over the wait, «Interrompi» is unreachable
    behind it and the analysis would start when it closed.
    @note The key after the wait, on the same button of a card started
        again, is the control: it does ask for the log.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    overlay = window.analysis_overlay
    # The real connection opens a modal; here the requests are only counted.
    overlay.log_requested.disconnect()
    asked = []
    overlay.log_requested.connect(lambda: asked.append(1))
    _shown(window, qapp)
    during = {}

    def press():
        try:
            during["enabled"] = overlay.log_button.isEnabled()
            QTest.keyClick(overlay.log_button, Qt.Key.Key_Space)
            QTest.mouseClick(overlay.log_button, Qt.MouseButton.LeftButton)
            during["asked"] = list(asked)
        finally:
            gate.set()
    _while_validating(preflight, press)
    window._extract_with_ai()
    assert during == dict(enabled=False, asked=[]) and len(continued) == 1
    assert overlay.log_button.isEnabled()
    try:
        overlay.start(abortable=False)
        assert overlay.log_button.hasFocus()
        QTest.keyClick(overlay.log_button, Qt.Key.Key_Space)
        assert asked == [1]
    finally:
        window._hide_analysis_overlay()


def test_r1_8_checks_that_end_under_another_modal_do_not_start_the_analysis(window, qapp, monkeypatch):
    """@brief Positive: a modal window is open when the checks end -> no start, and the user is told."""
    from PyQt6.QtWidgets import QApplication
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    warnings = _warnings(monkeypatch)
    told = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda parent, title, text, *a: told.append((title, text)))
    _shown(window, qapp)
    box = QDialog(window)
    box.setModal(True)
    seen = {}

    def open_modal():
        try:
            box.show()
            qapp.processEvents()
            seen["modal"] = QApplication.activeModalWidget() is box
        finally:
            gate.set()
    _while_validating(preflight, open_modal)
    try:
        window._extract_with_ai()
        assert seen == dict(modal=True)
        assert continued == [] and warnings == []
        assert len(told) == 1 and told[0][0] == window.i18n.text("Analisi non avviata")
        assert "non è stata avviata" in told[0][1]
        assert window._preflight_worker is None and not window._background_work_running()
        assert window.tabs.isEnabled() and window.analysis_overlay.isHidden()
        assert window.menuBar().isEnabled() and not window._extract_preparing
    finally:
        box.close()
        box.deleteLater()


def test_r1_8_a_refused_stop_question_open_at_the_end_lets_the_start_go_on(window, qapp, monkeypatch):
    """@brief Negative: the modal open at the end is the stop question, answered «No» -> the start goes on.

    The same structure as the test above (a modal window is open when the
    checks end), told apart by WHICH modal it is: the question's answer is
    the decision, so nothing is refused on its account.
    """
    from PyQt6.QtWidgets import QApplication
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch)
    told = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda parent, title, text, *a: told.append((title, text)))
    _shown(window, qapp)
    seen = {}

    def question(kind, phase):
        box = QDialog(window)
        box.setModal(True)
        box.show()
        qapp.processEvents()
        worker = window._preflight_worker
        gate.set()
        assert worker.wait(20000)
        for _ in range(5):
            qapp.processEvents()
        seen.update(modal=QApplication.activeModalWidget() is box, running=worker.isRunning())
        box.close()
        box.deleteLater()
        return False
    monkeypatch.setattr(window, "_confirm_abort", question)
    _while_validating(preflight, window._request_abort)
    window._extract_with_ai()
    assert seen == dict(modal=True, running=False)
    # The only box is the continuation's own (the fake documents step); no refusal.
    assert len(continued) == 1
    assert [title for title, _ in told] == [window.i18n.text("Completa la documentazione")]
    assert window._preflight_question_open is False


def test_r1_8_a_card_that_fails_to_open_leaves_no_exit_hook_behind(window, qapp, monkeypatch):
    """@brief The card raises before the thread starts: reported like a failed check, hook dropped.

    @note Negative: a pre-flight that ran keeps its hook until `finished`,
        which drops it.
    """
    from kicadgen.gui import quit_wait
    _Preflight(monkeypatch)
    continued = _ready(window, monkeypatch)
    warnings = _warnings(monkeypatch)
    created = []
    real = gui.DetectorPreflightWorker

    def recording(*args, **kwargs):
        created.append(real(*args, **kwargs))
        return created[-1]
    monkeypatch.setattr(gui, "DetectorPreflightWorker", recording)
    start = window.analysis_overlay.start

    def broken(*args, **kwargs):
        raise RuntimeError("scheda rotta")
    monkeypatch.setattr(window.analysis_overlay, "start", broken)
    window._extract_with_ai()
    worker = created[0]
    assert getattr(worker, quit_wait._HOOK) is None and not worker.isRunning()
    assert warnings == [(window._photo_detector_text("error_title"),
                         window._photo_detector_text("error", error="scheda rotta"))]
    assert continued == [] and window._preflight_worker is None
    assert window.tabs.isEnabled() and window.menuBar().isEnabled()
    assert all(shortcut.isEnabled() for shortcut in window._shortcuts)
    # Negative: the card works, the thread runs, the hook lives until `finished`.
    monkeypatch.setattr(window.analysis_overlay, "start", start)
    window._reenable_extract_button()
    gate = threading.Event()
    held = _Preflight(monkeypatch, gate=gate)
    hooks = []

    def look():
        try:
            hooks.append(getattr(created[-1], quit_wait._HOOK))
        finally:
            gate.set()
    _while_validating(held, look)
    window._extract_with_ai()
    assert len(created) == 2 and hooks[0] is not None and len(continued) == 1
    for _ in range(5):
        qapp.processEvents()
    assert getattr(created[1], quit_wait._HOOK) is None


def test_r1_8_a_refused_interrompi_lets_the_start_go_on(window, qapp, monkeypatch):
    """@brief Negative: «Interrompi» not confirmed -> the wait goes on and the start continues."""
    gate = threading.Event()
    preflight = _Preflight(monkeypatch, gate=gate)
    continued = _ready(window, monkeypatch, detector="yolo_smd")
    monkeypatch.setattr(gui, "smd_route_available", lambda: True)
    monkeypatch.setattr(window, "_confirm_abort", lambda kind, phase: False)
    revision = window._analysis_revision

    def refuse_then_finish():
        try:
            window._request_abort()
        finally:
            gate.set()
    _while_validating(preflight, refuse_then_finish)
    window._extract_with_ai()
    assert window._analysis_revision == revision
    assert len(continued) == 1 and continued[0]["tabs"] is True
    # Both checks ran, in order, the SMD one with the session's choice.
    assert preflight.smd_paths == [window._smd_model_option()]
    assert window._preflight_worker is None and window._abandoned_workers == []


def test_r1_8_a_thread_that_does_not_start_runs_the_checks_in_place(window, qapp, monkeypatch):
    """@brief A QThread that never starts must not hang the window: the checks run on this thread."""
    preflight = _Preflight(monkeypatch)
    continued = _ready(window, monkeypatch)
    monkeypatch.setattr(QThread, "start", lambda self, *a: None)
    window._extract_with_ai()
    assert preflight.yolo_threads == [qapp.thread()]
    assert len(continued) == 1 and window._preflight_worker is None
    assert window.tabs.isEnabled() and window.analysis_overlay.isHidden()


def test_r1_8_an_ai_only_start_runs_no_preflight(window, qapp, monkeypatch):
    """@brief Negative: with the AI detector no model is loaded and no worker is created."""
    preflight = _Preflight(monkeypatch)
    continued = _ready(window, monkeypatch, detector="ai")
    created = []
    monkeypatch.setattr(gui, "DetectorPreflightWorker",
                        lambda *a, **k: created.append(a) or pytest.fail("no pre-flight expected"))
    window._extract_with_ai()
    assert preflight.yolo_threads == [] and created == [] and len(continued) == 1


def test_r1_8_the_worker_records_the_outcome_and_never_raises(qapp, monkeypatch):
    """@brief Worker alone: success, failure with its text, SMD only when asked and not cancelled."""
    preflight = _Preflight(monkeypatch)
    worker = DetectorPreflightWorker("y.onnx", "s.onnx", check_smd=True)
    worker.run()
    assert (worker.failed, worker.error) == (False, "") and preflight.smd_paths == ["s.onnx"]
    worker.deleteLater()

    skipped = DetectorPreflightWorker("y.onnx", "s.onnx", check_smd=False)
    skipped.run()
    cancelled = DetectorPreflightWorker("y.onnx", "s.onnx", check_smd=True)
    cancelled.cancel_event.set()
    cancelled.run()
    assert preflight.smd_paths == ["s.onnx"], "no SMD check when not asked or once cancelled"
    assert not skipped.failed and not cancelled.failed
    skipped.deleteLater()
    cancelled.deleteLater()

    preflight.error = ValueError("runtime assente")
    failing = DetectorPreflightWorker(None)
    failing.run()
    assert (failing.failed, failing.error) == (True, "runtime assente")
    failing.deleteLater()
