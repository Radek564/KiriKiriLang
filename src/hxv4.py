"""Headless HXV4 / CxdecV2 support, in pure Python.

The game archives store an encrypted out-of-line ``Hxv4`` special index and
XOR-filter every ordinary file body.  This module reproduces the parts of the
engine needed to unpack them without the game, without DLL injection and
without the old GUI extractor:

* PE resource analysis of the game executable;
* bres resource decryption and BOOTSTRAP unpacking;
* the bootstrap KDF (Cx sponge + Argon2i + BLAKE2s) that yields the
  XChaCha20-Poly1305 key/nonce protecting the special index;
* the native FilterManager/DripValue interpreter used for file bodies;
* Hxv4 path / filename hashing (SipHash-2-4 and a BLAKE2s variant) so a name
  dictionary can be matched against the decrypted index.

Everything is CPU-only and deterministic.  Optional dependencies:
``pefile`` for resource parsing, ``argon2-cffi`` for Argon2i and
``pycryptodome`` for XChaCha20-Poly1305.
"""
import hashlib
import struct
import zlib

HXV4_SALT = 'xp3hnp'
RESOURCE_SALT_LEN = 0x2000
STARTUP_RESOURCE = 'STARTUP.TJS'
BOOTSTRAP_RESOURCE = 'BOOTSTRAP'
DEFAULT_ARCHIVE_SEED = bytes.fromhex('ceeaaf2cefbeadde')
KNOWN_ARCHIVE_SEED_RVA = 0x81758
XOPT_MARKER = (b'-\x00-\x00x\x00o\x00p\x00t\x00-\x00-\x00n\x00o\x00'
               b'\x00\x00\x00\x00')
OBFUSCATED_CHACHA_CONSTANT = bytes.fromhex(
    '9a878f9e919bdfcccdd29d868b9adf94')


class Hxv4Error(Exception):
    pass


# --------------------------------------------------------------------------
# small helpers

def _le16(data, off):
    return struct.unpack_from('<H', data, off)[0]


def _le32(data, off):
    return struct.unpack_from('<I', data, off)[0]


def _le64(data, off):
    return struct.unpack_from('<Q', data, off)[0]


def _utf16le(text):
    return text.encode('utf-16-le')


def _decode_utf16le(data):
    if len(data) % 2:
        raise Hxv4Error('odd-length UTF-16LE data')
    text = data.decode('utf-16-le')
    return text.rstrip('\0')


def _permutation_valid(data, n):
    if len(data) != n:
        return False
    seen = [False] * n
    for value in data:
        if value >= n or seen[value]:
            return False
        seen[value] = True
    return True


# --------------------------------------------------------------------------
# Cx sponge / Keccak-f1600

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808a,
    0x8000000080008000, 0x000000000000808b, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008a,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000a,
    0x000000008000808b, 0x800000000000008b, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800a, 0x800000008000000a, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39,
        41, 45, 15, 21, 8, 18, 2, 61, 56, 14]
_MASK64 = (1 << 64) - 1


def _rotl64(value, shift):
    shift &= 63
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f1600(a):
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20]
             for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for y in range(5):
            for x in range(5):
                a[x + 5 * y] ^= d[x]
        b = [0] * 25
        for y in range(5):
            for x in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = \
                    _rotl64(a[x + 5 * y], _ROT[x + 5 * y])
        for y in range(5):
            for x in range(5):
                a[x + 5 * y] = b[x + 5 * y] ^ (
                    (~b[(x + 1) % 5 + 5 * y]) &
                    b[(x + 2) % 5 + 5 * y])
        a[0] ^= rc


