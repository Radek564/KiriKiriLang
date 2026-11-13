"""Native PSB (KiriKiri) reader and writer.

This module replaces the bundled ``PsbDecompile.exe`` / ``PsBuild.exe`` for
everything the patch toolset needs.  It understands the PSB v2/v3/v4 layout
used by this game:

* the header + adler32 checksum;
* the names prefix-trie (``Charset`` / ``NamesData`` / ``NameIndexes``);
* the object stream (dictionary, list, number, float, string, resource);
* the string table and the chunk (resource) table.

The Python representation used for JSON is the same as a decompiled
JSON, so existing caches/workbooks keep working:

    PSB                     Python
    ----------------------  --------------------------------------------
    Objects                 dict (str -> value)
    List                    list
    String                  str
    Number / Float          int / float
    Bool / Null             bool / None
    Resource                "#resource#<n>" / "#resource@<n>" (extra)
    Array (uint[])          list of ints

``build()`` writes lists back as ``List`` objects (this module does the same when
compiling from JSON), which the game accepts.  The serializer does not
de-duplicate object contents, so output is larger than an optimized
build, but it is simpler and deterministic.
"""
import struct

SIGNATURE = b'PSB\x00'
RESOURCE_PREFIX = '#resource#'
EXTRA_RESOURCE_PREFIX = '#resource@'
NUMBER_PREFIX = '#0x'

NONE = 0x00
NULL = 0x01
FALSE = 0x02
TRUE = 0x03
NUMBER_N0 = 0x04
NUMBER_N8 = 0x0C
ARRAY_N1 = 0x0D
ARRAY_N8 = 0x14
STRING_N1 = 0x15
STRING_N4 = 0x18
RESOURCE_N1 = 0x19
RESOURCE_N4 = 0x1C
FLOAT0 = 0x1D
FLOAT = 0x1E
DOUBLE = 0x1F
LIST = 0x20
OBJECTS = 0x21
EXTRA_N1 = 0x22
EXTRA_N4 = 0x25

_HEADER_LEN = {2: 40, 3: 44, 4: 56}


class PsbError(Exception):
    pass


# ---------------------------------------------------------------------------
# integer helpers


def _signed_size32(value):
    for size in (1, 2, 3, 4):
        lo = -(1 << (8 * size - 1))
        hi = (1 << (8 * size - 1)) - 1
        if lo <= value <= hi:
            return size
    raise PsbError('integer %d does not fit in 32 bits' % value)


def _signed_size64(value):
    for size in range(1, 9):
        lo = -(1 << (8 * size - 1))
        hi = (1 << (8 * size - 1)) - 1
        if lo <= value <= hi:
            return size
    raise PsbError('integer %d does not fit in 64 bits' % value)


def _uint_size(value):
    if value == 0:
        return 1
    return (value.bit_length() + 7) // 8


def _set_list(values, index, value):
    while len(values) <= index:
        values.append(0)
    values[index] = value


# ---------------------------------------------------------------------------
# compact arrays (uint[] sections)


def _read_array(data, pos):
    if pos >= len(data):
        raise PsbError('truncated array header')
    type_byte = data[pos]
    pos += 1
    n = type_byte - ARRAY_N1 + 1
    if not 1 <= n <= 8:
        raise PsbError('bad array count size %d at 0x%x' % (n, pos - 1))
    count = int.from_bytes(data[pos:pos + n], 'little')
    pos += n
    if pos >= len(data):
        raise PsbError('truncated array entry-size byte')
    entry = data[pos] - NUMBER_N8
    pos += 1
    if not 0 <= entry <= 8:
        raise PsbError('bad array entry size %d' % entry)
    values = []
    for _ in range(count):
        raw = data[pos:pos + entry]
        if len(raw) != entry:
            raise PsbError('truncated array payload')
        values.append(int.from_bytes(raw, 'little'))
        pos += entry
    return values, pos


def _write_array(values):
    """Serialize a uint[]."""
    count = len(values)
    size = _signed_size32(count)
    out = bytearray()
    out.append(ARRAY_N1 + size - 1)
    out += count.to_bytes(size, 'little')
    entry = 0
    for value in values:
        entry = max(entry, _uint_size(value))
    if not values:
        entry = 0
    out.append(NUMBER_N8 + entry)
    for value in values:
        out += value.to_bytes(entry, 'little')
    return bytes(out)


