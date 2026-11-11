---
paths:
  - "kicadgen/gui/**"
  - "kicadgen/services/i18n.py"
  - "kicadgen/services/messages.py"
---

# User-visible text

Facts that every change here must keep. Catalog entry format and the tests:
`add-ui-text` skill.

- Source strings are **Italian**; every visible string has its four
  translations (IT, EN, FR, DE) in `DEFAULT_TRANSLATIONS` of
  `kicadgen/services/i18n.py`. A missing entry does not crash: it shows
  Italian inside a window in another language.
- Messages from services, logs and errors go through `tx("… {name}", name=…)`
  (`kicadgen/services/messages.py`), with the same placeholders in every
  language.
- Text composed at run time is re-composed on a language change, not written
  once.
- Product terms: «modello STEP», never «modello 3D».
- When `test_every_ui_string_has_a_four_language_entry` reports a string, add
  the entry; do not exempt it.
- The catalog reaches `data/analysis_profiles.sqlite3` at start-up: the
  database is committed with the change (`pre-commit-check` skill).
- An error is recognised by its type, never by its text, which is translated.
