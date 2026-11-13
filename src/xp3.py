"""Minimal, headless XP3 reader.

This module is deliberately independent of the game: it parses the archive
index and extracts plain entries without starting the game and without any
GUI tooling.

What it handles
---------------
* the standard XP3 header and the chained index chunks (raw or zlib);
* ``File`` / ``info`` / ``segm`` / ``adlr`` records;
* nested ``Directory`` chunks, so real paths are preserved;
* the ``Hxv4`` marker used by CxdecV2 archives.

The ``Hxv4`` index and every file body are encrypted.  Pass a filter from
:mod:`xp3_crypt` to :func:`parse_archive` and this module binds the special
index, renames the entries and decrypts bodies; without a filter, entries are
kept as opaque ``hxv4_XXXX.bin`` records for raw inspection.
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
TAG_HXV4 = b'Hxv4'
TAG_HNFN = b'hnfn'

INDEX_RAW = 0
INDEX_ZLIB = 1


class Xp3Error(Exception):
    pass


class Entry(object):
    __slots__ = ('index', 'name', 'fake_name', 'flags', 'original_size',
                 'stored_size', 'segments', 'adler', 'encrypted', 'extra')

    def __init__(self, index=0, name=None, flags=0, original_size=0,
                 stored_size=0, segments=None, adler=None, encrypted=False):
        self.index = index
        self.name = name
        self.fake_name = None
        self.flags = flags
        self.original_size = original_size
        self.stored_size = stored_size
        self.segments = segments or []
        self.adler = adler
        self.encrypted = encrypted
        # Per-entry state for Hx (or other) cipher extensions; the crypt
        # implementation fills this in when it binds its special index.
        self.extra = None

    def __repr__(self):
        return ('Entry(index=%d, name=%r, flags=0x%x, original_size=%d, '
                'stored_size=%d, segments=%d, adler=%r, encrypted=%r)'
                % (self.index, self.name, self.flags, self.original_size,
                   self.stored_size, len(self.segments), self.adler,
                   self.encrypted))


class Archive(object):
    def __init__(self, path, index, entries, hxv4=None, crypt=None):
        self.path = path
        self.index = index
        self.entries = entries
        self.hxv4 = hxv4          # None or dict(offset, size, kind)
        self.crypt = crypt        # None or a xp3_crypt filter

    @property
    def encrypted(self):
        return self.hxv4 is not None or self.crypt is not None


def _u16(data, off):
    return struct.unpack_from('<H', data, off)[0]


def _u32(data, off):
    return struct.unpack_from('<I', data, off)[0]


def _u64(data, off):
    return struct.unpack_from('<Q', data, off)[0]


def read_index(path):
    """Read and decompress the complete XP3 index chain.

    Each chain block is reached through an explicit little-endian pointer:
    the first pointer sits right after the 11-byte magic, and a block with the
    continuation bit (0x80) stores the pointer to the next block directly after
    its payload.  Simply reading the next bytes fails on CxdecV2 archives,
    which start with an empty continuation block.
    """
    with open(path, 'rb') as f:
        head = f.read(19)
        if len(head) < 19 or not head.startswith(MAGIC):
            raise Xp3Error('%s is not an XP3 archive' % path)

        pointer_pos = 11          # first pointer follows the magic
        parts = []
        for _ in range(1000000):
            f.seek(pointer_pos)
            raw_pointer = f.read(8)
            if len(raw_pointer) != 8:
                raise Xp3Error('truncated index pointer in %s' % path)
            absolute = _u64(raw_pointer, 0)

            f.seek(absolute)
            flag = f.read(1)
            if not flag:
                raise Xp3Error('truncated index chain in %s' % path)
            flags = flag[0]
            method = flags & 7

            if method == INDEX_RAW:
                size_raw = f.read(8)
                if len(size_raw) != 8:
                    raise Xp3Error('truncated raw index size in %s' % path)
                size = _u64(size_raw, 0)
                data = f.read(size)
                if len(data) != size:
                    raise Xp3Error('truncated raw index in %s' % path)
                position = absolute + 1 + 8 + size
            elif method == INDEX_ZLIB:
                sizes = f.read(16)
                if len(sizes) != 16:
                    raise Xp3Error('truncated zlib index sizes in %s' % path)
                stored, original = struct.unpack('<QQ', sizes)
                blob = f.read(stored)
                if len(blob) != stored:
                    raise Xp3Error('truncated zlib index in %s' % path)
                data = zlib.decompress(blob)
                if len(data) != original:
                    raise Xp3Error('index chunk size mismatch in %s' % path)
                position = absolute + 1 + 16 + stored
            else:
                raise Xp3Error('unknown index compression %d in %s'
                               % (method, path))

            parts.append(data)
            if not flags & 0x80:
                return b''.join(parts)
            pointer_pos = position

        raise Xp3Error('unreasonable index chain in %s' % path)


def iter_chunks(data):
    """Yield (tag, body) for a chunk stream."""
    pos = 0
    end = len(data)
    while pos + 12 <= end:
        tag = data[pos:pos + 4]
        size = _u64(data, pos + 4)
        body_start = pos + 12
        body_end = body_start + size
        if body_end > end:
            raise Xp3Error('truncated chunk %r' % tag)
        yield tag, data[body_start:body_end]
        pos = body_end


def _parse_info(entry, body):
    if len(body) < 22:
        return
    entry.flags = _u32(body, 0)
    entry.original_size = _u64(body, 4)
    entry.stored_size = _u64(body, 12)
    chars = _u16(body, 20)
    if chars and 22 + chars * 2 <= len(body):
        entry.name = body[22:22 + chars * 2].decode('utf-16le', 'replace')


def _parse_segm(entry, body):
    if len(body) % 28:
        return
    entry.segments = []
    for pos in range(0, len(body), 28):
        flags = _u32(body, pos)
        offset = _u64(body, pos + 4)
        original = _u64(body, pos + 12)
        stored = _u64(body, pos + 20)
        entry.segments.append((flags, offset, original, stored))


def _is_hash_name(name):
    """True for the 32-hex placeholder used when the index has no names."""
    if not name or len(name) != 32:
        return False
    return all(ch in '0123456789abcdefABCDEF' for ch in name)


def _parse_hnfn(body):
    """Parse one ``hnfn`` chunk: adler32 + UTF-16LE storage name.

    The name is NUL-terminated on disk; the stored character count excludes
    that terminator, so it is not part of the decoded text.
    """
    if len(body) < 8:
        return None
    key = _u32(body, 0)
    chars = _u16(body, 4)
    end = 6 + chars * 2
    if end > len(body):
        return None
    name = body[6:end].decode('utf-16le', 'replace')
    return key, name


def _apply_hnfn_names(entries, records):
    """Attach plaintext ``hnfn`` names to hash-named entries.

    KrKrz writes one ``hnfn`` chunk per entry when the index itself is not
    encrypted.  Its first four bytes are the entry adler32, so that is the
    reliable key.  A few assets share a checksum (duplicate or nearly
    duplicate images); the remaining names are assigned positionally because
    both the file chunks and the ``hnfn`` chunks keep archive order.
    """
    if not records:
        return
    unnamed = [entry for entry in entries if _is_hash_name(entry.name)]
    if not unnamed:
        return
    by_adler = {}
    for key, name in records:
        by_adler.setdefault(key, []).append(name)
    used = set()
    for entry in unnamed:
        for name in by_adler.get(entry.adler, ()):
            if name in used:
                continue
            entry.name = name
            used.add(name)
            break
    remaining = [name for _key, name in records if name not in used]
    left = [entry for entry in unnamed if _is_hash_name(entry.name)]
    if len(left) == len(remaining):
        for entry, name in zip(left, remaining):
            entry.name = name


def parse_file(body, index=0, encrypted=False, prefix=''):
    entry = Entry(index=index, encrypted=encrypted)
    for tag, sub in iter_chunks(body):
        if tag == TAG_INFO:
            _parse_info(entry, sub)
        elif tag == TAG_SEGM:
            _parse_segm(entry, sub)
        elif tag == TAG_ADLR:
            if len(sub) >= 4:
                entry.adler = _u32(sub, 0)
    if prefix:
        entry.name = prefix + (entry.name or '')
    return entry


def _walk_directory(body, prefix, out, encrypted):
    if len(body) < 2:
        return
    chars = _u16(body, 0)
    name = body[2:2 + chars * 2].decode('utf-16le', 'replace')
    child_prefix = prefix + name + '/'
    pos = 2 + chars * 2
    for tag, sub in iter_chunks(body[pos:]):
        if tag == TAG_FILE:
            out.append(parse_file(sub, len(out), encrypted, child_prefix))
        elif tag == TAG_DIRECTORY:
            _walk_directory(sub, child_prefix, out, encrypted)


def parse_archive(path, crypt=None):
    index = read_index(path)
    roots = list(iter_chunks(index))

    hxv4 = None
    for tag, body in roots:
        if tag == TAG_HXV4:
            if len(body) >= 14:
                hxv4 = {'offset': _u64(body, 0),
                        'size': _u32(body, 8),
                        'kind': _u16(body, 12)}
            else:
                hxv4 = {'offset': 0, 'size': 0, 'kind': 0}
            break

    encrypted = hxv4 is not None
    hnfn_names = []
    entries = []
    for tag, body in roots:
        if tag == TAG_FILE:
            entries.append(parse_file(body, len(entries), encrypted))
        elif tag == TAG_DIRECTORY:
            _walk_directory(body, '', entries, encrypted)
        elif tag == TAG_HNFN:
            record = _parse_hnfn(body)
            if record:
                hnfn_names.append(record)

    # An unencrypted index carries the real names in side-band ``hnfn``
    # chunks; the ``info`` chunks only hold 32-hex placeholders.
    if hnfn_names and not encrypted:
        _apply_hnfn_names(entries, hnfn_names)

    # Hxv4 file names come from the encrypted special index.  The info chunks
    # contain synthetic placeholders; give them stable, readable identities.
    if encrypted and crypt is None:
        for i, entry in enumerate(entries):
            entry.fake_name = entry.name
            entry.name = 'hxv4_%04x.bin' % i
            entry.encrypted = True

    archive = Archive(path, index, entries, hxv4, crypt)
    if crypt is not None:
        bind_crypt(archive, crypt)
    return archive


def extract_entry(path, entry):
    """Return the stored/decoded bytes for one entry.

    For Hxv4 archives this returns the filtered ciphertext; the caller decides
    whether to keep it.
    """
    data = bytearray()
    with open(path, 'rb') as f:
        for flags, offset, original, stored in entry.segments:
            f.seek(offset)
            blob = f.read(stored)
            if len(blob) != stored:
                raise Xp3Error('truncated segment for %r' % entry.name)
            method = flags & 7
            if method == 0:
                data += blob
            elif method == 1:
                data += zlib.decompress(blob)
            else:
                raise Xp3Error('unknown segment compression %d for %r'
                               % (method, entry.name))
    return bytes(data)


def adler32(data):
    return zlib.adler32(data) & 0xffffffff


def _entry_reader(archive):
    """Return a ``name -> bytes`` reader bound to one parsed archive."""
    def read_named(name):
        wanted = (name or '').replace('\\', '/')
        for entry in archive.entries:
            if (entry.name or '').replace('\\', '/') == wanted:
                return extract_entry(archive.path, entry)
        return None
    return read_named


def bind_crypt(archive, crypt):
    """Initialise a body filter and bind its archive-local parameters.

    Handles three behaviours of the known filters:

    * ``Init`` with an entry reader (``list.bin`` / ``TpmFileName``);
    * ``ObfuscatedIndex`` segment trimming (only the first segment is real);
    * the ``Hxv4`` special index, which supplies per-entry Hx keys and the
      real names when a ``NamesFile`` is available.
    """
    base_dir = os.path.dirname(os.path.abspath(archive.path))
    try:
        crypt.init(archive=archive, reader=_entry_reader(archive),
                   base_dir=base_dir)
    except TypeError:
        crypt.init()

    if getattr(crypt, 'obfuscated_index', False):
        for entry in archive.entries:
            if len(entry.segments) > 1:
                entry.segments = entry.segments[:1]
            if entry.segments:
                entry.original_size = entry.segments[0][2]
                entry.stored_size = entry.segments[0][3]

    if archive.hxv4 is not None and hasattr(crypt, 'read_index'):
        offset = archive.hxv4.get('offset') or 0
        size = archive.hxv4.get('size') or 0
        blob = b''
        if offset and size:
            with open(archive.path, 'rb') as f:
                f.seek(offset)
                blob = f.read(size)
        info_map = None
        if blob:
            try:
                info_map = crypt.read_index(os.path.basename(archive.path),
                                            blob)
            except Exception:
                info_map = None
        if info_map:
            for entry in archive.entries:
                info = info_map.get(entry.name)
                if info is None:
                    continue
                entry.extra = info
                prefix = info.path or ''
                if prefix and not prefix.endswith(('/', '\\')):
                    prefix += '/'
                entry.name = prefix + (info.name or entry.name)

    body_encrypted = getattr(crypt, 'body_encrypted', True)
    for entry in archive.entries:
        if getattr(crypt, 'startup_tjs_not_encrypted', False) \
                and entry.name == 'startup.tjs':
            entry.encrypted = False
        else:
            entry.encrypted = body_encrypted


def extract_plain(path, entry, crypt=None, read_filter=True):
    """Extract one entry with the archive's body filter applied.

    ``crypt`` may be omitted for plain archives.  When given, the body is
    decrypted with the scheme and then passed through the scheme's read
    filter (SimpleEncrypt / LZ4 / mdf unwrapping).
    """
    data = extract_entry(path, entry)
    if crypt is None:
        return data
    if getattr(entry, 'encrypted', True):
        # ``startup.tjs`` is loaded before the archive filter is active.  Most
        # titles store it verbatim even when every other body is encrypted;
        # recognise the TJS bytecode magic instead of trusting a scheme flag.
        if not ((entry.name or '').lower() == 'startup.tjs'
                and data[:3] == b'TJS'):
            data = crypt.decrypt_all(entry, data)
    if read_filter:
        data = crypt.entry_read_filter(entry, data)
    return data