# ---------------------------------------------------------------------------
# prefix trie


class _Node(object):
    __slots__ = ('char', 'parent', 'children', 'nid', 'begin', 'end')

    def __init__(self, char=0, parent=None):
        self.char = char
        self.parent = parent
        self.children = {}
        self.nid = 0
        self.begin = 0
        self.end = 0

    def min_char(self):
        return min(self.children) if self.children else 0

    def max_char(self):
        return max(self.children) if self.children else 0


def _path_text(node):
    chars = []
    prev = node.parent
    while prev is not None and prev.parent is not None:
        chars.append(prev.char & 0xFF)
        prev = prev.parent
    return bytes(reversed(chars)).decode('utf-8', 'replace')


def build_name_trie(names):
    """Return (name_indexes, tree, charset) for a list of unique names."""
    root = _Node()
    for name in names:
        node = root
        for byte in name.encode('utf-8'):
            child = node.children.get(byte)
            if child is None:
                child = _Node(byte, node)
                node.children[byte] = child
            node = child
        term = node.children.get(0)
        if term is None:
            term = _Node(0, node)
            node.children[0] = term

    charset = [1]
    tree = []
    results = {}

    def make_tree(node):
        if node.parent is root:
            node.nid = node.char + charset[0]
        else:
            node.nid = node.char - node.parent.min_char() + node.parent.begin
        _set_list(tree, node.nid, node.parent.nid)

    def make_offset(node):
        if node.char == 0:
            text = _path_text(node)
            index = names.index(text)
            _set_list(charset, node.nid, index)
            return
        if not node.children:
            return
        max_char = node.max_char()
        min_char = node.min_char()
        count = max_char - min_char
        pos = len(tree)
        if pos <= max_char:
            _set_list(tree, max_char, 0)
            pos = len(tree)
        end = pos + count
        offset = pos - min_char
        _set_list(tree, end, 0)
        _set_list(charset, node.nid, offset)
        node.begin = pos
        node.end = end

    def make_branch(node):
        ordered = [node.children[key] for key in sorted(node.children)]
        for child in ordered:
            make_tree(child)
        for child in ordered:
            make_offset(child)
        for child in ordered:
            make_branch(child)
        for key, child in node.children.items():
            if key == 0:
                results[_path_text(child)] = child.nid

    make_branch(root)
    indexes = [results[name] for name in names]
    return indexes, tree, charset


def load_name_trie(indexes, tree, charset):
    names = []
    for index in indexes:
        if index >= len(tree):
            raise PsbError('name index %d outside trie' % index)
        chars = []
        node = tree[index]
        while node != 0:
            if node >= len(tree):
                raise PsbError('name trie node %d outside tree' % node)
            code = tree[node]
            if code >= len(charset):
                raise PsbError('name trie code %d outside charset' % code)
            delta = charset[code]
            chars.append((node - delta) & 0xFF)
            node = code
        names.append(bytes(reversed(chars)).decode('utf-8', 'replace'))
    return names


# ---------------------------------------------------------------------------
# header


