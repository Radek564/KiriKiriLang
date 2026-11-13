"""Export every dialogue line from the scenario PSBs into an Excel workbook.

The workbook is the translation surface:

* one row per dialogue entry,
* every language already present in the base game as read-only reference,
* editable columns for the configured target language (see ``src/lang.py``),
* `length` is recomputed on import, and `status` shows the current state.

After translating, run ``import_text.py``.  It writes the target-language
columns back into the configured variant of each scenario PSB, fixes the
character-length field, and rebuilds only the `.scn` files whose text changed.

Nothing here names a concrete language: the tag, label and variant index all
come from ``config.json``.

Usage:
    py -X utf8 src/export_text.py
    py -X utf8 src/export_text.py --scn-dir "..\\_work\\scn_overrides" -o text_full.xlsx
"""
import argparse
import os
import sys

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import lang
import names_sheet
import psb_io

CONFIG = lang.CONFIG
TAG = lang.TAG
INDEX = lang.INDEX
LANG_LABEL = lang.LABEL
REF_TAGS = lang.REF_TAGS
BASE_TAG = lang.BASE_TAG
BASE_INDEX = lang.BASE_INDEX
TEXT_COL = lang.TEXT_COL
SPEAKER_COL = lang.SPEAKER_COL
TITLE_COL = lang.TITLE_COL
NAMES_SOURCE_COL, NAMES_TARGET_COL = names_sheet.columns()[:2]


def _path(key, default):
    raw = (CONFIG.get('paths') or {}).get(key) or default
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


# name, width, kind
_BASE_COLUMNS = [
    ('id', 34, 'meta'), ('file', 34, 'meta'), ('archive', 12, 'meta'),
    ('scene', 14, 'meta'), ('line', 6, 'meta'), ('voice', 16, 'meta'),
]
_REF_COLUMNS = []
for _tag in REF_TAGS:
    _REF_COLUMNS.append((lang.ref_column('speaker', _tag), 14, _tag))
    _REF_COLUMNS.append((lang.ref_column('text', _tag), 60, _tag))
_HEAD_COLUMNS = list(_BASE_COLUMNS)
# The raw speaker column is only a separate column when the base
# language is not itself ``jp``; otherwise the reference block already has it.
if not any(name == 'speaker_jp' for name, _w, _k in _REF_COLUMNS):
    _HEAD_COLUMNS.append(('speaker_jp', 14, 'ref'))
COLUMNS = (
    _HEAD_COLUMNS + _REF_COLUMNS
    + [(SPEAKER_COL, 14, 'ref'), (TEXT_COL, 60, TAG),
       ('length', 8, 'meta'), ('status', 12, 'meta'), ('notes', 30, 'free')]
)
COL_INDEX = {name: i + 1 for i, (name, _, _) in enumerate(COLUMNS)}

# Choice buttons.  The first reference language lives in ``selects[i].text``;
# the per-language copies live in ``selects[i].language[index]`` as
# ``{searchtext, speechtext, text, textlength}``.  Only ``text`` carries a
# user-visible difference: the game stores the search/speech mirrors as copies
# of it, and import writes them from the translated text.  Exporting those
# mirrors as extra columns would only repeat ``text_<tag>``.
CHOICES_COLUMNS = (
    [('id', 40, 'meta'), ('file', 34, 'meta'), ('archive', 12, 'meta'),
     ('scene', 14, 'meta'),
     ('choice', 7, 'meta'), (lang.ref_column('text', REF_TAGS[0]), 60,
                             REF_TAGS[0])]
    + [(lang.ref_column('text', t), 40, t) for t in REF_TAGS[1:]]
    + [(TEXT_COL, 60, TAG), ('status', 12, 'meta'), ('notes', 30, 'free')]
)

