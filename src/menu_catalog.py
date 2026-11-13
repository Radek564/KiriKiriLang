"""Discover every translatable menu / UI caption a KrKrz title actually uses.

The catalogue is built from what the game ships, never from a per-title table:

* ``syslangtext_<tag>.ini`` files - the authoritative source when a title
  already has selectable languages (the engine itself resolves every
  ``!section menu`` key as ``kag[key]``);
* plain-text ``.tjs`` scripts - dictionary literals such as
  ``%[ name: "saveMenuItem", caption: "セーブ(&S)" ]``, menu-item
  constructors and ``getSysArg*`` calls;
* compiled ``.tjs`` scripts, decompiled with an external decompiler
  when one is available.

Later sources only fill gaps: a caption from the game's own INI always wins
over a caption guessed from a script.  The result records the provenance of
every key so a kit can explain where a caption came from and flag entries that
still need translation.

The module is deliberately language-neutral: tags, labels and selector group
names are passed in by the caller (``langpatch.py`` / ``kit.py``).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import simplecrypt

# The template root: ``assets/`` holds the per-language reference INIs.
TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def read_text(path):
    """Decode a KrKrz text file (UTF-16 with/without BOM, UTF-8, CP932).

    Some engines store their INIs and TJS sources wrapped in SimpleEncrypt
    (``FE FE <mode> FF FE ...``) instead of plain UTF-16.  The wrapper is
    transparent here so callers never have to know which storage form a title
    uses.
    """
    with open(path, 'rb') as handle:
        raw = handle.read()
    if simplecrypt.is_wrapped(raw):
        raw = simplecrypt.decrypt(raw)
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', 'replace')
    if raw[:3] == b'\xef\xbb\xbf':
        return raw[3:].decode('utf-8', 'replace')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp932', 'replace')


def write_utf16_bom(path, text):
    """Write text the way the engine's INI loader expects it."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(('\ufeff' + text).encode('utf-16le'))


def is_compiled_tjs(path):
    with open(path, 'rb') as handle:
        return handle.read(7) == b'TJS2100'


def logical_name(name):
    """Strip an extractor-added ``.txt`` suffix from a script/INI name.

    Some extraction tools append ``.txt`` to text files so they open in an editor.
    ``custom.tjs.txt`` is the real ``custom.tjs`` and ``syslangtext_jp.ini.txt``
    is the real INI; treating the suffix as part of the extension would hide
    every menu caption in such an extraction tree.  Only a suffix after a
    known script/INI extension is stripped, so ordinary ``.txt`` data keeps
    its name.
    """
    low = name.lower()
    if low.endswith('.txt'):
        stem = name[:-4]
        if stem.lower().endswith(('.tjs', '.ini')):
            return stem
    return name


# ---------------------------------------------------------------------------
# catalogue model
# ---------------------------------------------------------------------------

class Entry(object):
    __slots__ = ('key', 'caption', 'section', 'kind', 'source',
                 'group', 'value', 'translated', 'default')

    def __init__(self, key, caption, section='menu', kind='menu',
                 source='', group=None, value=None):
        self.key = key
        self.caption = caption
        self.section = section
        self.kind = kind
        self.source = source
        self.group = group
        self.value = value
        self.translated = False
        # The text the script uses as its own default.  It is often the
        # reference wording, so a caption file that only knows this text has
        # to be replaced by the reference translation.
        self.default = None

    def to_dict(self):
        data = {
            'key': self.key,
            'caption': self.caption,
            'section': self.section,
            'kind': self.kind,
            'source': self.source,
        }
        if self.group:
            data['group'] = self.group
        if self.value:
            data['value'] = self.value
        if self.default is not None:
            data['default'] = self.default
        return data