class CxSponge(object):
    def __init__(self, rate, domain):
        self.rate = rate
        self.domain = domain
        self.position = 0
        self.state = [0] * 25

    def _xor_state(self, offset, data):
        for i, byte in enumerate(data):
            pos = offset + i
            self.state[pos // 8] ^= (byte << (8 * (pos % 8)))

    def absorb(self, data):
        off = 0
        while off < len(data):
            count = min(self.rate - self.position, len(data) - off)
            self._xor_state(self.position, data[off:off + count])
            self.position += count
            off += count
            if self.position == self.rate:
                _keccak_f1600(self.state)
                self.position = 0
        return self

    def squeeze(self, size):
        self._xor_state(self.position, bytes([self.domain]))
        last = self.rate - 1
        self.state[last // 8] ^= 0x80 << (8 * (last % 8))
        _keccak_f1600(self.state)
        out = bytearray()
        while len(out) < size:
            count = min(self.rate, size - len(out))
            for i in range(count):
                out.append((self.state[i // 8] >> (8 * (i % 8))) & 0xff)
            if len(out) < size:
                _keccak_f1600(self.state)
        return bytes(out)


# --------------------------------------------------------------------------
# bootstrap KDF

def derive_key_block(data, size, seed):
    """Custom HXV4 BLAKE2s key block (``sub_100157D0``)."""
    if size == 0 or size > 32 or size % 4:
        raise Hxv4Error('invalid BLAKE2s key-block size')
    words = [0] * (size // 4)
    state = (0x01000193 * (seed ^ 0x811c9dc5)) & 0xffffffff
    for index, byte in enumerate(data):
        value = (byte ^ state) & 0xffffffff
        value ^= value >> 17
        value = (value * 0xed5ad4bb) & 0xffffffff
        value ^= value >> 11
        value = (value * 0xac4c1b51) & 0xffffffff
        value ^= value >> 15
        value = (value * 0x31848bab) & 0xffffffff
        state = (value ^ (value >> 14)) & 0xffffffff
        words[index % len(words)] ^= state
    prehash = b''.join(struct.pack('<I', word) for word in words)
    hasher = hashlib.blake2s(digest_size=size)
    hasher.update(data)
    hasher.update(prehash)
    return hasher.digest()


def derive_bootstrap_shared(params, warning, prefix):
    """Return (key, nonce1, material) from STARTUP/paths + BOOTSTRAP items."""
    from argon2.low_level import hash_secret_raw, Type

    final_bootstrap = _utf16le(prefix) + _utf16le(warning)
    salt = CxSponge(0x90, 0x06).absorb(params).squeeze(16)
    material = hash_secret_raw(
        secret=final_bootstrap, salt=salt, time_cost=3, memory_cost=8,
        parallelism=1, hash_len=64, type=Type.I, version=19)

    key_a = derive_key_block(final_bootstrap, 32, 0)
    nonce1_a = derive_key_block(params, 32, 1)
    key = bytes(a ^ b for a, b in zip(key_a, material))
    nonce1_full = bytes(a ^ b for a, b in zip(nonce1_a, material))
    return key, nonce1_full[:24], material


def derive_nonce0(unique_utf16le, archive_seed):
    unique = derive_key_block(unique_utf16le, 32, 2)
    seed = _le32(archive_seed, 0)
    modifier = derive_key_block(archive_seed, 32, seed)
    full = bytes(a ^ b for a, b in zip(unique, modifier))
    return full[:24]


# --------------------------------------------------------------------------
# bres resource decryption

def _chacha_transform(x, double_rounds):
    def quarter(a, b, c, d):
        x[a] = (x[a] + x[b]) & 0xffffffff
        x[d] = _rotl32(x[d] ^ x[a], 16)
        x[c] = (x[c] + x[d]) & 0xffffffff
        x[b] = _rotl32(x[b] ^ x[c], 12)
        x[a] = (x[a] + x[b]) & 0xffffffff
        x[d] = _rotl32(x[d] ^ x[a], 8)
        x[c] = (x[c] + x[d]) & 0xffffffff
        x[b] = _rotl32(x[b] ^ x[c], 7)

    for _ in range(double_rounds):
        quarter(0, 4, 8, 12)
        quarter(1, 5, 9, 13)
        quarter(2, 6, 10, 14)
        quarter(3, 7, 11, 15)
        quarter(0, 5, 10, 15)
        quarter(1, 6, 11, 12)
        quarter(2, 7, 8, 13)
        quarter(3, 4, 9, 14)
    return x


def _rotl32(value, shift):
    return ((value << shift) | (value >> (32 - shift))) & 0xffffffff


def decrypt_bres_resource(data, path, salt):
    hasher = hashlib.sha3_384()
    hasher.update(_utf16le(path))
    hasher.update(salt)
    material = hasher.digest()

    stored = [0] * 16
    for i in range(4):
        stored[i] = _le32(OBFUSCATED_CHACHA_CONSTANT, i * 4)
    for i in range(8):
        stored[4 + i] = (~_le32(material, i * 4)) & 0xffffffff
    stored[12] = 0xffffffff
    stored[13] = 0xffffffff
    stored[14] = (~_le32(material, 0x20)) & 0xffffffff
    stored[15] = (~_le32(material, 0x24)) & 0xffffffff
    qword1_low = _le32(material, 0x28)
    qword1_high = _le32(material, 0x2c)

    output = bytearray(len(data))
    for block_index, start in enumerate(range(0, len(data), 64)):
        src = data[start:start + 64]
        block_stored = list(stored)
        counter = (~(((qword1_high << 32) |
                      ((qword1_low ^ block_index) & 0xffffffff)) &
                     ((1 << 64) - 1))) & ((1 << 64) - 1)
        block_stored[12] = counter & 0xffffffff
        block_stored[13] = (counter >> 32) & 0xffffffff
        initial = [(~value) & 0xffffffff for value in block_stored]
        transformed = _chacha_transform(list(initial), 4)
        stream = b''.join(
            struct.pack('<I', (transformed[i] + initial[i]) & 0xffffffff)
            for i in range(16))
        for i, byte in enumerate(src):
            output[start + i] = byte ^ stream[i]
    return bytes(output)


# --------------------------------------------------------------------------
# TJS2 string pool

def tjs2_string_constants(startup):
    if len(startup) < 28 or not startup.startswith(b'TJS2100\0'):
        raise Hxv4Error('not TJS2100 bytecode')
    declared = _le32(startup, 8)
    if declared != len(startup) or startup[12:16] != b'DATA':
        raise Hxv4Error('TJS2 size/DATA header mismatch')
    data_size = _le32(startup, 16)
    data_end = 12 + data_size
    if data_size < 8 or data_end > len(startup):
        raise Hxv4Error('TJS2 DATA chunk out of range')
    payload = startup[20:data_end]
    pos = 0

    def read_count():
        nonlocal pos
        value = _le32(payload, pos)
        pos += 4
        return value

    def skip(amount):
        nonlocal pos
        if pos + amount > len(payload):
            raise Hxv4Error('TJS2 pool payload truncated')
        pos += amount

    def align4():
        nonlocal pos
        pos = (pos + 3) & ~3
        if pos > len(payload):
            raise Hxv4Error('TJS2 aligned pool exceeds DATA')

    byte_count = read_count()
    skip(byte_count)
    align4()
    short_count = read_count()
    skip(short_count * 2)
    if short_count & 1:
        skip(2)
    for width in (4, 8, 8):
        count = read_count()
        skip(count * width)
    string_count = read_count()
    if string_count > len(payload) // 4:
        raise Hxv4Error('TJS2 string-pool count implausible')
    strings = []
    for _ in range(string_count):
        length = read_count()
        raw = payload[pos:pos + length * 2]
        if len(raw) != length * 2:
            raise Hxv4Error('TJS2 string truncated')
        strings.append(raw.decode('utf-16-le'))
        pos += length * 2
        if length & 1:
            skip(2)
    octet_count = read_count()
    for _ in range(octet_count):
        skip(read_count())
        align4()
    if pos != len(payload):
        raise Hxv4Error('TJS2 DATA constant-pool trailing bytes')
    return strings


def bootstrap_input_candidates(startup):
    values = set()
    try:
        constants = tjs2_string_constants(startup)
    except Hxv4Error:
        return []
    for value in constants:
        value = value.strip()
        if (4 <= len(value) <= 1024
                and not value.startswith('bres://')
                and not any(ch == '\0' or ord(ch) < 32 for ch in value)):
            values.add(value)

    def key(item):
        lower = item.lower()
        return (not ('rights reserved' in lower),
                not ('copyright' in lower or '(c)' in lower),
                not ('all' in lower),
                len(item), item)

    return sorted(values, key=key)


def find_bootstrap_filter_path(startup):
    matches = set()
    for value in tjs2_string_constants(startup):
        if value.startswith('bres://./') and value.endswith('/bootstrap'):
            path = value[len('bres://./'):-len('/bootstrap')]
            if path and '/' not in path and '\\' not in path:
                matches.add(path)
    if len(matches) != 1:
        raise Hxv4Error('bootstrap filter path is %s'
                        % ('missing' if not matches else 'ambiguous'))
    return matches.pop()


# --------------------------------------------------------------------------
# PE analysis

class PeImage(object):
    """Thin wrapper over pefile that keeps the Rust algorithm's layout."""

    def __init__(self, data, base=0):
        import pefile
        self.data = data
        self.base = base
        self.pe = pefile.PE(data=data, fast_load=False)

    def rva_slice(self, rva, size):
        import pefile
        try:
            offset = self.pe.get_offset_from_rva(rva)
        except pefile.PEFormatError:
            raise Hxv4Error('RVA 0x%x is not mapped' % rva)
        if offset < 0 or offset + size > len(self.data):
            raise Hxv4Error('RVA 0x%x slice is out of range' % rva)
        return self.data[offset:offset + size]

    def resource(self, type_name, name):
        return self._walk_resource(self.pe, type_name, name)

    def _walk_resource(self, pe, type_name, name):
        entries = getattr(pe, 'DIRECTORY_ENTRY_RESOURCE', None)
        if entries is None:
            raise Hxv4Error('PE has no resource directory')
        for type_entry in entries.entries:
            if type_entry.name is not None:
                type_value = type_entry.name.string
                if isinstance(type_value, bytes):
                    type_value = type_value.decode('latin-1')
            else:
                type_value = type_entry.id
            if not self._resource_name_matches(type_value, type_name):
                continue
            for name_entry in type_entry.directory.entries:
                if name_entry.name is not None:
                    name_value = name_entry.name.string
                    if isinstance(name_value, bytes):
                        name_value = name_value.decode('latin-1')
                else:
                    name_value = name_entry.id
                if not self._resource_name_matches(name_value, name):
                    continue
                for lang_entry in name_entry.directory.entries:
                    data_entry = lang_entry.data
                    if data_entry.struct.Size:
                        return pe.get_data(
                            data_entry.struct.OffsetToData,
                            data_entry.struct.Size)
        raise Hxv4Error('resource %r/%r not found' % (type_name, name))

    @staticmethod
    def _resource_name_matches(value, wanted):
        if isinstance(wanted, int):
            return value == wanted
        if isinstance(value, int):
            return False
        return value.upper() == wanted.upper()


def scan_pe_candidates(data):
    """Yield ``(offset, PeImage)`` for every structurally valid PE image."""
    out = []
    pos = 0
    while pos + 0x40 <= len(data):
        at = data.find(b'MZ', pos)
        if at < 0:
            break
        image = data[at:]
        try:
            pe_off = _le32(image, 0x3c)
            if pe_off > 0x200000 or image[pe_off:pe_off + 4] != b'PE\0\0':
                pos = at + 2
                continue
            sections = _le16(image, pe_off + 6)
            if not 0 < sections <= 96:
                pos = at + 2
                continue
            out.append((at, PeImage(image, at)))
            pos = at + 2
        except Exception:
            pos = at + 2
    return out


def find_resource_salt_rva(data):
    """Find the 0x2000-byte bres salt pointer pattern in a PE image."""
    image_base = _pe_image_base(data)
    matches = set()
    pos = 0
    while pos + 0x21 <= len(data):
        if (data[pos] == 0x83 and data[pos + 1] == 0x3d
                and data[pos + 6] == 0x00 and data[pos + 7] == 0x0f
                and data[pos + 8] == 0x86 and data[pos + 13] == 0xc7
                and data[pos + 14] == 0x05 and data[pos + 23] == 0xc7
                and data[pos + 24] == 0x05
                and data[pos + 29:pos + 33] == b'\x00\x20\x00\x00'):
            va = _le32(data, pos + 19)
            if va >= image_base:
                matches.add(va - image_base)
        pos += 1
    if len(matches) != 1:
        raise Hxv4Error('bres salt pointer pattern %s (%d candidates)'
                        % ('not found' if not matches else 'is ambiguous',
                           len(matches)))
    return matches.pop()


def _pe_image_base(data):
    if data[:2] != b'MZ':
        raise Hxv4Error('missing DOS MZ header')
    pe_off = _le32(data, 0x3c)
    optional = pe_off + 24
    if data[pe_off:pe_off + 4] != b'PE\0\0':
        raise Hxv4Error('missing PE signature')
    magic = _le16(data, optional)
    if magic == 0x10b:
        return _le32(data, optional + 0x1c)
    if magic == 0x20b:
        return _le64(data, optional + 0x18)
    raise Hxv4Error('unsupported PE optional-header magic 0x%04x' % magic)


def parse_bres_storage_path(value):
    value = value.strip('\0')
    if not value.startswith('bres://./') or not value.endswith('/'):
        raise Hxv4Error('unexpected startup base-storage resource %r' % value)
    inner = value[len('bres://./'):-1]
    if not inner or '/' in inner or '\\' in inner:
        raise Hxv4Error('invalid bres filter path %r' % inner)
    return inner


def unpack_bootstrap(data):
    if len(data) < 8:
        raise Hxv4Error('decrypted BOOTSTRAP payload truncated')
    packed = _le32(data, 0)
    unpacked = _le32(data, 4)
    if packed != len(data) - 8:
        raise Hxv4Error('BOOTSTRAP packed-size mismatch')
    out = zlib.decompress(data[8:])
    if len(out) != unpacked:
        raise Hxv4Error('BOOTSTRAP unpacked-size mismatch')
    return out


def find_bootstrap_item(data, name, valid):
    tag = name.encode('ascii') + b'\0'
    start = 0
    while True:
        at = data.find(tag, start)
        if at < 0:
            break
        len_at = at + len(tag)
        if len_at + 2 <= len(data):
            length = _le16(data, len_at)
            payload_at = len_at + 2
            if payload_at + length <= len(data):
                payload = data[payload_at:payload_at + length]
                if valid(payload):
                    return payload
        start = at + 1
    raise Hxv4Error('BOOTSTRAP %s item not found' % name)


def _valid_params(data):
    return (len(data) >= 0x16
            and _permutation_valid(data[14:17], 3)
            and _permutation_valid(data[8:14], 6)
            and _permutation_valid(data[0:8], 8))


def _valid_unique(data):
    if len(data) < 2 or len(data) % 2:
        return False
    try:
        text = _decode_utf16le(data)
    except (Hxv4Error, UnicodeDecodeError):
        return False
    return text.startswith('{') and text.endswith('}')


def archive_seed_candidates(bootstrap):
    values = set()
    try:
        pe = PeImage(bootstrap)
        raw = pe.rva_slice(KNOWN_ARCHIVE_SEED_RVA, 8)
        if any(raw):
            values.add(raw)
    except Exception:
        pass
    at = bootstrap.find(XOPT_MARKER)
    if at >= 0:
        seed = bootstrap[at + len(XOPT_MARKER):at + len(XOPT_MARKER) + 8]
        if len(seed) == 8 and any(seed):
            values.add(seed)
    pos = 0
    while pos + 0x50 <= len(bootstrap):
        if (bootstrap[pos] == 0xc7 and bootstrap[pos + 1] == 0x45
                and bootstrap[pos + 7] == 0xc7
                and bootstrap[pos + 8] == 0x45
                and bootstrap[pos + 14:pos + 17] == b'\x0f\x45\xf0'
                and bootstrap[pos + 9] == (bootstrap[pos + 2] + 4) & 0xff
                and _has_unique_kdf_use(bootstrap[pos + 17:pos + 0x50])):
            seed = (bootstrap[pos + 3:pos + 7]
                    + bootstrap[pos + 10:pos + 14])
            if any(seed):
                values.add(seed)
        pos += 1
    values.add(DEFAULT_ARCHIVE_SEED)
    return sorted(values)


def _has_unique_kdf_use(window):
    at = window.find(b'\xff\x36')
    if at < 0:
        return False
    tail = window[at + 2:]
    return b'\x6a\x08' in tail and b'\x56' in tail and b'\xe8' in tail


class ExeAnalysis(object):
    pass


def analyze_executable(path):
    with open(path, 'rb') as f:
        source = f.read()
    candidates = scan_pe_candidates(source)
    if not candidates:
        raise Hxv4Error('no valid PE image found in %s' % path)
    errors = []
    for offset, pe in candidates:
        try:
            analysis = _analyze_pe(path, pe, offset)
            analysis.pe_candidates = len(candidates)
            return analysis
        except Exception as exc:
            errors.append('PE@0x%x: %s' % (offset, exc))
    raise Hxv4Error('no PE yielded valid Kirikiri bres resources: %s'
                    % '; '.join(errors[:8]))


def _analyze_pe(path, pe, offset):
    salt_rva = find_resource_salt_rva(pe.data)
    salt = pe.rva_slice(salt_rva, RESOURCE_SALT_LEN)

    startup_encrypted = pe.resource(10, STARTUP_RESOURCE)       # RT_RCDATA
    text127 = pe.resource('TEXT', 127)
    startup_path = parse_bres_storage_path(_decode_utf16le(text127))
    startup_tjs = decrypt_bres_resource(
        startup_encrypted, startup_path, salt)
    if not startup_tjs.startswith(b'TJS2100\0'):
        raise Hxv4Error('STARTUP.TJS failed TJS2100 validation')

    bootstrap_path = find_bootstrap_filter_path(startup_tjs)
    bootstrap_encrypted = pe.resource(10, BOOTSTRAP_RESOURCE)
    bootstrap_packed = decrypt_bres_resource(
        bootstrap_encrypted, bootstrap_path, salt)
    bootstrap_dll = unpack_bootstrap(bootstrap_packed)
    if not bootstrap_dll.startswith(b'MZ'):
        raise Hxv4Error('decrypted BOOTSTRAP payload is not a PE image')

    warning = find_bootstrap_item(bootstrap_dll, 'WARNING', lambda p: (
        len(p) and all(32 <= b < 127 for b in p)
        and p.startswith(b'Warning!') and b'author' in p))
    warning = warning.decode('ascii')

    out = ExeAnalysis()
    out.exe = path
    out.pe_offset = offset
    out.salt_file_offset = pe.base + pe.pe.get_offset_from_rva(salt_rva)
    out.startup_bres_path = startup_path
    out.bootstrap_bres_path = bootstrap_path
    out.startup_tjs = startup_tjs
    out.bootstrap_dll = bootstrap_dll
    out.bootstrap_prefix_candidates = bootstrap_input_candidates(startup_tjs)
    out.params = find_bootstrap_item(bootstrap_dll, 'PARAMS', _valid_params)
    out.warning = warning
    out.unique_utf16le = find_bootstrap_item(
        bootstrap_dll, 'UNIQUE', _valid_unique)
    out.unique = _decode_utf16le(out.unique_utf16le)
    out.archive_seed_candidates = archive_seed_candidates(bootstrap_dll)
    return out


# --------------------------------------------------------------------------
# XChaCha20-Poly1305 (PyCryptodome) and index parsing

def decrypt_special_plaintext(blob, key, nonce):
    from Crypto.Cipher import ChaCha20_Poly1305
    if len(blob) < 21:
        raise Hxv4Error('Hxv4 special index is too short')
    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    cipher.update(b'')
    tag, ciphertext = blob[:16], blob[16:]
    try:
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError:
        raise Hxv4Error('Hxv4 XChaCha20-Poly1305 authentication failed')


def decrypt_special_payload(blob, key, nonce):
    plain = decrypt_special_plaintext(blob, key, nonce)
    if len(plain) <= 4:
        raise Hxv4Error('Hxv4 special-index plaintext is truncated')
    expected = _le32(plain, 0)
    if expected == 0 or expected > 256 * 1024 * 1024:
        raise Hxv4Error('Hxv4 special-index size is unreasonable')
    inflated = zlib.decompress(plain[4:])
    if len(inflated) != expected:
        raise Hxv4Error('Hxv4 special-index size mismatch')
    return inflated


class HxCursor(object):
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise Hxv4Error('truncated Hxv4 index object')
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self):
        return self.take(1)[0]

    def be_i32(self):
        return struct.unpack('>i', self.take(4))[0]

    def be_i64(self):
        return struct.unpack('>q', self.take(8))[0]


def _read_hx_string(cursor):
    chars = cursor.be_i32()
    if chars < 0:
        raise Hxv4Error('negative Hxv4 string length')
    raw = cursor.take(chars * 2)
    words = [struct.unpack_from('>H', raw, i)[0]
             for i in range(0, len(raw), 2)]
    return struct.pack('<%dH' % len(words), *words).decode(
        'utf-16-le', 'replace')


def read_hx_object(cursor, depth=0):
    if depth > 128:
        raise Hxv4Error('Hxv4 index nesting limit exceeded')
    kind = cursor.u8()
    if kind in (0x00, 0x01):
        return None
    if kind == 0x02:
        return _read_hx_string(cursor)
    if kind == 0x03:
        count = cursor.be_i32()
        if count < 0:
            raise Hxv4Error('negative Hxv4 byte array')
        return cursor.take(count)
    if kind == 0x04:
        return cursor.be_i64()
    if kind == 0x05:
        return struct.unpack('<d', struct.pack('<q', cursor.be_i64()))[0]
    if kind == 0x81:
        count = cursor.be_i32()
        if count < 0 or count > 2000000:
            raise Hxv4Error('Hxv4 array count is unreasonable')
        return [read_hx_object(cursor, depth + 1) for _ in range(count)]
    if kind == 0xC1:
        count = cursor.be_i32()
        if count < 0 or count > 1000000:
            raise Hxv4Error('Hxv4 dictionary count is unreasonable')
        out = {}
        for _ in range(count):
            name = _read_hx_string(cursor)
            out[name] = read_hx_object(cursor, depth + 1)
        return out
    raise Hxv4Error('unknown Hxv4 index object type 0x%02x' % kind)


class IndexEntry(object):
    __slots__ = ('record_index', 'packed', 'archive_slot', 'filter_flag',
                 'id', 'entry_key', 'path_hash', 'name_hash', 'path', 'name')

    def display_path(self):
        path = self.path or self.path_hash.hex().upper()
        name = self.name or self.name_hash.hex().upper()
        if path in ('/', ''):
            return name
        return '%s/%s' % (path.rstrip('/'), name)


def parse_special_index(inflated):
    cursor = HxCursor(inflated)
    root = read_hx_object(cursor)
    if cursor.pos != len(inflated):
        raise Hxv4Error('Hxv4 index deserializer left trailing bytes')
    if not isinstance(root, list) or len(root) % 2:
        raise Hxv4Error('Hxv4 root object is not an even array')
    entries = []
    for pair in zip(root[0::2], root[1::2]):
        path_hash, group = pair
        if not isinstance(path_hash, bytes) or len(path_hash) != 8:
            raise Hxv4Error('Hxv4 path hash is not 8 bytes')
        if not isinstance(group, list) or len(group) % 2:
            raise Hxv4Error('Hxv4 path group is not an even array')
        for name_hash, info in zip(group[0::2], group[1::2]):
            if not isinstance(name_hash, bytes) or len(name_hash) != 32:
                raise Hxv4Error('Hxv4 filename hash is not 32 bytes')
            if (not isinstance(info, list) or len(info) != 2
                    or not isinstance(info[0], int)
                    or not isinstance(info[1], int)):
                raise Hxv4Error('Hxv4 record metadata is malformed')
            packed = info[0]
            if packed < 0:
                raise Hxv4Error('Hxv4 packed locator is negative')
            entry = IndexEntry()
            entry.record_index = len(entries)
            entry.packed = packed
            entry.id = packed & 0xffffffff
            locator = (packed >> 32) & 0xffffffff
            entry.archive_slot = (locator >> 16) & 0xffff
            entry.filter_flag = locator & 0xffff
            entry.entry_key = info[1] & 0xffffffffffffffff
            entry.path_hash = path_hash
            entry.name_hash = name_hash
            entry.path = None
            entry.name = None
            entries.append(entry)
    if not entries:
        raise Hxv4Error('Hxv4 index contains no records')
    entries.sort(key=lambda e: (e.archive_slot, e.id, e.record_index))
    return entries


def recover_keys(analysis, special_blob, descriptor_flags):
    """Try every bootstrap-prefix/seed combination until the index authenticates.

    Returns ``(key, nonce, nonce0, nonce1, prefix, seed, material)``.
    """
    prefixes = list(analysis.bootstrap_prefix_candidates)
    if analysis.unique not in prefixes:
        prefixes.append(analysis.unique)
    if not prefixes:
        raise Hxv4Error('no bootstrap-prefix candidates in STARTUP.TJS')

    slot = descriptor_flags & 1
    errors = []
    for prefix in prefixes[:4096]:
        try:
            key, nonce1, material = derive_bootstrap_shared(
                analysis.params, analysis.warning, prefix)
        except Exception:
            continue
        if slot == 1:
            seed = (analysis.archive_seed_candidates[0]
                    if analysis.archive_seed_candidates
                    else DEFAULT_ARCHIVE_SEED)
            nonce0 = derive_nonce0(analysis.unique_utf16le, seed)
            try:
                decrypt_special_payload(special_blob, key, nonce1)
                return key, nonce1, nonce0, nonce1, prefix, seed, material
            except Hxv4Error as exc:
                errors.append(str(exc))
            continue
        for seed in analysis.archive_seed_candidates:
            nonce0 = derive_nonce0(analysis.unique_utf16le, seed)
            try:
                decrypt_special_payload(special_blob, key, nonce0)
                return key, nonce0, nonce0, nonce1, prefix, seed, material
            except Hxv4Error as exc:
                errors.append(str(exc))
    raise Hxv4Error('derived %d prefix(es) but no key/nonce authenticated '
                    'the Hxv4 Special (%s)'
                    % (len(prefixes), errors[-1] if errors else '-'))


# --------------------------------------------------------------------------
# hashes used by the special index

def _sipround(v):
    v[0] = (v[0] + v[1]) & _MASK64
    v[1] = _rotl64(v[1], 13) ^ v[0]
    v[0] = _rotl64(v[0], 32)
    v[2] = (v[2] + v[3]) & _MASK64
    v[3] = _rotl64(v[3], 16) ^ v[2]
    v[0] = (v[0] + v[3]) & _MASK64
    v[3] = _rotl64(v[3], 21) ^ v[0]
    v[2] = (v[2] + v[1]) & _MASK64
    v[1] = _rotl64(v[1], 17) ^ v[2]
    v[2] = _rotl64(v[2], 32)


def siphash24_zero(data):
    v = [0x736f6d6570736575, 0x646f72616e646f6d,
         0x6c7967656e657261, 0x7465646279746573]
    full = len(data) // 8 * 8
    for off in range(0, full, 8):
        m = _le64(data, off)
        v[3] ^= m
        _sipround(v)
        _sipround(v)
        v[0] ^= m
    tail = data[full:]
    b = (len(data) << 56) & _MASK64
    for i, byte in enumerate(tail):
        b |= byte << (8 * i)
    v[3] ^= b
    _sipround(v)
    _sipround(v)
    v[0] ^= b
    v[2] ^= 0xff
    for _ in range(4):
        _sipround(v)
    return v[0] ^ v[1] ^ v[2] ^ v[3]


def path_hash(path, seed=HXV4_SALT):
    """Domain/directory hash: SipHash-2-4(UTF16LE(path + salt)), byte-swapped."""
    text = '' if path in ('/', '%EmptyString%') else path
    text += seed
    return siphash24_zero(_utf16le(text)).to_bytes(8, 'little')


def domain_hash(path, seed=HXV4_SALT):
    """Alias used by the hash miner (matches ``resource_hash.cpp``)."""
    return path_hash(path, seed)


def fake_name(entry_id):
    """Encode an ``id`` as the synthetic XP3 ``info`` name (0x5000 + id)."""
    if entry_id < 0:
        raise Hxv4Error('negative Hxv4 entry id')
    words = []
    value = entry_id
    while value:
        words.append(0x5000 + (value & 0x3FFF))
        value >>= 14
    if not words:
        words.append(0x5000)
    return ''.join(chr(word) for word in words)


def fake_id(name):
    """Decode an Hxv4 synthetic XP3 ``info`` name, or ``None``."""
    words = [ord(ch) for ch in name or '']
    if not words or len(words) > 5:
        return None
    entry_id = 0
    for i, word in enumerate(words):
        if not (0x5000 <= word <= 0x8FFF):
            return None
        part = word - 0x5000
        shift = i * 14
        if shift >= 64:
            return None
        if shift > 50 and part >= (1 << (64 - shift)):
            return None
        entry_id |= part << shift
    return entry_id if fake_name(entry_id) == name else None


def _blake2s_compress_custom(state, block):
    iv = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
          0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19]
    sigma = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
        [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
        [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
        [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
        [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
        [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
        [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
        [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
        [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    ]

    def g(v, a, b, c, d, x, y):
        v[a] = (v[a] + v[b] + x) & 0xffffffff
        v[d] = _rotr32(v[d] ^ v[a], 16)
        v[c] = (v[c] + v[d]) & 0xffffffff
        v[b] = _rotr32(v[b] ^ v[c], 12)
        v[a] = (v[a] + v[b] + y) & 0xffffffff
        v[d] = _rotr32(v[d] ^ v[a], 8)
        v[c] = (v[c] + v[d]) & 0xffffffff
        v[b] = _rotr32(v[b] ^ v[c], 7)

    v = state[:8] + iv[:]
    v[12] ^= state[8]
    v[13] ^= state[9]
    v[14] ^= state[10]
    v[15] ^= state[11]
    m = [_le32(block, i * 4) for i in range(16)]
    for s in sigma:
        g(v, 0, 4, 8, 12, m[s[0]], m[s[1]])
        g(v, 1, 5, 9, 13, m[s[2]], m[s[3]])
        g(v, 2, 6, 10, 14, m[s[4]], m[s[5]])
        g(v, 3, 7, 11, 15, m[s[6]], m[s[7]])
        g(v, 0, 5, 10, 15, m[s[8]], m[s[9]])
        g(v, 1, 6, 11, 12, m[s[10]], m[s[11]])
        g(v, 2, 7, 8, 13, m[s[12]], m[s[13]])
        g(v, 3, 4, 9, 14, m[s[14]], m[s[15]])
    for i in range(8):
        state[i] ^= v[i] ^ v[i + 8]


def _rotr32(value, shift):
    return ((value >> shift) | (value << (32 - shift))) & 0xffffffff


def filename_hash(name, seed=HXV4_SALT):
    """File-name hash: BLAKE2s-256(UTF16LE(name + salt)), unkeyed."""
    import hashlib
    return hashlib.blake2s(_utf16le(name + seed),
                           digest_size=32).digest()


# --------------------------------------------------------------------------
# native FilterManager
# --------------------------------------------------------------------------
# The VM ops are named after the native handlers documented in hxv4_native.rs.
(OP_ADD_PARAM, OP_ENTER_BLOCK, OP_ADD_SCRATCH, OP_MUL_SCRATCH,
 OP_SCRATCH_MINUS_RESULT, OP_SHL_SCRATCH, OP_SHR_SCRATCH, OP_SUB_SCRATCH,
 OP_BIT_SHUFFLE, OP_LOAD_IMMEDIATE, OP_LOAD_SEED, OP_DEC, OP_INC, OP_NEG,
 OP_NOT, OP_LOAD_TABLE_IMMEDIATE, OP_LOAD_TABLE_MASKED, OP_SUB_PARAM,
 OP_STORE_SCRATCH, OP_XOR_PARAM, OP_STOP) = range(21)


def _splitmix64_final(value):
    value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & _MASK64
    return value ^ (value >> 31)


class _GeneratorRng(object):
    GAMMA = 0x9e3779b97f4a7c15

    def __init__(self, lane, random_type):
        base = (((~lane) & 0xffffffff) << 32) | lane
        self.s0 = _splitmix64_final((base + self.GAMMA) & _MASK64)
        self.s1 = _splitmix64_final(
            (base + self.GAMMA * 2) & _MASK64)
        self.random_type = random_type

    def next_u64(self):
        if self.random_type == 0:
            result = (_rotl64((self.s0 + self.s1) & _MASK64, 17)
                      + self.s0) & _MASK64
            self.s1 ^= self.s0
            self.s0 = _rotl64(self.s0, 49) ^ self.s1 ^ (self.s1 << 21)
            self.s1 = _rotl64(self.s1, 28)
        else:
            result = (_rotl64((self.s0 * 5) & _MASK64, 7) * 9) & _MASK64
            self.s1 ^= self.s0
            self.s0 = _rotl64(self.s0, 24) ^ self.s1 ^ (self.s1 << 16)
            self.s1 = _rotl64(self.s1, 37)
        self.s0 &= _MASK64
        self.s1 &= _MASK64
        return result

    def next_u32(self):
        return self.next_u64() & 0xffffffff


class _Generator(object):
    def __init__(self, params, rng):
        self.params = params
        self.rng = rng
        self.code_len = 0
        self.instructions = []

    def add_len(self, amount):
        if self.code_len + amount > 128:
            return False
        self.code_len += amount
        return True

    def emit(self, op, param=0):
        self.instructions.append((op, param))

    def gen_binary(self, depth):
        if depth <= 1:
            return self.gen_leaf()
        if not self.add_len(1):
            return False
        self.emit(OP_ENTER_BLOCK)
        if self.rng.next_u32() & 1:
            ok = self.gen_binary(depth - 1)
        else:
            ok = self.gen_unary(depth - 1)
        if not ok:
            return False
        if not self.add_len(2):
            return False
        self.emit(OP_STORE_SCRATCH)
        if self.rng.next_u32() & 1:
            ok = self.gen_binary(depth - 1)
        else:
            ok = self.gen_unary(depth - 1)
        if not ok:
            return False
        choice = self.rng.next_u32() % 6
        options = [
            (self.params[8], OP_ADD_SCRATCH, 2),
            (self.params[9], OP_SUB_SCRATCH, 2),
            (self.params[10], OP_SCRATCH_MINUS_RESULT, 4),
            (self.params[11], OP_MUL_SCRATCH, 3),
            (self.params[12], OP_SHL_SCRATCH, 9),
            (self.params[13], OP_SHR_SCRATCH, 9),
        ]
        for selector, op, code_len in options:
            if choice == selector:
                break
        else:
            return False
        if not self.add_len(code_len) or not self.add_len(1):
            return False
        self.emit(op)
        self.emit(OP_STOP)
        return True

    def gen_unary(self, depth):
        if depth <= 1:
            return self.gen_leaf()
        if self.rng.next_u32() & 1:
            ok = self.gen_binary(depth - 1)
        else:
            ok = self.gen_unary(depth - 1)
        if not ok:
            return False
        choice = self.rng.next_u32() & 7
        if choice == self.params[0]:
            if not self.add_len(2):
                return False
            self.emit(OP_NOT)
        elif choice == self.params[1]:
            if not self.add_len(2):
                return False
            self.emit(OP_NEG)
        elif choice == self.params[2]:
            if not self.add_len(1):
                return False
            self.emit(OP_INC)
        elif choice == self.params[3]:
            if not self.add_len(1):
                return False
            self.emit(OP_DEC)
        elif choice == self.params[4]:
            if not self.add_len(21):
                return False
            self.emit(OP_BIT_SHUFFLE, 0xaaaaaaaa)
        elif choice == self.params[5]:
            if not self.add_len(1):
                return False
            imm = self.rng.next_u32()
            if not self.add_len(4):
                return False
            self.emit(OP_XOR_PARAM, imm)
        elif choice == self.params[6]:
            add = bool(self.rng.next_u32() & 1)
            if not self.add_len(1):
                return False
            imm = self.rng.next_u32()
            if not self.add_len(4):
                return False
            self.emit(OP_ADD_PARAM if add else OP_SUB_PARAM, imm)
        elif choice == self.params[7]:
            if not self.add_len(13):
                return False
            self.emit(OP_LOAD_TABLE_MASKED, 1023)
        else:
            return False
        return True

    def gen_leaf(self):
        choice = self.rng.next_u32() % 3
        if choice == self.params[14]:
            if not self.add_len(1):
                return False
            imm = self.rng.next_u32()
            if not self.add_len(4):
                return False
            self.emit(OP_LOAD_IMMEDIATE, imm)
        elif choice == self.params[15]:
            if not self.add_len(2):
                return False
            self.emit(OP_LOAD_SEED)
        elif choice == self.params[16]:
            if not self.add_len(7):
                return False
            index = self.rng.next_u32() & 0x3ff
            if not self.add_len(4):
                return False
            self.emit(OP_LOAD_TABLE_IMMEDIATE, index)
        else:
            return False
        return True


def _build_lane(params, rng):
    for depth in range(5, 0, -1):
        gen = _Generator(params, rng)
        if not gen.add_len(9):
            continue
        if not gen.gen_binary(depth):
            continue
        if not gen.add_len(6):
            continue
        gen.emit(OP_STOP)
        return gen.instructions
    return None


class NativeFilterManager(object):
    def __init__(self, params, table, holder_low=0, holder_high=0):
        if len(params) < 0x16:
            raise Hxv4Error('HXV4 PARAMS truncated')
        if len(table) != 1024:
            raise Hxv4Error('HXV4 control table has %d words' % len(table))
        if not (_permutation_valid(params[0:8], 8)
                and _permutation_valid(params[8:14], 6)
                and _permutation_valid(params[14:17], 3)):
            raise Hxv4Error('HXV4 PARAMS opcode maps are not permutations')
        self.params = params
        self.table = list(table)
        self.control_mode = (params[17] & 1) + 1
        self.random_type = params[17] >> 7
        if self.random_type > 1:
            raise Hxv4Error('HXV4 PARAMS random type unsupported')
        self.mask = _le16(params, 18)
        self.offset = _le16(params, 20)
        self.holder_low = holder_low
        self.holder_high = holder_high
        self.lanes = []
        for lane in range(128):
            program = _build_lane(params, _GeneratorRng(lane, self.random_type))
            if program is None:
                raise Hxv4Error('HXV4 DripValue lane %d failed' % lane)
            self.lanes.append(program)

    @classmethod
    def from_bootstrap(cls, params, material, nonce0):
        if not _valid_params(params):
            raise Hxv4Error('invalid HXV4 PARAMS for FilterManager')
        shake = CxSponge(136, 0x1f).absorb(material).squeeze(0x2000)
        mode = (params[17] & 1) + 1
        words = []
        for index in range(1024):
            at = index * 4
            value = _le32(shake, at)
            if mode == 2:
                value ^= _le32(shake, 0x1000 + at)
            words.append(value)
        return cls(params, words, _le32(nonce0, 0), _le32(nonce0, 4))

    def _eval(self, program, seed):
        pc = 0
        scratch = 0
        result = 0

        def execute():
            nonlocal pc, scratch, result
            while pc < len(program):
                op, param = program[pc]
                pc += 1
                if op == OP_ADD_PARAM:
                    result = (result + param) & 0xffffffff
                elif op == OP_ENTER_BLOCK:
                    saved = scratch
                    nested = result
                    old_result = result
                    result = nested
                    execute()
                    scratch = saved
                elif op == OP_ADD_SCRATCH:
                    result = (result + scratch) & 0xffffffff
                elif op == OP_MUL_SCRATCH:
                    result = (result * scratch) & 0xffffffff
                elif op == OP_SCRATCH_MINUS_RESULT:
                    result = (scratch - result) & 0xffffffff
                elif op == OP_SHL_SCRATCH:
                    result = (result << (scratch & 0x0f)) & 0xffffffff
                elif op == OP_SHR_SCRATCH:
                    result = result >> (scratch & 0x0f)
                elif op == OP_SUB_SCRATCH:
                    result = (result - scratch) & 0xffffffff
                elif op == OP_BIT_SHUFFLE:
                    result = ((2 * (result & (~param & 0xffffffff))) |
                              ((param >> 1) & (result >> 1))) & 0xffffffff
                elif op == OP_LOAD_IMMEDIATE:
                    result = param
                elif op == OP_LOAD_SEED:
                    result = seed
                elif op == OP_DEC:
                    result = (result - 1) & 0xffffffff
                elif op == OP_INC:
                    result = (result + 1) & 0xffffffff
                elif op == OP_NEG:
                    result = (-result) & 0xffffffff
                elif op == OP_NOT:
                    result = (~result) & 0xffffffff
                elif op == OP_LOAD_TABLE_IMMEDIATE:
                    result = self.table[param]
                elif op == OP_LOAD_TABLE_MASKED:
                    result = self.table[param & result]
                elif op == OP_SUB_PARAM:
                    result = (result - param) & 0xffffffff
                elif op == OP_STORE_SCRATCH:
                    scratch = result
                elif op == OP_XOR_PARAM:
                    result ^= param
                elif op == OP_STOP:
                    return
            raise Hxv4Error('HXV4 DripValue program ended without STOP')

        execute()
        return result

    def drip64(self, value):
        lane = value & 0x7f
        seed = value >> 7
        lo = self._eval(self.lanes[lane], seed)
        hi = self._eval(self.lanes[lane], (~seed) & 0xffffffff)
        return (hi << 32) | lo

    def state_for_entry(self, entry_key, local_flag):
        lo = entry_key & 0xffffffff
        hi = (entry_key >> 32) & 0xffffffff
        open_flag = bool(local_flag & 1)
        if not open_flag:
            lo ^= self.holder_low
            hi ^= self.holder_high
        key = (hi << 32) | lo
        left_drip = self.drip64(lo)
        right_drip = self.drip64(hi)
        split = (self.offset + (self.mask & ((key >> 16) & 0xffffffff))) & _MASK64
        prefix = bytearray()
        cursor = (~key) & _MASK64
        for _ in range(2):
            cursor = (~self.drip64(cursor & 0xffffffff)) & _MASK64
            prefix += struct.pack('>Q', cursor)
        return NativeFilterState(
            split, bytes(prefix), _boundary(left_drip), _boundary(right_drip))


def _boundary(value):
    pos0 = (value >> 48) & 0xffff
    pos1 = (value >> 32) & 0xffff
    if pos0 == pos1:
        pos1 = (pos1 + 1) & 0xffff
    xor_byte = value & 0xff
    if xor_byte == 0:
        xor_byte = 0xa5
    return (pos0, pos1, xor_byte,
            (value >> 8) & 0xff, (value >> 16) & 0xff)


class NativeFilterState(object):
    __slots__ = ('split', 'prefix_xor', 'left', 'right')

    def __init__(self, split, prefix_xor, left, right):
        self.split = split
        self.prefix_xor = prefix_xor
        self.left = left
        self.right = right

    def apply(self, logical_offset, data):
        if not data:
            return
        end = logical_offset + len(data)
        start = min(logical_offset, 16)
        stop = min(end, 16)
        for position in range(start, stop):
            data[position - logical_offset] ^= self.prefix_xor[position]

        if logical_offset < self.split:
            left_end = min(end, self.split)
            _xor_range(data, logical_offset, logical_offset, left_end,
                       self.left[2])
        if end > self.split:
            right_start = max(logical_offset, self.split)
            _xor_range(data, logical_offset, right_start, end, self.right[2])

        self._corrections(self.left, True, logical_offset, end, data)
        self._corrections(self.right, False, logical_offset, end, data)

    def _corrections(self, boundary, left_side, start, end, data):
        for position, value in ((boundary[0], boundary[3]),
                                (boundary[1], boundary[4])):
            if value == 0 or position < start or position >= end:
                continue
            if left_side and position >= self.split:
                continue
            if not left_side and position < self.split:
                continue
            data[position - start] ^= value


_XOR_TABLES = {}


def _xor_table(key):
    table = _XOR_TABLES.get(key)
    if table is None:
        table = bytes(byte ^ key for byte in range(256))
        _XOR_TABLES[key] = table
    return table


def _xor_range(data, base, start, end, key):
    if key == 0 or start >= end:
        return
    offset = start - base
    span = end - start
    data[offset:offset + span] = data[offset:offset + span].translate(
        _xor_table(key))