# Localised scene titles (chapter cards).  Stored as ``scene.title``:
# ``["Prologue", "序章"]`` -> append the translated string at INDEX.
TITLES_COLUMNS = (
    [('id', 40, 'meta'), ('file', 34, 'meta'), ('archive', 12, 'meta'),
     ('scene', 14, 'meta')]
    + [(lang.ref_column('title', t), 44, t) for t in REF_TAGS]
    + [(TITLE_COL, 44, TAG), ('status', 12, 'meta'), ('notes', 30, 'free')]
)

HEADER_FILL = {
    'meta': PatternFill('solid', fgColor='1F3864'),
    'ref': PatternFill('solid', fgColor='44546A'),
    TAG: PatternFill('solid', fgColor='9C0006'),
    'free': PatternFill('solid', fgColor='595959'),
}
for _tag in REF_TAGS:
    HEADER_FILL.setdefault(_tag, PatternFill('solid', fgColor='2E7D32'))
HEADER_FONT = Font(bold=True, color='FFFFFF')
BAND_FILL = PatternFill('solid', fgColor='F2F2F2')
TEXT_ALIGN = Alignment(wrap_text=True, vertical='top')
CELL_ALIGN = Alignment(vertical='top')

# Native multilang builds translate the drawn name from the Names sheet, so
# the scenario speaker column is only a key.  Titles that draw names with the
# engine's own path already show their translated speaker column.
NATIVE_NAMES = bool((CONFIG.get('build') or {}).get('native_multilang'))
if NATIVE_NAMES:
    SPEAKER_HINT = [
        ('   Speaker names are translated in the Names sheet (%s), not in'
         % NAMES_TARGET_COL, ''),
        ('   the speaker columns: those hold the lookup key of the voice table.', ''),
    ]
    SPEAKER_COL_LINE = (
        '%s  speaker key, translated in Names (do not edit)' % SPEAKER_COL, '')
else:
    SPEAKER_HINT = [
        ('   A speaker name already translated in the speaker column keeps', ''),
        ('   working; a name still in the base language can be translated', ''),
        ('   in the `%s` column of the Names sheet.' % NAMES_TARGET_COL, ''),
    ]
    SPEAKER_COL_LINE = (
        '%s  EDIT: %s speaker column (variant %d)'
        % (SPEAKER_COL, LANG_LABEL, INDEX), '')

README = [
    ('Scenario text workbook — target language: %s (%s)' % (LANG_LABEL, TAG),
     'title'),
    ('', ''),
    ('The workbook has four editable sheets, all exported from the rebuilt scenarios:', 'heading'),
    ('  Text      every dialogue line (scene.texts)', ''),
    ('  Choices   choice buttons (scene.selects)', ''),
    ('  Titles    localised scene/chapter titles (scene.title lists)', ''),
    ('  Names     base-language speaker names found in the scenarios '
     '(%s -> %s)' % (NAMES_SOURCE_COL, NAMES_TARGET_COL), ''),
    ('', ''),
    ('Rows map to the %s slot at variant index %d (`%s`).' % (LANG_LABEL, INDEX, TAG), ''),
    ('The archive column records which game archive a scenario came from, so text', ''),
    ('recovered from a secondary archive stays recognisable.', ''),
    ('', ''),
    ('WORKFLOW', 'heading'),
    ('1. Run:  py -X utf8 src/export_text.py   (or export from another folder with --scn-dir)', ''),
    ('2. Translate only the RED text columns: %s (and %s on Titles).'
     % (TEXT_COL, TITLE_COL), ''),
] + SPEAKER_HINT + [
    ('3. Do not edit id, file, scene, line, choice, length or status.', ''),
    ('4. Run:  py -X utf8 src/import_text.py', ''),
    ('5. Rebuild the patch:  .\\build.ps1', ''),
    ('', ''),
    ('RULES', 'heading'),
    ('• Leave a target-language cell empty to keep the current value (the reference copy).', ''),
    ('• Use real line breaks inside a cell only when the original does.', ''),
    ('• A cell beginning with = is a formula, not text; the importer rejects it.', ''),
    ('• length values are recalculated automatically; you never need to type them.', ''),
    ('• Do not insert/delete rows. Edit the existing rows in place.', ''),
    ('• Each file is recompiled once, even if you changed all four sheets.', ''),
    ('', ''),
    ('COLUMNS — Text', 'heading'),
    ('id          stable row key: file#scene#line', ''),
    ('file        storage name of the scenario (root.name)', ''),
    ('archive     archive the scenario was recovered from', ''),
    ('scene       scene label inside the PSB', ''),
    ('line        index inside scene.texts', ''),
    ('voice       voice clip name, for context', ''),
    ('speaker_jp  base-language speaker/display name', ''),
]
for _tag in REF_TAGS:
    README.append(('%s / %s  %s reference (variant %d)'
                   % (lang.ref_column('speaker', _tag),
                      lang.ref_column('text', _tag), _tag.upper(),
                      lang.variant_index(_tag)), ''))
