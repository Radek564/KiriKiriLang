# Using the kit

The everyday guide: what to translate, which command to run, and what to check
afterwards.  Nothing here is specific to one title or one target language.

---

## 1. The three-minute version

```
py -m pip install -r requirements.txt                 # once
py -X utf8 langpatch.py "D:\Games\Some Game"          # once per game
py -X utf8 langpatch.py --config configs/<game>.json kit import --build
py -X utf8 langpatch.py --config configs/<game>.json deploy -y
```

The second command inspects the game and writes `configs/<game>.json`, the
extraction and the kit, so the two commands after it have a profile and a
workbook to work with.  The third command reads the translation workbook,
writes the translations into the scenario files and rebuilds `patch.xp3`; the
last one copies the patch into the game folder.

Translate in between: open

```
kits/<game>/<tag>/text/text.xlsx
```

fill the target-language columns, save the file, then run the two commands
above.

---

## 2. What you translate

| File | Editable columns |
|---|---|
| `kits/<game>/<tag>/text/text.xlsx` | `text_<tag>`, `title_<tag>`, `speaker_<tag>` (only where it should differ), `name_<tag>` on the `Names` sheet |
| `kits/<game>/<tag>/ui/syslangtext_<tag>.ini` | every caption |
| `kits/<game>/<tag>/ui/assets_work/…` | the interface graphics (same file name, edited picture) |

Everything else in the kit is generated and must not be edited.

---

## 3. The daily loop

### 3.1 Translate

Open the workbook in Excel.  The `README` sheet inside it explains every
column; the rules are repeated in [§4](#4-the-rules).

### 3.2 Import and build

```
py -X utf8 langpatch.py --config configs/<game>.json kit import --build
```

Output to read:

```
applied: 214 row(s) in 6 file(s); 17179 unchanged; 0 empty skipped
  by sheet: text 214, choice 0, title 0
imported: 2 edited, 56 unchanged, 2 written, 0 error(s)
built ...\<game>\build\patch.xp3 (… bytes)
```

* `applied` — rows that carried a translation and were written into the
  scenarios.
* `unchanged` — rows without a translation; they keep the reference text.
* `imported: N edited` — **scenario files** that were recompiled.  A number
  larger than a handful means the import touched many files; that is normal
  after a language change, but worth a look if it happens on every run.
* `built …` — the size of the new patch.

If a line says `error(s)`, the import failed: the message names the row and
the file.  Nothing was written for that file.

### 3.3 Install

```
py -X utf8 langpatch.py --config configs/<game>.json deploy -y
```

The game must be closed.  The command copies `patch.xp3` and `version.dll`
into the game folder; an existing patch is kept as `patch2.xp3`, `patch3.xp3`
and so on.

### 3.4 Check

```
py -X utf8 langpatch.py --config configs/<game>.json status
```

prints the workbook, the build size and whether the patch is installed.

---

## 4. The rules

### 4.1 The dialogue workbook

| Sheet | What it holds |
|---|---|
| `Text` | every dialogue line |
| `Choices` | the choice buttons |
| `Titles` | the chapter and scene titles |
| `Names` | the speaker names, one row per name |

Only the target-language columns are yours.  `id`, `file`, `scene`, `line`,
`choice`, `length` and `status` are written by the tools.

* Leave a target cell empty to keep the current value.
* `length` is recalculated on import; never type it.
* A cell starting with `=` is a formula; the importer rejects it.

### 4.2 Speaker names — the important rule

A scenario line carries **two** names:

* the **key** (`name` in the engine): the lookup key for the voice table, the
  alias table and the name plate.  It is the same in every language.
* the **display name** (`disp`): the string that is drawn in the name box.
  Every language has its own.

So:

* the **key** is never translated and never written into the scenario;
* the **display name** is translated through the `Names` sheet
  (`name_<base>` → `name_<tag>`), and the patch translates the drawn string
  at draw time (`dispNameFilter`).

The `speaker_<tag>` column of the `Text` sheet holds the **display name** of
the target language.  It is a copy of the base column, so it is usually
already correct — and it must stay that way:

* a **blank** display name means "this line shows no name box".  The game
  stores a single space there on purpose.  The importer never writes a
  speaker into such a line, so it stays nameless in every language.
* a display name that is not blank is written as it is.

If you want a line that hides the name box to show one after all, change the
`speaker_<tag>` cell: a value that differs from the base column is written,
an unchanged one is not.

### 4.3 The `Names` sheet

One row per name that appears in the scenarios:

| Column | Meaning |
|---|---|
| `name_<base>` | the name as the scenarios carry it — never edit it |
| `name_<tag>` | the translation that is drawn in the name box |

The names are collected from the scenarios by the exporter; the column is
filled in for you.

### 4.4 The interface captions

`kits/<game>/<tag>/ui/syslangtext_<tag>.ini` holds the menus, the dialogs and
the system messages.  The same key in another language file is the same
caption — the tools use that to keep the files in sync.

### 4.5 Images

The interface graphics live in `kits/<game>/<tag>/ui/assets_work/` with the
same names as the game.  Edit the picture, keep the name, and the tools ship
it.  A graphic that gets a language suffix in the game (`attention_<tag>.png`)
is shipped under that name.

---

## 5. Commands

| Command | What it does |
|---|---|
| `langpatch.py status` | folders, counts, install state |
| `langpatch.py profile --game "<game folder>"` | writes a profile from a game folder |
| `langpatch.py new "<game folder>"` | profile + extraction + slot + kit |
| `langpatch.py kit export` | scenarios → workbook (keeps finished translations) |
| `langpatch.py kit import [--build]` | workbook → scenarios, then build |
| `langpatch.py slot [--force]` | re-add the language slot from the untouched archives |
| `langpatch.py build` | build `patch.xp3` |
| `langpatch.py deploy [-y]` | copy the patch into the game folder |
| `langpatch.py images-export` | game graphics → editable `assets_work/` |
| `langpatch.py images-import` | edited `assets_work/` → shipped graphics |
| `py -X utf8 src/lint_tjs.py` | check the TJS templates |

`--config configs/<game>.json` selects the profile; without it the base
`config.json` is used.

### 5.1 When a game needs a clean rebuild

```
py -X utf8 langpatch.py --config configs/<game>.json slot --force
```

re-creates the scenarios from the untouched archives (the original language
columns exactly as the game ships them) and adds the language slot again.
Use it when the scenario data was damaged, after a manual edit, or when the
importer and the exporter disagree about the data.

Afterwards: `kit export` → translate → `kit import --build`.

---

## 6. When something looks wrong

| Symptom | Likely cause |
|---|---|
| the patch does nothing | the profile points at the wrong folder, or the game was not closed during the deploy |
| the menu is in the wrong language | the interface language was not applied — the patch asks for a confirmation and returns to the title |
| the dialogue is in the base language | the target column was never filled in; the exporter shows `copied` for those rows |
| a name box appears on a line that has none | the display name of that line was overwritten — check the `speaker_<tag>` cell |
| the name box never appears | the display name is blank in every language, which is how the game hides it |
| the backlog shows only one language | the second language is off (`build.backlog_sub_language`) or the title has no such feature |
| the backlog text is squeezed | the second language is drawn but the line spacing was not adapted |
| the interface graphics do not change with the language | the variant file (`_<tag>`) is missing from the patch |

---

## 7. What to check after every build

1. `kit import --build` printed no errors.
2. The built `patch.xp3` has the expected size (a sudden drop means files are
   missing).
3. `status` reports `patch installed`.
4. In the game: the menu, one dialogue line, one name box and the backlog.
