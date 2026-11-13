"""Import the translated columns from an Excel workbook into scenario PSBs.

Reads the workbook produced by ``export_text.py`` and writes:

* ``Text`` sheet    -> ``speaker_<tag>`` / ``text_<tag>`` into the configured
  dialogue variant
* ``Choices`` sheet -> ``text_<tag>`` into ``selects[i].language[index]``
* ``Titles`` sheet  -> ``title_<tag>`` into ``scene.title[index]``

The tag and variant index come from ``config.json`` (see ``src/lang.py``), so
the same code serves any target language.

Only rows whose translated value actually changed cause a PSB rebuild, and every
file is recompiled at most once even when all three sheets touch it, so a
partial translation pass stays fast.

Usage:
    py -X utf8 src/import_text.py
    py -X utf8 src/import_text.py --scn-dir "..\\_work\\scn_overrides" -w text_full.xlsx
    py -X utf8 src/import_text.py --dry-run

Safety rules:
    * an empty translated cell leaves the current value untouched (so an
      untranslated row is not wiped), unless ``--apply-empty`` is given;
    * cells stored as Excel formulas are rejected instead of silently writing
      ``=...`` into the script;
    * dialogue ``length`` is always recalculated from the display form.
"""
import argparse
import json
import os
import sys

from openpyxl import load_workbook

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import lang
import psb_io
from psb_lang_slot import add_lang_slot

CONFIG = lang.CONFIG
TAG = lang.TAG
INDEX = lang.INDEX
TEXT_COL = lang.TEXT_COL
SPEAKER_COL = lang.SPEAKER_COL
TITLE_COL = lang.TITLE_COL
# The columns of the languages the title already ships.  Their speaker
# names belong into their own variant, not only into the new one.
REF_TAGS = lang.REF_TAGS
ref_column = lang.ref_column
# The language the scenario is originally written in.  Its display name
# decides whether a line shows a name box at all.
BASE_INDEX = lang.BASE_INDEX


def _path(key, default):
    raw = (CONFIG.get('paths') or {}).get(key) or default
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)

TEXT_REQUIRED = ('file', 'scene', 'line', TEXT_COL)
CHOICE_REQUIRED = ('file', 'scene', 'choice', TEXT_COL)
TITLE_REQUIRED = ('file', 'scene', TITLE_COL)


def header_map(ws):
    result = {}
    for cell in ws[1]:
        if cell.value is None:
            continue
        result[str(cell.value).strip().lower()] = cell.column
    return result


def cell_text(ws, row, col):
    if col is None:
        return ''
    value = ws.cell(row=row, column=col).value
    if value is None:
        return ''
    return str(value)


def is_formula(ws, row, col):
    return col is not None and ws.cell(row=row, column=col).data_type == 'f'


def to_display(text):
    """Convert KAG-escaped text to the plain form stored in mirror fields.

    Some variants use five fields: ``[speaker, raw, length, plain, plain]``.
    The raw form escapes ``#`` as ``\\#`` and marks explicit breaks with
    ``\\n``; the plain mirrors drop both escapes and ``length`` counts the
    plain characters.  Verified exact against every five-field variant in the
    shipped scenarios.
    """
    return text.replace('\\#', '#').replace('\\n', '')


def resolve_file(scn_dir, name):
    name = name.strip()
    if not name:
        return None
    if not name.lower().endswith('.scn'):
        name += '.scn'
    path = os.path.join(scn_dir, name)
    if os.path.isfile(path):
        return path
    # The workbook stores ``root.name`` (no directory) while overrides may live
    # under the engine's storage path, so match the basename recursively.
    base = os.path.basename(name)
    path = os.path.join(scn_dir, base)
    if os.path.isfile(path):
        return path
    for root, dirs, files in os.walk(scn_dir):
        dirs.sort()
        if base in files:
            return os.path.join(root, base)
    return None


