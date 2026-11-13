"""Turn an extraction tree into ``filemap.json`` rows.

CxdecV2 archives hide their real paths, so extraction writes either the
resolved ``<archive>/<dir>/<name>`` path (when the name is known) or the
``<archive>/<dirhash>/<filehash>`` fallback.  This module owns the small amount
of layout logic; the actual hash-to-name discovery lives in
:mod:`hash_dumper`:

* a hash tree laid out as ``<archive>/<dirhash>/<filehash>``;
* a ``{filehash: name}`` / ``{dirhash: dir}`` mapping mined from the decrypted
  resources and verified against the special index.

``src/extract_xp3.py`` and ``src/hash_dumper.py`` use :func:`build_rows` to
refresh the merged map after extraction.
"""
import json
import os


def _looks_like_hash_dir(name):
    return len(name) == 16 and all(c in '0123456789ABCDEFabcdef' for c in name)


def _looks_like_hash_file(name):
    return len(name) == 64 and all(c in '0123456789ABCDEFabcdef' for c in name)


def iter_tree(extracted):
    """Yield ``(archive, dirhash, filehash, path)`` from a hash tree."""
    for archive in sorted(os.listdir(extracted)):
        adir = os.path.join(extracted, archive)
        if not os.path.isdir(adir):
            continue
        for dirhash in sorted(os.listdir(adir)):
            ddir = os.path.join(adir, dirhash)
            if not os.path.isdir(ddir) or not _looks_like_hash_dir(dirhash):
                continue
            for filehash in sorted(os.listdir(ddir)):
                path = os.path.join(ddir, filehash)
                if not os.path.isfile(path):
                    continue
                if not _looks_like_hash_file(filehash):
                    continue
                yield archive, dirhash.upper(), filehash.upper(), path


def build_rows(extracted, file_names=None, dir_names=None):
    """Return filemap rows for every file in the hash tree.

    ``file_names`` and ``dir_names`` are the verified ``hash -> name`` maps
    produced by :func:`hash_dumper.mine`; unknown hashes stay ``None``.
    """
    file_names = file_names or {}
    dir_names = dir_names or {}
    rows = []
    for archive, dirhash, filehash, path in iter_tree(extracted):
        rows.append({
            'archive': archive,
            'dirhash': dirhash,
            'filehash': filehash,
            'dir': dir_names.get(dirhash),
            'name': file_names.get(filehash),
            'size': os.path.getsize(path),
            'path': os.path.relpath(path, extracted).replace('\\', '/'),
        })
    return rows


def named_rows(extracted):
    """Rows for the entries written under their real storage name.

    :func:`iter_tree` only sees the hash fallback layout.  An archive whose
    names are readable is written as ``<dir>/<name>`` instead, and whomever
    reads the filemap afterwards (scripts, scenarios, interface discovery)
    needs those rows too - otherwise a perfectly extracted title looks empty.
    """
    rows = []
    if not extracted or not os.path.isdir(extracted):
        return rows
    for root, _dirs, files in os.walk(extracted):
        rel_root = os.path.relpath(root, extracted).replace(os.sep, '/')
        parts = [] if rel_root == '.' else rel_root.split('/')
        # ``<archive>/<dirhash>/<filehash>`` belongs to :func:`iter_tree`.
        if len(parts) == 2 and _looks_like_hash_dir(parts[1]) \
                and all(_looks_like_hash_file(name) for name in files):
            continue
        for name in sorted(files):
            path = name if rel_root == '.' else rel_root + '/' + name
            rows.append({
                'archive': None,
                'dir': '' if rel_root == '.' else rel_root + '/',
                'name': name,
                'size': os.path.getsize(os.path.join(root, name)),
                'path': path,
            })
    return rows


def write_rows(rows, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(rows, f, ensure_ascii=False, indent=0)
    named = sum(1 for row in rows if row.get('name'))
    print('wrote %s: %d files, %d named' % (out, len(rows), named))
