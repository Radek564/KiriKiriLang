"""Read the TJS literal dumps some tools write for PSB files.

A PSB can be rendered as a TJS literal instead of raw bytes.  The text
looks like::

    (const) %[
      "name" => "a01.txt",
      "scenes" => (const) [
        (const) %[ "firstLine" => 13, "title" => "" ],
      ],
      "empty" => void
    ]

The value types map one-to-one onto :mod:`psb`'s JSON model (dict, list, str,
int, float, bool, None), so a dump can be rebuilt into a PSB with
``psb.build(load_file(path), version=3)``.  This makes a text-only extraction
tree usable for the normal scenario pipeline without the original archive.
"""
import re

_TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*.*?\*/|'
    r'(?P<const>\(const\))|(?P<dict_open>%\[)|(?P<list_open>\[)|'
    r'(?P<close>\])|(?P<arrow>=>)|(?P<comma>,)|'
    r'(?P<string>"(?:[^"\\]|\\.)*")|'
    r'(?P<number>-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)|'
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)',
    re.S,
)

_ESCAPES = {
    'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
    'v': '\v', '0': '\0', '"': '"', "'": "'", '\\': '\\', '/': '/',
}


def _unescape(body):
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            out.append('\\')
            break
        ch = body[i]
        if ch == 'u' and i + 4 < len(body):
            out.append(chr(int(body[i + 1:i + 5], 16)))
            i += 5
        elif ch == 'x' and i + 2 < len(body):
            out.append(chr(int(body[i + 1:i + 3], 16)))
            i += 3
        elif ch in _ESCAPES:
            out.append(_ESCAPES[ch])
            i += 1
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


class _Parser(object):
    def __init__(self, text):
        self.tokens = []
        pos = 0
        while pos < len(text):
            match = _TOKEN.match(text, pos)
            if not match:
                raise ValueError('unexpected character at offset %d: %r'
                                 % (pos, text[pos:pos + 40]))
            pos = match.end()
            kind = match.lastgroup
            if kind is None:  # whitespace or comment
                continue
            self.tokens.append((kind, match.group(kind)))
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self, kind=None):
        token = self.peek()
        if token[0] is None:
            raise ValueError('unexpected end of input')
        if kind and token[0] != kind:
            raise ValueError('expected %s, got %s %r'
                             % (kind, token[0], token[1]))
        self.pos += 1
        return token

    def parse(self):
        value = self.value()
        if self.pos != len(self.tokens):
            raise ValueError('trailing tokens: %r' % (self.tokens[self.pos:],))
        return value

    def value(self):
        kind, text = self.peek()
        if kind == 'const':
            self.take()
            return self.value()
        if kind == 'dict_open':
            return self.dictionary()
        if kind == 'list_open':
            return self.list()
        if kind == 'string':
            self.take()
            return _unescape(text[1:-1])
        if kind == 'number':
            self.take()
            if any(ch in text for ch in '.eE'):
                return float(text)
            return int(text)
        if kind == 'name':
            self.take()
            if text == 'void' or text == 'null':
                return None
            if text == 'true':
                return True
            if text == 'false':
                return False
            return text
        raise ValueError('unexpected token %s %r' % (kind, text))

    def dictionary(self):
        self.take('dict_open')
        out = {}
        while self.peek()[0] != 'close':
            kind, text = self.peek()
            if kind == 'string':
                key = _unescape(self.take()[1][1:-1])
            elif kind == 'name':
                key = self.take()[1]
            else:
                raise ValueError('bad dictionary key %s %r' % (kind, text))
            self.take('arrow')
            out[key] = self.value()
            if self.peek()[0] == 'comma':
                self.take()
        self.take('close')
        return out

    def list(self):
        self.take('list_open')
        out = []
        while self.peek()[0] != 'close':
            out.append(self.value())
            if self.peek()[0] == 'comma':
                self.take()
        self.take('close')
        return out


def loads(text):
    """Parse one TJS literal PSB dump into Python values."""
    return _Parser(text).parse()


def load_file(path):
    with open(path, encoding='utf-8-sig') as handle:
        return loads(handle.read())