def file_from_id(fid):
    return fid.rsplit('#', 2)[0] if fid.count('#') >= 2 else ''


def read_text_sheet(ws, cols, args, buckets, stats, errors):
    for r in range(2, ws.max_row + 1):
        fid = cell_text(ws, r, cols.get('id')).strip()
        file_name = cell_text(ws, r, cols['file']).strip()
        scene = cell_text(ws, r, cols['scene']).strip()
        line_raw = cell_text(ws, r, cols['line']).strip()
        if not any((fid, file_name, scene, line_raw)):
            continue
        stats['rows'] += 1
        if not file_name:
            file_name = file_from_id(fid)

        text = cell_text(ws, r, cols[TEXT_COL])
        speaker = cell_text(ws, r, cols.get(SPEAKER_COL))
        # The name of the original language travels with its own column:
        # the engine reads the speaker of the language it renders from that
        # column, so a column without a name shows no name at all.
        ref_speakers = {}
        for ref_index, ref_tag in enumerate(REF_TAGS):
            ref_col = ref_column('speaker', ref_tag)
            if ref_col not in cols:
                continue
            value = cell_text(ws, r, cols[ref_col])
            if value != '':
                ref_speakers[ref_index] = value

        if is_formula(ws, r, cols[TEXT_COL]) or is_formula(ws, r, cols.get(SPEAKER_COL)):
            errors.append('Text row %d: cell is an Excel formula; use literal text' % r)
            continue
        if not file_name or not scene or not line_raw:
            errors.append('Text row %d: file/scene/line is incomplete' % r)
            continue
        try:
            line = int(float(line_raw))
        except ValueError:
            errors.append('Text row %d: line is not a number (%r)' % (r, line_raw))
            continue

        if text == '' and speaker == '' and not args.apply_empty:
            stats['empty_skipped'] += 1
            continue
        if args.only and args.only not in file_name:
            continue

        buckets.setdefault(file_name, {'text': [], 'choice': [], 'title': []})
        buckets[file_name]['text'].append(
            (r, scene, line, speaker, text, ref_speakers))


def read_choice_sheet(ws, cols, args, buckets, stats, errors):
    for r in range(2, ws.max_row + 1):
        fid = cell_text(ws, r, cols.get('id')).strip()
        file_name = cell_text(ws, r, cols['file']).strip()
        scene = cell_text(ws, r, cols['scene']).strip()
        choice_raw = cell_text(ws, r, cols['choice']).strip()
        if not any((fid, file_name, scene, choice_raw)):
            continue
        stats['rows'] += 1
        if not file_name:
            file_name = file_from_id(fid)

        text = cell_text(ws, r, cols[TEXT_COL])
        if is_formula(ws, r, cols[TEXT_COL]):
            errors.append('Choices row %d: cell is an Excel formula; use literal text' % r)
            continue
        if not file_name or not scene or not choice_raw:
            errors.append('Choices row %d: file/scene/choice is incomplete' % r)
            continue
        try:
            choice = int(float(choice_raw))
        except ValueError:
            errors.append('Choices row %d: choice is not a number (%r)' % (r, choice_raw))
            continue
        if text == '' and not args.apply_empty:
            stats['empty_skipped'] += 1
            continue
        if args.only and args.only not in file_name:
            continue

        buckets.setdefault(file_name, {'text': [], 'choice': [], 'title': []})
        buckets[file_name]['choice'].append((r, scene, choice, text))