README += [
    SPEAKER_COL_LINE,
    ('%s     EDIT: %s text (variant %d)' % (TEXT_COL, LANG_LABEL, INDEX), ''),
    ('length      character count written to the variant on import', ''),
    ('status      translated / copied / empty / missing (informational)', ''),
    ('notes       free space for translator notes (not imported)', ''),
    ('', ''),
    ('COLUMNS — Choices', 'heading'),
    ('id            stable row key: file#scene#choice', ''),
    ('archive       archive the scenario was recovered from', ''),
    ('choice        index inside scene.selects', ''),
    ('%s       %s button text (selects[].text)'
     % (lang.ref_column('text', REF_TAGS[0]), REF_TAGS[0].upper()), ''),
]
for _tag in REF_TAGS[1:]:
    README.append(('%s      %s button text (selects[].language[%d])'
                   % (lang.ref_column('text', _tag), _tag.upper(),
                      lang.variant_index(_tag)), ''))
README += [
    ('              The internal searchtext/speechtext copies hold the same',
     ''),
    ('              string and are written from the translation on import, so',
     ''),
    ('              they are not exported as separate columns.', ''),
    ('%s       EDIT: %s button text' % (TEXT_COL, LANG_LABEL), ''),
    ('', ''),
    ('COLUMNS — Titles', 'heading'),
]
for _tag in REF_TAGS:
    README.append(('%s      %s title (scene.title[%d])'
                   % (lang.ref_column('title', _tag), _tag.upper(),
                      lang.variant_index(_tag)), ''))
README += [
    ('%s      EDIT: %s title (scene.title[%d])' % (TITLE_COL, LANG_LABEL, INDEX), ''),
    ('              Only scenes with a localised title list appear here;', ''),
    ('              plain-string titles are language-neutral and are skipped.', ''),
    ('', ''),
    ('COLUMNS — Names', 'heading'),
    ('%s  base-language speaker name the scenarios carry (do not edit)'
     % NAMES_SOURCE_COL, ''),
    ('%s  EDIT: translated name drawn in the name box' % NAMES_TARGET_COL, ''),
    ('status      empty / copied / translated (informational)', ''),
    ('notes       free space for translator notes (not imported)', ''),
    ('', ''),
    ('STATUS VALUES', 'heading'),
    ('translated  target text differs from the language it was seeded from', ''),
    ('copied      target slot still equals that source (not translated yet)', ''),
    ('empty       target text is an empty string', ''),
    ('missing     PSB has no variant %d (rebuild with build_scenarios.py)' % INDEX, ''),
]


def clean(value):
    """Return a safe Excel string (strips control chars Excel rejects)."""
    if value is None:
        return ''
    text = str(value)
    return ILLEGAL_CHARACTERS_RE.sub('', text)


def put(ws, row, col, value, kind):
    cell = ws.cell(row=row, column=col)
    cell.value = clean(value)
    # openpyxl auto-detects '=...' as a formula; force every text cell to be
    # a string so dialogue like "=..." survives a round-trip.
    cell.data_type = 's'
    cell.alignment = TEXT_ALIGN if kind in ([TAG, 'free'] + REF_TAGS) else CELL_ALIGN
    return cell


