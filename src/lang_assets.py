"""Give the new language its own copy of every language-specific asset.

A title may ship only one UI language.  The patch then has to create the
``_<tag>`` files itself: a menu texture, ``syslangtext_en.ini``,
``help_tips_en.txt`` and so on all need a matching ``_<tag>`` file before the
engine can open them in the new language slot.

This tool finds those files and writes the missing variants into ``assets/``:

* a file name that ends in a *reference* tag (``option_en.pimg``,
  ``syslangtext_cn.ini``) becomes the same name with the new tag;
* a directory named after a reference tag (``uipsd/_en/``) becomes the new
  tag's directory (``uipsd/_<tag>/``);
* with ``assets.seed_untagged`` a single-language file such as
  ``help_tips.txt`` is duplicated as ``help_tips_<tag>.txt``.

Nothing is guessed: a target is only created from a file that really exists,
and an existing target is never overwritten, so translated assets survive a
re-run.  Copying a reference file leaves the original text in place; the
translator then edits ``assets/<name>_<tag>`` and rebuilds.

Usage:
    py -X utf8 src/lang_assets.py
    py -X utf8 src/lang_assets.py --from <english-patch-folder> --dry-run
    py -X utf8 src/lang_assets.py --force --only syslangtext
"""
import argparse
import fnmatch
import os
import re
import shutil
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import asset_io

CONFIG = asset_io.load_config()
LANGUAGE = CONFIG.get('language') or {}
TAG = LANGUAGE.get('tag')
if not TAG:
    raise SystemExit('config.json: language.tag is required')
LABEL = LANGUAGE.get('label') or TAG
PATHS = CONFIG.get('paths') or {}
ASSETS_CFG = CONFIG.get('assets') or {}

VARIANTS = list(LANGUAGE.get('variants') or CONFIG.get('ui_tags') or [])
if TAG not in VARIANTS:
    VARIANTS.append(TAG)
INDEX = LANGUAGE.get('index')
if INDEX is None:
    INDEX = VARIANTS.index(TAG)
REF_TAGS = [t for t in VARIANTS[:INDEX] if t and t != TAG]
if not REF_TAGS:
    REF_TAGS = [t for t in VARIANTS if t != TAG]

DEFAULT_TEXT_EXT = ('.ini', '.txt', '.csv', '.json', '.tjs', '.ks', '.dat')


def _path(key, default):
    return asset_io.resolve(PATHS.get(key) or default)


def _tagged_name(name, ref):
    """Replace a trailing ``_ref``/``-ref`` with the new tag."""
    stem, ext = os.path.splitext(name)
    for sep in ('_', '-'):
        suffix = sep + ref
        if stem.lower().endswith(suffix.lower()):
            return stem[:-len(suffix)] + sep + TAG + ext
    return None


def _tagged_dir(path, ref):
    """Swap a directory component named ``_ref`` / ``ref`` for the new tag."""
    parts = path.replace('\\', '/').split('/')
    out = []
    for part in parts:
        if part == '_' + ref:
            out.append('_' + TAG)
        elif part == ref:
            out.append(TAG)
        else:
            out.append(part)
    result = '/'.join(out)
    return result if result != path.replace('\\', '/') else None


def _is_text(name, extensions):
    return os.path.splitext(name)[1].lower() in extensions


def _iter_asset_dir(root):
    """Yield ``(absolute path, relative path)`` for every file under *root*."""
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace('\\', '/')
            yield path, rel


def _iter_filemap_entries(filemap, extracted):
    """Yield ``(absolute source, relative storage path, size)``.

    Only entries with a known name are useful: an unnamed Hxv4 entry has no
    storage name to start from, so no target name can be derived for it.
    """
    entries = asset_io.load_filemap(filemap)
    for entry in entries:
        name = entry.get('name')
        if not name:
            continue
        d = (entry.get('dir') or '').replace('\\', '/')
        if d in ('%EmptyString%', '%EmptyString%/'):
            d = ''
        if d and not d.endswith('/'):
            d += '/'
        storage = d + name
        try:
            src = asset_io.entry_path(entry, extracted)
        except Exception:
            continue
        if os.path.isfile(src):
            yield src, storage, entry.get('size')