def read_title_sheet(ws, cols, args, buckets, stats, errors):
    for r in range(2, ws.max_row + 1):
        fid = cell_text(ws, r, cols.get('id')).strip()
        file_name = cell_text(ws, r, cols['file']).strip()
        scene = cell_text(ws, r, cols['scene']).strip()
        if not any((fid, file_name, scene)):
            continue
        stats['rows'] += 1
        if not file_name:
            file_name = file_from_id(fid)

        text = cell_text(ws, r, cols[TITLE_COL])
        if is_formula(ws, r, cols[TITLE_COL]):
            errors.append('Titles row %d: cell is an Excel formula; use literal text' % r)
            continue
        if not file_name or not scene:
            errors.append('Titles row %d: file/scene is incomplete' % r)
            continue
        if text == '' and not args.apply_empty:
            stats['empty_skipped'] += 1
            continue
        if args.only and args.only not in file_name:
            continue

        buckets.setdefault(file_name, {'text': [], 'choice': [], 'title': []})
        buckets[file_name]['title'].append((r, scene, text))


def apply_text_edits(scene_by_label, edits, cols, file_name, args, errors, tally):
    for r, label, line, speaker, text, ref_speakers in edits:
        scene = scene_by_label.get(label)
        if scene is None:
            errors.append('row %d: scene %r not found in %s' % (r, label, file_name))
            continue
        texts = scene.get('texts')
        if not isinstance(texts, list) or not (0 <= line < len(texts)):
            errors.append('row %d: line %d out of range in %s/%s'
                          % (r, line, file_name, label))
            continue
        entry = texts[line]
        variants = psb_io.text_variants(entry)
        if not variants:
            errors.append('row %d: entry has no language variants' % r)
            continue
        while len(variants) <= INDEX:
            variants.append(json.loads(json.dumps(variants[0])))
        slot = variants[INDEX]
        if not isinstance(slot, list):
            slot = ['', '', 0]
            variants[INDEX] = slot
        while len(slot) < 3:
            slot.append(0 if len(slot) == 2 else '')

        new_speaker = (speaker
                       if SPEAKER_COL in cols and (speaker != '' or args.apply_empty)
                       else slot[0])

        # A line can show no name box at all.  The game stores a blank but
        # not empty display name (a single space) in the language column and
        # keeps the speaker key in the entry itself, for the voice table.
        # The engine draws the display name, trims it and ends up with an
        # empty string -- the plate stays hidden.  Such a line must stay
        # nameless in every language, so no speaker is written into any
        # column of it.
        base_display = psb_io.slot_speaker(variants, BASE_INDEX)
        hides_name = (base_display != '' and not base_display.strip())
        if hides_name:
            # The workbook copies the base column into the target column, so
            # the unchanged value means "keep the line nameless".  A value
            # the translator changed on purpose is written.
            base_column = ref_speakers.get(BASE_INDEX, '')
            if speaker == base_column:
                new_speaker = slot[0]
        new_text = text
        old_text = slot[1] if isinstance(slot[1], str) else str(slot[1] or '')

        # Some variants carry extra "plain" mirrors after index 2: they store
        # the KAG-escaped form in [1] and the display form in [3]/[4].  Keep
        # those in sync and use the display length for [2].
        has_plain = len(slot) > 3 and isinstance(slot[3], str)
        old_plain = slot[3] if has_plain else old_text

        # The name of the original language belongs into its own column.
        # The engine reads the speaker of the language it renders from the
        # column of that language; a column without a name (a title that
        # stores a blank one) makes the engine hide the name plate as soon
        # as it reads the name again -- which happens on every redraw of
        # the page, for example when the language is changed.
        #
        # Only a blank column is filled in: a column that already holds a
        # name is left alone (the game's own spelling wins), and a column
        # that holds no name at all (a title that keeps the name outside
        # the columns, in the entry itself) must not get one.
        for ref_index, ref_name in sorted(ref_speakers.items()):
            if hides_name:
                # The line shows no name box; every column keeps the game's
                # own value.
                break
            if ref_index >= len(variants):
                continue
            ref_slot = variants[ref_index]
            if not isinstance(ref_slot, list) or not ref_slot:
                continue
            if not isinstance(ref_slot[0], str):
                continue
            if ref_slot[0].strip():
                continue
            if ref_slot[0] == ref_name:
                continue
            ref_slot[0] = ref_name
            tally['ref_speaker'] += 1

        if slot[0] == new_speaker and old_plain == new_text:
            tally['unchanged'] += 1
            continue
        new_plain = (to_display(new_text)
                     if has_plain and old_plain != old_text
                     else new_text)
        # The raw text carries the inline markup of the engine; the workbook
        # holds the text without it, so the markup is put back.
        raw_text = new_text
        if has_plain and old_text != old_plain:
            segments = psb_io.markup_segments(old_text, old_plain)
            if segments:
                raw_text = psb_io.apply_markup(new_plain, segments,
                                               len(old_plain))
        slot[0] = new_speaker
        slot[1] = raw_text
        slot[2] = len(new_plain)
        if has_plain:
            for i in range(3, len(slot)):
                if isinstance(slot[i], str) and slot[i] == old_plain:
                    slot[i] = new_plain
        tally['text'] += 1


