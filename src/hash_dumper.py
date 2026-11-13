"""Headless replacement for the in-game ``CxdecStringDumper``.

The original dumper hooks ``PathNameHasher`` / ``FileNameHasher`` inside the
running game and writes ``DirectoryHash.log`` / ``FileNameHash.log``.  That is
convenient but not headless, and the logs are easy to lose.

This module mines the same mapping from the decrypted resources instead:

* filenames are hashed as ``BLAKE2s-256(UTF16LE(name + seed))``;
* directories as ``bswap64(SipHash-2-4(UTF16LE(path + seed), zero key))``;
* a PSB's own ``root.name`` is used first (the file declares its storage name);
* other candidate names are harvested from text-like resource files (scripts,
  PSB string tables, INI/CSV/TXT), then verified against the hashes that the
  Hxv4 special index actually stored.

Only candidates whose hash equals a hash found in the archive are reported, so
a wrong guess can never enter ``filemap.json``.

Mining does not need a pre-extracted tree: :func:`harvest_archive` feeds the
decrypted entry bodies of the ``.xp3`` files straight into a :class:`Miner`, and
``extract_xp3.py`` runs that before writing anything.  ``--extracted`` re-mines
an existing tree instead.

Usage:
    py -X utf8 src/hash_dumper.py --extracted extracted --filemap filemap.json
    py -X utf8 src/hash_dumper.py --extracted extracted --scan assets_work
"""
import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_all
import hxv4
import psb
import simplecrypt
import xp3

# Extensions the engine may append.  This is a superset of the extensions seen
# in shipped KrKrz titles; a title-specific list can be supplied in
# ``config.json`` under ``hashing.extensions``.
EXTENSIONS = (
    '.ogg', '.opus', '.png', '.tlg', '.pimg', '.pbd', '.jpg', '.asd',
    '.stand', '.mchx', '.wav', '.mp3', '.tjs', '.ini', '.csv', '.tft',
    '.ks', '.scn', '.mtn', '.toml', '.txt', '.dat', '.psb', '.json',
    '.sli', '.func', '.stage', '.otf', '.ttf', '.ttc', '.sinfo',
    '.wmv', '.mpg', '.mpeg', '.mp4', '.avi', '.asf', '.webm', '.mkv', '.mov',
)
AUDIO_EXTENSIONS = ('.opus', '.ogg', '.mchx', '.wav', '.mp3')
SIDECAR = '.sli'
# Language variants are named with a suffix in most KrKrz titles.  Do not
# assume a target language: these are *reference* variants that may exist.
LANG_SUFFIXES = ('_en', '_cn', '_jp', '_tw', '_kr')
# Extra naming conventions (e.g. a second resolution variant).  Empty by
# default so the dumper does not bake in one title's convention; a game can
# opt in with ``hashing.extra_suffixes`` in config.json.
EXTRA_SUFFIXES = ()

BINARY_MAGICS = (
    b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'BM', b'TLG',
    b'OggS', b'RIFF', b'RIFX', b'FORM', b'fLaC', b'ID3', b'MThd',
    b'\xff\xfb', b'\xff\xf3', b'\xff\xf2',          # MPEG audio frames
    b'\x00\x00\x01\xba', b'\x00\x00\x01\xb3',       # MPEG program/system
    b'\x1aE\xdf\xa3', b'0&\xb2u',                    # Matroska / ASF (WMV)
    b'\x7fELF', b'MZ', b'PK\x03\x04', b'7z\xbc\xaf',
    b'DDS ', b'SQLite format 3',
)

PSB_MAGIC = b'PSB\x00'

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CANDIDATE_LENGTH = 48
MAX_RUN_LENGTH = 200
MAX_PATH_LENGTH = 200

# Resource names are written with ASCII letters/digits, kana, kanji and a few
# separators.  ``\w`` is *not* enough: it drops the katakana middle dot
# (U+30FB) and the fullwidth parentheses, which split real names such as
# ``bg101_自宅・自室a_large.png`` and ``bg302_大通り(交差点)a.png`` into
# fragments that can never match a stored hash.
NAME_CHARS = (r'0-9A-Za-z_\-.\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff'
              r'\uff00-\uffef\u2010-\u2027\u3005-\u3007()')
RUN_RE = re.compile(r'[%s]+' % NAME_CHARS, re.UNICODE)
PATH_RE = re.compile(r'[%s/\\]+' % NAME_CHARS, re.UNICODE)

# Compiled TJS2 scripts keep their string table as UTF-16LE.
UTF16_RUN_RE = re.compile(rb'(?:[\x20-\x7e\x80-\xff]\x00){2,}')
UTF8_RUN_RE = re.compile(rb'[\x20-\x7e]{2,}')

# A single text entry larger than this cannot plausibly be a name table, but
# the bundled CJK fonts are 17-25 MB and name themselves in their ``name``
# table, so the cap must stay above them.
MAX_HARVEST_BYTES = 32 * 1024 * 1024