def write_readme(wb):
    ws = wb.create_sheet('README', 0)
    ws.column_dimensions['A'].width = 110
    for r, (text, style) in enumerate(README, 1):
        cell = ws.cell(row=r, column=1)
        cell.value = text
        cell.data_type = 's'
        cell.alignment = Alignment(wrap_text=True, vertical='top')
        if style == 'title':
            cell.font = Font(bold=True, size=15, color='1F3864')
        elif style == 'heading':
            cell.font = Font(bold=True, size=12, color='9C0006')
    ws.sheet_view.showGridLines = False
    return ws


def write_header(ws, columns):
    for i, (name, width, kind) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=i)
        cell.value = name
        cell.data_type = 's'
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL[kind]
        cell.alignment = Alignment(vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 26


def write_row(ws, row, columns, values, band):
    for i, (name, _width, kind) in enumerate(columns, 1):
        cell = put(ws, row, i, values[name], kind)
        if band:
            cell.fill = BAND_FILL


def finish_sheet(ws, columns, last_row):
    ws.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(columns)), max(last_row, 1))


def _entry_names(variants, entry):
    """The base-language speaker name a text entry can show.

    Only the base language is collected.  The other variant columns hold
    translations of the same name, and the target slot is seeded from the
    base language, so those are neither keys the name box draws nor extra
    names to translate.  A title that draws the name as a texture leaves the
    variant field blank; the entry's own display name is used then.
    """
    if variants is None:
        name = psb_io.speaker_name(entry)
    else:
        name = _speaker_of(variants, BASE_INDEX, entry)
    name = (name or '').strip()
    return [name] if name else []


def _speaker_of(variants, index, entry):
    """The speaker name of one language column.

    A title that draws the name as a texture leaves the variant's speaker
    field empty (a blank or a space); the name is then the one the entry
    itself carries, and it is shown in every column.
    """
    name = psb_io.slot_speaker(variants, index).strip()
    if name:
        return name
    return psb_io.speaker_name(entry)


def status_of(translated, reference, present):
    if not present:
        return 'missing'
    if translated == '':
        return 'empty'
    if translated == reference:
        return 'copied'
    return 'translated'


def load_previous(path):
    """Read the translated cells of an existing workbook, keyed by row id.

    A re-export must not throw away finished translations just because the
    scenario list grew (a newly extracted archive adds rows).  Only a
    cell whose status was ``translated`` is carried over: a ``copied`` cell is
    still the reference text and follows the new reference.

    The reference cell is stored as well.  When the game's own text of a row
    changed (a patch, a re-extraction), the previous translation was written
    against different source text and must not be presented as finished.
    """
    from openpyxl import load_workbook
    previous = {'text': {}, 'choice': {}, 'title': {}}
    if not path or not os.path.isfile(path):
        return previous
    old = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet, key, target_col, ref_col in (
                ('Text', 'text', TEXT_COL,
                 lang.ref_column('text', BASE_TAG)),
                ('Choices', 'choice', TEXT_COL,
                 lang.ref_column('text', BASE_TAG)),
                ('Titles', 'title', TITLE_COL,
                 lang.ref_column('title', BASE_TAG))):
            if sheet not in old.sheetnames:
                continue
            ws = old[sheet]
            rows = ws.iter_rows(values_only=True)
            try:
                head = next(rows)
            except StopIteration:
                continue
            names = {str(name): i for i, name in enumerate(head) if name}
            if 'id' not in names or target_col not in names:
                continue
            id_i = names['id']
            value_i = names[target_col]
            status_i = names.get('status')
            notes_i = names.get('notes')
            ref_i = names.get(ref_col)
            for row in rows:
                rid = row[id_i] if id_i < len(row) else None
                if not rid:
                    continue
                value = row[value_i] if value_i < len(row) else None
                status = row[status_i] if status_i is not None \
                    and status_i < len(row) else None
                notes = row[notes_i] if notes_i is not None \
                    and notes_i < len(row) else None
                reference = row[ref_i] if ref_i is not None \
                    and ref_i < len(row) else None
                previous[key][str(rid)] = {
                    'value': value,
                    'status': status,
                    'notes': notes,
                    'reference': reference,
                }
    finally:
        old.close()
    return previous


