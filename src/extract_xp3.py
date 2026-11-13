"""List or extract KiriKiri XP3 archives without any GUI tooling.

Usage:
    py -X utf8 src/extract_xp3.py data.xp3 --list
    py -X utf8 src/extract_xp3.py data.xp3 -o extracted --verify
    py -X utf8 src/extract_xp3.py data.xp3 -o ciphertext --raw

Plain archives are extracted into ``<out>/<archive-stem>/`` with their real
paths.  Hxv4/CxdecV2 archives are decrypted with :mod:`hxv4_extract`; names are
mined from the decrypted archive bodies *before* anything is written, so a
resolved entry lands at ``<out>/<archive>/<dir>/<name>`` and only an unnamed
one keeps the ``<out>/<archive>/<dirhash>/<filehash>`` layout.  The same pass
writes ``filemap.json``, which maps every hash back to its directory/file name.
The protected-warning dummy entry (id 0) is metadata, not a resource, and is
skipped.

No prior hash-named extraction is needed, and no game process, DLL injection or
dialog automation is involved.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_all
import hash_dumper
import hxv4
import hxv4_extract
import xp3
import xp3_crypt

# The stem of the engine's interface caption file.  Every KrKrz title keeps
# its captions in ``syslangtext_<tag>.ini``; the name is a convention, so no
# text in the game mentions it and the name miner cannot learn it.
CAPTION_STEM = 'syslangtext'


def safe_parts(name):
    text = (name or '').replace('\\', '/').strip('/')
    parts = []
    for part in text.split('/'):
        if not part or part in ('.', '..'):
            continue
        for ch in ':*?"<>|':
            part = part.replace(ch, '_')
        parts.append(part)
    return parts


def unique_path(root, parts):
    path = os.path.join(root, *parts)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = '%s_%d%s' % (stem, n, ext)
        if not os.path.exists(candidate):
            return candidate
        n += 1


def describe(arch):
    print('%s' % arch.path)
    print('  entries : %d' % len(arch.entries))
    if arch.crypt is not None:
        print('  scheme  : %s' % type(arch.crypt).__name__)
    if arch.hxv4:
        print('  Hxv4    : offset=0x%x size=%d kind=%d'
              % (arch.hxv4['offset'], arch.hxv4['size'], arch.hxv4['kind']))
    else:
        named = sum(1 for e in arch.entries if e.name)
        print('  names   : %d real' % named)


def entry_label(entry):
    if entry.fake_name is not None:
        entry_id = hxv4.fake_id(entry.fake_name)
        if entry_id is not None:
            return 'id=%-6d %s' % (entry_id, entry.fake_name)
    return entry.name or '<unnamed>'


def list_entries(arch, limit=0):
    describe(arch)
    shown = arch.entries if not limit else arch.entries[:limit]
    for entry in shown:
        seg = entry.segments[0] if entry.segments else (0, 0, 0, 0)
        print('  %5d  %-42s off=0x%x orig=%-9d stored=%-9d adlr=%s'
              % (entry.index, entry_label(entry), seg[1],
                 entry.original_size, entry.stored_size,
                 '%08x' % entry.adler if entry.adler is not None else '-'))
    if limit and len(arch.entries) > limit:
        print('  ... %d more' % (len(arch.entries) - limit))


def _same_content(path, data):
    """True when the file at *path* holds exactly *data*."""
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(path, 'rb') as handle:
            return handle.read() == data
    except OSError:
        return False


def _drop_duplicates(out_root, parts, data, remove_all=False):
    """Remove the copies an earlier pass left next to a written file.

    The same storage path can be written twice; the copy was named
    ``<name>_1``, ``<name>_2`` and so on.  They are the leftovers of an
    earlier extraction, so they go when the file itself is written again.
    """
    folder = os.path.join(out_root, *parts[:-1])
    stem, ext = os.path.splitext(parts[-1])
    index = 1
    while True:
        candidate = os.path.join(folder, '%s_%d%s' % (stem, index, ext))
        if not os.path.isfile(candidate):
            break
        if remove_all or _same_content(candidate, data):
            try:
                os.remove(candidate)
            except OSError:
                pass
        index += 1


def _write(out_root, parts, data, overwrite=False):
    path = os.path.join(out_root, *parts)
    if not overwrite:
        path = unique_path(out_root, parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def _hash_parts(archive_stem, record):
    if record is None:
        return None
    return [archive_stem, record.path_hash.hex().upper(),
            record.name_hash.hex().upper()]


def _parts_for(entry, record, stem, file_names, dir_names):
    """Pick the on-disk path for one entry.

    A resolved directory + file name is preferred, so extraction produces a
    readable tree straight away.  Hashes are only used when the name is still
    unknown, which keeps the layout stable for anything left to mine later.
    """
    if record is not None:
        dirhash = record.path_hash.hex().upper()
        filehash = record.name_hash.hex().upper()
        name = (file_names or {}).get(filehash)
        if name:
            parts = safe_parts((dir_names or {}).get(dirhash) or '')
            parts += safe_parts(name)
            if parts:
                return parts, dirhash, filehash
        parts = _hash_parts(stem, record)
        if parts:
            return parts, dirhash, filehash
    parts = safe_parts(entry.name)
    if parts:
        return parts, None, None
    return ['entry_%04d.bin' % entry.index], None, None


def extract(arch, out_root, raw=False, verify=False, exe=None, driver=None,
            file_names=None, dir_names=None, only=None):
    """Write one archive.

    ``only`` is a list of case-insensitive substrings; when given, entries whose
    storage name matches none of them are skipped.  This makes a pilot run on a
    multi-gigabyte archive cheap: extract ``scn/`` and the scripts, ignore the
    media.
    """
    stem = os.path.splitext(os.path.basename(arch.path))[0]
    out_dir = os.path.join(out_root, stem)
    os.makedirs(out_dir, exist_ok=True)

    crypt = None if raw else arch.crypt
    if driver is None and arch.crypt is None and arch.hxv4 is not None and not raw:
        driver = hxv4_extract.open_archive(arch.path, exe=exe)
        try:
            driver.prepare()
        except Exception as exc:
            print('  ERROR cannot decrypt %s: %s' % (arch.path, exc))
            return 1, []

    if crypt is not None and verify:
        print('  NOTE adler fields are cipher key hashes for this scheme; '
              'skipping --verify')

    manifest = []
    filemap_rows = []
    written = 0
    failed = 0
    skipped = 0
    filtered = 0

    for entry in arch.entries:
        entry_id = hxv4.fake_id(entry.fake_name) if entry.fake_name else None
        if only:
            haystack = (entry.name or entry.fake_name or '').lower()
            if not any(needle.lower() in haystack for needle in only):
                # An Hxv4 archive hides its names behind a hash, so the real
                # name is only known after the body is decrypted and resolved
                # below.  Keep the entry here and filter it post-decrypt.
                if driver is None or not file_names:
                    filtered += 1
                    continue
        if (entry.name or '').startswith('$$$'):
            skipped += 1
            manifest.append({
                'index': entry.index,
                'id': entry_id,
                'name': entry.name,
                'skipped': 'protected warning metadata',
            })
            continue
        if driver is not None and entry_id == 0:
            skipped += 1
            manifest.append({
                'index': entry.index,
                'id': 0,
                'name': entry.fake_name,
                'skipped': 'protected warning metadata',
            })
            continue

        try:
            record = None
            if crypt is not None:
                data = xp3.extract_plain(arch.path, entry, crypt)
            else:
                data = xp3.extract_entry(arch.path, entry)
                record = driver.record_of(entry, entry_id) if driver else None
                if driver is not None:
                    data = driver.decrypt(entry, data)
        except Exception as exc:  # keep going; report per entry
            print('  ERROR %-52s %s' % (entry_label(entry), exc))
            failed += 1
            manifest.append({'index': entry.index, 'id': entry_id,
                             'name': entry.fake_name or entry.name,
                             'error': str(exc)})
            continue

        if only and record is not None:
            filehash = record.name_hash.hex().upper()
            dirhash = record.path_hash.hex().upper()
            resolved = '/'.join(
                part for part in ((dir_names or {}).get(dirhash),
                                  (file_names or {}).get(filehash))
                if part) or filehash
            if not any(needle.lower() in resolved.lower() for needle in only):
                filtered += 1
                continue

        good = None
        if verify and crypt is None and entry.adler:
            good = (xp3.adler32(data) == entry.adler)
            if not good:
                print('  ADLER MISMATCH %-40s' % entry_label(entry))
                failed += 1

        parts, dirhash, filehash = _parts_for(entry, record, stem,
                                              file_names, dir_names)

        # A record-derived path is deterministic, so a re-run overwrites it
        # instead of piling up ``_1`` copies.  The stale hash-named file from an
        # older extraction is removed once its named replacement exists.
        deterministic = record is not None
        if not deterministic:
            # A path that comes from the storage name is the file's own
            # path: the archive is the truth, so a re-run overwrites it
            # instead of piling up ``_1`` copies.  Two archives may hold
            # the same path; identical content is the same file, so it is
            # written over as well.
            target = os.path.join(out_root, *parts)
            if entry.name:
                deterministic = True
            elif os.path.isfile(target) and _same_content(target, data):
                deterministic = True
        if deterministic and record is None:
            # The copies of an earlier pass are leftovers of the same
            # storage path: they go when the file is written again.
            _drop_duplicates(out_root, parts, data, remove_all=bool(entry.name))
        path = _write(out_root, parts, data, overwrite=deterministic)
        if deterministic and record is not None:
            stale = os.path.join(out_root, *_hash_parts(stem, record))
            if (os.path.abspath(stale) != os.path.abspath(path)
                    and os.path.isfile(stale)):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        written += 1
        row = {
            'index': entry.index,
            'id': entry_id,
            'path': os.path.relpath(path, out_root).replace('\\', '/'),
            'size': len(data),
            'adler_ok': good,
            'encrypted': arch.encrypted,
        }
        if dirhash is not None and filehash is not None:
            row['dirhash'] = dirhash
            row['filehash'] = filehash
            # Names are mined from the archives before this pass, so the
            # manifest is readable without a separate post-processing step.
            row['dir'] = (dir_names or {}).get(dirhash)
            row['name'] = (file_names or {}).get(filehash)
        else:
            row['name'] = entry.name
        manifest.append(row)
        filemap_rows.append({
            'archive': stem,
            'dirhash': row.get('dirhash'),
            'filehash': row.get('filehash'),
            'dir': row.get('dir'),
            'name': row.get('name'),
            'size': len(data),
            'path': row['path'],
        })

    # Drop hash-named folders that became empty after their files moved to
    # readable paths.
    for base, _dirs, _files in os.walk(out_dir, topdown=False):
        if base != out_dir and not os.listdir(base):
            try:
                os.rmdir(base)
            except OSError:
                pass

    manifest_path = os.path.join(out_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({
            'archive': os.path.abspath(arch.path),
            'hxv4': arch.hxv4,
            'encrypted': arch.encrypted,
            'written': written,
            'failed': failed,
            'skipped': skipped,
            'filtered': filtered,
            'entries': manifest,
        }, f, ensure_ascii=False, indent=1)

    print('  wrote %d file(s), %d skipped, %d filtered, %d error(s) -> %s'
          % (written, skipped, filtered, failed, out_dir))
    return (0 if failed == 0 else 1), filemap_rows


def load_filemap(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def refresh_filemap(out_root, filemap_path, mine_names=True, exe=None,
                    file_names=None, dir_names=None):
    if file_names is None or dir_names is None:
        file_names = {}
        dir_names = {}
        if mine_names:
            seed = hash_dumper.seed_from_exe(exe) if exe else None
            file_names, dir_names = hash_dumper.mine(
                out_root, scan_roots=[out_root], progress=True,
                **({'seed': seed} if seed else {}))
    rows = extract_all.build_rows(out_root,
                                  file_names=file_names,
                                  dir_names=dir_names)
    # Extraction writes an entry under its real name when it is known, so the
    # hash-only view of the tree is incomplete.  Merge both layouts; the named
    # rows are what script / scenario / interface discovery looks for.
    seen = {row['path'] for row in rows}
    rows.extend(row for row in extract_all.named_rows(out_root)
                if row['path'] not in seen)
    rows.sort(key=lambda row: row['path'])
    extract_all.write_rows(rows, filemap_path)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('archives', nargs='+', help='one or more .xp3 files')
    ap.add_argument('-o', '--out', default=None,
                    help='extract under this folder; omit to list only')
    ap.add_argument('--list', action='store_true', help='show entries')
    ap.add_argument('--limit', type=int, default=0,
                    help='limit --list output')
    ap.add_argument('--raw', action='store_true',
                    help='dump stored bytes even for Hxv4 archives')
    ap.add_argument('--verify', action='store_true',
                    help='check adler32 for every extracted body')
    ap.add_argument('--exe', default=None,
                    help='game executable used for Hxv4 key recovery')
    ap.add_argument('--scheme', default=None,
                    help='archive filter: exact title or unique fragment')
    ap.add_argument('--auto-scheme', action='store_true',
                    help='detect the XP3 crypt scheme by scoring decrypted samples')
    ap.add_argument('--list-schemes', action='store_true',
                    help='print every known XP3 scheme title and exit')
    ap.add_argument('--filemap', default=None,
                    help='merged filemap.json to refresh (default: <out>/filemap.json)')
    ap.add_argument('--no-filemap', action='store_true',
                    help='do not refresh filemap.json')
    ap.add_argument('--no-mine', action='store_true',
                    help='write rows without hash-name discovery (fast; mine later)')
    ap.add_argument('--only', action='append', default=None, metavar='TEXT',
                    help='extract only entries whose name contains TEXT '
                         '(repeatable, case-insensitive)')
    args = ap.parse_args()

    if args.list_schemes:
        for title in xp3_crypt.scheme_names():
            print(title)
        return 0

    if args.exe is None and args.archives:
        # Hxv4 key material lives in the game executable.  When the caller did
        # not name one, pick it from the folder that holds the archives.
        import gamecfg
        args.exe = gamecfg.detect_exe(
            os.path.dirname(os.path.abspath(args.archives[0])))
        if args.exe:
            print('executable: %s' % args.exe)

    rc = 0
    extracted = bool(args.out)
    scheme_used = False
    plans = []                 # (archive, driver)
    for path in args.archives:
        if not os.path.isfile(path):
            print('missing: %s' % path)
            rc = 2
            continue

        crypt = None
        if args.scheme:
            try:
                crypt = xp3_crypt.resolve_scheme(args.scheme)
            except KeyError as exc:
                print('%s: %s' % (path, exc))
                rc = 2
                continue
        elif args.auto_scheme:
            try:
                title = xp3_crypt.detect_scheme(path)
            except Exception as exc:
                print('%s: scheme detection failed: %s' % (path, exc))
                title = None
            if title is None:
                print('%s: no XP3 crypt scheme detected' % path)
            else:
                print('%s: detected scheme %r' % (path, title))
                crypt = xp3_crypt.load_scheme(title)
        if crypt is not None:
            scheme_used = True

        try:
            arch = xp3.parse_archive(path, crypt)
        except xp3.Xp3Error as exc:
            print('%s: %s' % (path, exc))
            rc = 2
            continue

        driver = None
        if extracted and not args.raw and crypt is None and arch.hxv4 is not None:
            driver = hxv4_extract.open_archive(path, exe=args.exe)
            try:
                driver.prepare()
            except Exception as exc:
                print('%s: cannot decrypt: %s' % (path, exc))
                rc = max(rc, 1)
                continue

        if args.list or not args.out:
            list_entries(arch, args.limit)
        if args.out:
            plans.append((arch, driver))

    # Mine names from the decrypted archive bodies *before* writing anything.
    # This makes extraction self-contained: no prior hash-named tree is needed.
    file_names = {}
    dir_names = {}
    mined = False
    if plans and not args.no_mine and not args.raw and not scheme_used:
        try:
            seed = hash_dumper.seed_from_exe(args.exe) if args.exe else hxv4.HXV4_SALT
            extensions, lang_suffixes, extra_suffixes = hash_dumper.naming_from_config()
            miner = hash_dumper.Miner(extensions, lang_suffixes,
                                      extra_suffixes, seed=seed)
            targets, target_dirs = hash_dumper.collect_archive_targets(
                [driver for _arch, driver in plans])
            print('mining names from %d archive(s), %d hash(es)'
                  % (len(plans), len(targets)))
            for arch, driver in plans:
                if driver is None:
                    continue
                used = hash_dumper.harvest_archive(arch, driver, miner)
                print('  scanned %d entr(ies) from %s'
                      % (used, os.path.basename(arch.path)))
            file_names, dir_names = miner.resolve(targets, target_dirs,
                                                  progress=True)
            # The interface captions have a fixed name in every KrKrz title
            # (``syslangtext_<tag>.ini``) and no text mentions it, so the
            # miner never learns the name and the file stays a hash.  Offer
            # the stem directly: the hash check decides whether the title
            # really ships such a file.
            miner.offer(CAPTION_STEM)
            file_names = {key.hex().upper(): value
                          for key, value in miner.found.items()}
            # Keep names discovered in an earlier pass (the tree miner sees a
            # few text files that archive-only harvesting cannot reach).  Every
            # stored name was hash-verified when it was first accepted.
            if not args.no_filemap:
                previous = args.filemap or (os.path.join(args.out, 'filemap.json')
                                            if args.out else None)
                old_rows = load_filemap(previous)
                if old_rows:
                    kept_files = kept_dirs = 0
                    for row in old_rows:
                        h, n = row.get('filehash'), row.get('name')
                        if h and n and h not in file_names:
                            file_names[h] = n
                            kept_files += 1
                        dh, d = row.get('dirhash'), row.get('dir')
                        if dh and d and dh not in dir_names:
                            dir_names[dh] = d
                            kept_dirs += 1
                    if kept_files or kept_dirs:
                        print('  kept %d file name(s) and %d directory name(s) '
                              'from %s' % (kept_files, kept_dirs, previous))
            mined = True
        except Exception as exc:
            print('WARN name mining failed: %s' % exc)

    filemap_rows = []
    for arch, driver in plans:
        code, rows = extract(arch, args.out, raw=args.raw,
                             verify=args.verify, exe=args.exe,
                             driver=driver,
                             file_names=file_names,
                             dir_names=dir_names,
                             only=args.only)
        rc = max(rc, code)
        filemap_rows.extend(rows)

    if extracted and not args.raw and not args.no_filemap:
        filemap = args.filemap or os.path.join(args.out, 'filemap.json')
        try:
            if not mined and not scheme_used and not args.no_mine:
                refresh_filemap(args.out, filemap, mine_names=True, exe=args.exe)
            else:
                # Rebuild the whole map from the tree: named entries, hash
                # fallbacks and the names mined above.  Rebuilding (instead of
                # appending this pass) keeps rows of earlier archives, which a
                # multi-archive extraction depends on.
                old = load_filemap(filemap) or []
                merged_files = {row['filehash']: row['name'] for row in old
                                if row.get('filehash') and row.get('name')}
                merged_files.update(file_names or {})
                merged_dirs = {row['dirhash']: row['dir'] for row in old
                               if row.get('dirhash') and row.get('dir')}
                merged_dirs.update(dir_names or {})
                refresh_filemap(args.out, filemap, mine_names=False,
                                file_names=merged_files,
                                dir_names=merged_dirs)
        except OSError as exc:
            print('WARN could not write %s: %s' % (filemap, exc))
            rc = max(rc, 1)
    return rc


if __name__ == '__main__':
    sys.exit(main())
