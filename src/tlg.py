"""Native TLG5 image codec (KiriKiri ``.tlg`` / PSB texture chunks).

The patch assets are ``TLG5.0`` images embedded in PSB containers.  This
module decodes them to raw BGRA and can write them back as uncompressed TLG5
blocks (raw blocks are valid and accepted by the game's decoder).

Decoded pixel order is BGRA, matching ``TlgImageConverter``.  Pillow is only
needed for PNG conversion helpers (``to_png`` / ``from_png``); the codec
itself is pure Python.
"""
import struct

MAGIC = b'TLG5.0\x00raw\x1a'
TLG0_MAGIC = b'TLG0.0\x00sds\x1a'


class TlgError(Exception):
    pass


def _meta(data):
    if len(data) < 32:
        raise TlgError('file is too short')
    offset = 0
    if data[:11] == TLG0_MAGIC:
        offset = 0x0F
    if data[offset + 6:offset + 11] != b'\x00raw\x1a':
        raise TlgError('not a raw TLG file')
    sig = data[offset:offset + 6]
    if sig == b'TLG5.0':
        version = 5
    elif sig == b'TLG6.0':
        version = 6
    elif sig == b'XXXYYY':
        version = 5
    elif sig == b'XXXZZZ':
        version = 6
    else:
        raise TlgError('unsupported TLG signature %r' % sig)
    colors = data[offset + 11]
    if version == 6:
        offset += 15
    else:
        if colors not in (3, 4):
            raise TlgError('unsupported TLG5 colour count %d' % colors)
        offset += 12
    width, height = struct.unpack_from('<II', data, offset)
    return version, colors, width, height, offset + 8


def _lzss(inbuf, text, r):
    out = bytearray()
    i = 0
    size = len(inbuf)
    flags = 0
    while i < size:
        flags >>= 1
        if not (flags & 0x100):
            flags = inbuf[i] | 0xFF00
            i += 1
        if flags & 1:
            mpos = inbuf[i] | ((inbuf[i + 1] & 0x0F) << 8)
            mlen = ((inbuf[i + 1] & 0xF0) >> 4) + 3
            i += 2
            if mlen == 18:
                mlen += inbuf[i]
                i += 1
            for _ in range(mlen):
                c = text[mpos]
                out.append(c)
                text[r] = c
                mpos = (mpos + 1) & 0xFFF
                r = (r + 1) & 0xFFF
        else:
            c = inbuf[i]
            i += 1
            out.append(c)
            text[r] = c
            r = (r + 1) & 0xFFF
    return out, r