# ``.stand`` files declare ``"filename"=>"st-yu02"`` (TJS) while other titles
# write ``filename:'st-yu02'`` or JSON ``"filename": "st-yu02"``.
FILENAME_FIELD_RE = re.compile(
    r'filename\s*"?\s*(?::|=>)\s*"([^"\r\n]{1,48})"', re.I)
FILENAME_COLON_RE = re.compile(
    r'filename\s*:\s*[\'"]([^\'"\r\n]{1,48})[\'"]', re.I)
MOVIE_RE = re.compile(r'\[edmovie\s+file\s*=\s*([^\]\s]+)', re.I)
QUOTED_RE = re.compile(r'"([^"\r\n]{2,40})"')
CSV_FIRST_RE = re.compile(r'(?m)^([^,\t\r\n]{2,24})[,\t]')

# Growth-pass budget.  Every candidate is still verified against a stored
# hash, so a larger budget can only add correct names, never wrong ones.  The
# seed pass alone needs roughly ``candidates * len(variants)`` probes.
MAX_PROBES = 120 * 1000 * 1000
MAX_GROW_NAMES = 3000
MAX_DIGIT_CAP = 1000
MAX_ROUNDS = 5
# Layer files use a *global* id (``st-yu02_01_1765.tlg``), not a per-base one.
MAX_LAYER_SWEEP = 20000


def is_candidate_char(ch):
    code = ord(ch)
    if code < 0x80:
        return (('0' <= ch <= '9') or ('A' <= ch <= 'Z')
                or ('a' <= ch <= 'z') or ch in '_.-()')
    return (0x3040 <= code <= 0x30FF or 0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF or 0xFF00 <= code <= 0xFFEF
            or 0x3005 <= code <= 0x3007)


def is_path_char(ch):
    return is_candidate_char(ch) or ch in '/\\'


def is_binary_magic(raw):
    return any(raw.startswith(magic) for magic in BINARY_MAGICS)


def is_utf16_text(raw):
    if len(raw) >= 2 and raw[0] == 0xFF and raw[1] == 0xFE:
        return True, True
    if len(raw) >= 2 and raw[0] == 0xFE and raw[1] == 0xFF:
        return True, False
    sample = raw[:8192]
    if len(sample) < 8:
        return False, True
    zeros = sample.count(0)
    if zeros * 100 // len(sample) < 25:
        return False, True
    even = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
    odd = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i + 1] == 0)
    return True, odd >= even


def looks_like_text(text):
    """True when *text* is mostly printable characters."""
    sample = text[:16384]
    if not sample:
        return False
    good = sum(1 for ch in sample if ch.isprintable() or ch in '\r\n\t')
    return good * 100 >= len(sample) * 70


def _plausible_ratio(text):
    """Fraction of characters that look like game text (ASCII/kana/kanji)."""
    sample = text[:16384]
    if not sample:
        return 0.0
    good = 0
    for ch in sample:
        code = ord(ch)
        if 0x20 <= code <= 0x7E or code in (0x09, 0x0A, 0x0D):
            good += 1
        elif (0x3000 <= code <= 0x30FF or 0x4E00 <= code <= 0x9FFF
              or 0x3400 <= code <= 0x4DBF or 0xFF00 <= code <= 0xFFEF
              or 0x2010 <= code <= 0x2027 or 0x3005 <= code <= 0x3007):
            good += 1
    return good / len(sample)


def decode_text(raw):
    """Return text for text-like files, or ``None`` for binaries.

    Older versions decided between UTF-8 and UTF-16 from the share of NUL
    bytes alone.  Non-latin UTF-16LE text (``imagemulti.txt``, ``st-*.sinfo``)
    contains few NULs, so it was decoded as UTF-8 and turned into mojibake --
    every literal name in it was silently lost.  The heuristic below decodes
    both ways and keeps the reading that is actually plausible game text.
    """
    if is_binary_magic(raw):
        return None
    # PSB containers are binary but their name/string tables are UTF-8.  They
    # must be decoded as UTF-8 *before* the UTF-16 heuristic, which would
    # otherwise see the many NUL bytes and produce mojibake.
    if raw.startswith(PSB_MAGIC):
        return raw.decode('utf-8', 'ignore')

    if raw[:2] == b'\xff\xfe':
        return raw[2:].decode('utf-16le', 'ignore')
    if raw[:2] == b'\xfe\xff':
        return raw[2:].decode('utf-16be', 'ignore')

    # A strict UTF-8 decode that leaves plausible text is the strongest
    # signal; UTF-16LE text with non-latin characters fails it at once.
    try:
        utf8 = raw.decode('utf-8')
    except UnicodeDecodeError:
        utf8 = None
    if utf8 is not None and _plausible_ratio(utf8) >= 0.95:
        return utf8

    if len(raw) >= 8:
        if len(raw) % 2 == 0:
            utf16 = raw.decode('utf-16le', 'ignore')
            if _plausible_ratio(utf16) >= 0.90:
                return utf16
        utf16 = raw.decode('utf-16be', 'ignore')
        if _plausible_ratio(utf16) >= 0.95:
            return utf16

    if utf8 is not None and looks_like_text(utf8):
        return utf8
    text = raw.decode('utf-8', 'ignore')
    if looks_like_text(text):
        return text
    return None


