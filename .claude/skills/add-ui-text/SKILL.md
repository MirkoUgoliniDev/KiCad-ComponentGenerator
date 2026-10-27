---
name: add-ui-text
description: Add or change any text the user reads in the GUI, a dialog, a log line or an error message, with its four translations (IT, EN, FR, DE) in the bundled catalog. Use it whenever a change touches a visible string.
---

# Adding or changing user-visible text

The interface is written with **Italian source strings**; the catalog maps
each one to Italian, English, French and German. A missing entry does not
crash: it shows an Italian phrase inside an English, French or German window,
which nobody notices until they look at the app in that language.

## 1. Where text comes from

- Widgets and dialogs: Italian literals, translated by
  `I18n.text` / `I18n.retranslate_widgets` (`kicadgen/services/i18n.py`).
  Text composed at run time (with a path or a count) must be re-composed on a
  language change, not written once.
- Messages from services, logs and errors: `tx("Italian template {name}", name=...)`
  from `kicadgen/services/messages.py`. Placeholders are `str.format` named
  fields; literal braces are doubled.
- Stable keys (menus, `app.*`, dotted keys): `self.i18n.tr("app.file")`.

## 2. The catalog entry

Add the entry to `DEFAULT_TRANSLATIONS` in `kicadgen/services/i18n.py`:

```python
"ui:Percorso footprint: {path}": {"it": "Percorso footprint: {path}", "en": "Footprint path: {path}",
                                   "fr": "Chemin de l’empreinte : {path}", "de": "Footprint-Pfad: {path}"},
```

- Key = `"ui:"` + the exact Italian source; all four languages filled.
- Same placeholders in every language.
- French typography as the rest of the catalog: `’` and a space before `:`,
  `;`, `?`, `!`; quotes `« … »`.
- Product terms as already used: «modello STEP» (never «modello 3D»),
  footprint, pad («Pastille» in French, «Pad» in German).
- No duplicate keys (a test checks the literal).

The bundled entries reach the SQLite catalog (`translations` table of
`data/analysis_profiles.sqlite3`) at start-up: existing databases receive new
keys, and text the user edited is not overwritten. Commit the database with
the change (see the `pre-commit-check` skill).

## 3. Removing or renaming text

Renaming the Italian source is a new key: remove the old entry only when no
source references it (`test_the_removed_entries_are_not_referenced_by_any_source`).

## 4. Verify

```
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-i18n tests/test_i18n.py tests/test_untranslated_texts.py tests/test_qt_translations.py
```

`test_every_ui_string_has_a_four_language_entry` scans the GUI code for
visible literals; if it reports a string, add the entry rather than
exempting it. Only machine values (IDs, file extensions, units) are exempt.
When the text sits in a widget that is rebuilt on a language change, add or
extend a test that switches language and checks the widget, as
`tests/test_untranslated_texts.py` does: positive (EN/FR) and negative (IT
unchanged).
