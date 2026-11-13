"""Refresh the speaker-name table without re-exporting the dialogue.

The dialogue exporter (``export_text.py``) fills the ``Names`` sheet from the
same pass that writes the text, so this tool is only needed to refresh the
list after adding scenes without rebuilding the whole workbook.

A speaker name is not a label the engine prints as-is: it is the key into the
title's voice table and name plate.  Writing a translated name into the
scenario text therefore breaks those lookups (and, worse, leaks the last name
onto lines that have no speaker).  The translation belongs into the ``Names``
sheet instead: ``build_patch.py`` turns it into a runtime map that only
changes the string drawn in the name box.

The sheet shares ``text.xlsx`` with the dialogue and is a plain table:

    name_<base> | name_<tag> | status | notes

``name_<base>`` is what the scenarios carry in the base language (for example
``name_jp`` or ``name_en``), ``name_<tag>`` is the translated name shown by the
name box.  Only the base-language name is collected, never the names stored in
the other language columns.  Rows are ordered by how often the name appears in
the game.

    py -X utf8 src/names_export.py                 # refresh from scenarios
    py -X utf8 src/names_export.py --dry-run

An existing table is never overwritten: finished names are carried over and
only new names are appended.
"""
import argparse
import os
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import lang
import names_sheet
import psb_io

CONFIG = lang.CONFIG


def _path(key, default):
    raw = (CONFIG.get('paths') or {}).get(key) or default
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def default_workbook():
    """``kits/<slug>/<tag>/text/text.xlsx``, the kit's dialogue workbook."""
    slug = os.path.splitext(
        os.path.basename(CONFIG.get('_config_path') or 'config.json'))[0]
    return os.path.join(TEMPLATE, 'kits', slug, lang.TAG, 'text', 'text.xlsx')


def collect(scn_dir):
    """Return ``{base_name: count}`` for the names the name box can draw.

    The target slot is seeded from the base language, so only that variant's
    speaker is a name the patch can translate; the other variant columns hold
    the same names in other languages.  When the variant field is empty (a
    name drawn as a texture) the entry's own display name is used.
    """
    counts = {}
    files = []
    for root, dirs, names in os.walk(scn_dir):
        dirs.sort()
        for name in sorted(names):
            if name.lower().endswith('.scn'):
                files.append(os.path.join(root, name))
    for path in files:
        try:
            data = psb_io.load_scn(path)
        except Exception as exc:
            print('WARN %-40s %s' % (os.path.basename(path), exc))
            continue
        for scene in data.get('scenes') or []:
            for entry in scene.get('texts') or []:
                variants = psb_io.text_variants(entry)
                if variants is None:
                    continue
                name = (psb_io.slot_speaker(variants, lang.BASE_INDEX)
                        or '').strip()
                if not name:
                    name = (psb_io.speaker_name(entry) or '').strip()
                if name:
                    counts[name] = counts.get(name, 0) + 1
    return counts


def previous(path):
    """Read the current names, keeping the translated column."""
    result = {}
    for name_base, name_target, _status, notes in names_sheet.read_rows(path):
        result[name_base] = {lang.TAG: name_target, 'notes': notes}
    return result


def write(path, counts, old):
    order = sorted(counts, key=lambda n: (-counts[n], n))
    rows = []
    for name in order:
        prev = old.get(name) or {}
        value = str(prev.get(lang.TAG) or '').strip()
        rows.append((name, value, names_sheet.status_of(name, value),
                     prev.get('notes')))
    names_sheet.replace(path, rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-o', '--out', default=None,
                    help='text workbook (default: '
                         'kits/<game>/<tag>/text/text.xlsx)')
    ap.add_argument('--scn-dir', default=_path('scn_dir', 'scn_overrides'))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    out = os.path.abspath(args.out or default_workbook())
    scn_dir = os.path.abspath(args.scn_dir)
    if not os.path.isdir(scn_dir):
        raise SystemExit('no scenario folder: %s' % scn_dir)

    counts = collect(scn_dir)
    old = previous(out)
    done = sum(1 for n in counts
               if (old.get(n) or {}).get(lang.TAG)
               and str(old[n][lang.TAG]).strip() not in ('', n))
    print('%d name(s) in %s; %d already translated'
          % (len(counts), os.path.basename(scn_dir), done))
    print('workbook: %s' % out)
    if args.dry_run:
        for name in sorted(counts, key=lambda n: (-counts[n], n)):
            target = (old.get(name) or {}).get(lang.TAG) or ''
            print('  %6d  %r -> %r' % (counts[name], name, target))
        return 0
    total = write(out, counts, old)
    print('wrote %d row(s)' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