def target_names(rel, flat=True):
    """Return ``[(target, ref, folder_match)]`` the new language needs.

    Both the directory and the file name may carry the language tag:

    * ``uipsd/_en/option.pimg`` -> ``uipsd/_<tag>/option.pimg``
    * ``syslangtext_en.ini``    -> ``syslangtext_<tag>.ini``
    * ``main/help_tips_en.txt`` -> ``main/help_tips_<tag>.txt`` *and*, because the
      patch folder is flat, ``help_tips_<tag>.txt`` (``flat=True``).

    A file that matches nothing returns an empty list.  A screen listed in
    :data:`asset_io.BASE_SCREENS` is never seeded under a tagged name: its
    untagged file is the complete screen and the tagged copy is only a
    partial overlay.
    """
    rel = rel.replace('\\', '/')
    key_stem = os.path.splitext(
        os.path.basename(asset_io.ui_key(rel, tuple(REF_TAGS) + (TAG,))))[0]
    if key_stem.lower() in asset_io.BASE_SCREENS:
        return []
    out = []
    seen = set()

    def push(target, ref, folder_match):
        if target not in seen:
            seen.add(target)
            out.append((target, ref, folder_match))

    for ref in REF_TAGS:
        folder = os.path.dirname(rel)
        swapped = _tagged_dir(folder, ref) if folder else None
        if swapped:
            stem = swapped + '/' + os.path.basename(rel)
        else:
            stem = rel
        tagged = _tagged_name(stem, ref)
        if tagged is None:
            if swapped:
                tagged = stem          # only the folder carried the tag
            else:
                continue
        push(tagged, ref, bool(swapped))
        if flat and folder:
            base = os.path.basename(rel)
            flat_name = _tagged_name(base, ref)
            if flat_name:
                push(flat_name, ref, False)
    return out


def _candidate_score(target, ref, folder_match, size, by_size, from_assets):
    """Prefer the version the game itself actually uses for this slot.

    The same override name can be reached from several reference files
    (``adult/option_<ref>.pimg``, ``uipsd/_<ref>/option_<ref>.pimg`` and the
    matching pair of a second language).  The explicit ``assets.by_size``
    table names the canonical UI file;
    if it matches, that source wins.  Otherwise the file inside the
    ``uipsd/_<ref>`` folder is the better template than a flat duplicate.
    """
    score = 0
    if from_assets:
        score += 500          # author-supplied reference beats game copies
    if str(size) in by_size and \
            by_size[str(size)].replace('{tag}', TAG) == target:
        score += 400
    if folder_match:
        score += 200
    try:
        score += 100 - REF_TAGS.index(ref) * 10
    except ValueError:
        pass
    return score