class Header(object):
    def __init__(self):
        self.signature = SIGNATURE
        self.version = 3
        self.header_encrypt = 0
        self.header_length = 44
        self.offset_names = 0
        self.offset_strings = 0
        self.offset_strings_data = 0
        self.offset_chunk_offsets = 0
        self.offset_chunk_lengths = 0
        self.offset_chunk_data = 0
        self.offset_entries = 0
        self.checksum = 0
        self.offset_extra_chunk_offsets = 0
        self.offset_extra_chunk_lengths = 0
        self.offset_extra_chunk_data = 0

    @classmethod
    def read(cls, data):
        if len(data) < 12 or data[:4].upper() != SIGNATURE:
            raise PsbError('not a PSB file')
        header = cls()
        header.signature = data[:4]
        header.version, header.header_encrypt = struct.unpack_from('<HH', data, 4)
        if header.version not in (2, 3, 4):
            raise PsbError('unsupported PSB version %d' % header.version)
        (header.header_length, header.offset_names, header.offset_strings,
         header.offset_strings_data, header.offset_chunk_offsets,
         header.offset_chunk_lengths, header.offset_chunk_data,
         header.offset_entries) = struct.unpack_from('<8I', data, 8)
        pos = 40
        if header.version > 2:
            header.checksum = struct.unpack_from('<I', data, pos)[0]
            pos += 4
        if header.version > 3:
            (header.offset_extra_chunk_offsets,
             header.offset_extra_chunk_lengths,
             header.offset_extra_chunk_data) = struct.unpack_from('<3I', data, pos)
        if header.offset_names != _HEADER_LEN[header.version]:
            raise PsbError('PSB header is encrypted or malformed')
        if header.offset_names > len(data):
            raise PsbError('PSB names offset outside file')
        return header

    def checksum_value(self):
        fields = [self.header_length, self.offset_names, self.offset_strings,
                  self.offset_strings_data, self.offset_chunk_offsets,
                  self.offset_chunk_lengths, self.offset_chunk_data,
                  self.offset_entries]
        if self.version > 3:
            fields += [self.offset_extra_chunk_offsets,
                       self.offset_extra_chunk_lengths,
                       self.offset_extra_chunk_data]
        import zlib
        payload = b''.join(struct.pack('<I', value) for value in fields)
        return zlib.adler32(payload) & 0xFFFFFFFF

    def to_bytes(self):
        out = bytearray()
        out += self.signature
        out += struct.pack('<HH', self.version, self.header_encrypt)
        out += struct.pack('<8I', self.header_length, self.offset_names,
                           self.offset_strings, self.offset_strings_data,
                           self.offset_chunk_offsets,
                           self.offset_chunk_lengths, self.offset_chunk_data,
                           self.offset_entries)
        if self.version > 2:
            out += struct.pack('<I', self.checksum_value())
        if self.version > 3:
            out += struct.pack('<3I', self.offset_extra_chunk_offsets,
                               self.offset_extra_chunk_lengths,
                               self.offset_extra_chunk_data)
        return bytes(out)


# ---------------------------------------------------------------------------
# object stream


class _Reader(object):
    def __init__(self, data, names, strings, resources, extra_resources):
        self.data = data
        self.names = names
        self.strings = strings
        self.resources = resources
        self.extra_resources = extra_resources

    def unpack(self, pos):
        if pos >= len(self.data):
            raise PsbError('truncated object stream at 0x%x' % pos)
        type_byte = self.data[pos]
        pos += 1

        if type_byte == NONE or type_byte == NULL:
            return None, pos
        if type_byte == FALSE:
            return False, pos
        if type_byte == TRUE:
            return True, pos
        if type_byte == NUMBER_N0:
            return 0, pos
        if NUMBER_N0 < type_byte <= NUMBER_N8:
            size = type_byte - NUMBER_N0
            raw = self.data[pos:pos + size]
            if len(raw) != size:
                raise PsbError('truncated number')
            return int.from_bytes(raw, 'little', signed=True), pos + size
        if ARRAY_N1 <= type_byte <= ARRAY_N8:
            values, pos = _read_array_at(self.data, pos, type_byte)
            return values, pos
        if STRING_N1 <= type_byte <= STRING_N4:
            size = type_byte - STRING_N1 + 1
            index = int.from_bytes(self.data[pos:pos + size], 'little')
            pos += size
            if index >= len(self.strings):
                raise PsbError('string index %d out of range' % index)
            return self.strings[index], pos
        if RESOURCE_N1 <= type_byte <= RESOURCE_N4:
            size = type_byte - RESOURCE_N1 + 1
            index = int.from_bytes(self.data[pos:pos + size], 'little')
            pos += size
            return RESOURCE_PREFIX + str(index), pos
        if EXTRA_N1 <= type_byte <= EXTRA_N4:
            size = type_byte - EXTRA_N1 + 1
            index = int.from_bytes(self.data[pos:pos + size], 'little')
            pos += size
            return EXTRA_RESOURCE_PREFIX + str(index), pos
        if type_byte == FLOAT0:
            return 0.0, pos
        if type_byte == FLOAT:
            raw = self.data[pos:pos + 4]
            if len(raw) != 4:
                raise PsbError('truncated float')
            value = struct.unpack('<f', raw)[0]
            if value != value or value in (float('inf'), float('-inf')):
                return NUMBER_PREFIX + '%08Xf' % struct.unpack('<I', raw)[0], pos + 4
            return value, pos + 4
        if type_byte == DOUBLE:
            raw = self.data[pos:pos + 8]
            if len(raw) != 8:
                raise PsbError('truncated double')
            value = struct.unpack('<d', raw)[0]
            if value != value or value in (float('inf'), float('-inf')):
                return NUMBER_PREFIX + '%016Xd' % struct.unpack('<Q', raw)[0], pos + 8
            return value, pos + 8
        if type_byte == LIST:
            offsets, pos = _read_array(self.data, pos)
            base = pos
            values = []
            for offset in offsets:
                value, _ = self.unpack(base + offset)
                values.append(value)
            return values, base
        if type_byte == OBJECTS:
            name_indexes, pos = _read_array(self.data, pos)
            offsets, pos = _read_array(self.data, pos)
            base = pos
            out = {}
            for name_index, offset in zip(name_indexes, offsets):
                if name_index >= len(self.names):
                    raise PsbError('name index %d out of range' % name_index)
                value, _ = self.unpack(base + offset)
                out[self.names[name_index]] = value
            return out, base

        raise PsbError('unknown PSB object type 0x%02X at 0x%x'
                       % (type_byte, pos - 1))