def tjs_string_table(raw):
    """Return the literal strings of a compiled TJS2 script.

    The bytecode header is ASCII (``TJS2100``) but the string table is
    UTF-16LE, which is why mining compiled scripts as UTF-8 found nothing.
    """
    parts = []
    for match in UTF16_RUN_RE.finditer(raw):
        try:
            parts.append(match.group().decode('utf-16le'))
        except UnicodeDecodeError:
            pass
    for match in UTF8_RUN_RE.finditer(raw):
        parts.append(match.group().decode('latin-1'))
    return '\n'.join(parts)


def psb_strings(raw, root=None):
    """Return every string value stored in a PSB container.

    Scenario PSBs keep their text in compressed chunks, so the raw bytes show
    only codec noise.  The scenarios are where the engine spells out the sound
    and background names it opens (``水がかかる``, ``bg302_大通り(交差点)a``);
    without decompressing them those literals are invisible to the miner.
    """
    if root is None:
        try:
            root = psb.load(raw).to_json()
        except Exception:
            return []
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str):
                    found.add(key)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.add(node)

    walk(root)
    # Sentences are not resource names, and hashing every word of the script
    # would only slow the verify phase down.
    return [value for value in found
            if 2 <= len(value) <= 64 and not any(ch.isspace() for ch in value)]


FONT_MAGICS = (b'OTTO', b'\x00\x01\x00\x00', b'true', b'ttcf')


def font_names(raw):
    """Return the family/PostScript names declared in a TrueType/OpenType font."""
    try:
        num_tables = int.from_bytes(raw[4:6], 'big')
    except Exception:
        return []
    tables = {}
    for index in range(num_tables):
        offset = 12 + index * 16
        tag = raw[offset:offset + 4]
        if len(tag) != 4:
            break
        table_offset = int.from_bytes(raw[offset + 8:offset + 12], 'big')
        tables[tag] = table_offset
    name_offset = tables.get(b'name')
    if not name_offset:
        return []
    try:
        count = int.from_bytes(raw[name_offset + 2:name_offset + 4], 'big')
        strings = name_offset + int.from_bytes(raw[name_offset + 4:name_offset + 6], 'big')
    except Exception:
        return []
    out = set()
    for index in range(count):
        record = name_offset + 6 + index * 12
        chunk = raw[record:record + 12]
        if len(chunk) < 12:
            break
        platform, _enc, _lang, name_id, length, offset = (
            int.from_bytes(chunk[i:i + 2], 'big') for i in range(0, 12, 2))
        if name_id not in (1, 4, 6, 16):
            continue
        value = raw[strings + offset:strings + offset + length]
        try:
            text = value.decode('utf-16be' if platform in (0, 3) else 'latin-1')
        except UnicodeDecodeError:
            continue
        if text.strip():
            out.add(text.strip())
    return sorted(out)


def _trim_run(run):
    start = 0
    end = len(run)
    while start < end and run[start] in '.-':
        start += 1
    while end > start and run[end - 1] in '.-':
        end -= 1
    return run[start:end]


def filename_candidates(text, structural=True):
    """Yield candidate bare names from text.

    With ``structural=True`` (Yua's rule) a candidate must contain a digit,
    ``_``, ``.`` or ``-``.  That keeps ordinary prose out of the PSB scan.
    ``structural=False`` is used for small, high-signal files (decrypted INI,
    TJS string tables) where bare words such as ``title`` are resource names.
    """
    seen = set()
    for match in RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) <= MAX_RUN_LENGTH:
            _flush_candidate(run, seen, structural)
    return seen


def _flush_candidate(run, out, structural=True):
    text = _trim_run(run)
    if not (2 <= len(text) <= MAX_CANDIDATE_LENGTH):
        return
    if structural and not any(ch.isdigit() or ch in '_.-' for ch in text):
        return
    out.add(text)


def directory_candidates(text):
    """Yield possible relative directory paths (with trailing slash)."""
    seen = set()
    for match in PATH_RE.finditer(text):
        run = match.group(0)
        if len(run) <= MAX_PATH_LENGTH:
            _flush_path(run, seen)
    return seen


def _flush_path(run, out):
    # Only runs that actually contain a path separator are directory
    # candidates.  Treating every word in the text as a directory produced
    # millions of junk paths and made the hash phase unusable.
    if '/' not in run and '\\' not in run:
        return
    text = run.replace('\\', '/').strip('/')
    if not text:
        return
    parts = [part for part in text.split('/') if part and part != '.']
    if not parts or len(parts) > 8:
        return
    for i in range(1, len(parts) + 1):
        candidate = '/'.join(parts[:i]) + '/'
        if len(candidate) <= MAX_PATH_LENGTH:
            out.add(candidate)