class Catalog(object):
    """Ordered key -> :class:`Entry` map with merge statistics."""

    def __init__(self):
        self.entries = {}
        self.conflicts = []
        self.sources = {}
        # Keys the scripts ask for without a literal default.  The text is
        # unknown then, but the key still belongs to the title: a language
        # reference may have its caption.
        self.requested = set()

    def add(self, key, caption, section='menu', kind='menu', source='',
            group=None, value=None, overwrite=False):
        key = (key or '').strip()
        caption = (caption or '').strip()
        if not key or not caption:
            return False
        if not _valid_key(key):
            return False
        if key in self.entries and not overwrite:
            old = self.entries[key]
            if old.caption != caption:
                self.conflicts.append({
                    'key': key,
                    'kept': old.caption,
                    'kept_source': old.source,
                    'ignored': caption,
                    'ignored_source': source,
                })
            if _is_script_source(source) and kind == 'langtext' \
                    and old.default is None:
                # The script carries the same text: remember it, so a file
                # that only knows this text can be recognised later.
                old.default = caption
            return False
        self.entries[key] = Entry(key, caption, section, kind, source,
                                  group, value)
        if kind == 'langtext' and _is_script_source(source):
            # The script's own text: the caption file may only know this
            # (often the reference wording), so it has to be recognisable.
            self.entries[key].default = caption
        self.sources[source] = self.sources.get(source, 0) + 1
        return True

    def get(self, key):
        return self.entries.get(key)

    def merge(self, other):
        for entry in other.entries.values():
            self.add(entry.key, entry.caption, entry.section, entry.kind,
                     entry.source, entry.group, entry.value)
        self.conflicts.extend(other.conflicts)

    def section(self, section):
        return {k: v for k, v in self.entries.items()
                if v.section == section}

    def to_dict(self):
        return {
            'entries': [entry.to_dict() for entry in self.entries.values()],
            'conflicts': self.conflicts,
            'sources': dict(sorted(self.sources.items())),
            'requested': sorted(self.requested),
        }

    @classmethod
    def from_dict(cls, data):
        catalog = cls()
        for item in data.get('entries') or []:
            catalog.add(item.get('key'), item.get('caption'),
                        item.get('section', 'menu'), item.get('kind', 'menu'),
                        item.get('source', ''), item.get('group'),
                        item.get('value'), overwrite=True)
        catalog.conflicts = list(data.get('conflicts') or [])
        catalog.sources = dict(data.get('sources') or {})
        catalog.requested = set(data.get('requested') or [])
        return catalog

    def write(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=1)

    @classmethod
    def read(cls, path):
        with open(path, encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle))


def _is_script_source(source):
    """True for a caption that came from a script rather than from an INI."""
    return bool(source) and (source.startswith('dis:') or source.startswith('tjs:'))


def _valid_key(key):
    """Reject expression fragments; keep item names and ``:arg=value`` keys.

    Menu keys are normally ASCII identifiers, but a few titles use a Unicode
    suffix (``ConfigRenameChar翠``).  Anything that looks like whitespace or
    source code is discarded instead of trying to guess a character set.
    """
    if not key or len(key) > 120:
        return False
    if key.startswith(':'):
        return '=' in key and not re.search(r'[\s"\'(){}\[\]]', key)
    return re.match(r'^[^\s"\'(){}[\],;]+$', key) is not None


# ---------------------------------------------------------------------------
# INI discovery
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r'^!\s*section\s+(\S+)', re.I)


def parse_ini(path, catalog=None, source=None, overwrite=False):
    """Add every ``key<TAB>caption`` line from every section of an INI."""
    catalog = catalog if catalog is not None else Catalog()
    text = read_text(path)
    source = source or 'ini:%s' % os.path.basename(path)
    section = 'menu'
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('!'):
            match = _SECTION_RE.match(stripped)
            if match:
                section = match.group(1).strip()
            continue
        if stripped.startswith(('#', ';')):
            continue
        match = re.match(r'^([^\s]+)[\t ]+(.*)$', line)
        if not match:
            continue
        catalog.add(match.group(1), match.group(2).strip(),
                    section=section,
                    kind='selector' if _selector_key(match.group(1)) else 'ini',
                    source=source, overwrite=overwrite)
    return catalog


def _selector_key(key):
    return (key.startswith(('textLanguageMenuItem_', 'subLanguageMenuItem_',
                            'uiLanguageMenuItem_'))
            or key in ('textLanguageMenu', 'textLanguageMenuItem',
                       'subLanguageMenuItem', 'uiLanguageMenuItem',
                       'subLanguageMenuItem_none',
                       'swapMainSubLangMenuItem'))


# ---------------------------------------------------------------------------
# plain-text TJS discovery
# ---------------------------------------------------------------------------

_RE_PAIR_NC = re.compile(
    r'["\']?name["\']?\s*(?::|=>)\s*["\']([^"\']+)["\']\s*,\s*'
    r'["\']?caption["\']?\s*(?::|=>)\s*["\']([^"\']*)["\']', re.S)
_RE_PAIR_CN = re.compile(
    r'["\']?caption["\']?\s*(?::|=>)\s*["\']([^"\']*)["\']\s*,\s*'
    r'["\']?name["\']?\s*(?::|=>)\s*["\']([^"\']+)["\']', re.S)
