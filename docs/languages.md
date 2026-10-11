# Languages

MacSafe is available in **English** and **Ukrainian**. The language is a setting. Change it in the menu bar popover under **Appearance → Language**.

## Choosing a language

- **auto** (the default): English, unless macOS lists Ukrainian as the first preferred language, in which case Ukrainian.
- **English** or **Українська**: set explicitly. The app restarts so the menu bar and the agent use the new language. The report window uses it the next time it opens.

## How translation works

Ukrainian is the source language. The code keeps its Ukrainian text as the key, and the translation table maps each key to English.

- **Python** (`i18n.py`): `T("Текст")` returns the English text for the current language and the original text when there is no translation. Placeholders are filled after translation: `T("Вільно: {n} МБ", n=5)`. The table is `i18n.EN`.
- **Report page** (`core/template.html`): the page holds the table as `I18N_EN`. A small script walks the text of the page and the attributes (title, aria-label, placeholder), and replaces each Ukrainian phrase it finds. The page's own script, the report data and dynamically rendered parts are translated the same way. Numbers are replaced with `#` in the lookup, so one entry covers "Port 7717: 26 errors" and "Port 80: 3 errors".
- **Placeholder page** (`app.html`): two strings, switched by the `lang` parameter.

Tests check that every `T()` call in the code has a translation and that placeholders match. A headless browser can render the page in English and show any Ukrainian text left on it.

## Adding a language

1. Add the code to `LANGS` in `i18n.py`, `LANGUAGES` in `prefs.py`, and the segment in `menubar.py`.
2. Add the translations to `i18n.EN` for the Python strings (the test lists missing keys).
3. Add a dictionary for the new language to the report template, following `I18N_EN`.
4. Run the tests.

## What is not translated

- Code comments, docstrings, and developer scripts (`build_app.sh`, `packaging/`) are in Ukrainian.
- The log messages of the report generator (`core/generate-report.sh`) are in Ukrainian. Messages that reach the interface are translated.
- Values that come from your Mac (app names, file names, paths) appear as they are.
