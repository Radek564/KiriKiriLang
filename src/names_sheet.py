"""The speaker-name table: a ``Names`` sheet inside the text workbook.

A speaker name is not a plain label.  It is the key into the title's voice
table, its alias table and its name-plate map, so a translated name must never
be written into the scenario columns.  Instead every line keeps the title's own
name and the workbook records the label that the name box should draw:

    name_<base> | name_<tag> | status | notes

``name_<base>`` is the name the scenarios carry in the base language (the
language the target slot is seeded from: ``name_jp``, ``name_en``, ...), never
edit it.  ``name_<tag>`` is the translation.  The column names follow
``language.base_tag`` and ``language.tag`` from the profile, so a title whose
base language gets ``name_<base>`` and is translated the same way.
``build_patch.py`` turns this sheet into the runtime map used by
``dispNameFilter``, so only the drawn string changes.

The sheet lives in ``kits/<game>/<tag>/text/text.xlsx`` next to ``Text`` /
``Choices`` / ``Titles``.  The helpers in this module are shared by the
exporter (which collects the sheet), the name collector (which refreshes it
from the scenarios) and the patch builder (which reads it).
"""

import os

import lang

SHEET = 'Names'
WIDTHS = (26, 26, 12, 40)


def columns(base_tag=None, tag=None):
    """The ``(source, target, status, notes)`` header of the sheet.

    ``source`` names the base language, ``target`` the translated one; the
    names come from the profile so any base language scans as naturally as
    any other.
    """
    base_tag = base_tag or lang.BASE_TAG
    tag = tag or lang.TAG
    return ('name_%s' % base_tag, 'name_%s' % tag, 'status', 'notes')


def status_of(name_base, name_target):
    """``empty`` / ``copied`` / ``translated`` for one row."""
    if not name_target:
        return 'empty'
    return 'copied' if name_target == name_base else 'translated'


def read_rows(path, base_tag=None, tag=None):
    """Return the ``Names`` rows of a workbook as ``(base, target, status, notes)``.

    An absent file or an absent sheet yields no rows: a title without name
    translations simply has nothing to draw differently.  A sheet written by
    an older version always used ``name_jp`` / ``name_pl``; those headers are
    still accepted while the table is migrated.
    """
    from openpyxl import load_workbook

    if not path or not os.path.isfile(path):
        return []
    source_col, target_col = columns(base_tag, tag)[:2]
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            return []
        rows = wb[SHEET].iter_rows(values_only=True)
        try:
            head = next(rows)
        except StopIteration:
            return []
        head = [str(value).strip() if value is not None else '' for value in head]
        if source_col in head:
            i_src = head.index(source_col)
        elif 'name_jp' in head:
            i_src = head.index('name_jp')
        else:
            return []
        if target_col in head:
            i_tgt = head.index(target_col)
        elif 'name_pl' in head:
            i_tgt = head.index('name_pl')
        else:
            return []
        i_st = head.index('status') if 'status' in head else None
        i_no = head.index('notes') if 'notes' in head else None

        def cell(row, index):
            value = row[index] if index is not None and index < len(row) else None
            return '' if value is None else str(value).strip()

        result = []
        for row in rows:
            base = cell(row, i_src)
            if not base:
                continue
            result.append((base, cell(row, i_tgt), cell(row, i_st),
                           cell(row, i_no)))
        return result
    finally:
        wb.close()


def read_map(path, base_tag=None, tag=None):
    """Return ``{base_name: translated_name}`` for finished rows."""
    table = {}
    for name_base, name_pl, _status, _notes in read_rows(path, base_tag, tag):
        if name_base and name_pl and name_base != name_pl:
            table[name_base] = name_pl
    return table


def write_sheet(wb, rows, base_tag=None, tag=None):
    """Replace the ``Names`` sheet of ``wb`` with ``rows``.

    ``rows`` is an iterable of ``(base, target, status, notes)``; the sheet is
    appended after the dialogue sheets so the translator finds it last.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header = columns(base_tag, tag)
    if SHEET in wb.sheetnames:
        del wb[SHEET]
    ws = wb.create_sheet(SHEET)
    fill = PatternFill('solid', fgColor='FF1F4E79')
    font = Font(color='FFFFFFFF', bold=True)
    for col, (title, width) in enumerate(zip(header, WIDTHS), 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(vertical='center')
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = 'A2'
    row = 1
    for values in rows:
        row += 1
        for col, value in enumerate(values, 1):
            ws.cell(row=row, column=col, value=value)
    ws.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(header)), max(row, 1))
    return ws


def replace(path, rows, base_tag=None, tag=None):
    """Write ``rows`` as the ``Names`` sheet of ``path``, keeping other sheets."""
    from openpyxl import Workbook, load_workbook

    if os.path.isfile(path):
        wb = load_workbook(path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
    try:
        write_sheet(wb, rows, base_tag, tag)
        wb.save(path)
    finally:
        wb.close()
