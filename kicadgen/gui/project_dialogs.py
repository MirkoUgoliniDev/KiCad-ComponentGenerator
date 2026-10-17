"""@brief Create, open and rename projects: a project IS its folder.

User decision of 2026-09-24: the name field is no longer free text that
silently opens whatever folder has the typed name. A new project cannot take
the name of an existing project folder, and the interface asks explicitly
whether to open an existing project or create a new one (File menu: «Nuovo
progetto…», «Apri progetto…», «Rinomina progetto…»).
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QVBoxLayout, QGroupBox, QRadioButton,
                             QButtonGroup, QHBoxLayout, QPushButton, QStackedWidget,
                             QWidget)

#: What makes a folder of the library root a project: the analysis creates the
#: documents folder, the log or the review bundle, the generation creates
#: `<name>.pretty`. A folder with none of them (a leftover cache, a copy made
#: by hand) is not listed, so it cannot be opened as if it were a project.
PROJECT_MARKERS = ("docs", "analisi-ai.log", "photo-review", "photo-cache")

#: Characters Windows refuses in a file or folder name.
_FORBIDDEN = set('<>:"/\\|?*')

#: Names Windows reserves for devices, with or without an extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class ProjectEntry:
    """@brief One existing project of the library root."""

    name: str
    path: Path
    modified: datetime


def is_project_folder(path: Path) -> bool:
    """@brief Tell whether a folder of the library root is a project.

    @param path Candidate folder.
    @return True when it holds one of PROJECT_MARKERS or a `<name>.pretty`.
    """
    if not path.is_dir() or path.name.startswith("."):
        return False
    if any((path / marker).exists() for marker in PROJECT_MARKERS):
        return True
    return (path / f"{path.name}.pretty").is_dir()


def list_projects(root) -> list:
    """@brief Existing projects of the library root, most recently modified first.

    @param root Library root (the «Cartella di lavoro»).
    @return List of ProjectEntry; empty when the root does not exist.
    @note The modification time is the newest one among the folder and its
        direct children, so a project whose documents changed yesterday is
        listed as changed yesterday.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    entries = []
    for path in root.iterdir():
        if not is_project_folder(path):
            continue
        try:
            stamps = [path.stat().st_mtime] + [child.stat().st_mtime for child in path.iterdir()]
        except OSError:
            continue
        entries.append(ProjectEntry(path.name, path, datetime.fromtimestamp(max(stamps))))
    return sorted(entries, key=lambda entry: entry.modified, reverse=True)


def taken_project_names(root) -> frozenset:
    """@brief Names of the folders directly under the library root, casefolded.

    @param root Library root.
    @return The casefolded folder names; empty when `root` is not a folder.
    """
    root = Path(root)
    if not root.is_dir():
        return frozenset()
    return frozenset(path.name.casefold() for path in root.iterdir() if path.is_dir())


def name_problem(root, name: str, current: str = "", taken=None) -> str:
    """@brief Why `name` cannot be used for a new or renamed project, or "".

    @param root Library root.
    @param name Proposed project name.
    @param current Name of the project being renamed ("" for a new one): a
        rename that only changes letter case is allowed.
    @param taken The casefolded folder names of `root` already read
        (`taken_project_names`), or None to read the folder now.
    @return Italian message (source text for i18n), or "" when the name is usable.
    @note Existing folders are compared ignoring letter case: on Windows
        «Test» and «TEST» are the same folder, so they are the same project.
    """
    name = name.strip()
    if not name:
        return "Scrivi il nome del progetto."
    if any(char in _FORBIDDEN or ord(char) < 32 for char in name):
        return "Il nome non puo' contenere questi caratteri: < > : \" / \\ | ? *"
    if name.endswith((".", " ")) or name in (".", ".."):
        return "Il nome non puo' finire con un punto o uno spazio."
    if name.split(".")[0].upper() in _RESERVED:
        return "Questo nome e' riservato da Windows."
    if taken is None:
        taken = taken_project_names(root)
    if name.casefold() in taken and name.casefold() != current.strip().casefold():
        return "Esiste gia' un progetto con questo nome: aprilo da «Apri progetto…»."
    return ""