def _read_array_at(data, pos, type_byte):
    n = type_byte - ARRAY_N1 + 1
    count = int.from_bytes(data[pos:pos + n], 'little')
    pos += n
    if pos >= len(data):
        raise PsbError('truncated array entry-size byte')
    entry = data[pos] - NUMBER_N8
    pos += 1
    if not 0 <= entry <= 8:
        raise PsbError('bad array entry size %d' % entry)
    values = []
    for _ in range(count):
        raw = data[pos:pos + entry]
        if len(raw) != entry:
            raise PsbError('truncated array payload')
        values.append(int.from_bytes(raw, 'little'))
        pos += entry
    return values, pos


# ---------------------------------------------------------------------------
# public container


class PsbFile(object):
    def __init__(self):
        self.header = Header()
        self.names = []
        self.strings = []
        self.resources = []
        self.extra_resources = []
        self.root = None

    @property
    def version(self):
        return self.header.version

    def to_json(self):
        return self.root


def _read_null_terminated(data, pos):
    end = data.find(b'\x00', pos)
    if end < 0:
        raise PsbError('unterminated string at 0x%x' % pos)
    return data[pos:end].decode('utf-8', 'replace'), end + 1


def load(data):
    """Parse a PSB byte string into a :class:`PsbFile`."""
    file = PsbFile()
    header = Header.read(data)
    file.header = header

    # names (v2+ trie)
    pos = header.offset_names
    charset, pos = _read_array(data, pos)
    tree, pos = _read_array(data, pos)
    name_indexes, pos = _read_array(data, pos)
    file.names = load_name_trie(name_indexes, tree, charset)

    # string offsets table + string data
    string_offsets, pos = _read_array(data, header.offset_strings)
    file.strings = []
    for offset in string_offsets:
        text, _ = _read_null_terminated(data, header.offset_strings_data + offset)
        file.strings.append(text)

    # resources
    chunk_offsets, _ = _read_array(data, header.offset_chunk_offsets)
    chunk_lengths, _ = _read_array(data, header.offset_chunk_lengths)
    file.resources = []
    for offset, length in zip(chunk_offsets, chunk_lengths):
        start = header.offset_chunk_data + offset
        blob = data[start:start + length]
        if len(blob) != length:
            raise PsbError('truncated resource chunk')
        file.resources.append(blob)
    file.extra_resources = []
    if header.version >= 4:
        extra_offsets, _ = _read_array(data, header.offset_extra_chunk_offsets)
        extra_lengths, _ = _read_array(data, header.offset_extra_chunk_lengths)
        for offset, length in zip(extra_offsets, extra_lengths):
            start = header.offset_extra_chunk_data + offset
            file.extra_resources.append(data[start:start + length])

    reader = _Reader(data, file.names, file.strings, file.resources,
                     file.extra_resources)
    root, _ = reader.unpack(header.offset_entries)
    file.root = root
    return file