def collect_specials(text, names, vocab=None, bases=None):
    """Harvest names that are declared by *structure*, not by plain tokens.

    * ``filename=>"st-yu02"`` / ``filename:'st-yu02'`` (``.stand`` / JSON)
    * ``[edmovie file=op01]`` -> ``op01.wmv``
    * quoted values and the first CSV column, which many titles use as the
      authoritative resource list (``imagemulti.txt``, ``cglist.csv``).
    """
    found = []
    for regex in (FILENAME_FIELD_RE, FILENAME_COLON_RE):
        for match in regex.finditer(text):
            value = match.group(1).strip()
            if value:
                found.append(value)
    for value in found:
        names.add(value)
        if vocab is not None:
            vocab.add(value)
        if bases is not None:
            bases.add(value)
    for match in MOVIE_RE.finditer(text):
        value = match.group(1).strip().strip('\'"')
        if value:
            names.add(value + '.wmv')
            if vocab is not None:
                vocab.add(value)
    if vocab is not None:
        for match in QUOTED_RE.finditer(text):
            value = match.group(1).strip()
            if 2 <= len(value) <= 16:
                vocab.add(value)
        for match in CSV_FIRST_RE.finditer(text):
            value = match.group(1).strip()
            if 2 <= len(value) <= 16:
                # CSV lists (``imagemulti.txt``, ``cglist.csv``) name ordinary
                # resources, not layer bases; the latter come from ``filename``
                # declarations only, so keep the two pools separate.
                vocab.add(value)


def scan_bytes(raw, names, paths, counters, self_names=None, vocab=None,
               bases=None):
    """Mine one already-decrypted resource body."""
    if not raw:
        counters['skipped'] += 1
        return
    rich = False
    if raw.startswith(PSB_MAGIC):
        # A PSB declares its own storage name in ``root.name``.  That is the
        # strongest possible evidence short of a runtime trace, so collect it
        # separately and verify it against the stored hash below.
        root = None
        try:
            root = psb.load(raw).to_json()
        except Exception:
            root = None
        if self_names is not None and root is not None:
            declared = root.get('name')
            if declared:
                self_names.add(declared)
        # Scenario PSBs also spell out the media names they open.
        values = psb_strings(raw, root)
        names.update(values)
        collect_specials('\n'.join(values), names, vocab, bases)
        counters['scanned'] += 1
        return
    if raw.startswith(FONT_MAGICS):
        # A font file names itself in its ``name`` table.  The shipped files
        # are lower-cased, so offer both readings.
        for value in font_names(raw):
            names.add(value)
            names.add(value.lower())
            if vocab is not None:
                vocab.add(value)
        counters['scanned'] += 1
        return
    if simplecrypt.is_wrapped(raw):
        # The UI layout/config files are SimpleEncrypt mode 1.  Their plaintext
        # is UTF-16LE and contains bare resource stems (``title``, ``select``).
        raw = simplecrypt.decrypt(raw)
        text = raw.decode('utf-16le', 'ignore')
        rich = True
    elif raw[:3] == b'TJS':
        # Compiled TJS keeps its string constants as UTF-16LE in plain sight.
        text = tjs_string_table(raw)
        rich = True
    else:
        text = decode_text(raw)
    if text is None:
        counters['skipped'] += 1
        return
    counters['scanned'] += 1
    names.update(filename_candidates(text, structural=not rich))
    paths.update(directory_candidates(text))
    collect_specials(text, names, vocab, bases)


def scan_file(path, names, paths, counters, self_names=None, vocab=None,
              bases=None):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            counters['skipped'] += 1
            return
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError:
        counters['skipped'] += 1
        return
    scan_bytes(raw, names, paths, counters, self_names, vocab, bases)


