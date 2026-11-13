# KiriKiriLang

A complete toolkit for producing a translation patch for a
KiriKiri Z (KrKrZ) game.

The kit does the whole job in three commands: point it at a game folder, let
it discover everything, translate two files, and let it pack the result into
`patch.xp3`.

```
py -X utf8 langpatch.py "D:\Games\Some Game"    # set everything up
py -X utf8 langpatch.py pack                     # build patch.xp3 from the kit
py -X utf8 langpatch.py deploy -y                # install it into the game
```

Nothing in the kit is tied to one title: the game folder is inspected, a
profile is written, the archives are read, and every path, tag and caption
comes from that profile.

---

## Start here

| I want to … | Read |
|---|---|
| translate a game, day by day | [docs/USAGE.md](docs/USAGE.md) |
| change the code, add a game, break something | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| know what the patch does | [§3](#3-what-the-patch-does) below |
| look up a command | [§10](#10-command-reference) below |

The shortest possible loop:

```
py -X utf8 langpatch.py "D:\Games\Some Game"                        # once
py -X utf8 langpatch.py --config configs/<game>.json kit import --build
py -X utf8 langpatch.py --config configs/<game>.json deploy -y
```

Translate in between, in `kits/<game>/<tag>/text/text.xlsx`.

---

## Contents

1. [Requirements](#1-requirements)
2. [Quick start](#2-quick-start)
3. [What the patch does](#3-what-the-patch-does)
4. [Repository layout](#4-repository-layout)
5. [Profiles](#5-profiles)
6. [Kits](#6-kits)
7. [The dialogue workbook](#7-the-dialogue-workbook)
8. [The interface captions](#8-the-interface-captions)
9. [Images](#9-images)
10. [Command reference](#10-command-reference)
11. [How it works](#11-how-it-works)
12. [Troubleshooting](#12-troubleshooting)

Further reading: [docs/USAGE.md](docs/USAGE.md) (the everyday guide) and
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (the code, the invariants and the
verification loop).

---

## 1. Requirements

* Python 3.8 or newer.
* A KiriKiriZ game folder.

The packages are listed in `requirements.txt`:

```
py -m pip install -r requirements.txt
```

Only `openpyxl` is needed for the everyday workflow — it reads and writes the
translation workbook.  The other packages belong to individual titles and are
imported where they are used, so a missing one only affects the feature that
needs it: `Pillow` for graphics stored as TLG, `lz4` and `cryptography` for
archive body filters, and `argon2-cffi`, `pefile` and `pycryptodome` for the
obfuscated archive format.

Everything else — reading the archives, decrypting them, recovering the file
names, rebuilding the scenario files, writing the patch — is implemented in
this repository under `src/`.

---

## 2. Quick start

### 2.1 Point at a game folder

```
py -X utf8 langpatch.py "D:\Games\Some Game"
```

The same command is available as `langpatch.py new "D:\Games\Some Game"` and
as `langpatch.py setup "D:\Games\Some Game"`.

It does all of this, in order:

1. **Inspect the game.**  The archives are listed, the largest one is opened,
   its scripts and scenario entries are found, and the multi-language tables
   are read.  The result is written to `configs/<game>.json`.
2. **Extract the archives** into the folder named by the profile
   (`paths.extract_output`), together with a `filemap.json` that maps every
   entry to its storage path.  `--extract-dir` puts the extraction somewhere
   else; an extraction that already exists is reused.
3. **Add the language slot** to the scenario files, so every scenario can
   carry one more text column.
4. **Build the kit** in `kits/<game>/<tag>/`: the dialogue workbook, the
   caption catalogue, one caption file per interface language, the interface
   images and a short README.

A folder that was set up before keeps its profile, its kit and its
extraction: pointing at the same game again refreshes them instead of
creating a second copy.  The manual settings of a profile survive the
refresh, and finished translations are never overwritten.

The name of the profile and of the kit folder is the name of the game's
executable (`lapla.exe` → `lapla`).  A folder that holds several
executables needs one of them picked:

```
py -X utf8 langpatch.py "D:\Games\Some Game" --exe game.exe
```

At the end the command prints the two files to translate.

### 2.2 Translate

| File | What it holds |
|---|---|
| `kits/<game>/<tag>/text/text.xlsx` | every dialogue line, choice button and chapter title |
| `kits/<game>/<tag>/ui/syslangtext_<tag>.ini` | the interface captions |

Only the target-language columns are editable; everything else is context.
See [§7](#7-the-dialogue-workbook) and [§8](#8-the-interface-captions).

### 2.3 Pack

```
py -X utf8 langpatch.py pack
```

The kit is written back into the game data, the patch is built, and the
result is copied into the kit:

```
kits/<game>/<tag>/dist/patch.xp3
kits/<game>/<tag>/dist/version.dll
```

`patch.xp3` plus `version.dll` is all a player needs.

### 2.4 Install

```
py -X utf8 langpatch.py deploy -y
```

The two files are copied into the game folder.  If the folder already holds a
`patch.xp3` from someone else, the new one is installed as `patch2.xp3`,
`patch3.xp3` and so on — the existing patch is never overwritten.

The game must be closed while the patch is installed.

---

## 3. What the patch does

The patch is an ordinary XP3 archive that the engine mounts next to the
game's own archives.  It contains:

| Content | Why |
|---|---|
| `custom.tjs`, `default.tjs`, `storages.tjs` | the game's own scripts, patched with the language slot, the menu and the caption loader |
| `scn/…` | the rebuilt scenario files with the extra text column |
| `syslangtext_<tag>.ini` | the interface captions of the target language |
| interface images | the translated interface graphics |

The engine reads the archive through the loader in `loader/version.dll`:
`arc://./<path>` resolves into `patch.xp3`, so a file in the patch replaces
the game's own file without touching the original archives.

The text of a scenario is stored per language: a scenario row carries one
column per language the game knows, and the patch adds one more.  The engine
picks the column that matches the selected text language, so the menu, the
dialogue and the history all follow the same choice.

The speaker name plate is drawn outside the text renderer, so it would keep
the interface font while the dialogue uses the text font.  By default the
patch copies the face of the message text onto the plate, in every language.
The face is taken once per session, so switching the interface or the text
language cannot change it afterwards.  Every message-layer class is wrapped
(titles that subclass `MessageLayer` install their own reset and would
otherwise be missed), and a face that would resolve to the system fallback
(`MS Shell Dlg 2`, the engine's `lang_*` entry) is never used.

`language.name_font` in the profile overrides that when a distinct face is
wanted:

```json
"name_font": { "face": "Georgia", "scale": 1.0 }
```

`face` replaces the copied face; `size` (or `scale` of the plate's own size)
and `bold` are optional.  Leave the key out — the default — to keep the
plate identical to the message text.

---

## 4. Repository layout

```
langpatch.py            the driver: every command lives here
langpatch.cmd           launcher that picks the right interpreter
build.ps1               thin wrapper around langpatch.py
config.json             the base profile (target language, shared paths)
configs/                one profile per game (written by `new`)
kits/                   the translation kits (written by `new`: text, interface, images)
assets/                 reference caption files, one per known language
schemes/                the archive-filter database
loader/version.dll      the loader that mounts the patch archive
docs/                   the everyday guide and the development guide
src/                    the implementation
src/templates/          the TJS that is injected into the game
<game>/                 the work tree of one game
```

The work tree of a game is created by `new` and holds everything that
belongs to that game:

```
<game>/
    extracted/           the archives, read once
    filemap.json         the index of that extraction
    scripts/             the game's own scripts
    scn_overrides/       the rebuilt scenarios with the extra column
    cache/psb/           the parsed scenarios
    assets/              the interface graphics that are shipped
    build/               patch.xp3 and version.dll
```

### `src/`

| Module | Purpose |
|---|---|
| `build_patch.py` | builds `patch.xp3`: scripts, scenarios, captions, images |
| `build_scenarios.py` | rebuilds the scenario files with the extra text column |
| `psb_lang_slot.py` | adds a language column to one scenario file |
| `psb.py`, `psb_io.py`, `psb_tjs.py` | PSB reader/writer and the text model |
| `xp3.py`, `xp3_pack.py` | XP3 reader and writer |
| `xp3_crypt.py`, `cxcrypt.py` | the archive body filters |
| `hxv4.py`, `hxv4_extract.py` | the obfuscated archive format |
| `hash_dumper.py` | recovers file names from the archive bodies |
| `extract_xp3.py`, `extract_all.py` | list and extract archives |
| `menu_catalog.py` | finds every caption the game uses |
| `export_text.py`, `import_text.py` | the Excel workflow |
| `export_assets.py`, `import_assets.py`, `asset_io.py`, `lang_assets.py` | the image workflow |
| `kit.py` | the portable per-language kits |
| `profile_gen.py` | writes a profile from a game folder |
| `configfile.py`, `lang.py`, `gamecfg.py`, `simplecrypt.py`, `tlg.py` | helpers |
| `lint_tjs.py` | checks the TJS templates |
| `name_report.py`, `pack_patch.py` | small stand-alone tools |

### `src/templates/`

The TJS that is written into the patched `custom.tjs`:

| Template | Purpose |
|---|---|
| `language_menu.tjs` | the language submenu (Main / Sub / UI) |
| `ui_language_menu.tjs` | the same menu for engines that build their own |
| `language_switch.tjs` | repaints the line on screen when the language changes |
| `language_assets.tjs` | loads the caption files from the patch archive |
| `ui_language_apply.tjs` | applies an interface language |
| `ui_language_confirm.tjs` | the confirmation dialog |
| `dialog_caption.tjs` | the dialog captions |
| `native_preload.tjs`, `native_activate.tjs` | uses the engine's own multi-language subsystem |
| `multilang_preload.tjs`, `text_wrapper.tjs` | the fallback: render the patched column directly |
| `name_font.tjs` | keeps the name plate in the message font |
| `name_images.tjs` | draws speaker names as text when the title uses name pictures |
| `names_filter.tjs` | translates the drawn speaker name from the workbook's `Names` sheet (non-native builds) |

---

## 5. Profiles

A profile describes one game.  `config.json` is the base;
`configs/<game>.json` overrides what differs.  The active profile is chosen
with `--config`, or with the `TRANSLATION_PATCH_CONFIG` environment
variable.

```json
{
  "game_root": "D:\\Games\\Some Game",
  "game_exe": "game.exe",
  "extract_dir": "D:/krkr-extract/some-game",
  "extract_exclude": ["patch"],
  "language": {
    "tag": "xx",
    "name": "target",
    "label": "&Target",
    "index": 2,
    "multilang": true,
    "variants": ["en", "cn", "xx"]
  },
  "ui_tags": ["en", "cn", "xx"],
  "assets": { "...": "..." },
  "build": { "...": "..." },
  "paths": { "...": "..." }
}
```

### `language`

| Key | Meaning |
|---|---|
| `tag` | the short tag of the target language (`xx` in the example) |
| `name` | its long name, used in the profile and the kit metadata |
| `label` | the caption in the language menu (`&` marks the underlined letter) |
| `index` | the slot the target text column gets; it is the position in `variants` |
| `multilang` | the title stores more than one language per scenario |
| `variants` | every text column the scenarios carry, base language first |
| `base_tag` | the language the new text is translated from; english when the title ships it |
| `font_size` | the size of the new language's message text; only used when the title has no english text |
| `default_ui` | the interface language used on a fresh install |
| `ui_text` | the menu captions per interface language |
| `name_font` | optional: a distinct face for the speaker name plate |

### `main_archive`

The archive the scenarios are read from (`data.xp3`).  A title that ships
several archives adds its extra branches in others; the workbook names the
archive every row came from and puts the additional archives last.

### `ui_tags`

The interface languages the game ships.  Each of them needs a caption file in
the patch; the base one is the reference, the target one is the translation.

### `assets`

| Key | Meaning |
|---|---|
| `tag_names` | the interface graphics to translate (authoritative) |
| `dirs` | folders that hold the per-language interface graphics |
| `text_dirs` | folders that hold only text |
| `exclude_dirs` | folders that are never translated |
| `ui_source` | where the interface graphics are read from |
| `seed_flat` | also copy the graphics to the top level of the patch |
| `seed_untagged` | graphics without a language suffix that still need a copy |
| `name_images` | the title draws speaker names as pictures (`image/nameimage/`); the new text language uses text names instead |

### `build`

| Key | Meaning |
|---|---|
| `script_dir` | the folder with the game's own scripts (required) |
| `script_files` | which file is `custom` / `default` / `storages` |
| `script_out_dir` | where the patched scripts go inside the archive |
| `script_out_also_root` | write a second copy at the archive root |
| `patch_only` | ship only the archive, no loose files |
| `scn_in_patch` | put the rebuilt scenarios into the archive |
| `assets_in_patch` | put the interface graphics into the archive |
| `deploy_loose` | also copy the loose files next to the game |
| `inject_multilang` | add the language slot to the scenarios |
| `inject_language_menu` | build the language submenu |
| `native_multilang` | use the engine's own multi-language subsystem |
| `wrap_language_text` | render the patched column through the plain message layer |
| `ui_menu_group` | show the third (interface) group in the language menu |
| `ui_language_command` | the command the interface group runs |
| `message_font_size` | the game's own message font size |
| `native_word_break` | `0` wraps latin at word boundaries (default), `1` breaks anywhere |
| `native_single_font_scale` | override the new language's single-column font scale; unset keeps the title's own size |

### `paths`

| Key | Meaning |
|---|---|
| `scn_dir` | the rebuilt scenario files |
| `build_dir` | the build output (`patch.xp3`, `version.dll`) |
| `cache_dir` | the parsed scenarios, reused between runs |
| `workbook` | the scenario workbook of the plain (non-kit) workflow |
| `extract_output` | where the archives are extracted |
| `extract_dir` | the extraction tree that is read |
| `filemap` | the index that maps archive entries to storage paths |
| `assets_dir` | the interface graphics that are shipped |
| `assets_work` | the editable copy of the interface graphics |
| `assets_workbook` | the index of the image export |
| `loader` | the folder with `version.dll` |

---

## 6. Kits

A kit is everything one translator needs for one target language, kept
outside the game folder:

```
kits/<game>/<tag>/
    kit.json                  metadata: profile, tags, sources, statistics
    README.md                 how to translate and import
    profile.json              a copy of the profile, so the kit stays portable
    text/text.xlsx            the dialogue workbook
    ui/catalog.json           every discovered caption and where it came from
    ui/syslangtext_<tag>.ini  one caption file per interface language
    ui/manifest.json          the image export index
    ui/…                      the exported interface graphics
    dist/                     patch.xp3 + version.dll after `pack`
```

```
py -X utf8 langpatch.py kit list
py -X utf8 langpatch.py kit status
py -X utf8 langpatch.py kit create --force        # refresh the kit
py -X utf8 langpatch.py kit export --text --ui    # refresh parts of it
py -X utf8 langpatch.py kit import --build        # write it back
```

`kit create` never discards finished work: the workbook is re-exported with
the translations carried over, and the caption file keeps every value that is
already translated.  `--reset` (instead of `--force`) starts over.

The build reads `ui/syslangtext_<tag>.ini` from the kit directly, so an edit
takes effect on the next `pack` even before the kit is imported.  For a
language the title ships itself, the game's own extracted
`syslangtext_<tag>.ini` is authoritative; the template reference is only used
to fill keys neither file has.

---

## 7. The dialogue workbook

`text/text.xlsx` has four editable sheets:

* **Text** — every dialogue line,
* **Choices** — the choice buttons,
* **Titles** — the localised chapter titles,
* **Names** — the speaker names drawn in the name box.

The reference columns are read-only context; the red column of the target
language is the one to fill in.

| Column | Editable | Meaning |
|---|---|---|
| `id` | no | `file#scene#line`, the stable row key |
| `file` | no | the storage name of the scenario |
| `archive` | no | the archive the scenario came from (`data.xp3`) |
| `scene` | no | the scene label inside the scenario |
| `line` | no | the index inside the scene |
| `voice` | no | the voice clip, for context |
| `speaker_<ref>` | no | the speaker name in a reference language |
| `text_<ref>` | no | the line in a reference language |
| `speaker_<tag>` | **yes** | the speaker name in the target language |
| `text_<tag>` | **yes** | the line in the target language |
| `length` | no | the character count, written on import |
| `status` | no | informational |
| `notes` | – | free space, never imported |

The `Choices` and `Titles` sheets carry the same `archive` column, so text
recovered from a secondary archive stays recognisable.

### Speaker names

`Names` keeps the base-language name in `name_<base>` (`name_jp`, `name_en`,
as configured by `language.base_tag`) and the translated label in
`name_<tag>`.  The exporter collects the names from the same pass as the
dialogue, so a new scene adds its speaker automatically; a translation already
typed into the target column is kept.

Only the base-language names are collected: the other language columns store
the same names translated, and the target slot is seeded from the base
language, so those are not keys the name box draws.  A title whose base
base language works the same way (`name_<base>`, `name_<tag>`, ...).
Every multilingual build applies `dispNameFilter` to the drawn name.  Native
builds (titles that use the engine's own multi-language renderer) translate
only the label, so their scenario speaker columns stay as they are.  Titles
that draw names with the engine's own path may keep their translated speaker
columns and use `Names` for the names still left in the base language.

`py -X utf8 src/names_export.py` refreshes the list without re-exporting the
dialogue (useful after adding scenes); translations already typed into the
target column are kept.

The rows are ordered by archive: everything from the main archive first, the
text of the additional archives (the extra branches a title ships) at the end.

### Rules

* An empty target cell keeps the current value — the reference copy.
* Use a real line break only when the original has one.
* A cell that starts with `=` is a formula; the importer rejects it.
* Do not add or delete rows; edit the existing ones in place.
* The `length` column is recalculated automatically.

### The merge guard

When the workbook is re-exported, a stored translation is only kept when the
reference text it was written against is still the same.  A row whose source
changed is exported again with the new reference, so a stale translation can
never survive a change in the game data.

### Importing

```
py -X utf8 langpatch.py kit import --build
```

The workbook is written back into the scenario files and the patch is built.
An empty target cell is not written, so unfinished work keeps the reference
text.

---

## 8. The interface captions

The interface captions live in `syslangtext_<tag>.ini` files — one per
interface language, exactly as the engine reads them.

```
kits/<game>/<tag>/ui/syslangtext_en.ini    the reference (base language)
kits/<game>/<tag>/ui/syslangtext_xx.ini    the translation (target language)
```

Every key of the reference file exists in the target file.  Values that are
still equal to the base language are counted as *not translated* and listed
in `kit.json`.

The reference file is the title's own english caption file when it ships one
(`syslangtext_en.ini`); otherwise it is the file of the title's base
language.  The file the game itself ships always wins over the template's
reference file in `assets/` — that one only adds the lines the title does
not have.

Only the interface languages the patch supports are written.  A copy of a
caption file for any other language that an old export left in `assets/` is
not packed: the game's own file already exists in its archive, and shipping
a stale copy would override it.

The caption catalogue in `ui/catalog.json` records where every key was
found — an INI, a script, a catalogue entry — and every conflict it found,
so a wrong caption can be traced back to its source.

### The language menu

The patch adds a language submenu with three groups:

```
[Main] <language> ...
-
[Sub] <language> ...
[Sub] None
-
Switch Main and Sub
-
[UI] <language> ...
```

The third group is the interface language; it can be switched off with
`build.ui_menu_group = false`.  The group labels are written in the language
of the interface they belong to.

---

## 9. Images

Interface graphics are stored as images and PSB textures.  Only the real
interface graphics are translated; the list in `assets.tag_names` is
authoritative.

```
py -X utf8 langpatch.py kit export --images ui-text   # export them
py -X utf8 langpatch.py images-import                  # write them back
```

An image that was not edited is never shipped: the game keeps its own
version.  A graphic that has no translation falls back to the reference
language, never to an empty or placeholder picture.

The title screen always comes from the untagged `title.pimg`, never from a
`title_<tag>.pimg` copy: the tagged file of a title is often a partial
overlay (a different logo), while the untagged file is the complete screen
with the main-menu buttons.  Starting from it is the only way those buttons
can be translated.

Two folders are checked by default:

* `image/tx_object/` holds the story pictures (the images that carry the
  game's own text).  They are single-language, so the kit holds them under
  their own name and the packer writes the copy the engine asks for
  (`<name>_<tag>.png`).
* `image/w_contents/` holds effect art.  It never belongs into the kit.

A title that uses other folders can name them in `assets.text_dirs` and
`assets.exclude_dirs`.

---

## 10. Command reference

Every command takes `--config <profile>` (before or after the command).

### Setup

| Command | What it does |
|---|---|
| `new <game folder>` | inspect the game, write the profile, extract, add the slot, build the kit |
| `setup <game folder>` | the same as `new` |
| `profile --game <dir>` | only write the profile |
| `extract` | list the archives of the game, or extract them |
| `slot` | rebuild the scenario files with the target column |

### Translating

| Command | What it does |
|---|---|
| `kit create` | build or refresh the kit |
| `kit export` | refresh parts of an existing kit |
| `kit import` | write the kit back into the game data |
| `kit status`, `kit list` | show what a kit holds |
| `export` | the scenario workbook of the plain workflow |
| `import` | the other direction |

### Building and installing

| Command | What it does |
|---|---|
| `pack` | import the kit and build `patch.xp3` into `<kit>/dist/` |
| `patch` | the same as `pack` |
| `build` | build the patch into the profile's `build` folder |
| `deploy [-y]` | copy the patch and the loader into the game folder |
| `auto` | slot → import → build |
| `all` | slot → import/export → build |
| `status` | show what exists and what is installed |

### Images

| Command | What it does |
|---|---|
| `images-export` | the game graphics → the editable tree |
| `images-import` | the edited tree → the shipped graphics |
| `assets-seed` | copy the reference graphics to the target-language names |

### `build.ps1`

```
.\build.ps1                     build the default profile
.\build.ps1 -Config <profile>   build one profile
.\build.ps1 -All                build every profile in configs/
.\build.ps1 -Fresh              refresh the kit first
.\build.ps1 -Pack               pack the kit
.\build.ps1 -Deploy             install the patch afterwards
```

---

## 11. How it works

### 11.1 The archives

`extract_xp3.py` reads an XP3 archive without any external tool.  Plain
archives are extracted with their real paths.  Obfuscated archives (the
`Hxv4` marker) are decrypted with the key material recovered from the game
executable; the real file names are mined from the decrypted bodies, so a
resolved entry lands at `<out>/<archive>/<dir>/<name>` and only an entry
that stays unnamed keeps the `<out>/<archive>/<dirhash>/<filehash>` layout.

Archives whose bodies are filtered are handled by the filters in
`src/xp3_crypt.py` and `src/cxcrypt.py`.  The parameters of the known
filters are collected in `schemes/xp3_schemes.json`; a profile can pin one
with `"scheme": "<title>"`, and `--auto-scheme` picks the one that fits.

### 11.2 The text slot

A scenario stores one entry per text: the speaker, the text, the search text
and the speech text.  A title that knows several languages stores one entry
per language in `entry.language[]`.  The patch adds one more entry for the
target language and writes it into every scenario.

`build_scenarios.py` collects the scenarios from the extraction, rebuilds
each file with the extra column and records which archive every scenario
came from in `<scn_dir>/.provenance.json`.  That file is what the `archive`
column of the workbook is built from.

### 11.3 The build

`build_patch.py` stages the patch in `<build>/patch/`:

* the three engine scripts, read from the game's own script folder, patched
  with the language slot and the menu, and written back in the engine's own
  encrypted form;
* the rebuilt scenarios;
* the caption files of every interface language;
* the interface graphics.

The folder is then packed into `patch.xp3`, and `loader/version.dll` is
copied next to it.

### 11.4 The scripts

The patch wraps the game's own `custom.tjs`.  The wrapper

* registers the patch archive as an auto path, so the engine finds the new
  files;
* installs the language slot and the caption loader;
* rebuilds the language submenu;
* keeps the window title in sync with the caption file.

The modules are loaded around the game's own code: the renderer before it
(the engine's `msghack` subclasses it), the multi-language subsystem after
it (it replaces the renderer at startup).

### 11.5 The loader

`loader/version.dll` is the archive layer: it mounts `patch.xp3` and serves
the `unencrypted/` folder.  It is the only binary in the repository; it is
copied into the game folder unchanged.

---

## 12. Troubleshooting

| Symptom | What to do |
|---|---|
| The language menu does not appear | the title has no multi-language tables: build the kit with `--images ui-text` and check `language.multilang` in the profile |
| The dialogue stays in the base language | the slot was not added: run `langpatch.py slot` |
| The interface captions are not translated | the caption file must keep every key of the reference file; `kit status` lists the missing ones |
| A line is missing from the workbook | the scenario was not rebuilt: run `langpatch.py slot --force` and export again |
| The patch is not installed | the game was running: close it and deploy again |
| A foreign `patch.xp3` is in the game folder | the new patch is installed as `patch2.xp3`; both are loaded |
| The name plate uses a different font | the plate copies the message font by default; set `language.name_font` in the profile to override it |

### Linting the templates

```
py -X utf8 src/lint_tjs.py
```

The linter checks the TJS templates for the constructs some engines reject.
Run it after editing a template.

---

## Licences

The readers and writers in `src/` are written for this repository.

`loader/version.dll` is a third-party archive loader, redistributed unchanged;
it keeps the licence of the project it comes from.  The archive-filter
parameters in `schemes/` describe the formats the games use.
