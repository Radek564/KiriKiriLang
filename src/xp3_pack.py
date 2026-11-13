"""Native XP3 writer.

The patch is a plain (unencrypted) XP3 archive.  This module writes the same
layout as the old ``Xp3Pack.exe``:

* 11-byte magic + ``u64`` index offset;
* one ``File`` chunk per file, body zlib-compressed (or stored when that is
  smaller), referenced by a single ``segm`` record;
* the index chunk is stored raw at the end of the file.

Nested folders become ``Directory`` chunks, so the archive keeps real paths.
"""
import os
import struct
import zlib

MAGIC = b'XP3\r\n \n\x1a\x8bg\x01'
TAG_FILE = b'File'
TAG_DIRECTORY = b'Directory'
TAG_INFO = b'info'
TAG_SEGM = b'segm'
TAG_ADLR = b'adlr'


class Xp3PackError(Exception):
    pass


def _chunk(tag, body):
    return tag + struct.pack('<Q', len(body)) + body


def _file_chunk(name, original_size, stored_size, method, offset, adler=0,
                encrypted=False):
    raw_name = name.encode('utf-16le')
    flags = 0x80000000 if encrypted else 0
    info = struct.pack('<IQQH', flags, original_size, stored_size,
                       len(raw_name) // 2) + raw_name
    segm = struct.pack('<IQQQ', method, offset, original_size, stored_size)
    adlr = struct.pack('<I', adler & 0xFFFFFFFF)
    return _chunk(TAG_FILE, _chunk(TAG_INFO, info) + _chunk(TAG_SEGM, segm)
                  + _chunk(TAG_ADLR, adlr))


def _directory_chunk(name, children):
    raw_name = name.encode('utf-16le')
    body = struct.pack('<H', len(raw_name) // 2) + raw_name
    return _chunk(TAG_DIRECTORY, body + b''.join(children))


def _walk(folder):
    items = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, folder).replace('\\', '/')
            parts = tuple(part for part in rel.split('/') if part)
            if parts:
                items.append((parts, full))
    # Sorted by the full path: the engine binary-searches the archive names.
    items.sort(key=lambda item: '/'.join(item[0]))
    return items


class PackEntry(object):
    """Minimal entry view handed to a crypt filter while packing."""

    __slots__ = ('name', 'original_size', 'stored_size', 'adler', 'type',
                 'extra')

    def __init__(self, name):
        self.name = name
        self.original_size = 0
        self.stored_size = 0
        self.adler = 0
        self.type = None
        self.extra = None


def pack_folder(folder, out_path, compress=True, crypt=None,
                write_adler=None):
    """Pack *folder* into *out_path*.  Returns the archive size.

    When *crypt* is given, every file body is first encrypted with the scheme
    and then compressed.  ``adlr`` holds the adler32 of the
    plaintext body, exactly like a game-made Cx archive; ``info.flags`` is
    marked encrypted.

    For plain archives ``adlr`` is left at zero unless *write_adler* is true.
    The engine ignores it for unencrypted entries, so the default keeps the
    long-verified patch layout byte-identical.

    The index is flat: every file is one ``File`` chunk whose name is the
    full path inside the archive (``ui/title_xx.pimg``).  That is the layout
    the engine's own archives use, and it is what makes a *folder* inside the
    archive enumerable -- the auto-path table is built from the archive names
    that start with the folder prefix, so a new file only becomes visible
    when it lives in a folder and that folder is registered as an auto path.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise Xp3PackError('not a folder: %s' % folder)
    items = _walk(folder)
    if not items:
        raise Xp3PackError('folder is empty: %s' % folder)
    if write_adler is None:
        write_adler = crypt is not None

    out = bytearray(MAGIC)
    out += b'\x00' * 8  # index offset placeholder
    file_chunks = {}
    for parts, full in items:
        with open(full, 'rb') as f:
            blob = f.read()
        rel_name = '/'.join(parts)
        entry = PackEntry(rel_name)
        entry.original_size = len(blob)
        entry.adler = zlib.adler32(blob) & 0xFFFFFFFF

        encrypted = crypt is not None
        if encrypted and getattr(crypt, 'startup_tjs_not_encrypted', False) \
                and rel_name == 'startup.tjs':
            encrypted = False
        payload = blob
        if encrypted:
            payload = crypt.encrypt_all(entry, payload)

        stored = zlib.compress(payload, 9) if compress else payload
        method = 0
        if compress and len(stored) < len(payload):
            method = 1
        else:
            stored = payload
        offset = len(out)
        out += stored
        entry.stored_size = len(stored)
        file_chunks[parts] = _file_chunk(rel_name, len(blob), len(stored),
                                         method, offset,
                                         entry.adler if write_adler else 0,
                                         encrypted=encrypted)

    tree = {}
    for parts, chunk in file_chunks.items():
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault('_files', []).append(chunk)

    def build_directory(node):
        children = list(node.get('_files', []))
        for name in sorted(key for key in node if key != '_files'):
            children.extend(build_directory(node[name]))
        return children

    # The engine's auto-path table is built by binary-searching the archive
    # names for a folder prefix, so the index has to be sorted by the *full*
    # path.  Grouping the chunks per directory would put the root files
    # first and break that search.
    ordered = sorted(file_chunks.items(), key=lambda item: '/'.join(item[0]))
    index = b''.join(chunk for _parts, chunk in ordered)
    index_offset = len(out)
    out += b'\x00' + struct.pack('<Q', len(index)) + index
    struct.pack_into('<Q', out, 11, index_offset)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'wb') as f:
        f.write(out)
    return len(out)


def pack_file(path, out_path, compress=True, crypt=None):
    return pack_folder(os.path.dirname(os.path.abspath(path)), out_path,
                       compress=compress, crypt=crypt)
