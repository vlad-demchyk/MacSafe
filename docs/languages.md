# Languages

MacSafe is available in **English** and **Ukrainian**. English is the default. The language is a setting with two places to change it: the menu bar popover under **Appearance → Language**, and the report window's **Settings**.

## Choosing a language

- **English** (the default), or **Українська**.
- In the menu bar popover: **Appearance → Language**. The agent saves the choice and restarts, so the menu bar shows the new language at once.
- In the report window: **Settings → Interface language**. The window stores the choice through the agent, which then restarts the menu bar too.

The settings file keeps the choice as `"language": "en"` or `"language": "uk"`. A missing or invalid value means English.

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

- Values that come from your Mac (app names, file names, paths, command output) appear as they are.
- Source code keeps the Ukrainian phrases as translation keys. Python and the page compare them with the dictionary, so changing a key means changing its entry too.
- Code comments, docstrings, build scripts and the report generator's log messages are in English.
