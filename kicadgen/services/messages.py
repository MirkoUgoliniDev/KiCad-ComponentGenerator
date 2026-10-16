"""@brief Translation of the messages the program writes: log, progress, errors.

User decision of 2026-09-24: everything the user reads is localized, the log
and the error messages included, and the analysis diary is written in the
language active at that moment.

Widgets are retranslated by `I18n.retranslate_widgets`; messages are built at
run time, often far from the GUI (services, worker threads), so they go
through `tx`. The source is an Italian TEMPLATE with named placeholders,
looked up as `ui:<template>` in the same catalog as the interface, and only
then formatted: a translation can move the values where its grammar wants
them.

This module imports no Qt on purpose: the services also run outside the
GUI (scripts, tests, the FreeCAD child process), where no translator is
registered and the Italian source is used as it is.
"""
from __future__ import annotations

import re
from typing import Callable

#: Lookup registered by the application: (key, default) -> unformatted text,
#: that is `I18n.raw`. None outside the GUI.
_translator: Callable[..., str] | None = None


def set_translator(translator: Callable[..., str] | None) -> None:
    """@brief Register the function that resolves catalog keys.

    @param translator `I18n.raw` of the running application, or None to go
        back to the Italian sources.
    @note The same I18n object follows the language chosen by the user, so
        registering it once is enough: a language change applies to the next
        message.
    """
    global _translator
    _translator = translator


def tx(source: str, /, **values: object) -> str:
    """@brief Translate an Italian message template and fill its placeholders.

    @param source Italian template, e.g. «Progetto aperto: {folder}». Literal
        braces are written doubled, as in `str.format`.
    @param values Values of the named placeholders.
    @return The message in the active language; the Italian one when no
        translator is registered or the catalog has no entry.
    @note Never raises for a formatting problem: a translation with a wrong
        placeholder must not turn a log line or an error message into a
        crash. The source is formatted instead, and failing that returned raw.
    """
    if _translator is not None:
        try:
            text = _translator("ui:" + source, default=source)
        except Exception:  # noqa: BLE001 - a broken catalog must not hide the message
            text = source
    else:
        text = source
    for candidate in (text, source):
        try:
            return _no_double_period(candidate, values).format(**values)
        # TypeError: a value that does not accept the format spec, e.g.
        # «{a:.2f}» with a=None -- same fallback as the other errors.
        except (KeyError, IndexError, ValueError, AttributeError, TypeError):
            continue
    return source


#: Characters that already close a sentence: a template period right after a
#: value ending with one of them would print «..» (HANDOFF point 133).
_SENTENCE_END = (".", "!", "?", "…")
#: A plain placeholder followed by a period, «{name}.», not a literal «{{name}}.».
_PLACEHOLDER_PERIOD = re.compile(r"(?<!\{)\{(\w+)\}\.")


def ends_sentence(value: object) -> bool:
    """@brief Tell whether a value's text already ends a sentence.

    @param value Any value; its str() is examined, trailing blanks ignored.
    @return True when it ends with a period, «!», «?» or an ellipsis.
    """
    return str(value).rstrip().endswith(_SENTENCE_END)


def end_sentence(value: object) -> str:
    """@brief A value's text closed by exactly one sentence end.

    @param value Usually an exception, to be followed by more text.
    @return The text with a period appended only when it has no sentence end
        yet: for messages built without `tx` (f-strings, AI prompts).
    """
    text = str(value).rstrip()
    return text if ends_sentence(text) else text + "."


def _no_double_period(template: str, values: dict) -> str:
    """Drop a template period that follows a value already ending a sentence.

    Structural, for every template and language: an exception text often
    ends with its own period, and «{exc}.» then printed «..».
    """
    return _PLACEHOLDER_PERIOD.sub(
        lambda match: ("{%s}" % match[1] if match[1] in values
                       and ends_sentence(values[match[1]]) else match[0]),
        template)


class Message(str):
    """@brief A translated message that also says what KIND of message it is.

    @note Code that sorted messages by reading their words («fallita», «Da
        verificare:», «validated cache hit») stopped working the moment the
        words were translated. The kind travels with the text instead: it is
        a `str`, so every consumer that only prints it is unaffected, and the
        one that has to route it reads `kind`.
    """

    kind: str = ""


#: Kind of a message that is also a warning of the analysis report.
WARNING = "warning"
#: Kind of the line that says a paid answer was replayed from the cache.
CACHE_HIT = "cache_hit"


def marked(kind: str, source: str, /, **values: object) -> Message:
    """@brief `tx`, plus the kind of the message for whoever has to route it.

    @param kind One of the kind constants of this module.
    @param source Italian template, as for `tx`.
    @param values Placeholder values.
    @return The translated text as a `Message` whose `kind` is set.
    """
    message = Message(tx(source, **values))
    message.kind = kind
    return message


def warning(source: str, /, **values: object) -> Message:
    """@brief A translated message that is also a warning of the report (`marked(WARNING, …)`).

    @param source Italian source text of the message.
    @param values Named substitutions of the translated text.
    @return The translated `Message` of kind WARNING.
    """
    return marked(WARNING, source, **values)


def as_warning(text: object) -> Message:
    """@brief An already built message marked as a warning of the report.

    @param text Message text, already translated.
    @return The same text as a `Message` of kind WARNING (unchanged when it
        already is one).
    """
    if kind_of(text) == WARNING:
        return text
    message = Message(str(text))
    message.kind = WARNING
    return message


def kind_of(message: object) -> str:
    """@brief Kind carried by a message; "" for a plain string.

    @param message Any object; only `Message` instances carry a kind.
    @return The kind (e.g. WARNING) or "".
    """
    return getattr(message, "kind", "") if isinstance(message, str) else ""


#: Marker of a warning printed by the FreeCAD child process.
CHILD_WARNING = "KICADGEN_WARN:"


def child_warning_text(payload: str) -> str:
    """@brief Text of a FreeCAD warning, in the user's language.

    @param payload What follows `KICADGEN_WARN:` on the child's line: JSON
        {"t": Italian template, "v": values} since 2026-09-24, plain text before.
    @return The translated, formatted message; the payload as it is when it
        is not the JSON form (an older child, or a line written by hand).
    @note The child runs inside FreeCAD's own interpreter, where the catalog
        does not exist: it sends the template, and the translation happens
        here, in the process that knows the user's language.
    """
    import json
    text = payload.strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict) or not isinstance(data.get("t"), str):
        return text
    values = data.get("v") if isinstance(data.get("v"), dict) else {}
    return tx(data["t"], **values)


def render_child_line(line: str) -> str:
    """@brief A line of the FreeCAD output with its warning, if any, translated.

    @param line One output line, e.g. «KICADGEN_WARN:{...}».
    @return The same line; after the marker, the readable warning. The
        marker stays: it is how the log and the progress card recognize a
        machine line.
    """
    head, marker, payload = line.partition(CHILD_WARNING)
    if not marker:
        return line
    return head + marker + child_warning_text(payload)
