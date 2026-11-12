"""@brief A collapsed CollapsibleGroupBox shrinks to its title in any layout (user report, 2026-11-12)."""
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from kicadgen.gui.collapsible_group import CollapsibleGroupBox
from tests.test_photo_inventory_gui import gui  # noqa: F401 - registers the window fixture


def _column(qapp, height=600):
    """A fixed-height column: a stretched group, a neighbour below, a trailing stretch."""
    host = QWidget()
    host.setFixedSize(250, height)
    column = QVBoxLayout(host)
    stretched = CollapsibleGroupBox("Proprietà", expanded=True)
    QVBoxLayout(stretched.body).addWidget(QLabel("contenuto"))
    below = CollapsibleGroupBox("Controlli", expanded=True)
    QVBoxLayout(below.body).addWidget(QLabel("riga"))
    column.addWidget(stretched, 1)
    column.addWidget(below)
    column.addStretch(0)
    host.show()
    qapp.processEvents()
    return host, stretched, below


def _settle(qapp, host):
    """Let the layout apply a size-policy change."""
    host.layout().activate()
    qapp.processEvents()


def test_a_collapsed_stretched_group_is_only_as_tall_as_its_title(qapp):
    """@brief Positive: closing the group that held the free height gives it back;
    the group below climbs right under its title."""
    host, stretched, below = _column(qapp)
    open_height = stretched.height()
    assert open_height > 300, "precondition: the open group takes the free height"
    stretched.setExpanded(False)
    _settle(qapp, host)
    # Qt never gives a widget less than its minimum size hint: that, not the
    # size hint alone, is the height of a title-only group.
    title_only = max(stretched.sizeHint().height(), stretched.minimumSizeHint().height())
    assert stretched.height() == title_only
    assert stretched.height() < 60
    assert below.y() < stretched.y() + stretched.height() + 20
    host.close()


def test_reopening_gives_the_free_height_back(qapp):
    """@brief Negative: the fixed height is only for the collapsed state."""
    host, stretched, _below = _column(qapp)
    open_height = stretched.height()
    stretched.setExpanded(False)
    _settle(qapp, host)
    stretched.setExpanded(True)
    _settle(qapp, host)
    assert stretched.height() == open_height
    host.close()


def test_the_callers_policy_is_kept_across_a_collapse(qapp):
    """@brief Negative: Fixed while collapsed must not become the caller's policy;
    a policy set while collapsed is the one restored on expanding."""
    group = CollapsibleGroupBox("Gruppo", expanded=False)
    assert group.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
    assert group.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    group.setExpanded(True)
    assert group.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
    group.setExpanded(False)
    group.setExpanded(True)
    assert group.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding


def test_the_footprint_properties_group_shrinks_when_closed(window, qapp):  # noqa: F811
    """@brief Positive on the real column of the footprint tab: «Proprietà»
    closed is a title row and «Controlli» sits right below it."""
    group = window.fp_properties_group
    panel = group.parentWidget()
    panel.setFixedHeight(900)
    panel.show()
    _settle(qapp, panel)
    assert group.height() > 200, "precondition: open, it takes the free height"
    group.setExpanded(False)
    _settle(qapp, panel)
    assert group.height() < 60
    checks = window.fp_checks_list.parentWidget().parentWidget()
    assert checks.y() < group.y() + group.height() + 20
    group.setExpanded(True)
    _settle(qapp, panel)
    assert group.height() > 200


def test_with_every_other_group_closed_the_checks_box_stays_tight(window, qapp):  # noqa: F811
    """@brief Negative of the screenshot of 2026-11-12: «Layer» open, «Disegno»
    and «Proprietà» closed; «Controlli» must not swallow the free height and
    show its row floating in an empty box: it stays as tall as its content."""
    group = window.fp_properties_group
    panel = group.parentWidget()
    panel.setFixedHeight(900)
    panel.show()
    checks = window.fp_checks_list.parentWidget().parentWidget()
    drawing = next(child for child in panel.findChildren(type(group)) if child.baseTitle() == "Disegno")
    _settle(qapp, panel)
    natural = checks.sizeHint().height()
    drawing.setExpanded(False)
    group.setExpanded(False)
    _settle(qapp, panel)
    assert checks.height() <= natural + 1, (checks.height(), natural)
    # The list sits right under the box title, not centred in a tall box.
    assert window.fp_checks_list.geometry().top() < 40
    # Every group above closed too (second screenshot of 2026-11-12).
    layers = next(child for child in panel.findChildren(type(group)) if child.baseTitle() == "Layer")
    layers.setExpanded(False)
    _settle(qapp, panel)
    assert checks.height() <= natural + 1, (checks.height(), natural)
    assert window.fp_checks_list.geometry().top() < 40
    layers.setExpanded(True)
    drawing.setExpanded(True)
    group.setExpanded(True)
