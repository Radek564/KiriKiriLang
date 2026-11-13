"""Export game assets (images + PSB textures) into an editable work folder.

Each named image becomes a file under ``assets_work/files/``; each PSB texture
container is unpacked natively into ``assets_work/psb/<name>/`` (JSON +
one PNG per texture).  A manifest records the SHA-256 so ``import_assets.py``
can tell exactly which exports you edited.

``--ui`` restricts the export to ``uipsd`` interface images.  It understands
``.pimg`` / ``.uipsd`` files, language suffixes (``option_en.pimg``), language
folders (``uipsd/en/`` and ``uipsd/_en/``), and ``.tjs`` dumps with
their sibling ``.tlg`` texture folders.  ``--text-only`` keeps only PSBs whose
layers mark rendered text (``layer_type == 2``).

Usage:
    py -X utf8 src/export_assets.py
    py -X utf8 src/export_assets.py --only title
    py -X utf8 src/export_assets.py --extensions .png,.pimg --reset
    py -X utf8 src/export_assets.py --ui --text-only
"""
import argparse
import json
import os
import shutil
import sys
import time

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import asset_io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

CONFIG = asset_io.load_config()
TAG = CONFIG['language']['tag']
LANGUAGE = CONFIG.get('language') or {}
ASSETS_CFG = CONFIG.get('assets') or {}
PATHS = CONFIG.get('paths') or {}

UI_TAGS = list(CONFIG.get('ui_tags') or [])
VARIANTS = list(LANGUAGE.get('variants') or UI_TAGS or [TAG])
BASE_TAG = (LANGUAGE.get('base_tag') or
            (UI_TAGS[0] if UI_TAGS else TAG))

TAGS = []
for _tag in UI_TAGS + VARIANTS + [BASE_TAG, TAG]:
    if _tag and _tag not in TAGS:
        TAGS.append(_tag)

PREFER_TAGS = []
for _tag in ('en', 'cn'):
    if _tag in TAGS and _tag != TAG:
        PREFER_TAGS.append(_tag)
for _tag in TAGS:
    if _tag != TAG and _tag not in PREFER_TAGS:
        PREFER_TAGS.append(_tag)

OVERRIDE_MODE = ASSETS_CFG.get('override_mode') or 'auto'
UI_DIRS = list(ASSETS_CFG.get('ui_dirs') or asset_io.UI_DIRS)
# The profile knows which images the game asks for by name.  Its list is
# authoritative: the generic patterns are only a fallback for a profile
# that says nothing, because they also match decorative images
# (``attention5.png``, ``attention_new.png``) that carry no text.
UI_PATTERNS = list(ASSETS_CFG.get('ui_patterns') or [])
if not UI_PATTERNS and not ASSETS_CFG.get('tag_names'):
    UI_PATTERNS = list(asset_io.UI_PATTERNS)
UI_TAG_NAMES = list(dict.fromkeys(
    list(asset_io.UI_TAG_NAMES) + list(ASSETS_CFG.get('tag_names') or [])))
# Folders whose images carry the game's own text (the story pictures).
TEXT_DIRS = list(ASSETS_CFG.get('text_dirs') or asset_io.TEXT_DIRS)
# Folders that never carry text (effect and background art).
EXCLUDE_DIRS = list(ASSETS_CFG.get('exclude_dirs') or asset_io.EXCLUDE_DIRS)


def _path(key, default):
    return asset_io.resolve(PATHS.get(key) or default)


def _in_text_dir(storage):
    """True when a file lives in a folder that carries the game's own text."""
    text = (storage or '').replace('\\', '/')
    for root in TEXT_DIRS:
        root = root.strip('/')
        if root and (text == root or text.startswith(root + '/')):
            return True
    return False


def _target_copy(storage):
    """The name the engine asks for when it loads a file in the new language.

    A single-language file (a story picture) has no language in its name;
    the engine looks for the copy with the tag in it.
    """
    folder, _sep, name = (storage or '').replace('\\', '/').rpartition('/')
    stem, ext = os.path.splitext(name)
    tagged = '%s_%s%s' % (stem, TAG, ext)
    return (folder + '/' + tagged) if folder else tagged