def load_file(path):
    with open(path, 'rb') as f:
        return load(f.read())


# ---------------------------------------------------------------------------
# serializer


class _Writer(object):
    def __init__(self, name_indexes):
        self.name_indexes = name_indexes
        self.names = []
        self.strings = []
        self.string_indexes = {}
        self.resources = []
        self.resource_indexes = {}
        self.extra_resources = []
        self.extra_indexes = {}

    # --- tables -----------------------------------------------------------

    def add_name(self, name):
        index = self.name_indexes.get(name)
        if index is None:
            raise PsbError('name %r is missing from the name table' % name)
        return index

    def add_string(self, value):
        index = self.string_indexes.get(value)
        if index is None:
            index = len(self.strings)
            self.string_indexes[value] = index
            self.strings.append(value)
        return index

    def add_resource(self, index, extra=False):
        table = self.extra_indexes if extra else self.resource_indexes
        if index not in table:
            table[index] = len(self.extra_resources if extra else self.resources)
            (self.extra_resources if extra else self.resources).append(index)
        return table[index]

    # --- numbers ----------------------------------------------------------

    @staticmethod
    def emit_number(value, out):
        if value == 0:
            out.append(NUMBER_N0)
            return
        if -(1 << 31) <= value <= (1 << 31) - 1:
            size = _signed_size32(value)
        else:
            size = _signed_size64(value)
        out.append(NUMBER_N0 + size)
        out += value.to_bytes(size, 'little', signed=True)

    @staticmethod
    def emit_float(value, out):
        packed = None
        try:
            packed = struct.pack('<f', value)
        except (OverflowError, struct.error):
            packed = None
        if value == 0.0:
            out.append(FLOAT0)
            return
        if packed is not None:
            single = struct.unpack('<f', packed)[0]
            # Only use the compact FLOAT form when it round-trips exactly.
            # The old 1e-8 tolerance silently changed double-precision seeds
            # and animation values in rebuilt scenarios.
            if single == single and single == value:
                out.append(FLOAT)
                out += packed
                return
        out.append(DOUBLE)
        out += struct.pack('<d', value)

    # --- objects ----------------------------------------------------------

    def emit(self, value, out):
        if value is None:
            out.append(NULL)
        elif value is True:
            out.append(TRUE)
        elif value is False:
            out.append(FALSE)
        elif isinstance(value, int):
            self.emit_number(value, out)
        elif isinstance(value, float):
            self.emit_float(value, out)
        elif isinstance(value, str):
            self.emit_string(value, out)
        elif isinstance(value, (bytes, bytearray)):
            raise PsbError('raw bytes are not a PSB value; use "#resource#N"')
        elif isinstance(value, (list, tuple)):
            self.emit_list(value, out)
        elif isinstance(value, dict):
            self.emit_dict(value, out)
        else:
            raise PsbError('cannot serialize %r' % type(value).__name__)

    def emit_string(self, value, out):
        if value.startswith(NUMBER_PREFIX):
            marker = value[len(NUMBER_PREFIX):]
            if marker.endswith('f') and len(marker) == 9:
                out.append(FLOAT)
                out += struct.pack('<I', int(marker[:-1], 16))
                return
            if marker.endswith('d') and len(marker) == 17:
                out.append(DOUBLE)
                out += struct.pack('<Q', int(marker[:-1], 16))
                return
            if marker and all(c in '0123456789abcdefABCDEF' for c in marker):
                return self.emit_number(int(marker, 16), out)
        if value.startswith(EXTRA_RESOURCE_PREFIX):
            index = int(value[len(EXTRA_RESOURCE_PREFIX):])
            self.emit_resource(index, out, extra=True)
            return
        if value.startswith(RESOURCE_PREFIX):
            index = int(value[len(RESOURCE_PREFIX):])
            self.emit_resource(index, out, extra=False)
            return
        index = self.add_string(value)
        size = _uint_size(index)
        out.append(STRING_N1 + size - 1)
        out += index.to_bytes(size, 'little')

    def emit_resource(self, index, out, extra=False):
        slot = self.add_resource(index, extra)
        size = _uint_size(slot)
        base = EXTRA_N1 if extra else RESOURCE_N1
        out.append(base + size - 1)
        out += slot.to_bytes(size, 'little')

    def emit_list(self, values, out):
        out.append(LIST)
        offsets = []
        body = bytearray()
        for value in values:
            offsets.append(len(body))
            self.emit(value, body)
        out += _write_array(offsets)
        out += body

    def emit_dict(self, mapping, out):
        out.append(OBJECTS)
        pairs = []
        body = bytearray()
        for key, value in mapping.items():
            index = self.add_name(key)
            pairs.append((index, len(body)))
            self.emit(value, body)
        pairs.sort(key=lambda item: item[0])
        out += _write_array([item[0] for item in pairs])
        out += _write_array([item[1] for item in pairs])
        out += body