class ProjectNameDialog(QDialog):
    """@brief Ask the name of a new project, or the new name of the current one.

    @note OK stays disabled while the name is unusable, and the reason is
        written under the field as it is typed: an existing project's name is
        refused here, never silently opened.
    """

    def __init__(self, root, title: str, current: str = "", tr=None, parent=None):
        super().__init__(parent)
        self._tr = tr or (lambda text: text)
        self._root = Path(root)
        self._current = current
        #: (modification stamp of the root, its folder names); see `_taken`.
        self._taken_snapshot = None
        self.setWindowTitle(self._tr(title))
        self.setModal(True)
        self.field = QLineEdit(current)
        self.field.setPlaceholderText(self._tr("Nome del progetto"))
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #f38ba8;")
        self.problem.setProperty("i18n_skip", True)
        hint = QLabel(self._tr("Il nome del progetto e' il nome della sua cartella, "
                               "del footprint e del modello STEP."))
        hint.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.field)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self.field.textChanged.connect(self._validate)
        self.resize(460, self.sizeHint().height())
        self._validate()

    def _taken(self, fresh: bool = False) -> frozenset:
        """The root's folder names, read again only when the root folder changed.

        Every keystroke validates the name: listing the library and testing
        each entry for a folder at each key was the cost of the typing. The
        root's modification stamp changes whenever a folder is created,
        renamed or removed in it, so it keys the snapshot; `fresh` reads the
        folder regardless (the final check of `accept`).
        """
        try:
            stamp = self._root.stat().st_mtime_ns
        except OSError:
            stamp = None
        if fresh or stamp is None or self._taken_snapshot is None or self._taken_snapshot[0] != stamp:
            self._taken_snapshot = (stamp, taken_project_names(self._root))
        return self._taken_snapshot[1]

    def _problem(self, fresh: bool = False) -> str:
        """Why the typed name cannot be used, or ""; `fresh` re-reads the root folder."""
        name = self.field.text()
        problem = name_problem(self._root, name, self._current, taken=self._taken(fresh))
        if not problem and self._current and name.strip() == self._current.strip():
            problem = "E' gia' il nome del progetto."
        return problem

    def _validate(self):
        problem = self._problem()
        self.problem.setText(self._tr(problem) if problem and self.field.text().strip() else "")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problem)

    def accept(self):
        """@brief Close with OK only for a usable name.

        @note The disabled OK button is not the only way to accept (Enter in
            the field, a caller): the name is checked again here, so an
            existing project's name can never get through.
        """
        if self._problem(fresh=True):
            self._validate()
            return
        super().accept()

    def project_name(self) -> str:
        """@brief The accepted name, without surrounding spaces.

        @return The stripped text of the name field.
        """
        return self.field.text().strip()


class OpenProjectDialog(QDialog):
    """@brief Choose one of the existing projects of the library root."""

    def __init__(self, root, current: str = "", tr=None, parent=None):
        super().__init__(parent)
        self._tr = tr or (lambda text: text)
        self.setWindowTitle(self._tr("Apri progetto"))
        self.setModal(True)
        self.projects = list_projects(root)
        self.list = QListWidget()
        for entry in self.projects:
            item = QListWidgetItem(f"{entry.name}    ·    {entry.modified:%d/%m/%Y %H:%M}")
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            item.setToolTip(str(entry.path))
            if entry.name == current:
                item.setText(item.text() + "    ·    " + self._tr("aperto"))
            self.list.addItem(item)
        self.empty = QLabel(self._tr("Nessun progetto nella cartella di lavoro: creane uno "
                                     "con «Nuovo progetto…»."))
        self.empty.setWordWrap(True)
        self.empty.setVisible(not self.projects)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.list.currentRowChanged.connect(self._update_open)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self._tr("Progetti nella cartella di lavoro, dal piu' recente:")))
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.buttons)
        if self.projects:
            self.list.setCurrentRow(0)
        self._update_open()
        self.resize(520, 420)

    def _update_open(self, *_args):
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(
            self.list.currentItem() is not None)

    def selected_name(self) -> str:
        """@brief Name of the chosen project, or "" when nothing is chosen.

        @return The project name stored in the current list item, or "".
        """
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""