def collect_targets(extracted):
    """Return ``(file_hashes, dir_hashes)`` present in the extraction tree.

    Hash-named trees are read directly.  A tree written with resolved names
    (extraction now prefers real paths) is read from the per-archive
    ``manifest.json`` that extraction leaves behind.
    """
    files = set()
    dirs = set()
    for archive, dirhash, filehash, _path in extract_all.iter_tree(extracted):
        files.add(bytes.fromhex(filehash))
        dirs.add(bytes.fromhex(dirhash))
    if files:
        return files, dirs
    for archive in sorted(os.listdir(extracted)):
        manifest = os.path.join(extracted, archive, 'manifest.json')
        if not os.path.isfile(manifest):
            continue
        try:
            with open(manifest, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        for row in data.get('entries') or ():
            filehash = row.get('filehash')
            dirhash = row.get('dirhash')
            if filehash:
                files.add(bytes.fromhex(filehash))
            if dirhash:
                dirs.add(bytes.fromhex(dirhash))
    return files, dirs


def collect_archive_targets(drivers):
    """Return the hash sets a set of Hxv4 drivers must explain.

    Mining straight from the archives means the target hashes come from the
    decrypted special index, not from a previously extracted tree.
    """
    files = set()
    dirs = set()
    for driver in drivers:
        if driver is None:
            continue
        for record in driver.records.values():
            files.add(record.name_hash)
            dirs.add(record.path_hash)
    return files, dirs


def harvest_archive(archive, driver, miner, max_bytes=MAX_HARVEST_BYTES):
    """Feed every text-like entry of *archive* into *miner*.

    This runs before any file is written, so extraction gets real names in the
    same pass instead of requiring a full hash-named extraction first.

    An Hxv4 entry is called ``hxv4_0005.bin`` until mining is finished, so the
    decision cannot use the entry name.  The decrypted body is inspected
    instead: :func:`scan_bytes` recognises PSB / font / TJS / SimpleEncrypt
    content, and media magics are rejected immediately, so the work spent on
    images and audio is just one decompression.
    """
    used = 0
    for entry in archive.entries:
        if entry.fake_name is not None:
            try:
                if hxv4.fake_id(entry.fake_name) == 0:
                    continue          # protected-warning metadata
            except (TypeError, ValueError):
                pass
        size = entry.original_size or 0
        if size == 0 or size > max_bytes:
            continue
        try:
            raw = xp3.extract_entry(archive.path, entry)
            if driver is not None:
                raw = driver.decrypt(entry, raw)
        except Exception:
            continue
        miner.add_bytes(raw)
        used += 1
    return used


def name_variants(candidate, extensions=None, lang_suffixes=None,
                  extra_suffixes=None):
    """Expand a stem into the exact storage names the engine may open."""
    extensions = EXTENSIONS if extensions is None else extensions
    lang_suffixes = LANG_SUFFIXES if lang_suffixes is None else lang_suffixes
    extra_suffixes = EXTRA_SUFFIXES if extra_suffixes is None else extra_suffixes
    stem, ext = os.path.splitext(candidate)
    bases = [candidate]
    for suffix in lang_suffixes:
        bases.append(stem + suffix + ext)
    for suffix in extra_suffixes:
        bases.append(stem + suffix + ext)
    out = list(bases)
    for base in bases:
        for extension in extensions:
            out.append(base + extension)
            if extension in AUDIO_EXTENSIONS:
                out.append(base + extension + SIDECAR)
    return out


def seed_from_exe(path, fallback=hxv4.HXV4_SALT):
    """Read the filename-hash seed from a KrKrz executable.

    The seed (``xp3hnp`` in stock KrKrz) is stored as a UTF-16LE literal inside
    the engine, so a patched build can be handled without guessing.  The
    detected game exe is therefore the source of truth.
    """
    if not path or not os.path.isfile(path):
        return fallback
    try:
        with open(path, 'rb') as handle:
            blob = handle.read()
    except OSError:
        return fallback
    needle = fallback.encode('utf-16le')
    if needle in blob:
        return fallback
    # Any short ASCII literal ending in "xp3hnp" would be the patched seed.
    marker = 'xp3hnp'.encode('utf-16le')
    at = blob.find(marker)
    if at >= 0:
        start = at
        while start >= 2 and 0x20 <= blob[start - 2] <= 0x7e and blob[start - 1] == 0:
            start -= 2
        try:
            value = blob[start:at + len(marker)].decode('utf-16le')
        except UnicodeDecodeError:
            return fallback
        if 2 <= len(value) <= 32 and value.endswith('xp3hnp'):
            return value
    return fallback


def naming_from_config(config_path=None):
    """Return ``(extensions, lang_suffixes, extra_suffixes)`` for a game.

    ``config.json`` may override any of the three lists under ``hashing``.
    Missing entries keep the generic defaults, so the dumper works for any
    KrKrz title without edit.
    """
    cfg = {}
    if config_path is None:
        try:
            import configfile
            config_path = configfile.config_path()
        except Exception:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'config.json')
    if os.path.isfile(config_path):
        try:
            import configfile
            cfg = configfile.load(config_path)
        except Exception:
            with open(config_path, encoding='utf-8') as handle:
                cfg = json.load(handle)
    hashing = cfg.get('hashing') or {}
    extensions = tuple(hashing.get('extensions') or EXTENSIONS)
    lang_suffixes = tuple(hashing.get('lang_suffixes') or LANG_SUFFIXES)
    extra_suffixes = tuple(hashing.get('extra_suffixes') or EXTRA_SUFFIXES)
    return extensions, lang_suffixes, extra_suffixes


NUM_RE = re.compile(r'\d+')
ALPHA_RE = re.compile(r'[a-z]{1,2}')