def _collect_names(value, out, seen):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in seen:
                seen.add(key)
                out.append(key)
            _collect_names(child, out, seen)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_names(child, out, seen)


def build(root, version=3, resources=None, extra_resources=None):
    """Serialize *root* to PSB bytes.

    *resources* / *extra_resources* are lists of raw byte chunks referenced by
    ``"#resource#N"`` / ``"#resource@N"`` placeholders in *root*.
    """
    if version not in (2, 3, 4):
        raise PsbError('unsupported PSB version %d' % version)
    if not isinstance(root, dict):
        raise PsbError('PSB root must be a dictionary')

    names = []
    _collect_names(root, names, set())
    name_indexes, tree, charset = build_name_trie(names)
    index_map = {name: i for i, name in enumerate(names)}

    writer = _Writer(index_map)
    objects = bytearray()
    writer.emit_dict(root, objects)

    resource_data = bytearray()
    resource_offsets = []
    resource_lengths = []
    for index in writer.resources:
        blob = _lookup_resource(resources, index)
        resource_offsets.append(len(resource_data))
        resource_lengths.append(len(blob))
        resource_data += blob

    extra_data = bytearray()
    extra_offsets = []
    extra_lengths = []
    for index in writer.extra_resources:
        blob = _lookup_resource(extra_resources, index)
        extra_offsets.append(len(extra_data))
        extra_lengths.append(len(blob))
        extra_data += blob

    name_blob = (b''.join(_write_array(values) for values in
                          (charset, tree, name_indexes)))

    string_offsets = []
    string_data = bytearray()
    for value in writer.strings:
        string_offsets.append(len(string_data))
        string_data += value.encode('utf-8') + b'\x00'

    header_len = _HEADER_LEN[version]
    out = bytearray(header_len)
    offset_names = len(out)
    out += name_blob
    offset_entries = len(out)
    out += objects
    offset_strings = len(out)
    out += _write_array(string_offsets)
    offset_strings_data = len(out)
    out += string_data
    offset_chunk_offsets = len(out)
    out += _write_array(resource_offsets)
    offset_chunk_lengths = len(out)
    out += _write_array(resource_lengths)
    offset_chunk_data = len(out)
    out += resource_data

    extra_fields = (0, 0, 0)
    if version >= 4:
        extra_offsets_offset = len(out)
        out += _write_array(extra_offsets)
        extra_lengths_offset = len(out)
        out += _write_array(extra_lengths)
        extra_data_offset = len(out)
        out += extra_data
        extra_fields = (extra_offsets_offset, extra_lengths_offset,
                        extra_data_offset)

    header = Header()
    header.version = version
    header.header_length = header_len
    header.offset_names = offset_names
    header.offset_entries = offset_entries
    header.offset_strings = offset_strings
    header.offset_strings_data = offset_strings_data
    header.offset_chunk_offsets = offset_chunk_offsets
    header.offset_chunk_lengths = offset_chunk_lengths
    header.offset_chunk_data = offset_chunk_data
    if version >= 4:
        (header.offset_extra_chunk_offsets,
         header.offset_extra_chunk_lengths,
         header.offset_extra_chunk_data) = extra_fields

    out[:header_len] = header.to_bytes()
    return bytes(out)


def _lookup_resource(table, index):
    if table is None or index >= len(table):
        raise PsbError('resource %d is not supplied' % index)
    blob = table[index]
    return bytes(blob)


def build_file(path, root, **kwargs):
    data = build(root, **kwargs)
    with open(path, 'wb') as f:
        f.write(data)
    return len(data)
