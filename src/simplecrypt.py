"""SimpleEncrypt codec used by the game's text storages.

Stored form:  FE FE <mode> FF FE <payload>
  mode 0 : for every wchar with ch >= 0x20 -> ch ^ (((ch & 0xFE) << 8) ^ 1)
  mode 1 : every wchar -> ((ch & 0xAAAA) >> 1) | ((ch & 0x5555) << 1)   (involution)
  mode 2 : payload = int64 compressed, int64 uncompressed, zlib stream

mode 0 is NOT an involution; encryption is derived by brute-forcing the low
16 bits against the decryption formula (the transform is bijective on the
high byte for a fixed low byte).
"""
import struct
import zlib

MAGIC = b'\xfe\xfe'


def _swap_bits(ch):
    return ((ch & 0xAAAA) >> 1) | ((ch & 0x5555) << 1)


def _mode0_dec(ch):
    if ch < 0x20:
        return ch
    return ch ^ (((ch & 0xFE) << 8) ^ 1) & 0xFFFF


_M0 = None


def _mode0_enc(ch):
    global _M0
    if _M0 is None:
        _M0 = {}
        for c in range(0x10000):
            _M0[_mode0_dec(c)] = c
    if ch < 0x20:
        return ch
    return _M0[ch]


def is_wrapped(data):
    return len(data) >= 5 and data[:2] == MAGIC and data[2] in (0, 1, 2) \
        and data[3:5] == b'\xff\xfe'


def decrypt(data):
    """Return plaintext bytes (UTF-16LE with BOM)."""
    if not is_wrapped(data):
        return data
    mode = data[2]
    body = data[5:]
    if mode == 2:
        comp, uncomp = struct.unpack_from('<qq', body, 0)
        text = zlib.decompress(body[16:16 + comp])
        assert len(text) == uncomp, (len(text), uncomp)
        return b'\xff\xfe' + text
    n = len(body) // 2
    vals = struct.unpack_from('<%dH' % n, body, 0)
    out = [_swap_bits(v) if mode == 1 else _mode0_dec(v) for v in vals]
    return b'\xff\xfe' + struct.pack('<%dH' % n, *out)


def encrypt(plain, mode=1, compress_level=9):
    """Inverse of decrypt(); `plain` must start with a UTF-16LE BOM."""
    assert plain[:2] == b'\xff\xfe'
    body = plain[2:]
    if mode == 2:
        comp = zlib.compress(body, compress_level)
        head = MAGIC + b'\x02\xff\xfe' + struct.pack('<qq', len(comp), len(body))
        return head + comp
    n = len(body) // 2
    vals = struct.unpack_from('<%dH' % n, body, 0)
    out = [_swap_bits(v) if mode == 1 else _mode0_enc(v) for v in vals]
    return MAGIC + bytes([mode]) + b'\xff\xfe' + struct.pack('<%dH' % n, *out)


if __name__ == '__main__':
    import sys
    p = sys.argv[1]
    d = open(p, 'rb').read()
    print('wrapped:', is_wrapped(d), 'mode:', d[2] if is_wrapped(d) else None)
    t = decrypt(d)
    print('plain bytes:', len(t))
    print(t[:64])