def _ui_source(extracted):
    raw = ASSETS_CFG.get('ui_source')
    if raw:
        return asset_io.resolve(raw)
    return extracted


COLUMNS = [
    ('storage', 42, 'meta'),
    ('kind', 10, 'meta'),
    ('source', 10, 'meta'),
    ('language', 10, 'meta'),
    ('override', 34, 'meta'),
    ('target', 38, 'meta'),
    ('archive', 12, 'meta'),
    ('dir', 18, 'meta'),
    ('size', 10, 'meta'),
    ('path', 52, 'meta'),
    ('sha256', 20, 'meta'),
    ('status', 14, 'meta'),
    ('text', 8, 'meta'),
    ('layers', 50, 'meta'),
]
COL_INDEX = {name: i + 1 for i, (name, _w, _k) in enumerate(COLUMNS, 1)}

HEADER_FILL = PatternFill('solid', fgColor='1F3864')
HEADER_FONT = Font(bold=True, color='FFFFFF')

README = [
    ('Asset workbook (images / PSB textures)', 'title'),
    ('', ''),
    ('Export folder : %s' % 'assets_work', ''),
    ('Import target : assets/ (shipped in unencrypted/ by langpatch build)', ''),
    ('', ''),
    ('HOW TO EDIT', 'heading'),
    ('1. Strings: a named image is a single file under files/.', ''),
    ('   PSB textures: open psb/<name>/ - the PNGs are the textures, the .json', ''),
    ('   is the PSB structure.  Replace PNGs, keep the file names.', ''),
    ('2. Do not rename or move files inside an exported PSB folder.', ''),
    ('3. Run:  langpatch images-import', ''),
    ('4. Rebuild:  langpatch build', ''),
    ('', ''),
    ('UI EXPORT (--ui)', 'heading'),
    ('Interface assets are selected: every uipsd screen plus standalone', ''),
    ('UI pictures such as attention.png, exchview_watermark.png and', ''),
    ('warning.pimg.  Language variants are collapsed to one source per asset:', ''),
    ('The first reference language wins over the next one; a single-language', ''),
    ('original falls back to its untagged base file.  The title screen is', ''),
    ('always the untagged title.pimg: a title_<tag> copy is an overlay.', ''),
    ('Language-aware names get a flat _<tag> file; images the engine loads', ''),
    ('by their real path keep that path.', ''),
    ('--text-only keeps only the screens the game loads in a language', ''),
    ('variant (so baked-in text is translated too) and PSBs with text', ''),
    ('layers.  A pure decoration image is not shipped.', ''),
    ('', ''),
    ('STATUS', 'heading'),
    ('exported   written by export_assets.py', ''),
    ('changed    imported since export (the importer only rebuilds these)', ''),
    ('', ''),
    ('NOTES', 'heading'),
    ('override = file name written into assets/; _en assets become _<tag>.', ''),
    ('target   = path under assets/.  A language override stays at the root', ''),
    ('           (GetUIFile looks it up by bare name); a same-name override', ''),
    ('           keeps its original folder, for example uipsd/dialog.pimg.', ''),
    ('text     = number of PSB text layers (layer_type 2).  0 means the image', ''),
    ('           has no baked text and usually needs no translation.', ''),
    ('Only PNG/JPEG/WebP/BMP and PSBs that really embed PNGs are exported.', ''),
]


def write_readme(wb):
    ws = wb.create_sheet('README', 0)
    ws.column_dimensions['A'].width = 100
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


