# Working on the kit

The map of the code, the rules that must not be broken, and the pitfalls that
cost the most time.  Read this before changing anything; it is the companion
of [USAGE.md](USAGE.md).

---

## 1. The layers

```
langpatch.py            the driver: every command lives here
  └─ src/<tool>.py      one tool per job, each usable on its own
       └─ src/templates/*.tjs   the TJS that is written into the game
```

* `langpatch.py` parses the command line, loads the profile and calls the
  tools.  It contains no game logic.
* The tools are independent scripts.  `export_text.py`, `import_text.py`,
  `build_patch.py` … can be run by hand with `--help`.
* The TJS templates are **fragments**: text with placeholders that
  `build_patch.py` fills in and writes into the game's `custom.tjs`.

Everything language-specific comes from the profile (`config.json` and
`configs/<game>.json`).  A hard-coded tag, caption or language name in the
code is a bug.

---

## 2. The data flow

```
game folder
   │  profile_gen.py          inspect the archives, write configs/<game>.json
   ▼
extracted/  +  filemap.json   the archives, read once
   │  build_scenarios.py      add the language slot to every scenario
   ▼
scn_overrides/  +  cache/psb/ the scenarios with one more text column
   │  export_text.py          scenarios → kits/<game>/<tag>/text/text.xlsx
   ▼
text.xlsx                     ← the only file a translator edits
   │  import_text.py          workbook → scenarios
   ▼
scn_overrides/                the scenarios with the translations
   │  build_patch.py          scripts + scenarios + captions + images
   ▼
build/patch.xp3  +  version.dll
   │  langpatch.py deploy
   ▼
game folder
```

The rule that keeps this safe: **the original language columns of a scenario
are read-only.**  The importer writes the target column and nothing else.

---

## 3. The scenario model

A dialogue entry holds one column per language:

```
texts[j] = [ key, [ [speaker, text, length, ...], … ], voice, delay, kagParams ]
             ^^^   ^ each language: its own display name and its own text
             the lookup key: the same in every language
```

* `key` — the lookup key for the voice table, the alias table and the name
  plate.  Never translated, never written.
* `speaker` (per language) — the **display name**, the string drawn in the
  name box.  A single space means "no name box on this line".

The engine draws the name like this (from `sysscn/rendermsgwin.tjs`):

```tjs
function drawName(rec) {
    disp = kag.getLangInfo(rec, "disp");
    name = (disp != "") ? disp : (rec.name != "" ? kag.getAliasName(rec.name) : "");
    name = global.dispNameFilter(name);          // ← our translation hook
    _fore_message.processName(name, rec);
}
```

and `processName` trims the string, so a display name of `" "` produces an
empty name and the plate stays hidden.

Two consequences:

* **A blank display name is meaningful.**  Writing the key into it makes the
  engine draw a name on a line the scenario shows without one.
* **The translation happens at draw time.**  `dispNameFilter` maps the drawn
  string through the `Names` sheet.  The key is never replaced in the data.

---

## 4. The TJS fragments

`build_patch.py` assembles the patched `custom.tjs` from the game's own
scripts plus the fragments it needs:

| Fragment | Enabled by |
|---|---|
| `native_activate.tjs` | `build.native_multilang` — titles that use the engine's own multi-language renderer |
| `names_filter.tjs` | every other title: the same name translation, without the renderer |
| `ui_language_menu.tjs` | `build.native_multilang` or `build.ui_menu_group` |
| `ui_storage_wrap.tjs` | `build.ui_storage_wrap` — titles that ask for their graphics without a language suffix |
| `text_wrapper.tjs` | `build.wrap_language_text` — titles whose message layer only reads `text` |
| `name_font.tjs` | every multi-language build: keeps the name plate in the message font |
| `ui_language_apply.tjs`, `ui_language_confirm.tjs` | the interface language handling |

Rules for a fragment:

* it must be **defensive**: a title without the feature must not break, so
  every entry point is guarded and wrapped in `try { … } catch (e) { }`.
* it must **not** change the behaviour of a title that does not need it.  A
  fragment that only makes sense for one feature has to be gated by the
  profile flag that describes that feature.
* the placeholders (`@@NAME_TABLE@@`, `@@INDEX@@`, `@@TAG@@`, …) are filled
  in by `build_patch.py`; a fragment must not contain a language name.

