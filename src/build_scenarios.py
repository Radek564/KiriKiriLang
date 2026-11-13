"""Add the target text variant to every scenario PSB and stage the rebuilds.

The game keeps scenarios inside hash-named PSBs in its script archives.
Each PSB stores its own storage name in `root.name` (e.g.
`パー・プロローグ・01a.txt`), and the engine opens `<name>.scn`.  The patched
loader redirects that open to `<game>/unencrypted/<path-after-first-slash>`,
so the override must keep the directory the engine uses (e.g.
`unencrypted/scn/<name>.scn` for a title that stores scenarios in `scn/`).

When the extracted PSB path already ends with the storage file name, its
parent path is reused; otherwise the override falls back to the flat
`scn_overrides/<name>.scn` layout used by titles that open scenarios at the
archive root.

Variant 0 is the base-language column.  `root.languages` (when present) lists
the additional columns; titles without it rely on
`multiLangLanguageTags` in default.tjs instead.  Every dialogue entry is
copied from variant 0 so the slot is never empty; a later pass replaces the
strings.

Extraction trees may contain `*.psb.tjs` text dumps
instead of raw PSBs.  Those are parsed with `src/psb_tjs.py` and rebuilt, so a
text-only extraction is enough to prepare the overrides.

All PSB parsing and building is native (`src/psb.py`); no external tools are
required.

Usage:
    py -X utf8 src/build_scenarios.py [--only TEXT] [--limit N] [--list] [--force]
"""
import argparse
import json
import os
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import psb
import psb_tjs
import configfile
from psb_lang_slot import add_lang_slot

CONFIG = configfile.load()
TAG = CONFIG['language']['tag']
INDEX = CONFIG['language']['index']


def _path(key, default):
    raw = (CONFIG.get('paths') or {}).get(key) or default
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def _config_path(key, default):
    raw = CONFIG.get(key) or default
    if raw is None:
        return None
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


OUT = _path('scn_dir', 'scn_overrides')
# ``scn_source`` lets a title keep its original extraction for assets while
# scenarios come from a smaller recovered tree.  It falls back to extract_dir.
EXTRACT = _config_path('scn_source', None) or _config_path('extract_dir', 'extracted')

# The main archive is read first: when two archives hold the same scenario,
# the one the extraction started with wins.  A folder that holds an overlay of
# somebody else's patch must not leak into the patch, so it is excluded even
# if it is still on disk.  Any other archive folder is scanned last so a
# stray scenario cannot hide.
EXCLUDED = ('patch_adult', 'patch_overlay')

# The archive the scenarios are read from.  The profile names it; without a
# profile the biggest archive of the extraction wins.
MAIN_ARCHIVE = CONFIG.get('main_archive') or ''

# The variant the new language starts from.  A title that ships an english
# text is translated from english: the new column then holds the english
# sentence, not the original one.
VARIANTS = list((CONFIG.get('language') or {}).get('variants') or [])
SOURCE_INDEX = VARIANTS.index('en') if 'en' in VARIANTS else 0


_MANIFESTS = None


def _manifests():
    """Read the extraction manifests.

    Every archive that was extracted has a ``manifest.json`` at its root.  It
    names the archive file the entries came from, so the workbook can point at
    the real ``*.xp3`` instead of the folder the extraction happens to use.

    Returns ``(folders, paths)``: the archive of every extraction folder and
    the archive of every storage path.
    """
    global _MANIFESTS
    if _MANIFESTS is not None:
        return _MANIFESTS
    folders = {}
    paths = {}
    if os.path.isdir(EXTRACT):
        manifests = []
        for name in sorted(os.listdir(EXTRACT)):
            folder = os.path.join(EXTRACT, name)
            manifest = os.path.join(folder, 'manifest.json')
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, encoding='utf-8') as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            archive = os.path.basename(data.get('archive') or '')
            if not archive:
                continue
            folders[name] = archive
            manifests.append((archive, data))
        # The main archive is read first: two archives may hold the same
        # storage path, and the one the extraction started with is the one
        # the file in the tree came from.
        manifests.sort(key=lambda item: (item[0] != MAIN_ARCHIVE, item[0]))
        for archive, data in manifests:
            for row in data.get('entries') or []:
                path = (row.get('path') or '').replace('\\', '/')
                if path:
                    paths.setdefault(path, archive)
    _MANIFESTS = (folders, paths)
    return _MANIFESTS


def archive_of(arch, src):
    """The archive file a scenario was read from (``adult.xp3`` and friends)."""
    folders, paths = _manifests()
    archive = folders.get(arch)
    if archive:
        return archive
    # A named entry is written to the root of the extraction, so the storage
    # path is what identifies it.
    try:
        rel = os.path.relpath(src, EXTRACT).replace('\\', '/')
    except ValueError:
        rel = ''
    return paths.get(rel, '') or arch