def merge_previous(values, previous, kind, target_col, reference=None):
    """Keep a finished translation of the same row, if its source is unchanged.

    A translation only stays finished while the line it translates is the same
    line.  When the reference text of the row changed since the last export,
    the stored value belongs to a different source line (a stale line
    from an older build, for example); carrying it over would mark it as a
    translation of the new text.  Those rows keep the fresh reference and get a
    note instead.
    """
    record = previous[kind].get(values.get('id'))
    if not record:
        return values
    value = record.get('value')
    status = record.get('status')
    notes = record.get('notes')
    old_reference = record.get('reference')
    if status == 'translated' and value not in (None, ''):
        if old_reference in (None, '') or reference is None \
                or old_reference == reference:
            values[target_col] = value
            values['status'] = 'translated'
        else:
            values['notes'] = ('source text changed since the last export; '
                               'the previous translation was not kept')
    elif notes:
        values['notes'] = notes
    return values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-o', '--out', default=_path('workbook', 'text_export.xlsx'))
    ap.add_argument('--scn-dir', default=_path('scn_dir', 'scn_overrides'),
                    help='folder containing rebuilt .scn files')
    ap.add_argument('--cache-dir', default=_path('cache_dir', 'cache/psb'))
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--only', default=None, help='substring filter on file names')
    ap.add_argument('--merge-from', default=None,
                    help='existing workbook whose finished translations are kept')
    args = ap.parse_args()

    previous = load_previous(args.merge_from)

    scn_dir = os.path.abspath(args.scn_dir)
    cache_dir = None if args.no_cache else os.path.abspath(args.cache_dir)
    # Which archive each rebuilt scenario came from (written by
    # build_scenarios.py).  Rows recovered from a secondary archive are
    # marked in the workbook so they stay recognisable.
    try:
        import build_scenarios
        provenance = build_scenarios.load_provenance(scn_dir)
    except Exception:
        provenance = {}
    # Overrides may mirror the engine's storage path (e.g. ``scn/foo.txt.scn``),
    # so walk the tree instead of assuming a flat folder.
    files = []
    for base, dirs, names in os.walk(scn_dir):
        dirs.sort()
        for n in sorted(names):
            if n.lower().endswith('.scn'):
                files.append(os.path.relpath(os.path.join(base, n), scn_dir))
    # The main archive comes first, the additional ones after it: the reader
    # follows the story of the main archive and finds the extra branches at
    # the end.
    main_archive = CONFIG.get('main_archive') or ''

    def archive_rank(name):
        archive = provenance.get(name.replace(os.sep, '/'), '')
        if not main_archive or not archive:
            return (1, name)
        return (0 if archive == main_archive else 1, name)

    files.sort(key=archive_rank)
    if args.only:
        files = [n for n in files if args.only in n]
    if not files:
        raise SystemExit('no .scn files in %s' % scn_dir)

    wb = Workbook()
    ws_text = wb.active
    ws_text.title = 'Text'
    ws_text.freeze_panes = 'F2'

    ws_choice = wb.create_sheet('Choices')
    ws_title = wb.create_sheet('Titles')

    write_header(ws_text, COLUMNS)
    write_header(ws_choice, CHOICES_COLUMNS)
    write_header(ws_title, TITLES_COLUMNS)

    counts = {'text': {'translated': 0, 'copied': 0, 'empty': 0, 'missing': 0},
              'choice': {'translated': 0, 'copied': 0, 'empty': 0, 'missing': 0},
              'title': {'translated': 0, 'copied': 0, 'empty': 0, 'missing': 0}}
    rows = {'text': 1, 'choice': 1, 'title': 1}
    name_counts = {}
    band = False
    failed = []

    for name in files:
        src = os.path.join(scn_dir, name)
        try:
            data = psb_io.load_scn(src, cache_dir)
        except Exception as exc:
            print('WARN %-44s %s: %s' % (name, type(exc).__name__, exc))
            failed.append(name)
            continue
        storage = data.get('name') or os.path.basename(name)[:-4]
        archive = provenance.get(name.replace(os.sep, '/'), '')
        band = not band

        # ---- dialogue -------------------------------------------------
        for _si, label, line, entry in psb_io.iter_texts(data):
            variants = psb_io.text_variants(entry)
            # The Names sheet is collected from the same pass as the text;
            # a separate walk over every scenario would double the export.
            for speaker in _entry_names(variants, entry):
                name_counts[speaker] = name_counts.get(speaker, 0) + 1
            refs = {}
            for tag in REF_TAGS:
                refs[tag] = (_speaker_of(variants, lang.variant_index(tag),
                                         entry),
                             psb_io.slot_text(variants, lang.variant_index(tag)))
            first = REF_TAGS[0]
            base_spk, base_txt = refs.get(BASE_TAG) or refs[first]
            if not base_txt:
                # The line has no text in the preferred language; the slot
                # was seeded from the first reference that does have one.
                for tag in REF_TAGS:
                    if refs[tag][1]:
                        base_spk, base_txt = refs[tag]
                        break
            target = psb_io.slot(variants, INDEX)
            if target is None:
                tgt_spk, tgt_txt = base_spk, base_txt
            else:
                tgt_spk = _speaker_of(variants, INDEX, entry)
                tgt_txt = psb_io.slot_text(variants, INDEX)
            status = status_of(tgt_txt, base_txt, target is not None)
            rows['text'] += 1
            values = {
                'id': '%s#%s#%d' % (storage, label, line),
                'file': storage,
                'archive': archive,
                'scene': label,
                'line': line,
                'voice': psb_io.voice_name(entry),
                'speaker_jp': psb_io.speaker_name(entry),
                SPEAKER_COL: tgt_spk,
                TEXT_COL: tgt_txt,
                # Some layouts store a display mirror (a string) in [2] and
                # recompute the real count on import; only an integer is a
                # character count.
                'length': (target[2]
                           if target and len(target) > 2
                           and isinstance(target[2], int) else len(tgt_txt)),
                'status': status,
                'notes': '',
            }
            for tag in REF_TAGS:
                values[lang.ref_column('speaker', tag)] = refs[tag][0]
                values[lang.ref_column('text', tag)] = refs[tag][1]
            values = merge_previous(values, previous, 'text', TEXT_COL, base_txt)
            values['status'] = status_of(values[TEXT_COL], base_txt,
                                         target is not None)
            counts['text'][values['status']] += 1
            write_row(ws_text, rows['text'], COLUMNS, values, band)

        # ---- choice buttons -------------------------------------------
        for _si, label, ci, sel in psb_io.iter_choices(data):
            ref_txt = psb_io.choice_text(sel)
            base = psb_io.choice_lang(sel, BASE_INDEX)
            base_txt = str(base.get('text') or '') if base else ''
            if not base_txt:
                # The preferred language has no text for this button; the
                # slot was seeded from the first reference that has one.
                for tag in REF_TAGS:
                    copy = psb_io.choice_lang(sel, lang.variant_index(tag))
                    if copy and copy.get('text'):
                        base_txt = str(copy.get('text'))
                        break
            if not base_txt:
                base_txt = ref_txt
            target = psb_io.choice_lang(sel, INDEX)
            tgt_txt = str(target.get('text') or '') if target else base_txt
            status = status_of(tgt_txt, base_txt, target is not None)
            rows['choice'] += 1
            values = {
                'id': '%s#%s#%d' % (storage, label, ci),
                'file': storage,
                'archive': archive,
                'scene': label,
                'choice': ci,
                lang.ref_column('text', REF_TAGS[0]): ref_txt,
                TEXT_COL: tgt_txt,
                'status': status,
                'notes': '',
            }
            for tag in REF_TAGS[1:]:
                copy = psb_io.choice_lang(sel, lang.variant_index(tag)) or {}
                values[lang.ref_column('text', tag)] = copy.get('text', '')
            values = merge_previous(values, previous, 'choice', TEXT_COL, base_txt)
            values['status'] = status_of(values[TEXT_COL], base_txt,
                                         target is not None)
            counts['choice'][values['status']] += 1
            write_row(ws_choice, rows['choice'], CHOICES_COLUMNS, values, band)

        # ---- localised titles -----------------------------------------
        for _si, label, title in psb_io.iter_titles(data):
            refs = {}
            for tag in REF_TAGS:
                idx = lang.variant_index(tag)
                refs[tag] = (str(title[idx]) if len(title) > idx
                             and title[idx] is not None else '')
            first = REF_TAGS[0]
            base_txt = refs.get(BASE_TAG, refs[first])
            if not base_txt:
                for tag in REF_TAGS:
                    if refs[tag]:
                        base_txt = refs[tag]
                        break
            present = len(title) > INDEX
            tgt_txt = (str(title[INDEX]) if present and title[INDEX] is not None
                       else base_txt)
            status = status_of(tgt_txt, base_txt, present)
            rows['title'] += 1
            values = {
                'id': '%s#%s' % (storage, label),
                'file': storage,
                'archive': archive,
                'scene': label,
                TITLE_COL: tgt_txt,
                'status': status,
                'notes': '',
            }
            for tag in REF_TAGS:
                values[lang.ref_column('title', tag)] = refs[tag]
            values = merge_previous(values, previous, 'title', TITLE_COL, base_txt)
            values['status'] = status_of(values[TITLE_COL], base_txt, present)
            counts['title'][values['status']] += 1
            write_row(ws_title, rows['title'], TITLES_COLUMNS, values, band)

    finish_sheet(ws_text, COLUMNS, rows['text'])
    finish_sheet(ws_choice, CHOICES_COLUMNS, rows['choice'])
    finish_sheet(ws_title, TITLES_COLUMNS, rows['title'])
    ws_choice.freeze_panes = 'E2'
    ws_title.freeze_panes = 'D2'

    write_readme(wb)
    wb.active = 1

    # The speaker-name table shares this workbook.  It is rebuilt from the
    # names found in the scenarios above; a translation already typed into
    # the target column of the previous workbook is carried over.
    out = os.path.abspath(args.out)
    source = args.merge_from or (out if os.path.isfile(out) else None)
    previous_names = {}
    if source and os.path.isfile(source):
        for name_base, name_pl, _status, notes in names_sheet.read_rows(source):
            previous_names[name_base] = (name_pl, notes)
    name_rows = []
    for name_base in sorted(name_counts, key=lambda n: (-name_counts[n], n)):
        name_pl, notes = previous_names.get(name_base, ('', ''))
        name_pl = (name_pl or '').strip()
        name_rows.append((name_base, name_pl,
                          names_sheet.status_of(name_base, name_pl), notes))
    names_sheet.write_sheet(wb, name_rows)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)

    print('wrote %s' % out)
    print('  files:   %d%s' % (len(files) - len(failed),
                               ' (%d failed)' % len(failed) if failed else ''))
    for key, label in (('text', 'Text'), ('choice', 'Choices'), ('title', 'Titles')):
        c = counts[key]
        print('  %-8s %5d entries   translated %d, copied %d, empty %d, missing %d'
              % (label, c['translated'] + c['copied'] + c['empty'] + c['missing'],
                 c['translated'], c['copied'], c['empty'], c['missing']))
    missing = sum(counts[k]['missing'] for k in counts)
    total = sum(counts[k][s] for k in counts for s in counts[k])
    if missing and missing == total:
        print('  note: run build_scenarios.py to add the %s slot first' % TAG)
    elif missing:
        print('  note: %d row(s) have no variant %d; see the status column'
              % (missing, INDEX))


if __name__ == '__main__':
    main()