_RE_SYSARG = re.compile(
    r'getSysArg(?:RadioMenu|CheckMenuItem|RadioSubMenu|RadioMenu2|CheckMenu)'
    r'\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']*)["\']'
    r'(?:\s*,\s*["\']([^"\']*)["\'])?')
_RE_NEWITEM = re.compile(
    r'([A-Za-z_][\w.]*)\s*=\s*new\s+(?:KAGMenuItem|KAGAutoFlagMenuItem)\s*'
    r'\(\s*(?:this|[^,]+)\s*,\s*["\']([^"\']*)["\']')
# Menu list strings:  "value :caption" or "*value:caption:menuName:flag".
_RE_VALUE_ITEM = re.compile(
    r'^\*?([^:\s]+)\s*:\s*(.+?)(?::([A-Za-z_]\w*))?(?::\d+)?$')


def parse_plain_tjs(path, catalog, source=None):
    text = read_text(path)
    source = source or 'tjs:%s' % os.path.basename(path)
    for match in _RE_PAIR_NC.finditer(text):
        catalog.add(match.group(1), match.group(2), source=source, kind='menu')
    for match in _RE_PAIR_CN.finditer(text):
        catalog.add(match.group(2), match.group(1), source=source, kind='menu')
    for match in _RE_SYSARG.finditer(text):
        catalog.add(match.group(1), match.group(2), source=source, kind='menu')
        if match.group(3) and ':' not in match.group(3):
            # The literal call is a good hint that the group exists; value
            # captions still come from the INI or an array literal.
            catalog.sources.setdefault('tjs-sysarg-group', 0)
            catalog.sources['tjs-sysarg-group'] += 1
    for match in _RE_NEWITEM.finditer(text):
        catalog.add(match.group(1).split('.')[-1], match.group(2),
                    source=source, kind='menu')
    _scan_value_strings(text, catalog, source)
    _scan_lang_texts(text, catalog, source)


def _scan_lang_texts(text, catalog, source):
    """Record the language-text lookups of a plain script.

    ``GetSysLangText("Key", "default")`` and its siblings are the engine's
    way of asking for a translated text: the key is what the generated
    ``syslangtext`` INI has to carry.
    """
    for match in re.finditer(
            r'(?:GetSysLangText|GetSystemLangMessage|GetSysLangMessage|'
            r'GetSystemLangText)\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"', text):
        key, default = match.group(1), match.group(2)
        if key and default:
            catalog.add(key, default, source=source, kind='langtext',
                        section=_text_section(key))
    for match in re.finditer(
            r'(?:GetSysLangText|GetSystemLangMessage|GetSysLangMessage|'
            r'GetSystemLangText)\s*\(\s*"([^"]*)"', text):
        catalog.requested.add(match.group(1))
    for match in re.finditer(
            r'(?:System\s*\.\s*)?(?:inform|confirm)\s*\(\s*"([^"]*)"', text):
        message = match.group(1)
        if message:
            catalog.add(_message_key(message), message, source=source,
                        kind='message', section='dialog')


def _scan_value_strings(text, catalog, source):
    """Record menu names embedded in ``value:caption:menuName`` strings."""
    for match in re.finditer(r'["\']([^"\'\n]*:[^"\'\n]*)["\']', text):
        item = _parse_value_item(match.group(1))
        if item and item[2]:
            catalog.add(item[2], item[1], source=source, kind='menu')


def _parse_value_item(value):
    """Return ``(value, caption, menu_name)`` for a menu-list string."""
    match = _RE_VALUE_ITEM.match(value)
    if not match:
        return None
    key_value, caption, menu_name = match.groups()
    if not caption or caption.startswith(('//', '/*')):
        return None
    if menu_name and not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', menu_name):
        menu_name = None
    return key_value, caption.strip(), menu_name


# ---------------------------------------------------------------------------
# compiled TJS discovery
# ---------------------------------------------------------------------------

_RE_STR = re.compile(r'const\s+(%[-\d]+)\s*,\s*#Var\.String\("((?:[^"\\]|\\.)*)"\)')
_RE_INT = re.compile(r'const\s+(%[-\d]+)\s*,\s*#Var\.Int\((-?\d+)\)')
_RE_NEW = re.compile(r'new\s+(%[-\d]+)\s*,\s*(%[-\d]+)\s*\(([^)]*)\)')
_RE_SPIS = re.compile(r'spis\s+(%[-\d]+)\.(%[-\d]+)\s*,\s*(%[-\d]+)')
_RE_SPDE = re.compile(
    r'spde\s+(%[-\d]+)\.#Var\.String\("((?:[^"\\]|\\.)*)"\)\s*,\s*(%[-\d]+)')
