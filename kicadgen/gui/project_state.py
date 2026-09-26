"""@brief The settings of a project, saved in its folder as `project_config.json`.

User decision of 2026-09-24: every option chosen in the windows and every
parameter of a project lives in a JSON file inside the project folder, so that
reopening the project applies the same settings. What goes in, who wins over
the general settings and when it is written are decided in `main_window`
(`_project_state`, `_apply_project_state`); this module only knows how to read
and write widget values and the file itself.

The file is never trusted blindly: a file that cannot be read, that is not a
KiCadGen project file, or that comes from a newer version is not applied and
not overwritten, and the caller says why in the log.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from ..fsutil import replace_file

from PyQt6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDoubleSpinBox,
                             QGroupBox, QLineEdit, QPlainTextEdit, QSpinBox, QTextEdit)
from ..services.messages import tx
from .component_state import BoundedValue, FlagValue, TextValue

#: File name inside the project folder.
PROJECT_CONFIG_NAME = "project_config.json"
#: Marker that tells this file apart from any other JSON a user might drop there.
PROJECT_CONFIG_FORMAT = "kicadgen-project"
#: Version of the file layout; a file with a higher one is left alone.
PROJECT_CONFIG_VERSION = 1


class ProjectConfigError(ValueError):
    """@brief A project file that exists but must not be applied."""


def widget_value(widget):
    """@brief JSON-friendly value of an input widget.

    @param widget QLineEdit, QTextEdit/QPlainTextEdit, QSpinBox,
        QDoubleSpinBox, QCheckBox, checkable QGroupBox or QComboBox, or one of
        the non-graphical parameters of `component_state` (saved exactly like
        the editor it replaced, so older project files stay valid).
    @return The text, number or state; for a combo box {"data", "text"}, so
        it can be restored by its data first and by its text otherwise.
    @throws TypeError For any other widget type.
    """
    if isinstance(widget, (QLineEdit, TextValue)):
        return widget.text()
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return widget.toPlainText()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox, BoundedValue)):
        return widget.value()
    if isinstance(widget, (QCheckBox, QGroupBox, FlagValue)):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        data = widget.currentData()
        return {"data": data if isinstance(data, (str, int, float, bool)) or data is None else str(data),
                "text": widget.currentText()}
    raise TypeError(f"Unsupported widget: {type(widget).__name__}")


def set_widget_value(widget, value) -> bool:
    """@brief Put a saved value back into an input widget.

    @param widget Same widget types as `widget_value`.
    @param value Value produced by `widget_value`.
    @return True when the value was applied; False when it does not fit the
        widget (wrong type, a number outside the range, a combo entry that no
        longer exists): the widget then keeps its current value.
    """
    if isinstance(widget, (QLineEdit, TextValue)) and isinstance(value, str):
        widget.setText(value)
        return True
    if isinstance(widget, (QTextEdit, QPlainTextEdit)) and isinstance(value, str):
        widget.setPlainText(value)
        return True
    if isinstance(widget, (QSpinBox, QDoubleSpinBox, BoundedValue)) \
            and isinstance(value, (int, float)) \
            and not isinstance(value, bool) and math.isfinite(float(value)):
        if not widget.minimum() <= value <= widget.maximum():
            return False
        integer = isinstance(widget, QSpinBox) or isinstance(widget.value(), int)
        widget.setValue(int(value) if integer else float(value))
        return True
    if isinstance(widget, (QCheckBox, FlagValue)) and isinstance(value, bool):
        widget.setChecked(value)
        return True
    if isinstance(widget, QGroupBox) and widget.isCheckable() and isinstance(value, bool):
        widget.setChecked(value)
        return True
    if isinstance(widget, QComboBox) and isinstance(value, dict):
        index = widget.findData(value.get("data")) if value.get("data") is not None else -1
        if index < 0 and isinstance(value.get("text"), str):
            index = widget.findText(value["text"])
        if index < 0:
            return False
        widget.setCurrentIndex(index)
        return True
    return False


def change_signal(widget):
    """@brief The signal a widget emits when the user changes its value, or None."""
    if isinstance(widget, QLineEdit):
        return widget.textChanged
    if isinstance(widget, (QTextEdit, QPlainTextEdit)):
        return widget.textChanged
    if isinstance(widget, QAbstractSpinBox) and hasattr(widget, "valueChanged"):
        return widget.valueChanged
    if isinstance(widget, (QCheckBox, QGroupBox)):
        return widget.toggled
    if isinstance(widget, QComboBox):
        return widget.currentIndexChanged
    return None


def project_config_path(project_dir) -> Path:
    """@brief Path of the project file inside a project folder."""
    return Path(project_dir) / PROJECT_CONFIG_NAME


def read_project_config(project_dir):
    """@brief Read the project file of a folder.

    @param project_dir Project folder.
    @return The saved state (the `state` object of the file), or None when
        the folder has no project file (an older project, or a new one).
    @throws ProjectConfigError When the file exists but is unreadable, is not
        a KiCadGen project file, or has a newer version: it must be neither
        applied nor overwritten.
    """
    path = project_config_path(project_dir)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(f"{path.name} illeggibile: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != PROJECT_CONFIG_FORMAT:
        raise ProjectConfigError(tx("{name} non e' un file di progetto di KiCadGen", name=path.name))
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProjectConfigError(tx("{name} senza versione valida", name=path.name))
    if version > PROJECT_CONFIG_VERSION:
        raise ProjectConfigError(
            tx("{name} scritto da una versione piu' recente ({version}): non applicato",
               name=path.name,
               version=version))
    state = document.get("state")
    if not isinstance(state, dict):
        raise ProjectConfigError(tx("{name} senza impostazioni", name=path.name))
    return state


def write_project_config(project_dir, state: dict) -> Path:
    """@brief Write the project file atomically.

    @param project_dir Project folder (created if missing).
    @param state JSON-serialisable settings of the project.
    @return Path of the written file.
    @throws OSError When the folder or the file cannot be written.
    @throws TypeError / ValueError When `state` is not JSON-serialisable or
        contains NaN/infinity: nothing is written then.
    @note Temporary file in the same folder, then os.replace: a crash while
        writing never leaves half a project file behind.
    """
    folder = Path(project_dir)
    folder.mkdir(parents=True, exist_ok=True)
    document = {"format": PROJECT_CONFIG_FORMAT, "version": PROJECT_CONFIG_VERSION,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "state": state}
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    handle, temporary = tempfile.mkstemp(prefix=".project_config-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
        target = project_config_path(folder)
        replace_file(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target