def apply_choice_edits(scene_by_label, edits, file_name, args, errors, tally):
    for r, label, choice, text in edits:
        scene = scene_by_label.get(label)
        if scene is None:
            errors.append('row %d: scene %r not found in %s' % (r, label, file_name))
            continue
        selects = scene.get('selects')
        if not isinstance(selects, list) or not (0 <= choice < len(selects)):
            errors.append('row %d: choice %d out of range in %s/%s'
                          % (r, choice, file_name, label))
            continue
        sel = selects[choice]
        if not isinstance(sel, dict):
            errors.append('row %d: choice entry is not an object' % r)
            continue
        langarr = sel.get('language')
        if not isinstance(langarr, list):
            langarr = []
            sel['language'] = langarr
        while len(langarr) <= INDEX:
            langarr.append(None)
        slot = langarr[INDEX]
        if not isinstance(slot, dict):
            slot = {}
            langarr[INDEX] = slot
        if (slot.get('text') == text
                and slot.get('searchtext') == text
                and slot.get('speechtext') == text):
            tally['unchanged'] += 1
            continue
        slot['text'] = text
        slot['searchtext'] = text
        slot['speechtext'] = text
        slot['textlength'] = len(text)
        tally['choice'] += 1


def apply_title_edits(scene_by_label, edits, file_name, errors, tally):
    for r, label, text in edits:
        scene = scene_by_label.get(label)
        if scene is None:
            errors.append('row %d: scene %r not found in %s' % (r, label, file_name))
            continue
        title = scene.get('title')
        if not isinstance(title, list) or len(title) < 2:
            errors.append('row %d: scene %r has no localised title list'
                          % (r, label))
            continue
        present = len(title) > INDEX
        while len(title) <= INDEX:
            title.append(title[0] if title else '')
        if present and title[INDEX] == text:
            tally['unchanged'] += 1
            continue
        title[INDEX] = text
        tally['title'] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-w', '--workbook',
                    default=_path('workbook', 'text_export.xlsx'))
    ap.add_argument('--scn-dir', default=_path('scn_dir', 'scn_overrides'))
    ap.add_argument('--cache-dir', default=_path('cache_dir', 'cache/psb'))
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--only', default=None, help='substring filter on file names')
    ap.add_argument('--apply-empty', action='store_true',
                    help='also write rows whose %s text is empty' % TAG)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    wb = load_workbook(os.path.abspath(args.workbook), data_only=False)
    if 'Text' not in wb.sheetnames:
        raise SystemExit('workbook has no Text sheet (run export_text.py first)')

    sheets = {}
    for name, required in (('Text', TEXT_REQUIRED),
                           ('Choices', CHOICE_REQUIRED),
                           ('Titles', TITLE_REQUIRED)):
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        cols = header_map(ws)
        missing = [c for c in required if c not in cols]
        if missing:
            raise SystemExit('%s sheet is missing columns: %s'
                             % (name, ', '.join(missing)))
        sheets[name] = (ws, cols)

    if SPEAKER_COL not in sheets['Text'][1]:
        print('note: no %s column; speaker names will not be changed' % SPEAKER_COL)

    scn_dir = os.path.abspath(args.scn_dir)
    cache_dir = None if args.no_cache else os.path.abspath(args.cache_dir)

    buckets = {}
    errors = []
    stats = {'rows': 0, 'empty_skipped': 0}

    read_text_sheet(sheets['Text'][0], sheets['Text'][1], args, buckets, stats, errors)
    if 'Choices' in sheets:
        read_choice_sheet(sheets['Choices'][0], sheets['Choices'][1], args,
                          buckets, stats, errors)
    if 'Titles' in sheets:
        read_title_sheet(sheets['Titles'][0], sheets['Titles'][1], args,
                         buckets, stats, errors)

    if not buckets:
        print('no edited rows to import (%d rows read, %d empty skipped)'
              % (stats['rows'], stats['empty_skipped']))
        for e in errors[:20]:
            print('  ' + e)
        return 1 if errors else 0

    files_changed = 0
    tally = {'text': 0, 'choice': 0, 'title': 0, 'unchanged': 0,
             'ref_speaker': 0}

    for file_name in sorted(buckets):
        work = buckets[file_name]
        if not (work['text'] or work['choice'] or work['title']):
            continue
        src = resolve_file(scn_dir, file_name)
        if not src:
            errors.append('%s: .scn not found in %s' % (file_name, scn_dir))
            continue
        try:
            data = psb_io.load_scn(src, cache_dir)
        except Exception as exc:
            errors.append('%s: decompile failed: %s' % (file_name, exc))
            continue

        if TAG not in (data.get('languages') or []):
            add_lang_slot(data, TAG, INDEX, source_index=0)

        scene_by_label = {}
        for scene in data.get('scenes') or []:
            label = scene.get('label')
            if label and label not in scene_by_label:
                scene_by_label[label] = scene

        before = dict(tally)
        apply_text_edits(scene_by_label, work['text'], sheets['Text'][1],
                         file_name, args, errors, tally)
        apply_choice_edits(scene_by_label, work['choice'], file_name,
                           args, errors, tally)
        apply_title_edits(scene_by_label, work['title'], file_name,
                          errors, tally)

        changed = {k: tally[k] - before[k] for k in ('text', 'choice', 'title')}
        total = sum(changed.values())
        if not total:
            continue

        parts = []
        for key, label in (('text', 'text'), ('choice', 'choice'), ('title', 'title')):
            if changed[key]:
                parts.append('%d %s' % (changed[key], label))
        summary = ', '.join(parts)

        if args.dry_run:
            print('DRY  %-44s %s' % (os.path.basename(src), summary))
        else:
            try:
                size = psb_io.save_and_build(data, src, cache_dir)
            except Exception as exc:
                errors.append('%s: rebuild failed: %s' % (file_name, exc))
                continue
            files_changed += 1
            print('OK   %-44s %-28s %8d bytes'
                  % (os.path.basename(src), summary, size))

    applied = tally['text'] + tally['choice'] + tally['title']
    print('')
    print('%s: %d row(s) in %d file(s); %d unchanged; %d empty skipped'
          % ('would apply' if args.dry_run else 'applied',
             applied, files_changed if not args.dry_run else len(buckets),
             tally['unchanged'], stats['empty_skipped']))
    if applied or tally['unchanged']:
        print('  by sheet: text %d, choice %d, title %d'
              % (tally['text'], tally['choice'], tally['title']))
    if tally['ref_speaker']:
        print('  speaker names of the original language: %d column(s)'
              % tally['ref_speaker'])
    if errors:
        print('%d error(s):' % len(errors))
        for e in errors[:30]:
            print('  ' + e)
        if len(errors) > 30:
            print('  ... %d more' % (len(errors) - 30))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