_RE_CALL = re.compile(
    r'calld\s+(%[-\d]+)\s*,\s*(%[-\d]+)\.#Var\.String\("([^"]+)"\)'
    r'\s*\((.*)\)\s*$')
_RE_CP = re.compile(r'cp\s+(%[-\d]+)\s*,\s*(%[-\d]+)')
_RE_INC = re.compile(r'inc\s+(%[-\d]+)')
_RE_GPD_REF = re.compile(
    r'gpd\s+(%[-\d]+)\s*,\s*.*?#Var\.String\("([^"]+)"\)')
_RE_FUNC = re.compile(
    r'^\.code\.begin\.(function|function_expression|top_level_script|'
    r'property|property_getter|property_setter)')

_ITEM_CLASSES = {'KAGMenuItem', 'KAGAutoFlagMenuItem', 'MenuItem',
                 'SystemMenuItem'}

# The engine's language-text helpers.  Every call carries a key and the text
# the game falls back to; both belong into the generated ``syslangtext`` INI,
# because that is where the game looks them up.
_LANG_TEXT_FUNCS = {'getsyslangtext', 'getsystemlangmessage',
                    'getsyslangmessage', 'getsystemlangtext'}
# Dialogs the game opens with a literal text.  The game cannot look such a
# text up, but the translator still needs to see it.
_MESSAGE_FUNCS = {'inform', 'confirm'}


def _message_key(text):
    """Stable key for a message that the game hard-codes.

    The key has to survive a refresh, otherwise every run would add a new
    entry and the translator's work would be lost.
    """
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]
    return 'msg_%s' % digest


def _text_section(key):
    """Section a discovered text belongs to.

    Menu captions keep the ``menu`` section even when the game has no INI of
    its own; everything else is a dialog text.
    """
    return 'menu' if 'menu' in key.lower() else 'dialog'