### 4.1 The language switch in the backlog

`ui_language_menu.tjs` replaces the title's `CustomBacklog.drawTextBlock` so
the backlog shows the active language instead of the base one.  Two things
belong to the title's own feature, not to the patch:

* the **second (sub) language** — only drawn when
  `build.backlog_sub_language` is on, and only when the title really has the
  feature;
* the **line spacing** that goes with it.

The sub language is a *live* value:

```tjs
kag.subLanguageType   // → SystemConfig.subLanguageType; void means "none"
```

`void` becomes `0` when it is converted to a number, so the empty value has
to be recognised **before** the number is taken.  Getting that wrong draws
the first language as the second column, always.

---

## 5. The profile

`config.json` is the base; `configs/<game>.json` overrides what differs.
The keys that decide the behaviour:

| Key | Meaning |
|---|---|
| `language.tag`, `language.index`, `language.variants` | the target language and its slot |
| `language.base_tag` | the language the target slot is seeded from |
| `ui_tags` | the interface languages the kit ships |
| `build.native_multilang` | the title has its own multi-language renderer |
| `build.wrap_language_text` | the message layer only reads `text` |
| `build.ui_storage_wrap` | the graphics are asked for without a language suffix |
| `build.backlog_sub_language` | the backlog can show a second language |
| `build.native_*_font_scale` | the font scales of the two-column layout |

A profile is written by `profile_gen.py`, but a generated profile is a
*guess*: check the values that the guess cannot know (`ui_storage_wrap`,
`backlog_sub_language`, `base_tag`) against the title's own scripts.

---

## 6. The verification loop

After every change to a tool or a fragment:

```
py -X utf8 -m py_compile src\<tool>.py
py -X utf8 langpatch.py --config configs/<game>.json kit import --build
py -X utf8 langpatch.py --config configs/<game>.json deploy -y
```

and then the checks that caught most of the bugs:

1. **The scenario data against the untouched archives.**  The original
   language columns must be identical to the game's own files:

   ```
   langpatch.py --config configs/<game>.json slot --force   # only when needed
   ```

   Compare `scn_overrides/` with `extracted/`: the speakers of the original
   language must match line by line.  A difference means the importer wrote
   something it must not.

2. **The hidden names.**  Lines whose display name is a single space must stay
   nameless in the target language too.

3. **The TJS.**  The built `custom.tjs` is encrypted; decrypt it and run it
   through the syntax checker:

   ```
   <KrkrExtract-master>\ToolSource\tjs2Compiler\Release\tjs2Compiler.exe <file>.tjs
   ```

   The exit code must be 0.

4. **The patch.**  The scenario entries in `patch.xp3` must match
   `scn_overrides/`, and the installed `patch.xp3` must be byte-identical to
   the build.

5. **In the game** (the only check that cannot be automated): the menu, one
   dialogue line, one name box, the backlog and the interface graphics.

---

## 7. The traps

| Trap | What it looks like | The rule |
|---|---|---|
| the key replaces the display name | a name box appears on a narration line | a blank display name stays blank; only the target column is written |
| a record is replaced while drawing a name | a stale name on the current line | the record the engine passes is authoritative |
| `void` read as `0` | a second language is always drawn | check `!== void` before the number |
| a fragment enabled for every title | the backlog is squeezed, a font shrinks | gate a fragment by the profile flag of the feature it belongs to |
| the live value read from the saved flag | a setting in the game has no effect | the game keeps it in the system config; read it through `kag.<property>` |
| a language name in a comment or an example | the template stops being generic | the tag and the caption come from the profile |
| an export that overwrites a finished translation | work lost | the exporter merges only the cells whose status is `translated` |

---

## 8. Adding a game

```
py -X utf8 langpatch.py "<game folder>" --slug <game>
py -X utf8 langpatch.py --config configs/<game>.json kit import --build
```

Then check the generated profile against the title's scripts (§5), and keep
the kit's translations: the exporter never overwrites a finished cell.

## 9. Adding a target language

1. Add the tag to the profile: `language.tag`, `language.index`,
   `language.variants`, `ui_tags`.
2. Create the kit: `langpatch.py kit create` (the tag is part of the kit
   folder name).
3. Translate the workbook and the caption file.
4. The `Names` sheet and `dispNameFilter` need no change — they follow the
   profile.
