"""Native Hxv4 / CxdecV2 archive decryption.

The game archives hide their real file names in an encrypted out-of-line
``Hxv4`` special index and XOR-filter every file body with a per-entry stream
derived from the title's FilterManager.  Everything needed to undo that is
recovered from the executable itself by :mod:`hxv4`; this module glues the
pieces together for one archive:

1. parse the ordinary XP3 ``File`` records (:mod:`xp3`);
2. recover the special-index key/nonce from the executable and the archive's
   ``Hxv4`` blob;
3. parse the special index into ``id -> record`` (path hash, name hash,
   entry key, filter flag);
4. build the native ``FilterManager`` from the bootstrap PARAMS;
5. decrypt an entry body in place.

The executable analysis is expensive (a few seconds), so it is cached on disk
under ``cache/hxv4`` keyed by the executable's path, size and mtime.

No game process, DLL injection or GUI tooling is involved.
"""
import hashlib
import os
import pickle
import struct
import sys
import time

SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import hxv4
import xp3

TEMPLATE = os.path.dirname(SRC)
CACHE_DIR = os.path.join(TEMPLATE, 'cache', 'hxv4')


class Hxv4ExtractError(Exception):
    pass


# --------------------------------------------------------------------------
# executable analysis cache

def _cache_key(exe):
    stat = os.stat(exe)
    return (os.path.abspath(exe), stat.st_size, int(stat.st_mtime))


def _cache_path(exe):
    digest = hashlib.sha256(
        ('%s|%d|%d' % _cache_key(exe)).encode('utf-8')).hexdigest()[:16]
    return os.path.join(CACHE_DIR, 'analysis-%s.pkl' % digest)


def analyze_executable(exe, use_cache=True):
    """Analyse the game executable, reusing a cached copy when possible."""
    exe = os.path.abspath(exe)
    cache = _cache_path(exe)
    if use_cache and os.path.isfile(cache):
        try:
            with open(cache, 'rb') as f:
                payload = pickle.load(f)
            if payload.get('key') == _cache_key(exe):
                return payload['analysis']
        except (OSError, ValueError, pickle.PickleError, EOFError):
            pass
    analysis = hxv4.analyze_executable(exe)
    if use_cache:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = cache + '.tmp'
            with open(tmp, 'wb') as f:
                pickle.dump({'key': _cache_key(exe), 'analysis': analysis}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache)
        except OSError:
            pass
    return analysis


# --------------------------------------------------------------------------
# one archive

class Hxv4Archive(object):
    """Decrypt the entries of a single Hxv4/CxdecV2 XP3 archive."""

    def __init__(self, path, analysis=None, exe=None):
        self.path = os.path.abspath(path)
        self.archive = xp3.parse_archive(self.path)
        if not self.archive.hxv4:
            raise Hxv4ExtractError('%s is not an Hxv4 archive' % self.path)
        self.analysis = analysis
        self.exe = exe
        self.key = None
        self.records = {}
        self.manager = None
        self.prefix = None
        self.error = None
        self._ready = False

    # -- setup ------------------------------------------------------------
    def prepare(self):
        if self._ready:
            return
        if self.hxv4_error():
            raise Hxv4ExtractError(self.error)
        info = self.archive.hxv4
        with open(self.path, 'rb') as f:
            f.seek(info['offset'])
            blob = f.read(info['size'])
        if len(blob) != info['size']:
            raise Hxv4ExtractError('truncated Hxv4 blob in %s' % self.path)

        analysis = self.analysis
        if analysis is None:
            if not self.exe:
                raise Hxv4ExtractError('executable path is required')
            analysis = analyze_executable(self.exe)
            self.analysis = analysis

        key, nonce, nonce0, nonce1, prefix, seed, material = (
            hxv4.recover_keys(analysis, blob, info['kind']))
        self.key = key
        self.prefix = prefix
        inflated = hxv4.decrypt_special_payload(blob, key, nonce)
        records = hxv4.parse_special_index(inflated)
        self.records = dict((record.id, record) for record in records)
        self.manager = hxv4.NativeFilterManager.from_bootstrap(
            analysis.params, material, nonce0)
        self._ready = True

    def hxv4_error(self):
        info = self.archive.hxv4
        if not info or not info.get('size'):
            self.error = 'Hxv4 blob is empty'
            return True
        return False

    # -- lookup -----------------------------------------------------------
    def id_of(self, entry):
        """Return the special-index id for an XP3 entry, or ``None``."""
        name = entry.fake_name if entry.fake_name is not None else entry.name
        return hxv4.fake_id(name)

    def record_of(self, entry, entry_id=None):
        if entry_id is None:
            entry_id = self.id_of(entry)
        if entry_id is None:
            return None
        return self.records.get(entry_id)

    def is_dummy(self, entry, entry_id=None):
        """True for the protected-warning pseudo entry (id 0)."""
        return self.id_of(entry) == 0

    # -- data -------------------------------------------------------------
    def decrypt(self, entry, data):
        """Return the decrypted body of *entry* (``bytes``)."""
        entry_id = self.id_of(entry)
        if entry_id is None:
            return data
        record = self.records.get(entry_id)
        if record is None:
            raise Hxv4ExtractError('no Hxv4 record for entry %d' % entry_id)
        self.prepare()
        buffer = bytearray(data)
        state = self.manager.state_for_entry(record.entry_key,
                                             record.filter_flag)
        state.apply(0, buffer)
        return bytes(buffer)


def open_archive(path, exe=None, analysis=None):
    return Hxv4Archive(path, analysis=analysis, exe=exe)


def default_exe(config):
    game = config.get('game_root')
    exe = config.get('game_exe')
    if not game:
        return None
    return os.path.join(game, exe)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('archive')
    ap.add_argument('--exe', default=None)
    args = ap.parse_args(argv)
    started = time.time()
    archive = open_archive(args.archive, exe=args.exe)
    archive.prepare()
    print('%s: %d records, prefix %r' % (
        os.path.basename(args.archive), len(archive.records), archive.prefix))
    print('prepared in %.1fs' % (time.time() - started))
    return 0


if __name__ == '__main__':
    sys.exit(main())