def archive_order():
    if not os.path.isdir(EXTRACT):
        return []
    names = [n for n in os.listdir(EXTRACT)
             if os.path.isdir(os.path.join(EXTRACT, n))
             and n not in EXCLUDED]
    head = []
    if MAIN_ARCHIVE:
        stem = os.path.splitext(MAIN_ARCHIVE)[0]
        if stem in names:
            head.append(stem)
    return head + sorted(n for n in names if n not in head)


def is_scenario_name(name):
    """True for both `foo.txt` and `foo.txt.scn` storage names."""
    if not name:
        return False
    low = name.lower()
    return low.endswith('.txt') or low.endswith('.txt.scn')


def scn_name(name):
    """File name the engine opens for this storage name."""
    return name if name.lower().endswith('.scn') else name + '.scn'


def override_rel(arch, src, name):
    """Path of the override relative to ``scn_dir``.

    The loader strips everything up to the first ``/`` of an opened storage
    name, so ``./scn/foo.txt.scn`` becomes ``unencrypted/scn/foo.txt.scn``.

    Two extraction layouts exist:

    * ``<extract>/<archive>/<storage path>`` — every archive written by
      ``extract_xp3.py`` has a ``manifest.json`` at its root.  The archive name
      is not part of the storage path, so it is stripped.
    * ``<extract>/<storage path>`` — a tree that was flattened to the storage
      root.  The path relative to the extraction root is already what the
      engine opens.

    A hash-named entry has no recoverable storage path, so it falls back to the
    flat ``<name>.scn`` layout used by titles that open scenarios at the root.
    """
    target = scn_name(name)
    arch_root = os.path.join(EXTRACT, arch)
    if os.path.isfile(os.path.join(arch_root, 'manifest.json')):
        root = arch_root
    else:
        root = EXTRACT
    rel = os.path.relpath(src, root)
    if rel.startswith('..' + os.sep):
        return target
    if src.lower().endswith('.psb.tjs'):
        # A text dump keeps the storage folder but ends in
        # ``.psb.tjs`` instead of ``.scn``.  The dump's directory is the path
        # the engine opens, so replace only the file name.
        parent = os.path.dirname(rel)
        return os.path.join(parent, target) if parent else target
    if os.path.basename(rel).lower() == target.lower():
        return rel
    return target


PROVENANCE = '.provenance.json'


def provenance_path(scn_dir=None):
    return os.path.join(scn_dir or OUT, PROVENANCE)