def decode(data):
    """Decode a TLG5 image.  Returns ``(width, height, bgra_bytes)``."""
    version, colors, width, height, pos = _meta(data)
    if version != 5:
        raise TlgError('TLG6 is not supported')
    if width <= 0 or height <= 0:
        raise TlgError('invalid image size %dx%d' % (width, height))

    blockheight = struct.unpack_from('<I', data, pos)[0]
    if blockheight <= 0:
        raise TlgError('invalid block height')
    blockcount = (height - 1) // blockheight + 1
    pos += 4 + blockcount * 4

    stride = width * 4
    image = bytearray(height * stride)
    text = bytearray(4096)
    planes = [bytearray(blockheight * width + 16) for _ in range(colors)]
    state = 0
    prevline = -1

    for yblk in range(0, height, blockheight):
        for c in range(colors):
            if pos >= len(data):
                raise TlgError('truncated TLG block header')
            mark = data[pos]
            size = struct.unpack_from('<i', data, pos + 1)[0]
            pos += 5
            if size < 0 or pos + size > len(data):
                raise TlgError('truncated TLG block payload')
            chunk = data[pos:pos + size]
            pos += size
            if mark == 0:
                out, state = _lzss(chunk, text, state)
                planes[c][:len(out)] = out
            else:
                planes[c][:size] = chunk

        ylim = min(yblk + blockheight, height)
        pos_in_block = 0
        for y in range(yblk, ylim):
            current = y * stride
            if prevline < 0:
                if colors == 3:
                    pb = pg = pr = 0
                    p0, p1, p2 = planes[0], planes[1], planes[2]
                    for x in range(width):
                        g = p1[pos_in_block + x]
                        b = (p0[pos_in_block + x] + g) & 0xFF
                        r = (p2[pos_in_block + x] + g) & 0xFF
                        pb = (pb + b) & 0xFF
                        pg = (pg + g) & 0xFF
                        pr = (pr + r) & 0xFF
                        o = current + x * 4
                        image[o] = pb
                        image[o + 1] = pg
                        image[o + 2] = pr
                        image[o + 3] = 0xFF
                else:
                    pb = pg = pr = pa = 0
                    p0, p1, p2, p3 = planes[0], planes[1], planes[2], planes[3]
                    for x in range(width):
                        g = p1[pos_in_block + x]
                        b = (p0[pos_in_block + x] + g) & 0xFF
                        r = (p2[pos_in_block + x] + g) & 0xFF
                        a = p3[pos_in_block + x]
                        pb = (pb + b) & 0xFF
                        pg = (pg + g) & 0xFF
                        pr = (pr + r) & 0xFF
                        pa = (pa + a) & 0xFF
                        o = current + x * 4
                        image[o] = pb
                        image[o + 1] = pg
                        image[o + 2] = pr
                        image[o + 3] = pa
            else:
                if colors == 3:
                    pc0 = pc1 = pc2 = 0
                    p0, p1, p2 = planes[0], planes[1], planes[2]
                    for x in range(width):
                        g = p1[pos_in_block + x]
                        b = (p0[pos_in_block + x] + g) & 0xFF
                        r = (p2[pos_in_block + x] + g) & 0xFF
                        pc0 = (pc0 + b) & 0xFF
                        pc1 = (pc1 + g) & 0xFF
                        pc2 = (pc2 + r) & 0xFF
                        o = current + x * 4
                        u = prevline + x * 4
                        image[o] = (pc0 + image[u]) & 0xFF
                        image[o + 1] = (pc1 + image[u + 1]) & 0xFF
                        image[o + 2] = (pc2 + image[u + 2]) & 0xFF
                        image[o + 3] = 0xFF
                else:
                    pc0 = pc1 = pc2 = pc3 = 0
                    p0, p1, p2, p3 = planes[0], planes[1], planes[2], planes[3]
                    for x in range(width):
                        g = p1[pos_in_block + x]
                        b = (p0[pos_in_block + x] + g) & 0xFF
                        r = (p2[pos_in_block + x] + g) & 0xFF
                        a = p3[pos_in_block + x]
                        pc0 = (pc0 + b) & 0xFF
                        pc1 = (pc1 + g) & 0xFF
                        pc2 = (pc2 + r) & 0xFF
                        pc3 = (pc3 + a) & 0xFF
                        o = current + x * 4
                        u = prevline + x * 4
                        image[o] = (pc0 + image[u]) & 0xFF
                        image[o + 1] = (pc1 + image[u + 1]) & 0xFF
                        image[o + 2] = (pc2 + image[u + 2]) & 0xFF
                        image[o + 3] = (pc3 + image[u + 3]) & 0xFF
            pos_in_block += width
            prevline = current

    return width, height, bytes(image)


def decode_file(path):
    with open(path, 'rb') as f:
        return decode(f.read())


def _match_length(hist, j, data, i, distance, limit):
    """Length of a (possibly overlapping) match against ``hist[j:]``."""
    length = 0
    while length < limit:
        if length < distance:
            ref = hist[j + length]
        else:
            ref = data[i + length - distance]
        if ref != data[i + length]:
            break
        length += 1
    return length