class Miner(object):
    """Collect candidate names, then verify them against stored hashes.

    The miner is independent of where the text came from, so extraction can
    feed it decrypted entry bodies directly (see :func:`harvest_archive`)
    instead of first writing a hash-named tree and reading it back.
    """

    def __init__(self, extensions=None, lang_suffixes=None,
                 extra_suffixes=None, seed=hxv4.HXV4_SALT):
        self.extensions = tuple(EXTENSIONS if extensions is None else extensions)
        self.lang_suffixes = tuple(LANG_SUFFIXES if lang_suffixes is None
                                   else lang_suffixes)
        self.extra_suffixes = tuple(EXTRA_SUFFIXES if extra_suffixes is None
                                    else extra_suffixes)
        self.seed = seed
        self.names = set()
        self.paths = {''}   # the empty/root directory is always a candidate
        self.self_names = set()
        self.vocab = set()   # short tokens usable as name components
        self.bases = set()   # declared layer bases (``filename=>...``)
        self.counters = collections.Counter()
        self.targets = set()
        self.found = {}
        self.found_names = set()
        self.probes = 0

    # -- harvesting -------------------------------------------------------
    def add_bytes(self, raw):
        try:
            scan_bytes(raw, self.names, self.paths, self.counters,
                       self.self_names, self.vocab, self.bases)
        except Exception:
            # One malformed entry must not abort name discovery for the whole
            # archive; the hash check still guards every accepted name.
            self.counters['errors'] += 1

    def add_file(self, path):
        try:
            scan_file(path, self.names, self.paths, self.counters,
                      self.self_names, self.vocab, self.bases)
        except Exception:
            self.counters['errors'] += 1

    def add_text(self, text):
        self.names.update(filename_candidates(text, structural=False))
        self.paths.update(directory_candidates(text))
        collect_specials(text, self.names, self.vocab, self.bases)

    # -- verification -----------------------------------------------------
    def probe(self, candidate):
        # Deliberately no global "already tried" set: the seed pass may look at
        # tens of millions of variants and keeping every string would cost
        # gigabytes.  Hashing one duplicate is far cheaper than remembering it.
        if not candidate or candidate in self.found_names:
            return None
        if self.probes >= MAX_PROBES:
            return None
        self.probes += 1
        digest = hxv4.filename_hash(candidate, self.seed)
        if digest in self.targets and digest not in self.found:
            self.found[digest] = candidate
            self.found_names.add(candidate)
            return candidate
        return None

    def offer(self, candidate):
        """Try *candidate* as a full name and as a stem plus extensions.

        A PSB may declare ``root.name`` as ``foo.txt`` while the archive stores
        ``foo.txt.scn``, so every candidate still has to go through the full
        variant expansion instead of being probed as-is only.
        """
        for name in name_variants(candidate, self.extensions,
                                  self.lang_suffixes, self.extra_suffixes):
            self.probe(name)

    # -- growth passes ----------------------------------------------------
    def grow_digits(self, names, digit_cap=MAX_DIGIT_CAP):
        """Enumerate numeric slots of already-found names (``ev101aa``...)."""
        added = []
        for name in names:
            stem, ext = os.path.splitext(name)
            for match in NUM_RE.finditer(stem):
                start, end = match.start(), match.end()
                width = end - start
                if width > 4:
                    continue
                for value in range(0, min(digit_cap, 10 ** width)):
                    candidate = (stem[:start] + str(value).zfill(width)
                                 + stem[end:] + ext)
                    if self.probe(candidate):
                        added.append(candidate)
                if self.probes >= MAX_PROBES:
                    return added
        return added

    def grow_neighbors(self, names, radius=4):
        """Probe names adjacent to an already-found one.

        Numbered resources come in consecutive blocks (``b05_0126`` ..
        ``b05_0130``, ``map01`` .. ``map29``) and enumerated layer variants add
        a trailing letter/digit (``map501c`` -> ``map501c2``).  The full
        ``grow_digits`` sweep only covers a truncated batch; this cheap pass
        runs over *every* found name and walks outwards, so blocks keep growing
        across rounds without exhausting the probe budget.
        """
        added = []
        for name in names:
            stem, ext = os.path.splitext(name)
            for match in NUM_RE.finditer(stem):
                start, end = match.start(), match.end()
                width = end - start
                if width > 6:
                    continue
                try:
                    value = int(match.group())
                except ValueError:
                    continue
                for delta in range(1, radius + 1):
                    for neighbour in (value - delta, value + delta):
                        if neighbour < 0:
                            continue
                        candidate = (stem[:start] + str(neighbour).zfill(width)
                                     + stem[end:] + ext)
                        if self.probe(candidate):
                            added.append(candidate)
            # Optional trailing digit: ``map501c`` -> ``map501c2``.
            for digit in '0123456789':
                candidate = stem + digit + ext
                if self.probe(candidate):
                    added.append(candidate)
            # ... and drop it: ``map602b2`` -> ``map602b``.
            if len(stem) > 2 and stem[-1].isdigit():
                candidate = stem[:-1] + ext
                if self.probe(candidate):
                    added.append(candidate)
            if self.probes >= MAX_PROBES:
                return added
        return added

    def grow_alpha(self, names):
        """Enumerate one/two-letter slots (``sd012aa`` -> ``sd012ab``)."""
        added = []
        for name in names:
            stem, ext = os.path.splitext(name)
            for match in ALPHA_RE.finditer(stem):
                start, end = match.start(), match.end()
                width = end - start
                for char in 'abcdefghijklmnopqrstuvwxyz':
                    candidate = stem[:start] + char * width + stem[end:] + ext
                    if self.probe(candidate):
                        added.append(candidate)
                if self.probes >= MAX_PROBES:
                    return added
        return added

    def derive_sidecars(self, names):
        """Every audio entry usually has a matching ``<name>.sli`` sidecar."""
        added = []
        for name in names:
            stem, _dot, _ext = name.rpartition('.')
            lower = name.lower()
            if lower.endswith(AUDIO_EXTENSIONS):
                for candidate in (name + SIDECAR, stem + SIDECAR):
                    if self.probe(candidate):
                        added.append(candidate)
            elif lower.endswith(SIDECAR):
                base = name[:-len(SIDECAR)]
                if self.probe(base):
                    added.append(base)
        return added

    def substitute_from(self, names, limit=4000):
        """Swap the leading component for other component-like tokens."""
        vocab = [value for value in self.vocab if 2 <= len(value) <= 16]
        vocab.sort(key=lambda value: (len(value), value))
        vocab = vocab[:limit]
        cjk = [value for value in vocab
               if re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', value)]
        ascii_vocab = [value for value in vocab if value.isascii()
                       and re.fullmatch(r'[A-Za-z0-9_]+', value)]
        added = []
        skeletons = {}
        for name in names:
            stem, ext = os.path.splitext(name)
            match = re.match(r'([A-Za-z\u3040-\u30ff\u4e00-\u9fff]+)', stem)
            if not match:
                continue
            run = match.group(1)
            if len(run) < 2:
                continue
            skeletons.setdefault((stem[len(run):], ext), set()).add(run)
        for (rest, ext), runs in skeletons.items():
            sample = next(iter(runs))
            pool = cjk if re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', sample) \
                else ascii_vocab
            for value in pool:
                if value in runs:
                    continue
                candidate = value + rest + ext
                if self.probe(candidate):
                    added.append(candidate)
            if self.probes >= MAX_PROBES:
                break
        return added

    def enumerate_components(self, limit=4000, nums=128, base_nums=MAX_LAYER_SWEEP):
        """Combine name components with the numbering styles this engine uses.

        ``.stand``/``.sinfo`` declare a *base* (``st-yu02``) while the archive
        stores numbered variants (``st-yu02_01_1765.tlg``).  The layer id is a
        global counter, so declared bases are swept over a wide id range
        instead of the small 0..99 window used for ordinary components.

        Every list is sorted with an explicit tiebreaker: truncating a
        length-sorted list of set members would otherwise make the set of
        discovered names depend on Python's hash seed.
        """
        components = [value for value in self.vocab if 2 <= len(value) <= 12]
        components.sort(key=lambda value: (len(value), value))
        components = components[:limit]
        extensions = ('.tlg', '.png', '.psb', '.ogg', '.opus')
        added = []
        for component in components:
            for ext in extensions:
                for number in range(nums):
                    for candidate in (component + '_%02d' % number,
                                      component + '%02d' % number,
                                      component + '_' + str(number),
                                      component + str(number)):
                        if self.probe(candidate + ext):
                            added.append(candidate + ext)
                for char in 'abcdefgh':
                    if self.probe(component + char + ext):
                        added.append(component + char + ext)
                    for number in range(min(nums, 40)):
                        candidate = component + char + '_%02d' % number + ext
                        if self.probe(candidate):
                            added.append(candidate)
                if self.probes >= MAX_PROBES:
                    return added

        # Every ``filename`` declaration is a layer base, including numeric
        # ones (``st-yu02``); do not filter by the trailing character.
        bases = [value for value in self.bases if 2 <= len(value) <= 16]
        bases.sort(key=lambda value: (len(value), value))
        # ``imagemulti.txt`` names the resolution variants ``<base>_01`` ..
        # ``_03``; the stored layer file uses that variant plus a global id.
        variant_bases = []
        for component in bases[:400]:
            variant_bases.append(component)
            for variant in range(1, 5):
                variant_bases.append('%s_%02d' % (component, variant))
        for component in variant_bases:
            for ext in ('.tlg', '.png', '.psb'):
                if self.probe(component + ext):
                    added.append(component + ext)
                for number in range(base_nums):
                    candidate = component + '_' + str(number) + ext
                    if self.probe(candidate):
                        added.append(candidate)
                    if number < 100:
                        candidate = component + '_%02d' % number + ext
                        if self.probe(candidate):
                            added.append(candidate)
                if self.probes >= MAX_PROBES:
                    return added
        return added

    # -- resolve ----------------------------------------------------------
    def resolve(self, targets, target_dirs=(), progress=False):
        started = time.time()
        self.targets = set(targets)
        target_dirs = set(target_dirs)

        # Directory names are often written like file stems (``翠``, ``uipsd``).
        self.paths.update(name + '/' for name in self.names)

        # PSB self-declared names first: they are the strongest evidence.
        for candidate in sorted(self.self_names):
            self.offer(candidate)
        for candidate in sorted(self.names):
            self.offer(candidate)

        pending = sorted(self.found.values())
        if progress:
            print('  seed pass: %d/%d names (%d probes)'
                  % (len(self.found), len(self.targets), self.probes),
                  flush=True)
        for round_no in range(MAX_ROUNDS):
            if self.probes >= MAX_PROBES:
                break
            before = len(self.found)
            new = []
            # The component sweep is independent of the seed hits: a title can
            # declare layer bases in ``.stand`` files even when no text token
            # matched during the seed pass.
            if round_no == 0:
                new += self.enumerate_components()
            batch = pending[:MAX_GROW_NAMES]
            all_found = sorted(self.found.values())
            new += self.grow_neighbors(all_found)
            new += self.grow_alpha(all_found)
            new += self.derive_sidecars(all_found)
            if batch:
                new += self.grow_digits(batch)
                new += self.substitute_from(batch)
            pending = new
            if progress:
                print('  growth %d: %d names (+%d, %d probes)'
                      % (round_no, len(self.found),
                         len(self.found) - before, self.probes), flush=True)
            if not pending:
                break

        dir_map = {}
        for candidate in self.paths:
            digest = hxv4.path_hash(candidate, self.seed)
            if digest in target_dirs and digest not in dir_map:
                dir_map[digest] = candidate or '%EmptyString%'

        if progress:
            remaining = self.targets - set(self.found)
            print('hash names: %d/%d files, %d/%d directories in %.1fs'
                  % (len(self.found), len(self.targets), len(dir_map),
                     len(target_dirs), time.time() - started), flush=True)
            if remaining:
                print('unmatched file hashes: %d' % len(remaining), flush=True)
        return ({key.hex().upper(): value for key, value in self.found.items()},
                {key.hex().upper(): value for key, value in dir_map.items()})


def mine(extracted, scan_roots=None, seed=hxv4.HXV4_SALT, progress=True,
         extensions=None, lang_suffixes=None, extra_suffixes=None):
    """Mine hash->name mappings from an already extracted hash tree.

    No ``filemap.json`` or dumper log is read: every name comes from text that
    was found inside the archive and is accepted only when its hash equals a
    hash stored in the special index.
    """
    if extensions is None and lang_suffixes is None and extra_suffixes is None:
        extensions, lang_suffixes, extra_suffixes = naming_from_config()
    targets, target_dirs = collect_targets(extracted)

    miner = Miner(extensions, lang_suffixes, extra_suffixes, seed=seed)
    for root in scan_roots or [extracted]:
        for base, _dirs, files in os.walk(root):
            for name in files:
                miner.add_file(os.path.join(base, name))
        if progress:
            print('scanned %d text file(s), %d name candidate(s), %d path candidate(s)'
                  % (miner.counters['scanned'], len(miner.names),
                     len(miner.paths)), flush=True)
    return miner.resolve(targets, target_dirs, progress=progress)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--extracted', required=True)
    ap.add_argument('--filemap', required=True,
                    help='filemap.json to write (never read back)')
    ap.add_argument('--scan', action='append', default=None,
                    help='extra folder to scan for candidate names')
    ap.add_argument('--game', default=None,
                    help='game folder; the exe is detected and the hash seed '
                         'is read from it')
    ap.add_argument('--exe', default=None,
                    help='explicit game executable (overrides --game detection)')
    ap.add_argument('--config', default=None,
                    help='config.json providing hashing.extensions / '
                         'lang_suffixes / extra_suffixes (default: template)')
    ap.add_argument('--seed', default=None,
                    help='filename-hash seed (default: read from the game exe, '
                         'falling back to %s)' % hxv4.HXV4_SALT)
    ap.add_argument('--extension', action='append', default=None,
                    help='extra extension to try (repeatable)')
    ap.add_argument('--suffix', action='append', default=None,
                    help='extra name suffix before the extension (repeatable)')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamecfg

    extensions, lang_suffixes, extra_suffixes = naming_from_config(args.config)
    if args.extension:
        extensions = tuple(extensions) + tuple(args.extension)
    if args.suffix:
        extra_suffixes = tuple(extra_suffixes) + tuple(args.suffix)

    seed = args.seed
    if not seed:
        exe = gamecfg.detect_exe(args.game, args.exe) if (args.game or args.exe) else None
        seed = seed_from_exe(exe) if exe else hxv4.HXV4_SALT

    roots = [args.extracted] + list(args.scan or [])
    file_map, dir_map = mine(args.extracted, roots, seed=seed,
                             progress=not args.quiet,
                             extensions=extensions,
                             lang_suffixes=lang_suffixes,
                             extra_suffixes=extra_suffixes)
    rows = extract_all.build_rows(args.extracted,
                                  file_names=file_map, dir_names=dir_map)
    extract_all.write_rows(rows, args.filemap)
    return 0


if __name__ == '__main__':
    sys.exit(main())
