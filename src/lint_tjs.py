"""Lint the TJS2 templates for constructs the older engines reject.

Some engines are stricter than the ones the templates were
written against:

* ``null`` and ``void`` are not literals -- ``void`` is fine as a value,
  but ``null`` is a syntax error;
* a statement ends at the newline, so an expression continued on the next
  line is a syntax error;
* ``setter``, ``getter``, ``property``, ``int``, ``string`` ... are
  reserved words and cannot be used as identifiers.

Run:  py -X utf8 src/lint_tjs.py [src/templates]
"""
import re
import sys
from pathlib import Path

RESERVED = set("""
break case catch class const continue debugger default delete do else enum
export extends false finally for function getter global goto if import in
incontextof Infinity instanceof int invalidate isvalid NaN new null octet
private property protected public real return setter static string super
switch synchronized this throw true try typeof var void while with
""".split())

IDENT = r'[A-Za-z_][A-Za-z0-9_]*'
PATTERNS = [
    (re.compile(r'\b(?:var|const)\s+(%s)' % IDENT), 'declaration'),
    (re.compile(r'\bfunction\s+(%s)' % IDENT), 'function name'),
    (re.compile(r'\bfunction\s*%s?\s*\(([^)]*)\)' % IDENT), 'parameter'),
    (re.compile(r'\bcatch\s*\(\s*(%s)' % IDENT), 'catch variable'),
    (re.compile(r'(?<![.\w])(%s)\s*(?:=(?!=)|\+=|-=|\*=|/=)' % IDENT), 'assignment'),
]
CONTINUATION = re.compile(r'^\s*(?:\.|\+|-|\*|/|%|&&|\|\||\?)')
# A statement that continues on the next line: the older engines end the
# statement at the newline.
NULL_LITERAL = re.compile(r'(?<![\w.])null(?![\w.])')


def _read(path):
    """Read a TJS file.

    The templates are UTF-8, the generated ones UTF-16 and, when they come
    from a title that stores its scripts encrypted, wrapped in
    SimpleEncrypt.
    """
    raw = path.read_bytes()
    try:
        import simplecrypt
        if simplecrypt.is_wrapped(raw):
            raw = simplecrypt.decrypt(raw)
    except ImportError:
        pass
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16')
    if raw[:3] == b'\xef\xbb\xbf':
        return raw[3:].decode('utf-8')
    return raw.decode('utf-8', 'replace')


def lint(path):
    problems = []
    lines = _read(path).splitlines()
    depth = 0
    for number, raw in enumerate(lines, 1):
        line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', raw)
        line = re.sub(r'//.*$', '', line)
        if '/*' in line:
            line = line.split('/*', 1)[0]
        stripped = line.strip()
        for pattern, what in PATTERNS:
            for match in pattern.finditer(line):
                words = match.group(1).split(',') if what == 'parameter' \
                    else [match.group(1)]
                for word in words:
                    word = word.strip().split('=')[0].strip()
                    if word in RESERVED:
                        problems.append((number, 'reserved word %r used as %s'
                                         % (word, what)))
        if NULL_LITERAL.search(line):
            problems.append((number, 'the literal null is not supported'))
        # A statement may only continue on the next line when a bracket is
        # open: inside brackets the older engines accept the newline.
        if depth == 0 and stripped and not stripped.startswith('//') \
                and CONTINUATION.match(stripped) and number > 1:
            previous = lines[number - 2].strip()
            if previous and not previous.startswith('//') \
                    and not previous.endswith(('(', '[', '{')):
                problems.append((number,
                                 'statement continues on the next line'))
        depth += line.count('(') + line.count('[') + line.count('{')
        depth -= line.count(')') + line.count(']') + line.count('}')
        if depth < 0:
            depth = 0
    return problems


def main():
    roots = [Path(a) for a in sys.argv[1:]] or [Path(__file__).resolve().parent
                                                 / 'templates']
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.glob('*.tjs')))
        else:
            files.append(root)
    problems = 0
    for path in files:
        for number, message in lint(path):
            problems += 1
            print('%s:%d: %s' % (path.name, number, message))
    print('%d file(s) checked, %d problem(s)' % (len(files), problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