def collect_candidates(sources, untagged, flat=True, by_size=None):
    """Return ``[(source, target, kind)]`` for every seedable asset.

    *sources* is a list of ``(label, path, kind)`` where ``kind`` is ``dir``
    or ``filemap``.  When several reference files map to one target, the one
    with the highest score wins (see :func:`_candidate_score`).
    """
    by_size = by_size or {}
    candidates = {}
    order = []

    def add(src, target, note, score):
        current = candidates.get(target)
        if current is None:
            candidates[target] = (src, note, score)
            order.append(target)
        elif score > current[2]:
            candidates[target] = (src, note, score)

    for label, path, kind in sources:
        from_assets = (kind == 'dir' and os.path.abspath(path)
                       == os.path.abspath(_path('assets_dir', 'assets')))
        if kind == 'dir':
            for src, rel in _iter_asset_dir(path):
                for target, ref, folder_match in target_names(rel, flat):
                    score = _candidate_score(target, ref, folder_match,
                                             os.path.getsize(src), by_size,
                                             from_assets)
                    add(src, target, 'tagged asset', score)
            continue

        # Filemap entries keep the storage path, including its directory.
        for src, storage, size in _iter_filemap_entries(path, label):
            for target, ref, folder_match in target_names(storage, flat):
                score = _candidate_score(target, ref, folder_match,
                                         size or os.path.getsize(src), by_size,
                                         from_assets)
                add(src, target, 'tagged asset', score)

    if untagged:
        # Single-language titles have no ``_en`` to derive from.  Duplicate a
        # named base file so the engine's ``<stem>_<tag><ext>`` lookup works.
        def seed_untagged(src, rel):
            base = os.path.basename(rel)
            if not any(fnmatch.fnmatch(base.lower(), p.lower())
                       for p in untagged):
                return
            stem, ext = os.path.splitext(base)
            # Do not duplicate a base file when a reference-tagged sibling
            # already exists; that one is the better source.
            if any(os.path.isfile(os.path.join(os.path.dirname(src),
                                               stem + '_' + ref + ext))
                   for ref in REF_TAGS):
                return
            add(src, stem + '_' + TAG + ext, 'untagged base', 10)

        for label, path, kind in sources:
            if kind == 'dir':
                for src, rel in _iter_asset_dir(path):
                    seed_untagged(src, rel)
            elif kind == 'filemap':
                for src, storage, _size in _iter_filemap_entries(path, label):
                    seed_untagged(src, storage)

    if text_dirs:
        # The story pictures are single-language: the engine asks for the
        # ``<stem>_<tag>`` copy, so it is made from the game's own file.
        def seed_text_dir(src, rel):
            text = (rel or '').replace('\\', '/')
            for root in text_dirs:
                root = root.strip('/')
                if not root or not (text == root or text.startswith(root + '/')):
                    continue
                folder = os.path.dirname(text)
                stem, ext = os.path.splitext(os.path.basename(text))
                target = os.path.join(folder, stem + '_' + TAG + ext) \
                    if folder else stem + '_' + TAG + ext
                add(src, target.replace('\\', '/'), 'story picture', 10)
                return

        for label, path, kind in sources:
            if kind == 'dir':
                for src, rel in _iter_asset_dir(path):
                    seed_text_dir(src, rel)
            elif kind == 'filemap':
                for src, storage, _size in _iter_filemap_entries(path, label):
                    seed_text_dir(src, storage)

    return [(target, candidates[target][0], candidates[target][1])
            for target in order]


def _read_ini(path):
    raw = open(path, 'rb').read()
    if raw[:2] == b'\xff\xfe':
        return raw[2:].decode('utf-16le'), 'utf-16le'
    if raw[:2] == b'\xfe\xff':
        return raw[2:].decode('utf-16be'), 'utf-16be'
    return raw.decode('utf-8', 'replace'), 'utf-8'


def _write_ini(path, text, encoding):
    data = text.encode(encoding)
    if encoding.startswith('utf-16'):
        data = (b'\xff\xfe' if encoding == 'utf-16le' else b'\xfe\xff') + data
    with open(path, 'wb') as f:
        f.write(data)


def _ini_key(line):
    return line.split('\t', 1)[0].split('=', 1)[0].strip()


def _insert_menu_item(lines, base, key, value):
    """Insert ``key<TAB>value`` after the last ``base*`` entry."""
    if any(_ini_key(line) == key for line in lines):
        return False
    anchor = None
    for index, line in enumerate(lines):
        k = _ini_key(line)
        if k == base or k.startswith(base + '_'):
            anchor = index
    if anchor is None:
        return False
    match = re.match(r'^(\S+)(\s+)(.*?)(\r?\n)?$', lines[anchor])
    if not match:
        return False
    sep = match.group(2)
    newline = match.group(4) or '\n'
    lines.insert(anchor + 1, key + sep + value + newline)
    return True