class ProjectKindDialog(QDialog):
    """@brief Two-stage choice: project class first, then its mounting subtype.

    @note Nothing is preselected: the user has to pick both the class and the
        subtype before «Next» enables, so a hurried OK cannot choose a type.
    """

    OPTIONS = {
        "module": (("aggregate_tht", "project_kind.board_tht"),
                   ("aggregate_castellated", "project_kind.board_castellated")),
        "single": (("single_tht", "project_kind.single_tht"),
                   ("single_smd", "project_kind.single_smd")),
    }
    RADIO_STYLE = (
        "QRadioButton { spacing: 9px; padding: 6px 3px; }"
        "QRadioButton::indicator { width: 16px; height: 16px; "
        "border: 1px solid #6c7086; border-radius: 8px; background: #313244; }"
        "QRadioButton::indicator:checked { border: 2px solid #89b4fa; "
        "background: #89b4fa; }"
    )

    def __init__(self, tr=None, selected="", parent=None):
        super().__init__(parent)
        self._tr = tr or (lambda text: text)
        self.setWindowTitle(self._tr("project_kind.title"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self._selected_key = selected if selected in {
            key for options in self.OPTIONS.values() for key, _ in options
        } else ""
        self._kind = ("single" if self._selected_key.startswith("single_") else
                      "module" if self._selected_key else "")
        self._refinement_buttons = {}

        layout = QVBoxLayout(self)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages)
        self._build_kind_page()
        self._build_refinement_page()

        buttons = QHBoxLayout()
        self.back_button = QPushButton(self._tr("project_kind.back"), self)
        self.cancel_button = QPushButton(self._tr("project_kind.cancel"), self)
        self.next_button = QPushButton(self._tr("project_kind.next"), self)
        self.confirm_button = QPushButton(self._tr("project_kind.confirm"), self)
        self.back_button.clicked.connect(self._back)
        self.cancel_button.clicked.connect(self.reject)
        self.next_button.clicked.connect(self._advance)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.next_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        self.back_button.hide()
        self.confirm_button.hide()
        self.next_button.setEnabled(bool(self._kind))

        if self._selected_key:
            self._kind_buttons[self._kind].setChecked(True)
            self._refinement_buttons[self._selected_key].setChecked(True)

    def _radio(self, key, label_key, group, layout):
        button = QRadioButton(self._tr(label_key), group.parent())
        button.setStyleSheet(self.RADIO_STYLE)
        button.setProperty("project_type_key", key)
        button.setProperty("i18n_skip", True)
        group.addButton(button)
        layout.addWidget(button)
        button.toggled.connect(self._on_kind_toggled)
        return button

    def _build_kind_page(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        intro = QLabel(self._tr("project_kind.intro"), page)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        group_box = QGroupBox(self._tr("project_kind.kind_title"), page)
        group_layout = QVBoxLayout(group_box)
        self._kind_buttons = {}
        self._kind_group = QButtonGroup(group_box)
        self._kind_group.setExclusive(True)
        for kind, label in (("single", "project_kind.single"),
                            ("module", "project_kind.module")):
            button = self._radio(kind, label, self._kind_group, group_layout)
            self._kind_buttons[kind] = button
        layout.addWidget(group_box)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _build_refinement_page(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self.refinement_intro = QLabel(page)
        self.refinement_intro.setWordWrap(True)
        layout.addWidget(self.refinement_intro)
        self._refinement_boxes = {}
        for kind, title in (("single", "project_kind.single_title"),
                            ("module", "project_kind.board_title")):
            box = QGroupBox(self._tr(title), page)
            box_layout = QVBoxLayout(box)
            group = QButtonGroup(box)
            group.setExclusive(True)
            for key, label in self.OPTIONS[kind]:
                button = self._radio(key, label, group, box_layout)
                self._refinement_buttons[key] = button
            self._refinement_boxes[kind] = box
            layout.addWidget(box)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _on_kind_toggled(self, checked=False):
        if self.pages.currentIndex() == 0:
            self._kind = next((key for key, button in self._kind_buttons.items()
                               if button.isChecked()), "")
            self.next_button.setEnabled(bool(self._kind))

    def _advance(self):
        self._kind = next((key for key, button in self._kind_buttons.items()
                           if button.isChecked()), "")
        if not self._kind:
            return
        for kind, box in self._refinement_boxes.items():
            box.setVisible(kind == self._kind)
        self.refinement_intro.setText(self._tr("project_kind.refine"))
        choices = self.OPTIONS[self._kind]
        if not any(self._refinement_buttons[key].isChecked() for key, _ in choices):
            self._refinement_buttons[choices[0][0]].setChecked(True)
        self.pages.setCurrentIndex(1)
        self.back_button.show()
        self.next_button.hide()
        self.confirm_button.show()

    def _back(self):
        self.pages.setCurrentIndex(0)
        self.back_button.hide()
        self.confirm_button.hide()
        self.next_button.show()
        self.next_button.setEnabled(bool(self._kind))

    def selected_type(self) -> str:
        """@brief Return the accepted built-in component profile key.

        @return Profile key such as "single_tht", or "" when nothing was chosen.
        """
        return next((key for key, _ in self.OPTIONS.get(self._kind, ())
                     if self._refinement_buttons[key].isChecked()), "")