def load_provenance(scn_dir=None):
    """Map ``override rel path -> archive name`` written by :func:`main`.

    A scenario can be recovered from a secondary archive, and the workbook
    has to show where each line came from.  The
    archive folder name is the only reliable source of that information, so it
    is recorded next to the rebuilt scenarios.
    """
    path = provenance_path(scn_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {str(k).replace('\\', '/'): str(v) for k, v in data.items()}


def save_provenance(rows, scn_dir=None):
    path = provenance_path(scn_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def iter_psb_files():
    """Yield every scenario PSB (or PSB text dump) in the extraction tree.

    Extraction writes resolved names when they are known and hash paths
    otherwise, so the walk cannot assume the ``<dirhash>/<filehash>`` shape.
    An extraction may also leave ``*.psb.tjs`` dumps instead of raw bytes; those
    are scenario candidates when their name ends in ``.txt.psb.tjs``.
    """
    for arch in archive_order():
        base = os.path.join(EXTRACT, arch)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for f in sorted(files):
                p = os.path.join(root, f)
                lower = f.lower()
                if lower.endswith('.txt.psb.tjs'):
                    yield arch, p
                    continue
                try:
                    if os.path.getsize(p) <= 8:
                        continue
                    with open(p, 'rb') as fh:
                        magic = fh.read(4)
                except OSError:
                    continue
                # A title may keep its scenarios encrypted (every byte
                # flipped with 0x80); those are scenarios as well.
                if magic == b'PSB\x00' or magic == _xored(b'PSB\x00'):
                    yield arch, p


def is_dump(src):
    return src.lower().endswith('.psb.tjs')


# A title may keep its scenarios encrypted: every byte is flipped with 0x80.
# The engine decrypts them when it opens them, so a rebuild has to be
# encrypted the same way.
PSB_XOR = 0x80


def _xored(data):
    """The bytes with every byte flipped with 0x80."""
    return bytes(byte ^ PSB_XOR for byte in data)


def _scenario_bytes(src):
    """The bytes of a scenario and whether the title stores them encrypted."""
    with open(src, 'rb') as handle:
        raw = handle.read()
    if raw[:4].upper() == b'PSB\x00':
        return raw, False
    plain = _xored(raw)
    if plain[:4].upper() == b'PSB\x00':
        return plain, True
    return raw, False


def _parse_scenario(src):
    """``(json, parsed, encrypted)`` for one scenario file."""
    raw, encrypted = _scenario_bytes(src)
    parsed = psb.load(raw)
    return parsed.to_json(), parsed, encrypted


def _encrypt_scenario(blob, encrypted):
    if not encrypted:
        return blob
    return _xored(blob)


def load_json(src):
    """Parse *src* and return its JSON dict.

    Text dumps are expensive to parse, and the main loop inspects every file
    before rebuilding the chosen ones.  A JSON cache keyed by mtime keeps the
    second pass cheap; raw PSBs already parse quickly.
    """
    if not is_dump(src):
        return psb.load_file(src).to_json()
    cache_dir = os.path.join(_path('cache_dir', 'cache/psb'), 'dumps')
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, os.path.basename(src) + '.json')
    if (os.path.isfile(cache)
            and os.path.getmtime(cache) >= os.path.getmtime(src)):
        try:
            with open(cache, encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    data = psb_tjs.load_file(src)
    tmp = cache + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, cache)
    return data


def inspect(src):
    """Return (storage name, already-has-slot?) without modifying anything."""
    if is_dump(src):
        data = load_json(src)
    else:
        data = _parse_scenario(src)[0]
    return data.get('name'), TAG in (data.get('languages') or [])


def process(src, arch, force=False):
    try:
        encrypted = False
        if is_dump(src):
            data = load_json(src)
            version = int((CONFIG.get('build') or {}).get('psb_dump_version') or 3)
            resources = extra_resources = None
        else:
            data, parsed, encrypted = _parse_scenario(src)
            version = parsed.version
            resources = parsed.resources
            extra_resources = parsed.extra_resources
        name = data.get('name')
        if not is_scenario_name(name):
            return ('skip', str(name), 0, 'not a scenario')
        if TAG in (data.get('languages') or []) and not force:
            return ('skip', name, 0, 'already has slot')

        old = list(data.get('languages') or [])
        if TAG not in old:
            n = add_lang_slot(data, TAG, INDEX, source_index=SOURCE_INDEX)
        else:
            n = 0

        blob = psb.build(data, version,
                         resources=resources,
                         extra_resources=extra_resources)
        # The title stores its scenarios encrypted; the engine expects the
        # same form back.
        blob = _encrypt_scenario(blob, encrypted)

        rel = override_rel(arch, src, name)
        dst = os.path.join(OUT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.tmp'
        with open(tmp, 'wb') as fh:
            fh.write(blob)
        os.replace(tmp, dst)

        # Drop a flat copy left by an older build so the patch cannot carry two
        # different versions of the same scenario.
        flat = os.path.join(OUT, scn_name(name))
        if os.path.normcase(os.path.abspath(flat)) != os.path.normcase(os.path.abspath(dst)):
            try:
                os.remove(flat)
            except OSError:
                pass
        return ('ok', rel, len(blob), '%d entries' % n)
    except Exception as exc:
        return ('error', src, 0, '%s: %s' % (type(exc).__name__, exc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default=None)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--force', action='store_true',
                    help='rebuild even when the slot already exists')
    args = ap.parse_args()

    files = list(iter_psb_files())
    print('extract dir: %s' % EXTRACT)
    print('archives scanned: %s' % ', '.join(archive_order()))
    print('PSB candidates: %d' % len(files))

    chosen = {}
    for i, (arch, src) in enumerate(files, 1):
        try:
            name, has_slot = inspect(src)
        except Exception as exc:
            print('[%d/%d] %-48s ERROR %s' % (i, len(files), os.path.basename(src), exc))
            continue
        if not is_scenario_name(name):
            continue
        if (args.force or not has_slot) and name not in chosen:
            chosen[name] = (arch, src)
        print('[%d/%d] %-48s %s' % (i, len(files), name, arch))

    if args.list:
        print(json.dumps(sorted(chosen), ensure_ascii=False, indent=1))
        return 0

    items = [(n, a, s) for n, (a, s) in sorted(chosen.items())]
    if args.only:
        items = [(n, a, s) for n, a, s in items if args.only in n]
    if args.limit:
        items = items[:args.limit]
    print('building %d scenario overrides' % len(items))

    provenance = load_provenance()
    ok = skipped = errors = 0
    for name, arch, src in items:
        status, label, size, note = process(src, arch, force=args.force)
        if status in ('ok', 'skip'):
            # The workbook points at the archive the scenario was read from,
            # so the reader sees ``adult.xp3`` and not the folder the
            # extraction happens to use.
            provenance[override_rel(arch, src, name).replace('\\', '/')] \
                = archive_of(arch, src)
        if status == 'ok':
            ok += 1
            print('OK   %-48s %9d  %s' % (label, size, note))
        elif status == 'skip':
            skipped += 1
            print('SKIP %-48s %s' % (label, note))
        else:
            errors += 1
            print('ERR  %-48s %s' % (label, note))
    print('done: %d built, %d skipped, %d errors' % (ok, skipped, errors))
    save_provenance(provenance)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