def update_syslangtext(out, dry_run=False):
    """Add ``*MenuItem_<tag>`` entries so the new language appears in the menu.

    ``syslangtext_<ui>.ini`` is the database of menu captions for one UI
    language.  A reference file lists the languages it knows; without a
    ``textLanguageMenuItem_<tag>`` entry the new language is missing from that
    menu even though the engine supports it.
    """
    added = 0
    checked = 0
    label = LABEL if LABEL.startswith('&') else '&' + LABEL
    plain = label.lstrip('&')
    for base, _dirs, files in os.walk(out):
        for name in sorted(files):
            if not (name.lower().startswith('syslangtext')
                    and name.lower().endswith('.ini')):
                continue
            path = os.path.join(base, name)
            text, encoding = _read_ini(path)
            lines = text.splitlines(True)
            if not any(_ini_key(line) == 'textLanguageMenu' for line in lines):
                continue
            checked += 1
            changed = []
            for stem, value in (('textLanguageMenuItem', label),
                                ('subLanguageMenuItem', plain),
                                ('uiLanguageMenuItem', plain)):
                key = stem + '_' + TAG
                if _insert_menu_item(lines, stem, key, value):
                    changed.append(key)
            if not changed:
                continue
            print('%s %-40s %s' % ('DRY ' if dry_run else 'MENU', name,
                                   ', '.join(changed)))
            added += len(changed)
            if not dry_run:
                _write_ini(path, ''.join(lines), encoding)
    if checked and not added:
        print('syslangtext: %d file(s) already list %s' % (checked, TAG))
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--filemap', default=None,
                    help='hash index (default: config paths.filemap)')
    ap.add_argument('--extracted', default=None,
                    help='extracted tree (default: config extract_dir)')
    ap.add_argument('--out', default=None,
                    help='patch assets folder (default: config paths.assets_dir)')
    ap.add_argument('--from', dest='extra', action='append', default=[],
                    help='extra folder with reference assets (repeatable)')
    ap.add_argument('--only', default=None, help='substring filter on target name')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true',
                    help='overwrite existing target files')
    args = ap.parse_args()

    out = os.path.abspath(args.out or _path('assets_dir', 'assets'))
    filemap = args.filemap or _path('filemap', 'filemap.json')
    extracted = (args.extracted or asset_io.resolve(CONFIG.get('extract_dir')))
    extensions = [e.lower() for e in
                  (ASSETS_CFG.get('seed_text_extensions') or DEFAULT_TEXT_EXT)]
    untagged = ASSETS_CFG.get('seed_untagged') or []
    # The folders that carry the game's own text: their files are
    # single-language, so a copy for the new language is made from them.
    text_dirs = list(ASSETS_CFG.get('text_dirs') or asset_io.TEXT_DIRS)
    flat = bool(ASSETS_CFG.get('seed_flat', True))

    sources = []
    # The patch folder itself is the first reference: files the author already
    # dropped there (``syslangtext_en.ini``) are the best evidence of naming.
    if os.path.isdir(out):
        sources.append(('assets', out, 'dir'))
    for folder in args.extra:
        folder = os.path.abspath(folder)
        if os.path.isdir(folder):
            sources.append((folder, folder, 'dir'))
        else:
            print('skip (not a folder): %s' % folder)
    if extracted and os.path.isdir(extracted):
        if not os.path.isfile(filemap):
            print('note: %s missing; only asset folders are scanned' % filemap)
        else:
            sources.append((extracted, filemap, 'filemap'))
    else:
        print('note: extracted tree not found (%s); only asset folders are scanned'
              % (extracted or '<unset>'))

    if not sources:
        raise SystemExit('nothing to scan: pass --extracted/--from or run langpatch extract')

    candidates = collect_candidates(sources, untagged, flat=flat,
                                    by_size=ASSETS_CFG.get('by_size'))
    if args.only:
        needle = args.only.lower()
        candidates = [c for c in candidates if needle in c[0].lower()]

    os.makedirs(out, exist_ok=True)
    seeded = present = errors = 0
    if not candidates:
        print('no new language-specific assets to copy')
    for target, src, note in candidates:
        dest = os.path.join(out, target.replace('/', os.sep))
        name = os.path.basename(target)
        if os.path.exists(dest) and not args.force:
            present += 1
            print('HAVE  %-52s %s' % (target, 'keep existing'))
            continue
        if args.dry_run:
            print('DRY   %-52s <- %s (%s)' % (target, src, note))
            seeded += 1
            continue
        try:
            os.makedirs(os.path.dirname(dest) or out, exist_ok=True)
            shutil.copyfile(src, dest)
            kind = 'text, still in the reference language' \
                if _is_text(name, extensions) else 'binary copy'
            print('SEED  %-52s %s' % (target, kind))
            seeded += 1
        except OSError as exc:
            errors += 1
            print('ERR   %-52s %s' % (target, exc))

    print('')
    print('%s: %d seeded, %d already present, %d error(s)'
          % ('would seed' if args.dry_run else 'seeded',
             seeded, present, errors))
    update_syslangtext(out, args.dry_run)
    if seeded and not args.dry_run:
        print('next: translate the text assets under %s, then langpatch build' % out)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
