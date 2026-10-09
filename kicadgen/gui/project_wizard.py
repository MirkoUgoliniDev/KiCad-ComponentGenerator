"""@brief Guided project preparation with independent pages and persistent navigation."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget)


class CollapsibleTools(QGroupBox):
    """@brief Keep AI settings available in an initially closed section."""

    def __init__(self, title, parent=None):
        """@brief Build a header toggle and a separately hidden content widget."""
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setProperty("i18n_skip", True)
        header.addWidget(self.summary, 1)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.toggled.connect(self.setExpanded)
        header.addWidget(self.toggle)
        layout.addLayout(header)
        self.content = QWidget()
        layout.addWidget(self.content)
        self.content.hide()

    def setExpanded(self, expanded):
        """@brief Open settings without changing any saved model selection."""
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)


class ProjectWizard(QWidget):
    """@brief Guide documents and analysis, returning to the project chooser at the start."""

    page_changed = pyqtSignal(int)
    projects_requested = pyqtSignal()

    def __init__(self, pages, extract_button, translate, parent=None):
        """@brief Reparent existing editors, retaining their signals and project state."""
        super().__init__(parent)
        self._translate = translate
        self._sources = ("Documenti", "Montaggio e analisi")
        self.setStyleSheet(
            "QPushButton#wizardStep:checked { background: #313b53; "
            "color: #89b4fa; border: 1px solid #89b4fa; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        nav = QHBoxLayout()
        self.steps = []
        for index in range(len(pages)):
            button = QPushButton()
            button.setObjectName("wizardStep")
            button.setCheckable(True)
            button.setProperty("i18n_skip", True)
            button.clicked.connect(lambda checked=False, i=index: self.set_page(i))
            nav.addWidget(button)
            self.steps.append(button)
        layout.addLayout(nav)
        self.stack = QStackedWidget()
        self.scrolls = []
        for editor in pages:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.addWidget(editor)
            page_layout.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.scrolls.append(scroll)
            self.stack.addWidget(scroll)
        layout.addWidget(self.stack, 1)
        footer = QHBoxLayout()
        self.position = QLabel()
        self.position.setProperty("i18n_skip", True)
        footer.addWidget(self.position, 1)
        self.back = QPushButton()
        self.back.setProperty("i18n_skip", True)
        self.back.clicked.connect(self._go_back)
        footer.addWidget(self.back)
        self.next = QPushButton()
        self.next.setObjectName("primary")
        self.next.setProperty("i18n_skip", True)
        self.next.clicked.connect(lambda: self.set_page(self.stack.currentIndex() + 1))
        footer.addWidget(self.next)
        self.extract_button = extract_button
        footer.addWidget(extract_button)
        layout.addLayout(footer)
        self.retranslate()
        self.set_page(0)

    def retranslate(self):
        """@brief Translate dynamic navigation without changing the current page."""
        for index, (button, source) in enumerate(zip(self.steps, self._sources)):
            button.setText(f"{index + 1} · {self._translate(source)}")
        self.next.setText(self._translate("Continua"))
        self._update_navigation()

    def _update_navigation(self):
        """@brief Keep one selected step and one primary action in the footer."""
        index = self.stack.currentIndex()
        for i, button in enumerate(self.steps):
            button.setChecked(i == index)
        self.back.setText(self._translate("Indietro" if index > 0 else "Torna ai progetti"))
        last = self.stack.count() - 1
        self.next.setVisible(index < last)
        self.extract_button.setVisible(index == last)
        self.position.setText(f"{index + 1} / {self.stack.count()}")

    def _go_back(self):
        """@brief Go to documents, or ask the owner to save and close the project."""
        if self.stack.currentIndex() == 0:
            self.projects_requested.emit()
        else:
            self.set_page(self.stack.currentIndex() - 1)

    def set_page(self, index):
        """@brief Navigate locally; moving forward never starts a paid analysis."""
        self.stack.setCurrentIndex(max(0, min(self.stack.count() - 1, index)))
        self._update_navigation()
        self.page_changed.emit(self.stack.currentIndex())