def _write_text_only(wb, text_only):
    ws = wb.create_sheet('TextOnly')
    for i, (name, width) in enumerate((('storage', 52), ('layers', 80)), 1):
        cell = ws.cell(row=1, column=i)
        cell.value = name
        cell.data_type = 's'
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        ws.column_dimensions[chr(64 + i)].width = width
    for r, item in enumerate(text_only, 2):
        ws.cell(row=r, column=1).value = item['storage']
        ws.cell(row=r, column=1).data_type = 's'
        ws.cell(row=r, column=2).value = ', '.join(item.get('text_layers') or [])
        ws.cell(row=r, column=2).data_type = 's'
    ws.freeze_panes = 'A2'


def _reset_export(out):
    """Remove the exported parts of *out* and keep everything else.

    The export folder is the kit's interface folder: the caption files live
    next to the exported images, so a reset removes the images and the
    textures and leaves the kit's own files alone.
    """
    keep = ('catalog.json', 'assets_export.xlsx', 'README.md')
    manifest_path = os.path.join(out, 'manifest.json')
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        for asset in data.get('assets') or []:
            rel = asset.get('path') or ''
            if not rel:
                continue
            path = os.path.join(out, rel.replace('/', os.sep))
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    # Everything else in the folder is export output: the images the
    # previous run copied and the unpacked textures.
    for base, dirs, files in os.walk(out, topdown=False):
        for name in files:
            if name in keep or name.startswith('syslangtext_'):
                continue
            try:
                os.remove(os.path.join(base, name))
            except OSError:
                pass
        for name in dirs:
            path = os.path.join(base, name)
            if name in ('psb', 'tmp'):
                shutil.rmtree(path, ignore_errors=True)
            elif not os.listdir(path):
                try:
                    os.rmdir(path)
                except OSError:
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--filemap', default=None,
                    help='index produced by hash_dumper.py (default: config paths.filemap)')
    ap.add_argument('--extracted', default=None,
                    help='extracted tree (default: config extract_dir)')
    ap.add_argument('--ui-source', default=None,
                    help='folder containing uipsd/ (default: config assets.ui_source '
                         'or extract_dir)')
    ap.add_argument('--out', default=None,
                    help='work folder (default: config paths.assets_work)')
    ap.add_argument('--workbook', default=None,
                    help='manifest workbook (default: config paths.assets_workbook)')
    ap.add_argument('--only', default=None, help='substring filter on storage name')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--extensions', default=None,
                    help='comma list; default: config assets.extensions')
    ap.add_argument('--dirs', default=None,
                    help='comma list of unnamed asset dirs; default: config assets.dirs')
    ap.add_argument('--ui', action='store_true',
                    help='export only interface images/PSBs (uipsd + tagged '
                         'standalone UI pictures)')
    ap.add_argument('--text-only', action='store_true',
                    help='with --ui, keep only PSBs that contain text layers')
    ap.add_argument('--reset', action='store_true', help='wipe the work folder first')
    args = ap.parse_args()

    filemap = args.filemap or _path('filemap', 'build/filemap.json')
    extracted = args.extracted or asset_io.resolve(CONFIG.get('extract_dir'))
    source = args.ui_source or _ui_source(extracted)
    out = os.path.abspath(args.out or _path('assets_work', 'assets_work'))
    workbook = os.path.abspath(args.workbook or _path('assets_workbook', 'assets_export.xlsx'))

    if not args.ui and not os.path.isfile(filemap):
        raise SystemExit('filemap not found: %s\n'
                         'run: py -X utf8 src/hash_dumper.py --extracted %s --filemap %s'
                         % (filemap, extracted, filemap))
    if args.ui and not os.path.isdir(source):
        raise SystemExit('UI source folder not found: %s\n'
                         'set assets.ui_source in the profile or pass --ui-source'
                         % source)

    extensions = ([e.strip() for e in args.extensions.split(',')]
                  if args.extensions else ASSETS_CFG.get('extensions')
                  or ['.pimg', '.png', '.jpg', '.jpeg', '.webp', '.bmp'])
    dirs = ([d.strip() for d in args.dirs.split(',')]
            if args.dirs else ASSETS_CFG.get('dirs') or ['uipsd/_en/'])
    by_size = {int(k): v for k, v in (ASSETS_CFG.get('by_size') or {}).items()}

    picked = []
    if args.ui and os.path.isfile(filemap) and os.path.isdir(extracted):
        entries = asset_io.load_filemap(filemap)
        picked = asset_io.select_assets(
            entries, extracted, dirs=dirs, extensions=extensions,
            by_size=by_size, include=args.only, tag=TAG, ui_only=True,
            prefer_tags=PREFER_TAGS, override_mode=OVERRIDE_MODE, tags=TAGS,
            ui_dirs=UI_DIRS, patterns=UI_PATTERNS, tag_names=UI_TAG_NAMES,
            text_dirs=TEXT_DIRS, exclude_dirs=EXCLUDE_DIRS)
    if args.ui and not picked:
        picked = asset_io.scan_ui_tree(
            source, extensions=extensions, tags=TAGS, target_tag=TAG,
            prefer_tags=PREFER_TAGS, override_mode=OVERRIDE_MODE,
            include=args.only, ui_dirs=UI_DIRS, patterns=UI_PATTERNS,
            tag_names=UI_TAG_NAMES, text_dirs=TEXT_DIRS,
            exclude_dirs=EXCLUDE_DIRS)
    if not args.ui:
        if not extracted or not os.path.isdir(extracted):
            raise SystemExit('extracted folders not found: %s' % extracted)
        entries = asset_io.load_filemap(filemap)
        picked = asset_io.select_assets(
            entries, extracted, dirs=dirs, extensions=extensions,
            by_size=by_size, include=args.only, tag=TAG)
    if args.limit:
        picked = picked[:args.limit]
    if not picked:
        if args.ui:
            print('no UI images found under %s' % source)
            print('this title may store its interface as text dumps only '
                  '(for example *.psb.tjs without textures)')
            return 0
        raise SystemExit('no matching assets (check --extensions/--dirs)')

    print('assets selected: %d' % len(picked))
    if args.ui:
        print('ui source      : %s' % source)
        print('language tags  : %s (prefer %s)'
              % (', '.join(TAGS), ', '.join(PREFER_TAGS) or 'base'))

    if args.reset:
        # The export folder also holds the kit's caption files, so only
        # the exported parts are removed: the textures and the files the
        # previous manifest names.
        _reset_export(out)
    os.makedirs(out, exist_ok=True)
    files_dir = out
    # The unpacked textures keep the game's own path (``uipsd/<name>.pimg``),
    # so an edited texture replaces the game's file.
    psb_dir = out
    tmp_dir = os.path.join(out, 'tmp')
    os.makedirs(psb_dir, exist_ok=True)

    manifest = []
    text_only = []
    skipped = []
    for i, item in enumerate(picked, 1):
        storage = item['storage']
        if item.get('kind') == 'psb_dump_text':
            text_only.append({
                'storage': storage,
                'source': item.get('source'),
                'text_layers': item.get('text_layers') or [],
            })
            print('[%d/%d] TEXT %-44s %d text layer(s), no textures'
                  % (i, len(picked), storage,
                     len(item.get('text_layers') or [])))
            continue

        safe = asset_io.sanitize(storage)
        rel_path = None
        source_kind = item.get('kind') or 'image'
        text = []
        # The engine asks for this file in the new language when the target
        # differs from the storage (a language override).  Those and the
        # story pictures belong into the kit; the rest of the interface art
        # is the game's own.
        language_aware = bool(item.get('target')
                              and item['target'] != storage)
        if item['kind'] == 'image':
            if (args.text_only and not language_aware
                    and not _in_text_dir(storage)):
                skipped.append((storage, 'plain image (not a PSB)'))
                print('[%d/%d] SKIP %-44s plain image'
                      % (i, len(picked), storage))
                continue
            # The kit keeps the file under its own name; the name the
            # engine asks for in the new language is the target of the
            # manifest, so the packer writes it under that one.
            dest = os.path.join(files_dir, safe)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(item['source'], dest)
            rel_path = os.path.relpath(dest, out)
            note = 'image'
            has_text = None
            if _in_text_dir(storage):
                item['target'] = _target_copy(storage)
        else:
            dest = os.path.join(psb_dir, safe)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            if item['kind'] == 'psb_dump':
                tmp_pimg = os.path.join(tmp_dir, safe + '.pimg')
                asset_io.build_psb_from_dump(
                    item['source'], item['texture_dir'], tmp_pimg)
                json_path, pngs, text = asset_io.decompile_psb(tmp_pimg, dest)
                try:
                    os.remove(tmp_pimg)
                except OSError:
                    pass
                source_kind = 'psb_dump'
            else:
                json_path, pngs, text = asset_io.decompile_psb(
                    item['source'], dest)
            if not pngs:
                shutil.rmtree(dest, ignore_errors=True)
                skipped.append((storage, 'PSB has no PNG textures'))
                print('[%d/%d] SKIP %-44s no PNG textures'
                      % (i, len(picked), storage))
                continue
            rel_path = os.path.relpath(dest, out)
            note = '%d texture(s)' % pngs
            has_text = bool(text)
            if args.text_only and not has_text and not language_aware:
                # A screen the game loads in the new language is kept even
                # when its text is baked into the picture instead of living
                # in text layers: that picture is exactly what has to be
                # translated.
                shutil.rmtree(dest, ignore_errors=True)
                skipped.append((storage, 'no text layers'))
                print('[%d/%d] SKIP %-44s no text layers'
                      % (i, len(picked), storage))
                continue

        digest = asset_io.hash_path(os.path.join(out, rel_path))
        manifest.append({
            'storage': storage,
            'override': item['override'],
            'target': item.get('target') or item['override'],
            'kind': item['kind'],
            'source_kind': source_kind,
            'language': item.get('language'),
            'archive': item.get('archive'),
            'dir': item.get('dir'),
            'name': item.get('name'),
            'size': item.get('size'),
            'path': rel_path.replace('\\', '/'),
            'sha256': digest,
            'status': 'exported',
            'has_text': has_text,
            'text_layers': text,
        })
        print('[%d/%d] OK   %-44s %-12s %s%s'
              % (i, len(picked), storage, note,
                 item['override'] or '(no override)',
                 ' [text]' if has_text else ''))

    manifest_path = os.path.join(out, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({
            'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
            'tag': TAG,
            'out': out,
            'ui': bool(args.ui),
            'assets': manifest,
            'text_only': text_only,
        }, f, ensure_ascii=False, indent=1)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Images'
    for i, (name, width, _kind) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=i)
        cell.value = name
        cell.data_type = 's'
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical='center', wrap_text=True)
        ws.column_dimensions[chr(64 + i)].width = width
    for r, asset in enumerate(manifest, 2):
        values = {
            'storage': asset['storage'],
            'kind': asset['kind'],
            'source': asset.get('source_kind') or '',
            'language': asset.get('language') or '',
            'override': asset['override'] or '',
            'target': asset.get('target') or '',
            'archive': asset.get('archive') or '',
            'dir': asset.get('dir') or '',
            'size': asset.get('size'),
            'path': asset['path'],
            'sha256': asset['sha256'],
            'status': asset['status'],
            'text': len(asset.get('text_layers') or []),
            'layers': ', '.join(asset.get('text_layers') or []),
        }
        for cname, _w, _k in COLUMNS:
            cell = ws.cell(row=r, column=COL_INDEX[cname])
            cell.value = values[cname]
            cell.data_type = 's'
    ws.freeze_panes = 'A2'
    if text_only:
        _write_text_only(wb, text_only)
    write_readme(wb)
    wb.active = 1
    wb.save(workbook)

    print('')
    print('wrote %s' % manifest_path)
    print('wrote %s' % workbook)
    print('exported %d asset(s), skipped %d, text-only %d'
          % (len(manifest), len(skipped), len(text_only)))
    for name, why in skipped:
        print('  skipped %-40s %s' % (name, why))


if __name__ == '__main__':
    main()