class _LzState(object):
    """Decoder-side LZSS dictionary shared by every plane of an image."""

    def __init__(self):
        self.hist = bytearray()
        self.head = {}
        self._r_now = 0

    def encode(self, data):
        out = bytearray()
        tokens = []
        flags = 0
        bit = 0
        i = 0
        size = len(data)

        def flush():
            nonlocal flags, tokens
            if tokens:
                out.append(flags)
                for token in tokens:
                    out.extend(token)
            flags = 0
            tokens = []

        while i < size:
            distance = 0
            length = 0
            if i + 2 < size:
                key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                j = self.head.get(key)
                if j is not None:
                    dist = len(self.hist) - j
                    if 1 <= dist <= 4095:
                        limit = min(273, size - i)
                        run = _match_length(self.hist, j, data, i, dist, limit)
                        if run >= 3:
                            distance = dist
                            length = run

            if length:
                token = bytearray()
                token.append((self._r_now - distance) & 0xFF)
                nibble = length - 3
                if nibble < 15:
                    token.append((nibble << 4) | (((self._r_now - distance) >> 8) & 0x0F))
                else:
                    token.append((15 << 4) | (((self._r_now - distance) >> 8) & 0x0F))
                    token.append(length - 18)
                tokens.append(bytes(token))
                flags |= 1 << bit
                consumed = length
            else:
                tokens.append(bytes((data[i],)))
                consumed = 1

            # update dictionary
            for t in range(consumed):
                pos = len(self.hist)
                self.hist.append(data[i + t])
                if i + t + 2 < size:
                    k = ((data[i + t] << 16) | (data[i + t + 1] << 8)
                         | data[i + t + 2])
                    self.head[k] = pos
            self._r_now = (self._r_now + consumed) & 0xFFF
            i += consumed
            bit += 1
            if bit == 8:
                flush()
                bit = 0
        flush()
        return bytes(out)

    _r_now = 0


def encode(width, height, bgra, colors=4, blockheight=4):
    """Encode BGRA bytes as a TLG5 image (LZSS-compressed blocks)."""
    if colors not in (3, 4):
        raise TlgError('unsupported colour count %d' % colors)
    expected = width * height * 4
    if len(bgra) < expected:
        raise TlgError('pixel buffer is too small')
    if width <= 0 or height <= 0:
        raise TlgError('invalid image size %dx%d' % (width, height))
    if blockheight <= 0:
        raise TlgError('invalid block height')

    stride = width * 4
    blockcount = (height - 1) // blockheight + 1

    # transform each block into the four planar difference streams
    blocks = []
    prev = bytearray(stride)
    for yblk in range(0, height, blockheight):
        ylim = min(yblk + blockheight, height)
        rows = ylim - yblk
        planes = [bytearray(rows * width) for _ in range(colors)]
        for y in range(yblk, ylim):
            base = y * stride
            target = [0, 0, 0, 0]
            row = (y - yblk) * width
            for x in range(width):
                o = base + x * 4
                px = x * 4
                delta = [0, 0, 0, 0]
                for c in range(4):
                    value = (bgra[o + c] - prev[px + c]) & 0xFF
                    delta[c] = (value - target[c]) & 0xFF
                    target[c] = value
                planes[1][row + x] = delta[1]
                planes[0][row + x] = (delta[0] - delta[1]) & 0xFF
                planes[2][row + x] = (delta[2] - delta[1]) & 0xFF
                if colors == 4:
                    planes[3][row + x] = delta[3]
            prev[0:stride] = bgra[base:base + stride]
        blocks.append(planes)

    state = _LzState()
    streams = []
    sizes = []
    for planes in blocks:
        encoded = [state.encode(plane) for plane in planes]
        streams.append(encoded)
        sizes.append(sum(5 + len(chunk) for chunk in encoded))

    out = bytearray()
    out += MAGIC
    out.append(colors)
    out += struct.pack('<III', width, height, blockheight)
    for value in sizes:
        out += struct.pack('<I', value)
    for encoded in streams:
        for chunk in encoded:
            out.append(0)  # LZSS-compressed block
            out += struct.pack('<i', len(chunk))
            out += chunk
    return bytes(out)


def encode_file(path, width, height, bgra, colors=4):
    data = encode(width, height, bgra, colors)
    with open(path, 'wb') as f:
        f.write(data)
    return len(data)


# ---------------------------------------------------------------------------
# Pillow helpers

def to_png(tlg_bytes, out_path):
    from PIL import Image
    width, height, bgra = decode(tlg_bytes)
    image = Image.frombytes('RGBA', (width, height), bgra, 'raw', 'BGRA')
    image.save(out_path)
    return width, height


def from_png(path, colors=4):
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert('RGBA')
        width, height = image.size
        bgra = image.tobytes('raw', 'BGRA')
    return width, height, encode(width, height, bgra, colors)
