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
                             QListWidgetItem, QVBoxLayout)

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


def name_problem(root, name: str, current: str = "") -> str:
    """@brief Why `name` cannot be used for a new or renamed project, or "".

    @param root Library root.
    @param name Proposed project name.
    @param current Name of the project being renamed ("" for a new one): a
        rename that only changes letter case is allowed.
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
    root = Path(root)
    if root.is_dir():
        taken = {path.name.casefold() for path in root.iterdir() if path.is_dir()}
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
        self.setWindowTitle(self._tr(title))
        self.setModal(True)
        self.field = QLineEdit(current)
        self.field.setPlaceholderText(self._tr("Nome del progetto"))
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #f38ba8;")
        self.problem.setProperty("i18n_skip", True)
        hint = QLabel(self._tr("Il nome del progetto e' il nome della sua cartella, "
                               "del footprint e del modello 3D."))
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

    def _problem(self) -> str:
        """Why the typed name cannot be used, or ""."""
        name = self.field.text()
        problem = name_problem(self._root, name, self._current)
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
        if self._problem():
            self._validate()
            return
        super().accept()

    def project_name(self) -> str:
        """@brief The accepted name, without surrounding spaces."""
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
        """@brief Name of the chosen project, or "" when nothing is chosen."""
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
