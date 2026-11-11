"""@brief One registry of every prompt KiCadGen sends to a model, with its status against the current default.

User request of 2026-11-11 evening («dobbiamo evidenziare questi prompt
nell'interfaccia, non possiamo lasciarli embeddati nel codice» = the prompts
must be shown in the interface, not left embedded in the code). The problem
behind it: when a new program version changes a default prompt, a stored
profile text that is not a KNOWN former default (a user edit, or a default the
superseded list missed) was kept silently, so an analysis could send an old
prompt together with a new tool schema and nobody saw it.

This module is the single place the profile store, the profile dialog and the
analysis log read from:

- `PROMPTS` lists every prompt with its profile field, its Italian title (a
  catalog string), its stage, where its current default lives and where the
  digests of its former defaults live;
- `prompt_status` tells «current», «custom» or «outdated» for a stored text;
- `prompt_diff` shows how a stored text differs from the current default;
- `restore_default` gives the current default back.

The defaults stay in their owning modules (datasheet_extractor, photo_*), read
LAZILY by module and attribute name at every call: this module imports none of
them at load time (no import cycle), and a test that monkeypatches a module's
constant or SUPERSEDED_* set is seen at once.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
import difflib
import importlib
import re
from typing import Callable, Optional

from .messages import tx

#: Status of a stored text equal to the default of this program version.
CURRENT = "current"
#: Status of a stored text equal to a KNOWN former default of the program:
#: nobody edited it, and the current default can replace it.
OUTDATED = "outdated"
#: Status of any other stored text: a user edit, or a former default whose
#: digest the program does not list. It is never replaced automatically.
CUSTOM = "custom"

#: Stages, in the order of the profile dialog's tabs.
STAGE_DATASHEET = "datasheet"
STAGE_PHOTO = "photo"
STAGE_VERIFICATION = "verification"

#: Longest diff returned by `prompt_diff`, in lines: a whole prompt rewritten
#: stays readable in a dialog and a log, and the rest is summarised in one line.
MAX_DIFF_LINES = 400

#: Package of the owning modules, for the lazy imports.
_PACKAGE = __package__ or "kicadgen.services"


def _attribute(module: str, name: str):
    """Value of `name` in the sibling module `module`, imported now (lazily)."""
    return getattr(importlib.import_module(f"{_PACKAGE}.{module}"), name)


def _text_ref(module: str, name: str, *path) -> Callable[[], str]:
    """A callable returning the text `module.name[path...]`, read at every call."""
    def read() -> str:
        value = _attribute(module, name)
        for step in path:
            value = value[step]
        return value
    read.__doc__ = f"Current text of {module}.{name}{''.join(f'[{p!r}]' for p in path)}."
    return read


def _digests_ref(module: str, name: str) -> Callable[[], frozenset]:
    """A callable returning the digest set `module.name`, read at every call."""
    def read() -> frozenset:
        return frozenset(_attribute(module, name))
    read.__doc__ = f"Digests of the former defaults listed in {module}.{name}."
    return read


def _no_digests() -> frozenset:
    """No former default is known for this prompt (it never changed in a released database)."""
    return frozenset()


def _empty_text() -> str:
    """Factory value of a text whose default is blank (the type's photo notes)."""
    return ""


@dataclass(frozen=True)
class PromptEntry:
    """@brief One prompt of the program: where it is stored, what it is called, what its default is.

    @note `in_profile` tells whether the analysis sends the PROFILE's text.
        Since step P2 of the 2026-11-11 plan every entry of PROMPTS is wired
        (True). False is used only by CODE_ONLY_PROMPTS: texts the program
        keeps in the code on purpose, each with its `reason`, shown read-only
        by the profile dialog. The dialog shows an editor only for entries
        with `in_profile` True, so the user never edits a text the analysis
        ignores.
    """

    #: Profile field (ai_defaults/ai_overrides name), or "instructions" for
    #: the instructions of a built-in component type.
    field: str
    #: Italian title, a catalog string (translated with `tx`/`I18n.text`).
    title: str
    #: One of STAGE_DATASHEET, STAGE_PHOTO, STAGE_VERIFICATION.
    stage: str
    #: Returns the CURRENT default text (read from the owning module).
    default: Callable[[], str]
    #: Returns the SHA-256 digests (component_types.text_digest) of the
    #: former defaults; an empty set when none is known.
    superseded: Callable[[], frozenset] = _no_digests
    #: True when the analysis sends the profile's text (see the class note).
    in_profile: bool = True
    #: Where the default lives, for people reading the registry: "module.NAME".
    source: str = ""
    #: Where the owning module's former digests live, "module.NAME"; "" when
    #: that module has no set. A profile prompt also reads
    #: SUPERSEDED_DIGESTS[field] of this module (review V4-4).
    superseded_source: str = ""
    #: Italian catalog title of the nested sub-tab grouping this entry in the
    #: profile dialog (e.g. the six document roles); "" for a sub-tab of its own.
    group: str = ""
    #: Code-only entries (in_profile False): Italian catalog sentence saying
    #: why the text stays in the code; "" for a profile prompt.
    reason: str = ""

    def current_default(self) -> str:
        """@brief The default text of this program version.

        @return The text as the owning module defines it, byte for byte.
        """
        return self.default()


#: SHA-256 digests (component_types.text_digest) of the FORMER defaults of
#: every profile prompt, by field, kept HERE for every entry of PROMPTS (review
#: V4-4, 2026-11-11). A registered field had no set when its default lived in
#: a module without one (the 15 prompts brought into the profile on
#: 2026-11-11): at its first change the old default, stored in every user's
#: database, would have stayed there for ever and been called «custom» --
#: the class of system_prompt_single dc7044b8. Each set is merged with the
#: owning module's own set, when the module has one (`_entry(superseded=)`).
#: WHEN A DEFAULT CHANGES: add the digest of the OLD text to its field's set
#: (tests/test_review_round7.py pins the digest of every current default,
#: the built-in type instructions included, and fails naming the digest to add).
#: Empty sets: no default of these fields has changed in a released version.
SUPERSEDED_DIGESTS: dict[str, frozenset] = {
    "system_prompt": frozenset(),
    "system_prompt_single": frozenset(),
    "numbering_prompt": frozenset(),
    "pad_positions_prompt": frozenset(),
    "body_dimensions_prompt": frozenset(),
    "contact_style_prompt": frozenset(),
    "role_prompt_pinout": frozenset(),
    "role_prompt_top": frozenset(),
    "role_prompt_hires": frozenset(),
    "role_prompt_land_pattern": frozenset(),
    "role_prompt_side_view": frozenset(),
    "role_prompt_datasheet": frozenset(),
    "pin_type_prompt": frozenset(),
    "grounding_prompt": frozenset(),
    "mechanical_evidence_prompt": frozenset(),
    "image_instructions": frozenset(),
    "terminal_prompt": frozenset(),
    "lead_prompt": frozenset(),
    "crop_prompt": frozenset(),
    "inventory_prompt": frozenset(),
    "inventory_enumerate_prompt": frozenset(),
    "inventory_enumerate_empty_prompt": frozenset(),
    "marking_prompt": frozenset(),
    "verification_prompt": frozenset(),
}


def _catalog_digests_ref(field: str, module_ref: Optional[Callable[[], frozenset]] = None
                         ) -> Callable[[], frozenset]:
    """A callable returning SUPERSEDED_DIGESTS[field], merged with the owning module's set when given.

    Both are read at every call, so a test that monkeypatches either set is
    seen at once (as `_digests_ref`).
    """
    def read() -> frozenset:
        own = frozenset(globals()["SUPERSEDED_DIGESTS"].get(field, ()))
        return own | (module_ref() if module_ref is not None else frozenset())
    read.__doc__ = f"Digests of the former defaults of {field!r} (catalog and owning module)."
    return read


def _entry(field, title, stage, module, name, *path, superseded=None, in_profile=True,
           group="", reason=""):
    """Shorthand for a PromptEntry whose default is `module.name[path...]`.

    A profile prompt (`in_profile` True) always reads its former defaults
    from SUPERSEDED_DIGESTS[field], merged with the owning module's set
    `superseded` = (module, name) when given; a code-only text has none.
    """
    module_ref = _digests_ref(*superseded) if superseded else None
    digests = _catalog_digests_ref(field, module_ref) if in_profile else (module_ref or _no_digests)
    return PromptEntry(
        field=field, title=title, stage=stage, default=_text_ref(module, name, *path),
        superseded=digests, in_profile=in_profile,
        source=f"{module}.{name}" + "".join(f"[{p!r}]" for p in path),
        # The owning module's set only ("module.NAME"); every profile prompt
        # also reads SUPERSEDED_DIGESTS[field] (see that table).
        superseded_source=".".join(superseded) if superseded else "",
        group=group, reason=reason)


#: Nested sub-tab of the six document-role instructions (profile dialog).
GROUP_ROLES = "Ruoli documenti"
#: Nested sub-tab of the three global-inventory prompts (profile dialog).
GROUP_INVENTORY = "Inventario fotografico"


#: Document roles whose model-facing instruction is a prompt of its own. The
#: keys are datasheet_extractor.DOCUMENT_ROLE_KEYS (a test checks it); only
#: the INSTRUCTION (second item of _ROLE_PROMPTS) is a prompt: the role NAME is
#: the label the user reads in the documents card and stays in the code.
#: One field per role, not one field for all: each role's instruction is sent
#: alone with the files of that role, so each has its own status and diff, and
#: no format (separators, keys) has to be parsed out of a single free text.
_ROLE_TITLES = (
    ("pinout", "Ruolo documento: pinout"),
    ("top", "Ruolo documento: immagine con quote"),
    ("hires", "Ruolo documento: foto ad alta risoluzione"),
    ("land_pattern", "Ruolo documento: piazzole consigliate"),
    ("side_view", "Ruolo documento: vista laterale"),
    ("datasheet", "Ruolo documento: datasheet del costruttore"),
)

#: Every prompt the program sends to a model, in the order of the dialog.
PROMPTS: tuple[PromptEntry, ...] = (
    _entry("system_prompt", "Datasheet: schede e moduli", STAGE_DATASHEET,
           "datasheet_extractor", "DEFAULT_SYSTEM_PROMPT",
           superseded=("datasheet_extractor", "SUPERSEDED_SYSTEM_PROMPT_DIGESTS")),
    _entry("system_prompt_single", "Datasheet: componente singolo", STAGE_DATASHEET,
           "datasheet_extractor", "DEFAULT_SINGLE_SYSTEM_PROMPT",
           superseded=("datasheet_extractor", "SUPERSEDED_SINGLE_PROMPT_DIGESTS")),
    _entry("numbering_prompt", "Datasheet: numerazione pin", STAGE_DATASHEET,
           "datasheet_extractor", "DEFAULT_PIN_NUMBERING_PROMPT",
           superseded=("datasheet_extractor", "SUPERSEDED_NUMBERING_PROMPT_DIGESTS")),
    _entry("pad_positions_prompt", "Datasheet: posizioni dei pad", STAGE_DATASHEET,
           "datasheet_extractor", "DEFAULT_PAD_POSITIONS_PROMPT"),
    _entry("body_dimensions_prompt", "Datasheet: quote del corpo", STAGE_DATASHEET,
           "datasheet_extractor", "DEFAULT_BODY_DIMENSIONS_PROMPT"),
    _entry("contact_style_prompt", "Datasheet: ricontrollo del tipo di contatto", STAGE_DATASHEET,
           "datasheet_extractor", "CONTACT_STYLE_PROMPT"),
    *(_entry(f"role_prompt_{role}", title, STAGE_DATASHEET,
             "datasheet_extractor", "_ROLE_PROMPTS", role, 1, group=GROUP_ROLES)
      for role, title in _ROLE_TITLES),
    _entry("pin_type_prompt", "Simbolo: tipi elettrici dei pin", STAGE_DATASHEET,
           "pin_type_ai", "PIN_TYPE_PROMPT"),
    _entry("grounding_prompt", "Foto: localizzazione", STAGE_PHOTO,
           "photo_grounding", "GROUNDING_PROMPT"),
    _entry("mechanical_evidence_prompt", "Foto: evidenza meccanica", STAGE_PHOTO,
           "photo_grounding", "MECHANICAL_EVIDENCE_PROMPT"),
    PromptEntry(field="image_instructions", title="Foto: istruzioni della tipologia",
                stage=STAGE_PHOTO, default=_empty_text, source="(blank)",
                superseded=_catalog_digests_ref("image_instructions")),
    _entry("terminal_prompt", "Foto: terminali", STAGE_PHOTO,
           "photo_terminals", "PROMPT",
           superseded=("photo_terminals", "SUPERSEDED_PROMPT_DIGESTS")),
    _entry("lead_prompt", "Foto: reofori", STAGE_PHOTO, "photo_leads", "PROMPT"),
    _entry("crop_prompt", "Foto: lettura ritagli", STAGE_PHOTO,
           "photo_crop_reading", "SYSTEM_PROMPT",
           superseded=("photo_crop_reading", "SUPERSEDED_CROP_PROMPT_DIGESTS")),
    _entry("inventory_prompt", "Foto: inventario con i candidati del rilevatore", STAGE_PHOTO,
           "photo_global_inventory", "PROMPT", group=GROUP_INVENTORY),
    _entry("inventory_enumerate_prompt", "Foto: inventario senza rilevatore", STAGE_PHOTO,
           "photo_global_inventory", "PROMPT_ENUMERATE", group=GROUP_INVENTORY),
    _entry("inventory_enumerate_empty_prompt", "Foto: inventario dopo un rilevatore vuoto",
           STAGE_PHOTO, "photo_global_inventory", "PROMPT_ENUMERATE_AFTER_EMPTY_DETECTOR",
           group=GROUP_INVENTORY),
    _entry("marking_prompt", "Foto: lettura delle marcature", STAGE_PHOTO,
           "photo_markings", "PROMPT"),
    _entry("verification_prompt", "Foto: verifica", STAGE_VERIFICATION,
           "photo_grounding", "VERIFICATION_PROMPT"),
)

#: Texts the program sends that stay in the CODE on purpose (decision of step
#: P2, 2026-11-11), each with the reason the profile dialog shows next to a
#: read-only copy of it. They have no profile field and no status: the
#: dialog lists them so that no text sent to a model is hidden, but editing
#: them would change nothing the analysis does (a path no analysis of the
#: program takes) or would break a contract (a correction composed with data).
CODE_ONLY_PROMPTS: tuple[PromptEntry, ...] = (
    _entry("code:semantic_package_verification",
           "Foto: verifica semantica dei package (percorso storico)", STAGE_PHOTO,
           "photo_grounding", "SEMANTIC_VERIFICATION_PROMPT", in_profile=False,
           reason="Resta nel codice: lo usa solo il percorso storico senza verifica meccanica, "
                  "che nessuna analisi del programma percorre."),
    _entry("code:legacy_strip_localization",
           "Foto: localizzazione per strisce (percorso storico)", STAGE_PHOTO,
           "photo_inventory", "LEGACY_STRIP_PROMPT", in_profile=False,
           reason="Resta nel codice: lo usa solo il percorso storico senza localizzazione "
                  "dedicata, tenuto per i confronti riproducibili e mai percorso dall'analisi."),
    _entry("code:inventory_omission_reminder",
           "Foto: richiamo dei campi omessi nell'inventario", STAGE_PHOTO,
           "photo_global_inventory", "_OMISSION_REMINDER", in_profile=False,
           reason="Resta nel codice: e' una correzione fissa dello schema, composta con i nomi dei "
                  "campi omessi e aggiunta al prompt dell'inventario solo nel secondo tentativo."),
)

#: Prompts still built inside a function (a local variable) and therefore
#: invisible to the registry. Empty since step P2 (2026-11-11): every such
#: text was lifted into a module constant and registered, in PROMPTS or in
#: CODE_ONLY_PROMPTS. A test keeps it empty.
UNREGISTERED_INLINE_PROMPTS: tuple[tuple[str, str], ...] = ()

#: Title of the instructions of a built-in component type (catalog string).
INSTRUCTION_TITLE = "Istruzioni tipologia"

_BY_FIELD = {entry.field: entry for entry in PROMPTS}


def fields() -> tuple[str, ...]:
    """@brief Profile fields of every registered prompt, in registry order.

    @return The `field` of each entry of PROMPTS.
    """
    return tuple(entry.field for entry in PROMPTS)


def entry(field: str) -> PromptEntry:
    """@brief Registry entry of a profile prompt field.

    @param field Profile field name, e.g. "system_prompt".
    @return Its PromptEntry.
    @throws KeyError When the field is not a registered prompt.
    """
    return _BY_FIELD[field]


def instruction_entry(type_key: str) -> Optional[PromptEntry]:
    """@brief Registry entry of the instructions of a BUILT-IN component type.

    @param type_key Component type key.
    @return A PromptEntry whose default is the type's current built-in
        instruction and whose former defaults are
        component_types.SUPERSEDED_INSTRUCTION_DIGESTS[type_key]; None for a
        custom type (no program default to compare with).
    """
    from . import component_types
    if component_types.get(type_key) is None:
        return None

    def default() -> str:
        """Current built-in instruction of the type."""
        return importlib.import_module(f"{_PACKAGE}.component_types").get(type_key).prompt

    def superseded() -> frozenset:
        """Digests of the type's former built-in instructions."""
        table = _attribute("component_types", "SUPERSEDED_INSTRUCTION_DIGESTS")
        return frozenset(table.get(type_key, ()))

    return PromptEntry(field="instructions", title=INSTRUCTION_TITLE, stage=STAGE_DATASHEET,
                       default=default, superseded=superseded,
                       source=f"component_types.get({type_key!r}).prompt",
                       superseded_source="component_types.SUPERSEDED_INSTRUCTION_DIGESTS")


def text_digest(text: str) -> str:
    """@brief SHA-256 hex digest of a text: the key of the former defaults.

    @param text Stored text.
    @return component_types.text_digest(text).
    """
    from .component_types import text_digest as digest
    return digest(text)


def entry_status(prompt: PromptEntry, stored_text: str) -> str:
    """@brief Status of a stored text against one registry entry.

    @param prompt Registry entry (from `entry` or `instruction_entry`).
    @param stored_text Text stored in the profile or shown in an editor.
    @return CURRENT when the text equals the current default, ignoring
        whitespace around the whole text; OUTDATED when its digest is one of
        the KNOWN former defaults; CUSTOM otherwise.
    @throws TypeError When `stored_text` is not a string.
    @note Surrounding whitespace is not content: the profile dialog strips
        every text on save (and stores the exact default when only that
        differs), and a model reads no meaning in it. Inner whitespace counts.
    @note Empty text, decided explicitly: when the default is NOT blank, a
        blank text is CUSTOM -- never the default, never a known former
        default; the store refuses to save a blank prompt anyway, so it only
        reaches this function from an editor being typed in. When the default
        IS blank (the type's photo notes), blank is CURRENT and any text is
        CUSTOM.
    @note CURRENT wins over OUTDATED: a current text listed by mistake among
        the former defaults is still current.
    @note Line endings are not content either (review V5-4, 2026-11-11): a
        text that differs only by CRLF/CR against LF has the status of its
        LF form, so the store, the start-up notice and the dialog (whose
        editor turns every line end into LF) agree. A former default is
        recognised by the digest of the text as stored or of its LF form
        (the digest stays exact otherwise: one more blank is not a former
        default).
    """
    if not isinstance(stored_text, str):
        raise TypeError("stored_text must be a string")
    current = prompt.current_default()
    plain = normalized_text(stored_text)
    if stored_text == current or plain.strip() == normalized_text(current).strip():
        return CURRENT
    former = prompt.superseded()
    if {text_digest(stored_text), text_digest(plain)} & former:
        return OUTDATED
    return CUSTOM


def normalized_text(text: str) -> str:
    """@brief A text with every line end (CRLF, CR) turned into LF.

    @param text Any string.
    @return The same text with LF line ends only.
    @note The form compared by `entry_status` and digested by
        `notice_digest`: line ends are not content (review V5-4).
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def notice_digest(text: str) -> str:
    """@brief SHA-256 of a stored text as the start-up notice keys it: line ends normalised.

    @param text Stored text.
    @return text_digest of `normalized_text(text)`, so a CRLF copy and its
        LF form are one text for «Non mostrare più per questi testi».
    """
    return text_digest(normalized_text(text))


def prompt_status(field: str, stored_text: str) -> str:
    """@brief Status of a stored profile prompt: «current», «custom» or «outdated».

    @param field Profile prompt field.
    @param stored_text Its stored (or edited) text.
    @return CURRENT, CUSTOM or OUTDATED (see `entry_status`).
    @throws KeyError When the field is not a registered prompt.
    """
    return entry_status(entry(field), stored_text)


#: A sentence end followed by blanks: where a long prompt line is split for
#: the diff. Prompts are long paragraphs; a diff by whole lines would show a
#: 5000-character line removed and added for one changed word.
_SENTENCE_BREAK = re.compile(r"(?<=[.;:!?])\s+")


def _diff_lines(text: str) -> list[str]:
    """The text split into lines, and each line into sentences, for a readable diff."""
    lines = []
    for line in text.splitlines():
        parts = [part for part in _SENTENCE_BREAK.split(line) if part]
        lines.extend(parts or [""])
    return lines


def entry_diff(prompt: PromptEntry, stored_text: str,
               max_lines: int = MAX_DIFF_LINES) -> list[str]:
    """@brief Unified diff from a stored text to the current default of one entry.

    @param prompt Registry entry.
    @param stored_text Stored or edited text («-» lines).
    @param max_lines Longest result; the excess is replaced by one closing line.
    @return Diff lines (without line ends), the stored text as «-» and the
        current default as «+», split by sentence; an empty list when the
        status is CURRENT (no difference that counts).
    @note Presentation only: sentence splitting and the bound never feed a
        decision; the status comes from `entry_status`.
    """
    if entry_status(prompt, stored_text) == CURRENT:
        return []
    lines = list(difflib.unified_diff(
        _diff_lines(stored_text), _diff_lines(prompt.current_default()),
        fromfile=tx("Testo salvato"), tofile=tx("Predefinito di questa versione"),
        n=1, lineterm=""))
    if len(lines) > max_lines:
        rest = len(lines) - max_lines
        lines = lines[:max_lines] + [tx("… altre {count} righe", count=rest)]
    return lines


def prompt_diff(field: str, stored_text: str, max_lines: int = MAX_DIFF_LINES) -> list[str]:
    """@brief Unified diff from a stored profile prompt to its current default.

    @param field Profile prompt field.
    @param stored_text Stored or edited text.
    @param max_lines Longest result (see `entry_diff`).
    @return Diff lines; empty when the text is the current default.
    @throws KeyError When the field is not a registered prompt.
    """
    return entry_diff(entry(field), stored_text, max_lines)


def restore_default(field: str) -> str:
    """@brief The current default of a profile prompt, to put back in its editor.

    @param field Profile prompt field.
    @return The default text, byte for byte as the owning module defines it.
    @throws KeyError When the field is not a registered prompt.
    """
    return entry(field).current_default()


def effective_text(field: str, prompts=None) -> str:
    """@brief The text the analysis sends for one profile prompt field.

    @param field Profile prompt field (an entry of PROMPTS).
    @param prompts Mapping field -> profile text (see `profile_prompts`), or None.
    @return `prompts[field]` when it is a string with some non-blank
        character, otherwise the registry default of the field, byte for byte.
    @throws KeyError When the field is not a registered prompt.
    @note One fallback rule for every caller: a missing, None or blank value
        never sends an empty prompt, it sends the current default.
    @note A value whose status is CURRENT (`entry_status`: equal to the
        default apart from surrounding blanks and line ends) sends the
        DEFAULT's bytes (review V4-5, 2026-11-11): the status already calls
        such a text the default, so it must not change a request or miss a
        cache keyed on the text sent.
    """
    item = entry(field)
    value = prompts.get(field) if prompts else None
    if isinstance(value, str) and value.strip() and entry_status(item, value) != CURRENT:
        return value
    return item.current_default()


def profile_prompts(values) -> dict:
    """@brief The prompt texts of a profile snapshot that the analysis sends from the profile.

    @param values ProfileStore.resolve or ProfileStore.snapshot mapping.
    @return {field: text} for every PROMPTS entry with `in_profile` True whose
        value is a string; image_instructions is left out (it is not a
        prompt of its own: the store appends it to the whole-board photo
        prompts).
    @note The texts are those of the snapshot as given: the caller decides
        whether they carry the image instructions.
    """
    values = values or {}
    return {e.field: values[e.field] for e in PROMPTS
            if e.in_profile and e.field != "image_instructions"
            and isinstance(values.get(e.field), str)}


def log_status_label(status: str) -> str:
    """@brief Italian catalog source of the short status word written in the analysis log.

    @param status CURRENT, CUSTOM or OUTDATED.
    @return «predefinito», «personalizzato» or «vecchio predefinito».
    @throws KeyError For an unknown status.
    """
    return {CURRENT: "predefinito", CUSTOM: "personalizzato",
            OUTDATED: "vecchio predefinito"}[status]


def prompt_log_line(field: str, text: str) -> str:
    """@brief One analysis-log line naming a prompt sent, its status and its digest.

    @param field Profile prompt field.
    @param text The text sent (after the blank fallback of `effective_text`).
    @return Translated line with the title, the short status and the first
        8 hex characters of the SHA-256 of `text`. Never the text itself:
        the log is shared in crash reports, and a prompt may hold user notes.
    @throws KeyError When the field is not a registered prompt.
    """
    item = entry(field)
    return tx("Prompt inviato «{title}»: {status}, sha256 {digest}.",
              title=tx(item.title), status=tx(log_status_label(entry_status(item, text))),
              digest=text_digest(text)[:8])


#: Prompt fields a request being built declares it sends, {field: text sent},
#: set by `sending` around the call (review V4-6, 2026-11-11). A context
#: variable, not a keyword argument: the request reaches the provider call,
#: and fake calls in tests, unchanged.
_DECLARED = contextvars.ContextVar("kicadgen_prompt_fields", default=None)


@contextlib.contextmanager
def sending(fields):
    """@brief Declare which profile prompts the request made inside this block sends.

    @param fields {field: text sent} (the text after `effective_text`), or an
        iterable of fields whose text is the effective default-or-profile text.
    @note The call site knows which prompt it used; `SentPromptLog` attributes
        a declared field directly, with the status of the text actually sent,
        and never looks for it by substring in that request.
    """
    declared = dict(fields) if isinstance(fields, dict) else {field: None for field in fields}
    token = _DECLARED.set(declared)
    try:
        yield
    finally:
        _DECLARED.reset(token)


def _occurrences(needle: str, haystack: str) -> int:
    """Number of possibly overlapping occurrences of `needle` in `haystack`."""
    count, start = 0, haystack.find(needle)
    while start != -1:
        count += 1
        start = haystack.find(needle, start + 1)
    return count


def _request_texts(kwargs) -> list[str]:
    """Every text of one model request: its system prompt and the text blocks of its content."""
    texts = []
    system = kwargs.get("system_prompt")
    if isinstance(system, str):
        texts.append(system)
    for block in kwargs.get("content_blocks") or ():
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


class SentPromptLog:
    """@brief Writes one log line the first time each profile prompt reaches a model request.

    The analysis log must say, for every prompt actually sent, whether it was
    the default, a custom text or an old default (user request 2026-11-11,
    point 4). The call sites know their own field, but there are many of them
    and some compose the prompt with other text (a role instruction inside a
    file description, the mechanical contract after the verification text).
    This wrapper sits on the ONE logged call every request of a run goes
    through and recognises the texts in the request.
    @note A request built inside `sending` (the call site declares its
        fields) reports exactly the declared fields, with the status of the
        text the call site says it sent (review V4-6, 2026-11-11). Otherwise,
        as a fallback, a prompt is reported when its text, stripped of
        surrounding blanks, is contained in the system prompt or in a text
        block of a request -- but never when every occurrence lies inside
        the occurrences of ANOTHER registered prompt that contains it (a
        short custom text that is a sentence of a longer prompt was reported
        as sent although it was not).
        A text the program rewrites before sending (an obsolete clause of the
        verification prompt replaced, user notes filtered out of the inventory
        context) is reported only when it reaches the request verbatim. An
        answer replayed from the photo cache is not a request: its prompt is
        not reported (the cache hit has its own log line).
    @note Each field is reported once per run, in the order the requests use them.
    """

    def __init__(self, prompts, log):
        """@brief Prepare the texts to look for.

        @param prompts Mapping field -> profile text (see `profile_prompts`);
            every in_profile PROMPTS field missing from it is looked for with
            its default text.
        @param log Callable(str) receiving the lines; None writes nothing.
        """
        self._log = log
        self._pending = {}
        for item in PROMPTS:
            if not item.in_profile or item.field == "image_instructions":
                continue
            text = effective_text(item.field, prompts)
            if text.strip():
                self._pending[item.field] = text
        #: Every registered text of the run, stripped, reported or not: the
        #: texts a fallback match must not be found inside.
        self._needles = {field: text.strip() for field, text in self._pending.items()}

    def _report(self, field: str, text: str) -> None:
        """Write the line of `field` once, with the status of `text`."""
        del self._pending[field]
        if self._log is not None:
            self._log(prompt_log_line(field, text))

    def _found_alone(self, field: str, texts) -> bool:
        """Whether the text of `field` occurs in a request text OUTSIDE every other registered prompt containing it."""
        needle = self._needles[field]
        hosts = [other for name, other in self._needles.items()
                 if name != field and other != needle and needle in other]
        for haystack in texts:
            found = _occurrences(needle, haystack)
            if not found:
                continue
            inside = sum(_occurrences(host, haystack) * _occurrences(needle, host) for host in hosts)
            if found > inside:
                return True
        return False

    def observe(self, kwargs) -> None:
        """@brief Report the not yet reported prompts of one request.

        @param kwargs Keyword arguments of the request (model, system_prompt,
            content_blocks, ...).
        @note Declared fields (`sending`) first; the others by the guarded
            text match (see the class note).
        """
        if not self._pending:
            return
        declared = _DECLARED.get() or {}
        for field, sent in declared.items():
            if field in self._pending:
                self._report(field, sent if isinstance(sent, str) else self._pending[field])
        texts = _request_texts(kwargs)
        for field, text in list(self._pending.items()):
            if field not in declared and self._found_alone(field, texts):
                self._report(field, text)

    def wrap(self, call):
        """@brief A call that reports the prompts of each request, then forwards it unchanged.

        @param call The logged paid call (keyword arguments of a request).
        @return The wrapping callable; it returns what `call` returns.
        """
        import functools

        @functools.wraps(call)
        def reporting(*args, **kwargs):
            """Report the prompts of this request, then make it."""
            self.observe(kwargs)
            return call(*args, **kwargs)
        return reporting


def notice_entries(store, key: str = "", dismissed=()) -> list[dict]:
    """@brief Prompts of a profile the start-up notice must name: custom or outdated, not dismissed.

    @param store ProfileStore (prompt_statuses, resolve, get_type).
    @param key Component type key of the active profile; "" for the global defaults.
    @param dismissed Collection of (field, digest) pairs the user chose
        «Non mostrare più per questi testi» for.
    @return One dict per prompt (ProfileStore.prompt_statuses row plus
        `digest`, `notice_digest` of the stored text: line ends normalised,
        review V5-4) whose status is CUSTOM or
        OUTDATED and whose (field, digest) is not dismissed, in registry order.
    @throws ProfileError When `key` is not in the catalogue.
    @note Keyed on the TEXT, not on the field: a dismissed custom text stays
        quiet, while a new difference (another digest, e.g. a program update
        that changed the default and made the stored text outdated, or a new
        edit) is named again.
    """
    rows = store.prompt_statuses(key)
    values = store.resolve(key)
    row = store.get_type(key) if key else None
    dismissed = {tuple(item) for item in dismissed}
    result = []
    for status in rows:
        if status["status"] == CURRENT:
            continue
        text = (row or {}).get("instructions") if status["field"] == "instructions" \
            else values.get(status["field"])
        if not isinstance(text, str):
            continue
        digest = notice_digest(text)
        if (status["field"], digest) in dismissed:
            continue
        result.append(dict(status, digest=digest))
    return result


def status_label(status: str) -> str:
    """@brief Italian catalog source of the label of a status, as the dialog shows it.

    @param status CURRENT, CUSTOM or OUTDATED.
    @return The Italian source (translate with `I18n.text` or `tx`).
    @throws KeyError For an unknown status.
    """
    return {
        CURRENT: "Predefinito di questa versione",
        CUSTOM: "Personalizzato",
        OUTDATED: "Predefinito di una versione precedente: aggiornamento disponibile",
    }[status]