def _unescape(value):
    return (value.replace('\\"', '"').replace('\\\\', '\\')
            .replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r'))


def _split_args(raw):
    out, current = [], []
    depth = 0
    quote = None
    escaped = False
    for char in raw:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in '"\'':
            quote = char
            current.append(char)
        elif char in '([{':
            depth += 1
            current.append(char)
        elif char in ')]}':
            depth -= 1
            current.append(char)
        elif char == ',' and depth == 0:
            out.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        out.append(''.join(current).strip())
    return out


class _DisParser(object):
    """Small stack-machine reader for decompiler output."""

    def __init__(self, catalog, source):
        self.catalog = catalog
        self.source = source
        self.reset()

    def reset(self):
        self.consts = {}
        self.refs = {}
        self.dicts = {}
        self.arrays = {}
        self.items = {}
        # Every string the file ever puts into a register.  The sequential
        # view is dropped whenever a call invalidates a register, but the
        # last known value is still the best guess for a default text.
        self.all_consts = {}

    def flush(self, reg=None):
        for key in ([reg] if reg is not None else list(self.dicts)):
            data = self.dicts.get(key)
            if not data:
                continue
            name = data.get('name')
            caption = data.get('caption')
            if isinstance(name, str) and isinstance(caption, str):
                self.catalog.add(name, caption, source=self.source, kind='menu')
        if reg is not None:
            self.dicts.pop(reg, None)

    def resolve(self, reg):
        if reg in self.consts:
            return self.consts[reg]
        if reg in self.refs:
            return self.refs[reg]
        return None

    def parse(self, text):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _RE_FUNC.match(line):
                self.flush()
                self.reset()
                continue
            match = _RE_STR.search(line)
            if match:
                self.refs.pop(match.group(1), None)
                value = _unescape(match.group(2))
                self.consts[match.group(1)] = value
                self.all_consts[match.group(1)] = value
                continue
            match = _RE_INT.search(line)
            if match:
                self.refs.pop(match.group(1), None)
                self.consts[match.group(1)] = int(match.group(2))
                continue
            match = _RE_GPD_REF.search(line)
            if match:
                self.consts.pop(match.group(1), None)
                self.refs[match.group(1)] = match.group(2)
                continue
            match = _RE_NEW.search(line)
            if match:
                reg, cls_reg, raw_args = match.groups()
                cls = self.consts.get(cls_reg) or self.refs.get(cls_reg)
                args = _split_args(raw_args)
                if cls == 'Dictionary':
                    self.flush(reg)
                    self.dicts[reg] = {}
                    self.arrays.pop(reg, None)
                elif cls == 'Array':
                    self.arrays[reg] = []
                    self.dicts.pop(reg, None)
                elif cls in _ITEM_CLASSES:
                    caption = None
                    if len(args) >= 2:
                        arg = args[1]
                        if re.fullmatch(r'%[-\d]+', arg):
                            caption = self.consts.get(arg)
                    self.items[reg] = caption
                continue
            match = _RE_SPIS.search(line)
            if match:
                obj, key_reg, value_reg = match.groups()
                key = self.consts.get(key_reg)
                value = self.consts.get(value_reg)
                if obj in self.dicts and isinstance(key, str):
                    self.dicts[obj][key] = value
                elif obj in self.arrays and isinstance(key, int):
                    array = self.arrays[obj]
                    while len(array) <= key:
                        array.append(None)
                    array[key] = value
                continue
            match = _RE_SPDE.search(line)
            if match:
                obj, name, value_reg = match.groups()
                name = _unescape(name)
                value = self.consts.get(value_reg)
                # ``spde dict.name, value`` is how dynamically built menu
                # items get their name and caption.
                if obj in self.dicts and name in ('name', 'caption'):
                    self.dicts[obj][name] = value
                if obj == '%-1' and value_reg in self.items:
                    caption = self.items[value_reg]
                    if caption and caption != '-' and name:
                        self.catalog.add(name, caption, source=self.source,
                                         kind='menu')
                continue
            match = _RE_CALL.search(line)
            if match:
                _ret, obj, func, raw_args = match.groups()
                # We cannot evaluate this call, so the destination register no
                # longer holds whatever constant it held before.  Keeping the
                # stale value would, for example, turn
                # ``calld %3, %7.sprintf(...)`` followed by
                # ``spis %1.caption, %3`` into a caption copied from an
                # unrelated earlier string.
                self.consts.pop(_ret, None)
                self.refs.pop(_ret, None)
                self.dicts.pop(_ret, None)
                self.arrays.pop(_ret, None)
                args = _split_args(raw_args)
                values = []
                arrays = []
                for arg in args:
                    if re.fullmatch(r'%[-\d]+', arg):
                        value = self.consts.get(arg)
                        if value is None:
                            # The register was invalidated by an earlier
                            # call; the last string it held is the best
                            # available guess for a default text.
                            value = self.all_consts.get(arg)
                        values.append(value)
                        arrays.append(self.arrays.get(arg))
                    else:
                        values.append(None)
                        arrays.append(None)
                if func.startswith('getSysArg') and len(values) >= 2:
                    if isinstance(values[0], str) and isinstance(values[1], str):
                        group = values[2] if isinstance(values[2], str) else None
                        self.catalog.add(values[0], values[1],
                                         source=self.source, kind='menu',
                                         group=group)
                        self._add_sysarg_values(
                            group, arrays[3:],
                            add_values=bool(group and ':' not in group))
                lowered = func.lower()
                if lowered in _LANG_TEXT_FUNCS and len(values) >= 2:
                    # GetSysLangText("Key", "default"): the game looks the
                    # key up in the syslangtext INI, so the pair belongs
                    # into the generated INI.
                    key, default = values[0], values[1]
                    if isinstance(key, str) and isinstance(default, str) \
                            and key and default:
                        self.catalog.add(key, default, source=self.source,
                                         kind='langtext',
                                         section=_text_section(key))
                    elif isinstance(key, str) and key:
                        # The default is computed, so the text is unknown --
                        # but the key still belongs to the title.
                        self.catalog.requested.add(key)
                elif lowered in _MESSAGE_FUNCS and values:
                    text = values[0]
                    if isinstance(text, str) and text:
                        self.catalog.add(_message_key(text), text,
                                         source=self.source, kind='message',
                                         section='dialog')
                if func == 'push' and obj in self.arrays:
                    for value in values:
                        if isinstance(value, str):
                            self.arrays[obj].append(value)
                continue
            match = _RE_CP.search(line)
            if match:
                dst, src = match.groups()
                if src in self.consts:
                    self.consts[dst] = self.consts[src]
                elif src in self.arrays:
                    self.arrays[dst] = list(self.arrays[src])
                elif src in self.dicts:
                    self.dicts[dst] = dict(self.dicts[src])
                continue
            match = _RE_INC.search(line)
            if match and isinstance(self.consts.get(match.group(1)), int):
                self.consts[match.group(1)] += 1
        self.flush()

    def _add_sysarg_values(self, group, arrays, add_values=True):
        seen = set()
        for array in arrays:
            if not isinstance(array, list):
                continue
            for value in array:
                if not isinstance(value, str):
                    continue
                item = _parse_value_item(value)
                if not item:
                    continue
                key_value, caption, menu_name = item
                if menu_name:
                    self.catalog.add(menu_name, caption, source=self.source,
                                     kind='menu')
                if not add_values or not group or key_value in seen:
                    continue
                seen.add(key_value)
                self.catalog.add(':%s=%s' % (group, key_value), caption,
                                 source=self.source, kind='sysarg',
                                 group=group, value=key_value)


# ---------------------------------------------------------------------------
# decompiler
# ---------------------------------------------------------------------------

def find_decompiler(template, configured=None):
    """Locate an external script decompiler or return ``None``.

    The decompiler is a build-time helper and is intentionally not shipped in
    the patch.  A profile can pin it with ``build.tjs2_decompiler``; otherwise
    a few common places next to the template are searched.
    """
    candidates = []
    if configured:
        candidates.append(configured)
    env = os.environ.get('TJS2_DECOMPILER')
    if env:
        candidates.append(env)
    candidates += [
        os.path.join(template, 'decompiler.exe'),
        os.path.join(template, 'tools', 'decompiler.exe'),
        os.path.join(os.path.dirname(template), 'decompiler',
                     'ToolSource', 'tjs2Disassembler', 'Release',
                     'decompiler.exe'),
        os.path.join(os.path.dirname(template), 'tools',
                     'ToolSource', 'tjs2Disassembler', 'Release',
                     'decompiler.exe'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


class Decompiler(object):
    """Cached ``.tjs`` -> disassembly-text converter."""

    def __init__(self, exe, cache_dir, log=None):
        self.exe = exe
        self.cache_dir = cache_dir
        self.log = log or (lambda *a: None)
        self.failed = []

    def text(self, path):
        if not self.exe:
            return None
        digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()[:16]
        rel = re.sub(r'[^0-9A-Za-z._-]+', '_', os.path.basename(path))
        dest = os.path.join(self.cache_dir, '%s-%s' % (digest, rel))
        res = os.path.join(dest, 'res.txt')
        if os.path.isfile(res) and os.path.getsize(res):
            return read_text(res)
        work = tempfile.mkdtemp(prefix='lpdis_')
        try:
            copy = os.path.join(work, os.path.basename(path))
            shutil.copy2(path, copy)
            proc = subprocess.run([self.exe, os.path.basename(path)],
                                  cwd=work, capture_output=True, timeout=120)
            produced = os.path.join(work, 'res.txt')
            if proc.returncode != 0 or not os.path.isfile(produced):
                self.failed.append((path, proc.returncode))
                return None
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(produced, res)
            return read_text(res)
        except Exception as exc:  # noqa: BLE001 - a missing script is not fatal
            self.failed.append((path, str(exc)))
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# selector captions
# ---------------------------------------------------------------------------

def _strip_accel(text):
    return (text or '').replace('&', '')


def _caption_set(ui_text, tag):
    data = (ui_text or {}).get(tag) or {}
    return data if isinstance(data, dict) else {}


# The captions the language selector owns: the menu title and the group
# names.  Every language names them in its own words.
SELECTOR_KEYS = ('textLanguageMenu', 'textLanguageMenuItem',
                 'subLanguageMenuItem', 'uiLanguageMenuItem',
                 'subLanguageMenuItem_none', 'swapMainSubLangMenuItem')


def reference_menu_captions(ui_tag, keys=SELECTOR_KEYS):
    """The menu captions of the template's reference INI for one language.

    The reference files carry the engine-level captions in the language
    they belong to, so every language names the interface-language entry
    in its own wording.
    """
    path = os.path.join(TEMPLATE, 'assets', 'syslangtext_%s.ini' % ui_tag)
    if not os.path.isfile(path):
        return {}
    catalog = Catalog()
    try:
        parse_ini(path, catalog, source='reference:%s' % ui_tag)
    except (OSError, ValueError):
        return {}
    out = {}
    for key, entry in catalog.entries.items():
        if entry.section != 'menu' or not entry.caption:
            continue
        if keys and key not in keys:
            continue
        out[key] = entry.caption
    return out


def add_selector_captions(catalog, ui_tags, base_tag, ui_text=None,
                           new_tag=None, new_label=None):
    """Add the language selector keys in the *base* interface language.

    The selector is engine-level, so its keys exist no matter which title is
    patched.  Captions come from the profile (``language.ui_text``), with a
    neutral fallback: the tag itself.  They are only *added*; an INI or script
    caption that was already discovered is never replaced.
    """
    data = _caption_set(ui_text, base_tag)
    labels = data.get('labels') or {}
    sub_labels = data.get('sub_labels') or {}
    ui_labels = data.get('ui_labels') or {}
    # The template's reference file for the base interface language names the
    # selector's own captions in that language (every reference file uses its
    # own wording for the interface-language entry).  A title without a
    # language menu of its own takes its wording from there.
    ref = reference_menu_captions(base_tag)
    # The per-language labels are ordinary menu keys too; the full reference
    # carries them (one entry per language), so a title
    # without its own selector names the languages the way its language does.
    ref_all = reference_menu_captions(base_tag, keys=None)

    def add(key, value, kind='selector'):
        if value:
            # The selector owns these captions: they are refreshed on every
            # run, so a stale placeholder cannot survive.
            catalog.add(key, value, source='selector', kind=kind,
                        section='menu', overwrite=True)

    add('textLanguageMenu', data.get('title')
        or ref.get('textLanguageMenu')
        or 'テキスト言語(&L)')
    add('textLanguageMenuItem', data.get('main')
        or ref.get('textLanguageMenuItem') or 'Main')
    add('subLanguageMenuItem', data.get('sub')
        or ref.get('subLanguageMenuItem') or 'Sub')
    add('uiLanguageMenuItem', data.get('ui')
        or ref.get('uiLanguageMenuItem') or 'UI')
    add('subLanguageMenuItem_none', data.get('none')
        or ref.get('subLanguageMenuItem_none') or 'None')
    add('swapMainSubLangMenuItem', data.get('swap')
        or ref.get('swapMainSubLangMenuItem') or 'Switch Main and Sub')
    # The captions of the engine's message boxes.  The engine hard-codes
    # them, so the patch wraps the two entry points and takes the caption
    # from the language file; the wording below is the engine's own.
    add('DialogInformation', ref.get('DialogInformation') or 'Information')
    add('DialogConfirmation', ref.get('DialogConfirmation') or 'Confirmation')
    for tag in ui_tags or []:
        if tag == new_tag and new_label:
            # The new language names itself; the tag is not a name.
            label = new_label
        else:
            # A language names itself in its own words; the reference file of
            # the base language carries those autonyms ("日本語", not "JP").
            label = (labels.get(tag)
                     or ref_all.get('textLanguageMenuItem_' + tag)
                     or tag.upper())
        add('textLanguageMenuItem_' + tag, label)
        add('subLanguageMenuItem_' + tag,
            sub_labels.get(tag)
            or ref_all.get('subLanguageMenuItem_' + tag)
            or _strip_accel(label))
        add('uiLanguageMenuItem_' + tag,
            ui_labels.get(tag)
            or ref_all.get('uiLanguageMenuItem_' + tag)
            or _strip_accel(label))


# ---------------------------------------------------------------------------
# top-level discovery
# ---------------------------------------------------------------------------

def _iter_tjs(roots, exclude=()):
    seen = set()
    excluded = {name.lower() for name in exclude}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in excluded]
            for name in files:
                lower = logical_name(name).lower()
                if lower.endswith('.psb.tjs'):
                    # A PSB dump: structured data rendered as a TJS
                    # literal, not executable script.  Its string values are
                    # scene data, never menu captions.
                    continue
                if not lower.endswith('.tjs'):
                    continue
                path = os.path.abspath(os.path.join(base, name))
                if path in seen:
                    continue
                seen.add(path)
                yield path


def _iter_inis(roots, tags, exclude=()):
    seen = set()
    excluded = {name.lower() for name in exclude}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in excluded]
            for name in files:
                lower = logical_name(name).lower()
                if not (lower.startswith('syslangtext_') and lower.endswith('.ini')):
                    continue
                path = os.path.abspath(os.path.join(base, name))
                if path in seen:
                    continue
                seen.add(path)
                yield path


def _rel(path, template):
    try:
        rel = os.path.relpath(path, template)
    except ValueError:
        return path
    return path if rel.startswith('..') else rel.replace('\\', '/')


def discover(template, config, *, profile_path=None, game_root=None,
             extract_dir=None, seed_dirs=(), base_tag=None, ui_tags=None,
             cache_dir=None, decompiler=None, scan_compiled=True,
             exclude_dirs=(), log=None):
    """Build a :class:`Catalog` from everything the installation ships.

    ``seed_dirs`` are extra folders (typically the profile's ``assets_dir``)
    whose ``syslangtext_*.ini`` files act as a curated starting point.  A
    game-shipped INI always wins over a seed; a seed wins over script
    discovery.
    """
    log = log or (lambda *a: None)
    language = config.get('language') or {}
    tag = language.get('tag')
    ui_tags = list(ui_tags or config.get('ui_tags') or [tag])
    if not ui_tags:
        raise ValueError('no ui_tags in the profile')
    base_tag = base_tag or ui_tags[0]
    game_root = game_root or config.get('game_root')
    extract_dir = extract_dir or config.get('extract_dir')
    if extract_dir and not os.path.isabs(extract_dir):
        extract_dir = os.path.join(template, extract_dir)

    roots = [p for p in (game_root, extract_dir) if p]
    excluded = set(exclude_dirs or ())
    excluded.update({'unencrypted', 'patch', 'patch_adult', 'savedata',
                     'save', '.git'})
    catalog = Catalog()

    # 1. The game's own INIs, base language first.
    ini_paths = list(_iter_inis(roots, ui_tags, excluded))
    ini_paths.sort(key=lambda p: (0 if logical_name(os.path.basename(p)).lower()
                                  == 'syslangtext_%s.ini' % base_tag else 1,
                                  p.lower()))
    for path in ini_paths:
        source = 'ini:%s' % _rel(path, template)
        parse_ini(path, catalog, source=source)
        log('ini  %s' % source)

    # 2. Curated INIs from the profile's asset folder / template assets.
    #    The base language comes first: its captions are the title's own,
    #    the other languages are only a fallback.
    for root in seed_dirs:
        if not root or not os.path.isdir(root):
            continue
        names = []
        for name in sorted(os.listdir(root)):
            if not (logical_name(name).lower().startswith('syslangtext_')
                    and logical_name(name).lower().endswith('.ini')):
                continue
            names.append(name)
        names.sort(key=lambda n: 0 if logical_name(n).lower()
                   == 'syslangtext_%s.ini' % base_tag else 1)
        for name in names:
            path = os.path.join(root, name)
            source = 'seed:%s' % _rel(path, template)
            parse_ini(path, catalog, source=source)
            log('seed %s' % source)

    # 3. Scripts.  Plain text is cheap, compiled scripts go through the
    #    decompiler when one is configured/found.
    tjs_files = list(_iter_tjs(roots, excluded))
    for path in tjs_files:
        if is_compiled_tjs(path):
            continue
        source = 'tjs:%s' % _rel(path, template)
        try:
            parse_plain_tjs(path, catalog, source=source)
        except Exception as exc:  # noqa: BLE001
            log('warn %s: %s' % (source, exc))
    if scan_compiled:
        exe = decompiler or find_decompiler(
            template, (config.get('build') or {}).get('tjs2_decompiler'))
        if exe:
            cache_dir = cache_dir or os.path.join(template, 'cache', 'menu_dis')
            worker = Decompiler(exe, cache_dir, log=log)
            for path in tjs_files:
                if not is_compiled_tjs(path):
                    continue
                text = worker.text(path)
                if not text:
                    continue
                source = 'dis:%s' % _rel(path, template)
                _DisParser(catalog, source).parse(text)
                log('dis  %s' % source)
            if worker.failed:
                log('warn decompiler failed for %d script(s)' % len(worker.failed))
        else:
            log('warn no external decompiler found; compiled scripts were not scanned')

    # 4. Engine-level selector keys in the base language.
    ui_text = language.get('ui_text') or {}
    if not ui_text and language.get('menu_title'):
        ui_text = {base_tag: {'title': language.get('menu_title'),
                              'labels': language.get('menu_labels') or {}}}
    add_selector_captions(catalog, ui_tags, base_tag, ui_text,
                          new_tag=language.get('tag'),
                          new_label=language.get('label'))
    return catalog


def stats(catalog, base_tag=None):
    """Small summary used by ``kit status`` and the generated README."""
    sections = {}
    kinds = {}
    for entry in catalog.entries.values():
        sections[entry.section] = sections.get(entry.section, 0) + 1
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
    return {
        'total': len(catalog.entries),
        'sections': sections,
        'kinds': kinds,
        'conflicts': len(catalog.conflicts),
        'sources': dict(sorted(catalog.sources.items())),
    }
