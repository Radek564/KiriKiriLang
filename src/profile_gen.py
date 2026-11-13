"""Generate a language-patch profile for a KrKrz installation.

The profile is what ``config.json`` becomes for one game: game path, archive
scheme, where the engine keeps its scripts, how many text variants a scenario
already has, and which paths the build pipeline should use.

Nothing here is hard-coded per title.  The generator reads what the game
actually ships:

* the executable (``gamecfg.detect_exe``),
* the main ``*.xp3`` archive and its encryption scheme (``xp3_crypt``),
* the script folder (``main/custom.tjs`` and friends inside the archive),
* the scenario prefix and variant layout (a small ``*.scn`` PSB is parsed),
* whether the scripts already carry multi-language tables.

The result is a normal profile; edit it only when a title needs an asset map or
a hand-tuned build flag.

Usage:
    py -X utf8 src/profile_gen.py --game "D:\\Games\\..." --tag xx \
        --out configs/mygame.json [--exe game.exe] [--scheme TITLE] [--force]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import asset_io
import gamecfg
import psb
import psb_io
import simplecrypt
import xp3
import xp3_crypt

CJK_RE = re.compile('[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]')
HELPER_XP3 = ('patch', '_t', 'unencrypted')
# Entry names that carry dialogue or other translatable text.  An extra
# archive is searched for these only.  ``.ini`` is in the list because the
# interface captions of a title live in ``syslangtext_<tag>.ini``: a title
# that keeps them in a second archive would otherwise be translated against
# an empty caption file.
TEXT_ONLY_NEEDLES = ('.scn', '.psb', '.txt', '.ks', '.csv', '.ini')


def _slugify(name):
    slug = re.sub(r'[^0-9A-Za-z._-]+', '_', name).strip('._-')
    return slug or 'game'


def _archives(game_root):
    found = []
    for name in sorted(os.listdir(game_root)):
        low = name.lower()
        if not low.endswith('.xp3'):
            continue
        stem = low[:-4]
        if any(stem.startswith(prefix) for prefix in HELPER_XP3):
            continue
        path = os.path.join(game_root, name)
        if os.path.isfile(path):
            found.append(path)
    return found


def _pick_archive(game_root):
    archives = _archives(game_root)
    if not archives:
        return None
    for path in archives:
        if os.path.basename(path).lower() == 'data.xp3':
            return path
    return max(archives, key=os.path.getsize)


def _detect_scheme(archive_path):
    try:
        title = xp3_crypt.detect_scheme(archive_path)
    except Exception:
        return None, 'scheme detection failed'
    if not title:
        return None, 'no known scheme (plain archive)'
    return title, 'detected %s' % title


def _open_archive(archive_path, scheme_title):
    crypt = None
    if scheme_title:
        crypt = xp3_crypt.resolve_scheme(scheme_title)
    return xp3.parse_archive(archive_path, crypt)


def _entry_bytes(archive, entry):
    return xp3.extract_plain(archive.path, entry, archive.crypt)


def _script_layout(archive):
    """Return (script_out_dir, {role: archive_name}) for custom/default/Storages."""
    roles = {'custom': 'custom.tjs', 'default': 'default.tjs',
             'storages': 'storages.tjs'}
    by_dir = {}
    for entry in archive.entries:
        name = (entry.name or '').replace('\\', '/')
        base = name.rsplit('/', 1)[-1]
        low = base.lower()
        for role, expected in roles.items():
            if low == expected:
                folder = name.rsplit('/', 1)[0] if '/' in name else ''
                by_dir.setdefault(folder, {})[role] = name
    if not by_dir:
        return '', {}
    # Prefer the folder that contains custom.tjs and as many of the others as
    # possible; ties go to the shallowest path.
    def score(item):
        folder, found = item
        return (len(found), -folder.count('/'), -len(folder))
    folder, found = max(by_dir.items(), key=score)
    return folder, found


def _scenario_entries(archive):
    entries = [e for e in archive.entries
               if (e.name or '').lower().endswith('.scn')]
    # Scenario archives usually keep dialogue under ``scn/``; prefer those.
    preferred = [e for e in entries
                 if (e.name or '').lower().startswith('scn/')
                 or '/scn/' in (e.name or '').lower()]
    return preferred or entries


def _pick_scenario(archive):
    entries = _scenario_entries(archive)
    if not entries:
        return None
    # The smallest file is enough to learn the layout and is cheap to decrypt.
    return min(entries, key=lambda e: e.original_size or e.stored_size or 0)


def _variant_strings(scene):
    """Yield every display string the scene stores, per variant slot.

    Two layouts exist in the wild:

    * ``texts`` - each entry is ``[speaker, [variant, ...], ...]`` and every
      variant is ``[length, text, ...]`` (the script-engine form);
    * OLD/KAG form - dialogue lives in ``lines`` commands and the variant list
      is the scene ``title`` (``["Prologue", "序章", ...]``).

    Returning a list of variant slots per line lets the caller measure the
    widest row and vote on which language slot 0 holds.
    """
    for entry in scene.get('texts') or []:
        variants = psb_io.text_variants(entry)
        if not variants:
            continue
        texts = []
        for variant in variants:
            if isinstance(variant, list) and len(variant) > 1:
                texts.append(variant[1] if isinstance(variant[1], str) else '')
            elif isinstance(variant, str):
                texts.append(variant)
            else:
                texts.append('')
        yield texts
    title = scene.get('title')
    if isinstance(title, list) and title:
        yield [item if isinstance(item, str) else '' for item in title]


def _variant_layout(root):
    """Return (variant_count, languages, base_is_cjk)."""
    languages = root.get('languages')
    if isinstance(languages, dict):
        languages = list(languages.values())
    elif not isinstance(languages, list):
        languages = []
    count = 1
    cjk_votes = 0
    samples = 0
    for scene in root.get('scenes') or []:
        for variants in _variant_strings(scene):
            count = max(count, len(variants))
            base = variants[0] if variants else ''
            if base:
                samples += 1
                if CJK_RE.search(base):
                    cjk_votes += 1
    base_is_cjk = samples > 0 and cjk_votes * 2 >= samples
    return count, languages, base_is_cjk


def _decode_script(data):
    """Decode a TJS script the way KrKrz stores it (UTF-16, UTF-8, CP932)."""
    if simplecrypt.is_wrapped(data):
        try:
            data = simplecrypt.decrypt(data)
        except Exception:
            return ''
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return data.decode('utf-16', 'ignore')
    if data.startswith(b'\xef\xbb\xbf'):
        return data[3:].decode('utf-8', 'ignore')
    # UTF-16 without a byte order mark: a decrypted archive loses the BOM,
    # and every second byte of the ASCII text is NUL.
    if len(data) > 8 and data[1:16:2].count(b'\x00') >= 4:
        return data.decode('utf-16le', 'ignore')
    for encoding in ('utf-8', 'cp932'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1', 'ignore')


def _script_text(archive, script_names, needle):
    """Yield decoded scripts that contain *needle*."""
    for name in script_names:
        entry = next((e for e in archive.entries if e.name.replace('\\', '/')
                      == name), None)
        if entry is None:
            continue
        try:
            data = _entry_bytes(archive, entry)
        except Exception:
            continue
        text = _decode_script(data)
        if needle in text:
            yield text


def _has_multilang_tables(archive, script_names):
    """True/False when the scripts are readable, None when they are not.

    Multi-language support normally lives in the game's own ``custom.tjs``,
    but some engines put it in ``default.tjs`` instead, so every known script
    is checked.  ``None`` means the archive names were obfuscated (Hx) and the
    question cannot be answered without mining the real names.
    """
    checked = 0
    for name in script_names:
        entry = next((e for e in archive.entries if e.name.replace('\\', '/')
                      == name), None)
        if entry is None:
            continue
        checked += 1
        try:
            data = _entry_bytes(archive, entry)
        except Exception:
            continue
        if 'multiLangLanguageTags' in _decode_script(data):
            return True
    return False if checked else None


_MULTILANG_TAGS_RE = re.compile(
    r'multiLangLanguageTags\s*=\s*\[([^\]]*)\]', re.S)


def _multilang_tags(archive, script_names):
    """Return the tag list from ``multiLangLanguageTags``, or None.

    The list is the engine's text-variant order.  Reading it lets a generated
    profile describe games whose PSBs carry no ``languages`` metadata (the
    some titles): the scenario still has three columns, but only the
    engine tables know their tags.
    """
    for text in _script_text(archive, script_names, 'multiLangLanguageTags'):
        match = _MULTILANG_TAGS_RE.search(text)
        if not match:
            continue
        tags = re.findall(r'"([^"]+)"', match.group(1))
        if tags:
            return tags
    return None


def _uipsd_folders(archive):
    found = set()
    for entry in archive.entries:
        match = re.search(r'(?:^|/)uipsd/_([^/]+)/', (entry.name or '').lower())
        if match:
            found.add(match.group(1))
    return sorted(found)


def _ui_language_tags(entries, archive=None, extracted=None):
    """Interface languages the title ships artwork for.

    A language with a ``uipsd/<tag>/`` (or ``uipsd/_<tag>/``) folder has an
    interface of its own.  A title may keep several interfaces even though
    its scenarios carry no language list, so the interface languages cannot
    be derived from the scenarios alone.
    """
    found = set()
    if entries:
        for entry in entries:
            rel = _entry_rel(entry).lower()
            match = re.search(r'(?:^|/)uipsd/_?([a-z]{2,3})/', rel)
            if match:
                found.add(match.group(1))
    if not found and archive is not None:
        for entry in archive.entries:
            match = re.search(r'(?:^|/)uipsd/_?([a-z]{2,3})/', (entry.name or '').lower())
            if match:
                found.add(match.group(1))
    return found


# ---------------------------------------------------------------------------
# filemap-backed discovery
# ---------------------------------------------------------------------------
#
# Hx-obfuscated archives hide entry names, but an extraction plus filemap has
# the resolved names.  These helpers let the generator use that tree for the
# same facts it normally reads from the archive: scripts, scenarios, UI folders
# and UI image names.  Nothing here is title-specific.

UI_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.pimg', '.uipsd'}
UI_CALL_RE = re.compile(
    r'(?:uilangimage|GetUIImage|GetUIFile|GetUIPSD)\s*\(\s*["\']([^"\']+)["\']')
UI_FORMAT_RE = re.compile(r'["\']([A-Za-z][A-Za-z0-9_./-]*%[sd])["\']')
LANG_SUFFIX_RE = re.compile(r'^(.*)_([A-Za-z]{2,3})$')
KNOWN_LANG_TAGS = {'en', 'cn', 'jp', 'pl', 'tw', 'zh', 'ko', 'fr', 'de',
                   'es', 'it', 'ru', 'pt', 'id', 'th', 'vi'}


def _load_filemap(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def _tree_entries(folder):
    """Filemap-like rows for an extraction tree without a ``filemap.json``."""
    rows = []
    if not folder or not os.path.isdir(folder):
        return rows
    for root, _dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder).replace(os.sep, '/')
        if rel == '.':
            rel = ''
        for name in files:
            path = name if not rel else rel + '/' + name
            rows.append({
                'name': name,
                'dir': rel + '/' if rel else '',
                'path': path,
                'size': os.path.getsize(os.path.join(root, name)),
            })
    rows.sort(key=lambda row: row['path'])
    return rows


def _entry_rel(entry):
    return (entry.get('path') or entry.get('name') or '').replace('\\', '/')


def _storage_rel(entry):
    """Storage path of a filemap row, without the extraction archive prefix."""
    rel = _entry_rel(entry)
    archive = (entry.get('archive') or '').replace('\\', '/').strip('/')
    prefix = archive + '/'
    if archive and rel.lower().startswith(prefix.lower()):
        return rel[len(prefix):]
    return rel


def _entry_file(entry, extracted):
    if not extracted:
        return None
    path = asset_io.entry_path(entry, extracted)
    if not path:
        return None
    return path if os.path.isfile(path) else None


def _filemap_script_layout(entries):
    roles = {'custom': 'custom.tjs', 'default': 'default.tjs',
             'storages': 'storages.tjs'}
    by_dir = {}
    for entry in entries:
        rel = _storage_rel(entry)
        base = rel.rsplit('/', 1)[-1].lower()
        for role, expected in roles.items():
            if base == expected:
                folder = rel.rsplit('/', 1)[0] if '/' in rel else ''
                by_dir.setdefault(folder, {})[role] = rel
    if not by_dir:
        return '', {}
    folder, found = max(by_dir.items(),
                        key=lambda item: (len(item[1]), -item[0].count('/'),
                                          -len(item[0])))
    return folder, found


def _filemap_scenarios(entries, limit=60):
    found = [e for e in entries
             if _entry_rel(e).lower().endswith('.scn')]
    found.sort(key=lambda e: e.get('size') or 0)
    return found[:limit]


def _filemap_text(entry, extracted):
    path = _entry_file(entry, extracted)
    if not path:
        return None
    with open(path, 'rb') as handle:
        return _decode_script(handle.read())


def _script_filter(scripts):
    """(full paths, basenames) for the engine's active scripts, lower-cased."""
    if not scripts:
        return None
    names = {str(name).replace('\\', '/').lower() for name in scripts}
    return names, {name.rsplit('/', 1)[-1] for name in names}


def _entry_is_active(entry, flt):
    if flt is None:
        return True
    rel = _storage_rel(entry).lower()
    full, bases = flt
    return rel in full or rel.rsplit('/', 1)[-1] in bases


def _multilang_tags_map(entries, extracted, scripts=None):
    flt = _script_filter(scripts)
    for entry in entries:
        if not _entry_is_active(entry, flt):
            continue
        rel = _storage_rel(entry).lower()
        if not rel.endswith(('.tjs', '.txt')):
            continue
        text = _filemap_text(entry, extracted)
        if not text or 'multiLangLanguageTags' not in text:
            continue
        match = _MULTILANG_TAGS_RE.search(text)
        if match:
            tags = re.findall(r'"([^"]+)"', match.group(1))
            if tags:
                return tags
    return None


def _has_multilang_tables_map(entries, extracted, scripts=None):
    flt = _script_filter(scripts)
    checked = 0
    for entry in entries:
        if not _entry_is_active(entry, flt):
            continue
        rel = _storage_rel(entry).lower()
        if not rel.endswith('.tjs'):
            continue
        text = _filemap_text(entry, extracted)
        if text is None:
            continue
        checked += 1
        if 'multiLangLanguageTags' in text:
            return True
    return False if checked else None


def _has_native_multilang_modules(entries, extracted=None):
    """True when the extraction ships the engine's own multi-language code.

    Titles on this engine may carry ``sysscn/multilang.tjs`` and
    ``sysscn/rendermsgwin.tjs`` without loading them.  When the scenario data
    already contains extra language columns (even though ``default.tjs`` has
    no ``multiLangLanguageTags``), loading that native subsystem is the only
    path that can actually select them: the renderer resolves
    ``kag.getLangInfo()`` per language, while the plain message layer always
    prints the base column.  The check is name based because the modules are
    often compiled TJS2100 blobs.
    """
    if not entries:
        return False
    found = set()
    for entry in entries:
        rel = _storage_rel(entry).lower()
        base = rel.rsplit('/', 1)[-1]
        if base in ('multilang.tjs', 'rendermsgwin.tjs'):
            found.add(base)
    return {'multilang.tjs', 'rendermsgwin.tjs'} <= found


_DEFAULT_MSG_FONT_RE = re.compile(
    r'defaultMessageFontSize\s*=\s*(\d+(?:\.\d+)?)')
_FSZ_DECL_RE = re.compile(r'\bfsz\s*=\s*[^;\r\n]*?(\d+(?:\.\d+)?)')
_CONFIG_FONT_RE = re.compile(
    r'\bdefaultFontSize\s*=\s*(?:SizeCoef\()?\s*(\d+(?:\.\d+)?)')
_NATIVE_SCALE_RES = (
    ('native_text_font_scale',
     re.compile(r'multiLangTextFontScale\s*=\s*([^;\r\n]+)')),
    ('native_select_font_scale',
     re.compile(r'multiLangSelectFontScale\s*=\s*([^;\r\n]+)')),
)
_NATIVE_SINGLE_RE = re.compile(
    r'multiLangSingleFontScaleMap\s*=\s*%\[([^\]]*)\]', re.S)
_NATIVE_LINE_RE = re.compile(
    r'multiLangLineSpaceScale\s*=\s*([0-9]+(?:\.[0-9]+)?)')


def _scale_expr(raw):
    """Strip a trailing comment from a TJS scale expression."""
    return raw.split('//', 1)[0].strip().rstrip(',').strip()


def _config_font_size(extracted):
    """``defaultFontSize`` from the extraction's ``Config.tjs``, if any."""
    if not extracted or not os.path.isdir(extracted):
        return None
    found = []
    for root, _dirs, files in os.walk(extracted):
        for name in files:
            if name.lower() == 'config.tjs':
                found.append(os.path.join(root, name))
    found.sort(key=lambda path: ('/main/' not in
                                 path.replace(os.sep, '/').lower(), path))
    for path in found:
        try:
            with open(path, 'rb') as handle:
                text = _decode_script(handle.read())
        except OSError:
            continue
        match = _CONFIG_FONT_RE.search(text)
        if match:
            return float(match.group(1))
    return None


def _fmt_size(value):
    return str(int(value)) if float(value).is_integer() else ('%g' % value)


def _base_font_size():
    """The message font size the base profile hard-codes, if it has one."""
    path = os.path.join(TEMPLATE, 'config.json')
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    size = (data.get('language') or {}).get('font_size')
    try:
        return float(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def _native_font_settings(texts, tag=None, base_tag=None, extracted=None,
                          font_size=None, has_english=False):
    """Native multilang font scales for the injected ``default.tjs`` block.

    Titles that already ship ``multiLang*FontScale`` keep those expressions
    when they are self-contained; titles without them use the
    default ratios relative to the game's message font size.  The
    base font is discovered from ``defaultMessageFontSize``/``fsz`` in the
    active scripts, then from ``Config.tjs``, then the engine default 24.
    """
    joined = '\n'.join(texts or [])
    settings = {}
    native = {}
    for key, pattern in _NATIVE_SCALE_RES:
        match = pattern.search(joined)
        if match:
            native[key] = _scale_expr(match.group(1))
    fsz_defined = re.search(r'\bvar\s+fsz\b', joined) is not None

    base = None
    match = _DEFAULT_MSG_FONT_RE.search(joined)
    if match:
        base = float(match.group(1))
    if base is None:
        base = _config_font_size(extracted)
    if base is None:
        match = _FSZ_DECL_RE.search(joined)
        if match:
            base = float(match.group(1))
    if base is None:
        base = 24.0
    settings['message_font_size'] = int(base) if base.is_integer() else base

    def keep_or_default(value, count):
        if value and (fsz_defined or 'fsz' not in value):
            return value
        return '%.1f/%s' % (count, _fmt_size(base))

    text_scale = keep_or_default(native.get('native_text_font_scale'), 16.0)
    select_scale = keep_or_default(
        native.get('native_select_font_scale'), 16.0)
    settings['native_text_font_scale'] = text_scale
    settings['native_select_font_scale'] = select_scale

    # multiLangSingleFontScaleMap is a *per-tag* typographic choice, not a
    # generic latin fallback.  The native renderer leaves the base font size
    # untouched for tags missing from the map, which is exactly what a
    # translation with the same glyph metrics as the source expects.  Only
    # preserve an entry the title actually ships for our tag; never borrow
    # another language's ratio (e.g. 20/26) and never invent one.
    single_map = {}
    match = _NATIVE_SINGLE_RE.search(joined)
    if match:
        for entry in re.finditer(r'([A-Za-z0-9_]+)\s*:\s*([^,\]}]+)',
                                 match.group(1)):
            single_map[entry.group(1)] = _scale_expr(entry.group(2))
    single = None
    if tag is not None and tag != base_tag:
        candidate = single_map.get(tag)
        if candidate and ('fsz' not in candidate or fsz_defined):
            single = candidate
    if not single and font_size and not has_english:
        # A title without a latin text has no size to copy: the size of the
        # new language's text is the fixed number from the base profile.
        # It is written relative to the title's own base size, so a title
        # with a bigger message font keeps its proportions.
        size = _fmt_size(float(font_size))
        single = ('%s/fsz' % size if fsz_defined
                  else '%s/%s' % (size, _fmt_size(base)))
    if single:
        settings['native_single_font_scale'] = single

    match = _NATIVE_LINE_RE.search(joined)
    settings['native_line_space_scale'] = (
        float(match.group(1)) if match else 0.8)
    return settings


def _has_ui_language_menu(entries, extracted, scripts=None):
    """True when the shipped scripts build a ``[UI]`` language group.

    ``multilang.tjs`` on some engines only exposes the main/sub groups;
    A copy that also registers ``uiLanguageMenuItem_*`` is enough.  The check looks at
    raw bytes because several titles ship compiled TJS (UTF-16 strings inside
    a TJS2100 container).
    """
    flt = _script_filter(scripts)
    checked = 0
    needle = 'uiLanguageMenuItem'
    for entry in entries:
        if not _entry_is_active(entry, flt):
            continue
        rel = _storage_rel(entry).lower()
        if not rel.endswith('.tjs'):
            continue
        path = _entry_file(entry, extracted)
        if not path:
            continue
        checked += 1
        try:
            data = open(path, 'rb').read()
        except OSError:
            continue
        if needle.encode('ascii') in data or needle.encode('utf-16-le') in data:
            return True
    return False if checked else None


def _materialize_scripts(entries, extracted, slug, script_names):
    """Copy resolved script bytes into ``<slug>/scripts`` for the builder."""
    if not script_names or not extracted:
        return None
    root = os.path.join(TEMPLATE, slug, 'scripts')
    copied = 0
    for rel in script_names.values():
        entry = next((item for item in entries
                      if _storage_rel(item) == rel), None)
        if entry is None:
            continue
        source = _entry_file(entry, extracted)
        if not source:
            continue
        dest = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(source, dest)
        copied += 1
    return root if copied else None


def _script_ui_names(entries, extracted):
    """UI stems the scripts ask for through a language-aware lookup.

    Literal arguments to ``GetUIFile``/``GetUIImage``/``uilangimage`` and
    formatted names such as ``"attention2%s"`` are both collected.  Formatted
    names become a family pattern (``attention2*``) so every generated variant
    (``attention2a`` ...) is covered without listing them by hand.
    """
    names = set()
    patterns = set()
    for entry in entries:
        rel = _entry_rel(entry).lower()
        if not rel.endswith(('.tjs', '.txt', '.ks')):
            continue
        text = _filemap_text(entry, extracted)
        if not text:
            continue
        for name in UI_CALL_RE.findall(text):
            stem = os.path.splitext(os.path.basename(name))[0]
            if stem:
                names.add(stem)
        for value in UI_FORMAT_RE.findall(text):
            prefix = value.split('%', 1)[0]
            if len(prefix) >= 4:
                patterns.add(prefix + '*')
    return names, patterns


def _ui_names_from_map(entries):
    """Stems of standalone UI images and language-tagged variants."""
    stems = set()
    tagged = set()
    tags = set(KNOWN_LANG_TAGS)
    for entry in entries:
        name = entry.get('name') or ''
        folder = (entry.get('dir') or '').replace('\\', '/')
        storage = folder + name if name else ''
        if not storage:
            continue
        low = storage.lower()
        if low.startswith('uipsd/_'):
            match = re.match(r'uipsd/_([^/]+)/', low)
            if match:
                tags.add(match.group(1))
        ext = os.path.splitext(name)[1].lower()
        if ext not in UI_EXTENSIONS:
            continue
        if not asset_io.is_ui_storage(storage, folder, asset_io.UI_DIRS,
                                      asset_io.UI_PATTERNS,
                                      asset_io.UI_TAG_NAMES):
            continue
        stem = os.path.splitext(name)[0]
        match = LANG_SUFFIX_RE.match(stem)
        if match and match.group(2).lower() in tags:
            tagged.add(match.group(1))
        else:
            stems.add(stem)
    return stems, tagged, tags


def _script_texts_from_dir(folder, names=None):
    """Decoded text of scripts under *folder* (``.tjs``/``.txt``).

    *names* restricts the result to the engine's active scripts (the paths
    returned by :func:`_script_layout`).  Without it every file is read, which
    is useful for UI discovery but wrong for multi-language detection: an
    unused ``sysscn/multilang.tjs`` must not make a title look like it ships
    multi-language tables.
    """
    if not folder or not os.path.isdir(folder):
        return []
    wanted = None
    wanted_bases = None
    if names:
        wanted = {str(n).replace('\\', '/').lower() for n in names}
        wanted_bases = {os.path.basename(n).lower() for n in wanted}
    texts = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(('.tjs', '.txt', '.ks')):
                continue
            path = os.path.join(root, name)
            if wanted is not None:
                rel = os.path.relpath(path, folder).replace(os.sep, '/').lower()
                if rel not in wanted and name.lower() not in wanted_bases:
                    continue
            try:
                with open(path, 'rb') as handle:
                    text = _decode_script(handle.read())
            except OSError:
                continue
            if text:
                texts.append(text)
    return texts


def _ui_names_from_texts(texts):
    names = set()
    patterns = set()
    for text in texts:
        for name in UI_CALL_RE.findall(text):
            stem = os.path.splitext(os.path.basename(name))[0]
            if stem:
                names.add(stem)
        for value in UI_FORMAT_RE.findall(text):
            prefix = value.split('%', 1)[0]
            if len(prefix) >= 4:
                patterns.add(prefix + '*')
    return names, patterns


def _multilang_tags_texts(texts):
    for text in texts:
        if 'multiLangLanguageTags' not in text:
            continue
        match = _MULTILANG_TAGS_RE.search(text)
        if match:
            tags = re.findall(r'"([^"]+)"', match.group(1))
            if tags:
                return tags
    return None


def _has_multilang_texts(texts):
    return any('multiLangLanguageTags' in text for text in texts)


def _scenario_cache_files(path, limit=80):
    """``*.scn.json`` dumps under *path*, smallest first."""
    if not path or not os.path.isdir(path):
        return []
    found = []
    for name in os.listdir(path):
        if name.lower().endswith('.json'):
            full = os.path.join(path, name)
            if os.path.isfile(full):
                found.append(full)
    found.sort(key=lambda item: os.path.getsize(item))
    return found[:limit]


def _message_font_size(script_texts):
    """The title's default message font size, when it states one."""
    for text in script_texts or []:
        match = re.search(r'defaultMessageFontSize\s*=\s*(\d+)', text)
        if match:
            return int(match.group(1))
    return None


def _script_contains(entry, extracted, needle):
    """True when a script mentions *needle*.

    The scripts of a title are often compiled bytecode, where the strings
    live in the constant pool as UTF-16.  The raw bytes are searched, so
    both a plain script and a compiled one are found.
    """
    path = _entry_file(entry, extracted)
    if not path:
        return False
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError:
        return False
    return (needle.encode('ascii') in data
            or needle.encode('utf-16le') in data)


def _ui_language_command(script_texts, entries=None, extracted=None):
    """The command that switches the interface language in this title.

    A title whose own module offers a confirming member
    (``SetUILanguageType.WithAskAndGoToTitle``) that also returns to the
    title; other titles only have the plain setter, and the patch ports the
    reference implementation for them.  The member lives in the engine's own
    module (``multilang.tjs``), so the whole extraction is searched, not
    only the title's active scripts.
    """
    for text in script_texts or []:
        if 'WithAskAndGoToTitle' in text:
            return 'SetUILanguageType.WithAskAndGoToTitle'
    for entry in entries or []:
        rel = _entry_rel(entry).lower()
        if not rel.endswith(('.tjs', '.ks')):
            continue
        if _script_contains(entry, extracted, 'WithAskAndGoToTitle'):
            return 'SetUILanguageType.WithAskAndGoToTitle'
    return '__lpUiLanguageWithAsk'


def _ui_asset_hooks(script_texts):
    """Interface-file lookup functions the title defines.

    Some titles resolve their interface images through a global function
    (``GetUIFile`` for standalone pictures and ``GetUIPSD`` for the
    uipsd screens).  The patch can wrap those functions so the new
    language's images are looked up inside the patch archive, where the
    engine cannot see them.
    """
    hooks = []
    for text in script_texts or []:
        for name, kind in (('GetUIFile', 'file'), ('GetUIPSD', 'psd')):
            if re.search(r'function\s+%s\s*\(' % re.escape(name), text):
                hooks.append('%s:%s' % (name, kind))
    return hooks


def build_profile(game_root, tag, name=None, label=None, exe=None,
                  scheme=None, slug=None, menu_title=None, menu_labels=None,
                  ui_text=None, default_ui=None, filemap=None, extracted=None,
                  variants=None, index=None, ui_tags=None,
                  native_multilang=False, scripts_dir=None,
                  scenario_cache=None, font_size=None):
    """Return ``(profile, notes)`` for *game_root*."""
    notes = []
    game_root = os.path.abspath(game_root)
    if not os.path.isdir(game_root):
        raise SystemExit('not a folder: %s' % game_root)
    if slug is None:
        slug = _slugify(os.path.basename(game_root))
    entries = _load_filemap(filemap)
    if not entries and extracted:
        entries = _tree_entries(extracted)
        if entries:
            notes.append('extraction tree: %s (%d file(s))'
                         % (os.path.relpath(os.path.abspath(extracted),
                                            TEMPLATE), len(entries)))
    script_texts = _script_texts_from_dir(scripts_dir)
    if script_texts:
        notes.append('script folder: %s (%d file(s))'
                     % (os.path.relpath(os.path.abspath(scripts_dir), TEMPLATE),
                        len(script_texts)))

    exe_path = gamecfg.detect_exe(game_root, exe)
    exe_name = os.path.basename(exe_path) if exe_path else None
    if exe_name:
        notes.append('executable: %s' % exe_name)
    else:
        notes.append('WARNING: no executable found; set game_exe by hand')

    archive_path = _pick_archive(game_root)
    if not archive_path:
        raise SystemExit('no *.xp3 archive found in %s' % game_root)
    notes.append('archive: %s' % os.path.basename(archive_path))

    if scheme:
        scheme_title = scheme
        notes.append('scheme: given %s' % scheme_title)
    else:
        scheme_title, why = _detect_scheme(archive_path)
        notes.append('scheme: %s' % why)

    archive = _open_archive(archive_path, scheme_title)
    hx_obfuscated = any((entry.name or '').startswith('hxv4_')
                        for entry in archive.entries)
    script_out_dir, script_names = _script_layout(archive)
    if entries:
        map_out, map_names = _filemap_script_layout(entries)
        if map_names:
            script_out_dir, script_names = map_out, map_names
    script_dir = '%s/extracted' % slug
    materialized = None
    if entries and script_names:
        materialized = _materialize_scripts(entries, extracted, slug,
                                            script_names)
        if materialized:
            script_dir = '%s/scripts' % slug
            notes.append('scripts materialized: %s'
                         % os.path.relpath(materialized, TEMPLATE))
    active_names = list(script_names.values()) if script_names else None
    active_texts = []
    active_source = materialized or scripts_dir
    if active_source:
        active_texts = _script_texts_from_dir(active_source, active_names)
        if active_names:
            notes.append('active scripts read: %d of %d'
                         % (len(active_texts), len(active_names)))
    if script_names:
        notes.append('scripts: %s' % ', '.join(sorted(script_names.values())))
    elif scripts_dir:
        script_dir = os.path.relpath(os.path.abspath(scripts_dir),
                                     TEMPLATE).replace(os.sep, '/')
        notes.append('scripts: using %s' % script_dir)
    elif hx_obfuscated:
        notes.append('WARNING: Hx-obfuscated archive; script names and text '
                     'model cannot be read without mining the real names')
    else:
        notes.append('WARNING: custom.tjs/default.tjs not found in the archive')

    scenario_candidates = []
    if entries:
        scenario_candidates = [('map', entry)
                               for entry in _filemap_scenarios(entries)]
    if not scenario_candidates:
        archive_scenarios = _scenario_entries(archive)
        archive_scenarios.sort(
            key=lambda e: e.original_size or e.stored_size or 0)
        scenario_candidates = [('archive', entry)
                               for entry in archive_scenarios[:60]]
    if not scenario_candidates and scenario_cache:
        scenario_candidates = [('cache', path)
                               for path in _scenario_cache_files(
                                   scenario_cache)]
        if scenario_candidates:
            notes.append('scenario dumps: %s'
                         % os.path.relpath(os.path.abspath(scenario_cache),
                                           TEMPLATE))
    variant_count = 1
    languages = []
    base_is_cjk = True
    text_model = 'unknown'
    best_score = (-1, -1)
    scenario_label = None
    scenario_errors = 0
    for kind, entry in scenario_candidates:
        try:
            if kind == 'map':
                path = _entry_file(entry, extracted)
                blob = open(path, 'rb').read()
                sample_name = _storage_rel(entry)
                root = psb.load(blob).to_json()
            elif kind == 'cache':
                with open(entry, 'rb') as handle:
                    root = json.load(handle)
                sample_name = os.path.basename(entry)
            else:
                blob = _entry_bytes(archive, entry)
                sample_name = entry.name
                root = psb.load(blob).to_json()
        except Exception:
            scenario_errors += 1
            continue
        scene = (root.get('scenes') or [{}])[0]
        texts = sum(len(item.get('texts') or [])
                    for item in (root.get('scenes') or []))
        current = _variant_layout(root)
        score = (current[0], texts)
        if score > best_score:
            best_score = score
            variant_count, languages, base_is_cjk = current
            text_model = 'texts' if scene.get('texts') else (
                'kag-lines' if scene.get('lines') else 'unknown')
            scenario_label = sample_name
    if scenario_label:
        notes.append('scenario sample: %s (%d text row(s))'
                     % (scenario_label, best_score[1]))
    elif scenario_errors:
        notes.append('WARNING: could not parse any scenario sample')
    else:
        notes.append('WARNING: no .scn entries; text model unknown')
    base_tag = 'jp' if base_is_cjk else 'en'
    notes.append('base tag: %s, variants: %d, languages: %s, text model: %s'
                 % (base_tag, variant_count, languages or '-', text_model))

    multi_tags = None
    if active_texts:
        multi_tags = _multilang_tags_texts(active_texts)
    if multi_tags is None and entries:
        try:
            multi_tags = _multilang_tags_map(entries, extracted, active_names)
        except Exception:
            multi_tags = None
    if multi_tags is None and script_names:
        try:
            multi_tags = _multilang_tags(archive, list(script_names.values()))
        except Exception:
            multi_tags = None
    variant_order = None
    if variants:
        variant_order = list(variants)
    elif multi_tags:
        variant_order = list(multi_tags)
    if variant_order is not None:
        if tag not in variant_order:
            variant_order.append(tag)
        notes.append('text variants: %s' % ', '.join(variant_order))
    if multi_tags:
        notes.append('multiLangLanguageTags: %s' % ', '.join(multi_tags))

    new_tag = tag
    if index is not None:
        slot = int(index)
    elif variant_order:
        slot = variant_order.index(new_tag)
    elif languages:
        slot = len(languages) + 1
    else:
        slot = variant_count
    if ui_tags:
        derived_ui_tags = list(ui_tags)
    elif languages:
        derived_ui_tags = [base_tag] + list(languages) + [new_tag]
    else:
        # The scenarios carry no language list, so the interface languages
        # come from the title's own multi-language tables and the artwork
        # it ships for them (uipsd/en, uipsd/cn, ...).
        ui_folders = _ui_language_tags(entries, archive, extracted)
        # The base language owns the untagged interface, so it is always
        # there; the other languages only when the title ships artwork for
        # them.  The new language is added by the kit, so it counts too.
        derived_ui_tags = [base_tag]
        for candidate in list(multi_tags or []):
            if candidate in derived_ui_tags:
                continue
            if ui_folders and candidate not in ui_folders:
                continue
            derived_ui_tags.append(candidate)
        if new_tag not in derived_ui_tags:
            derived_ui_tags.append(new_tag)

    tables = None
    if active_texts:
        tables = _has_multilang_texts(active_texts)
    if tables is None and entries:
        try:
            tables = _has_multilang_tables_map(entries, extracted, active_names)
        except Exception:
            tables = None
    if tables is None and script_names:
        try:
            tables = _has_multilang_tables(archive, list(script_names.values()))
        except Exception:
            tables = None
    # A title without ``multiLangLanguageTags`` can still ship the engine's
    # native subsystem.  The hand-written text wrapper cannot work there --
    # this engine has no ``convertSceneText`` -- so the native renderer is the
    # only path that can select a scenario column.  Detect the layout instead
    # of requiring a per-game flag.  ``slot > 0`` means the target is an extra
    # column, which is exactly what the native tables are for.
    if not native_multilang and tables is False and slot > 0:
        if _has_native_multilang_modules(entries, extracted):
            native_multilang = True
            notes.append('native multi-language modules -> '
                         'native_multilang=True (no shipped tables)')
    uipsd = _uipsd_folders(archive)
    ui_menu = (_has_ui_language_menu(entries, extracted, active_names)
               if entries else None)
    ui_stems = set()
    ui_tagged = set()
    ui_tag_names = set()
    name_images = False
    if entries:
        for entry in entries:
            rel = _entry_rel(entry).replace('\\', '/').lower()
            if rel.startswith('image/nameimage/'):
                name_images = True
                break
    if script_texts:
        call_names, call_patterns = _ui_names_from_texts(script_texts)
        ui_tag_names.update(call_names)
        ui_tag_names.update(call_patterns)
    if entries:
        map_uipsd = set()
        for entry in entries:
            match = re.search(r'(?:^|/)uipsd/_([^/]+)/',
                              _entry_rel(entry).lower())
            if match:
                map_uipsd.add(match.group(1))
        if map_uipsd:
            uipsd = sorted(set(uipsd) | map_uipsd)
        ui_stems, ui_tagged, ui_language_tags = _ui_names_from_map(entries)
        call_names, call_patterns = _script_ui_names(entries, extracted)
        ui_tag_names.update(ui_tagged)
        ui_tag_names.update(call_names)
        ui_tag_names.update(call_patterns)
    if uipsd:
        notes.append('uipsd folders: %s' % ', '.join(uipsd))
    if ui_tag_names:
        notes.append('UI tag names: %s' % ', '.join(sorted(ui_tag_names)))
    if ui_stems:
        notes.append('standalone UI images: %d' % len(ui_stems))
    build = {
        # A single patch.xp3 is the deployable artifact: scripts, interface
        # INIs, UI assets and scenario overrides all live inside it, so no
        # unencrypted/ tree has to be copied next to the executable.
        'patch_only': True,
        'deploy_loose': False,
        'assets_in_patch': True,
        'scn_in_patch': True,
        'mirror_to_root': False,
        'autopath_recursive': False,
        # Only the scenario folder is overridden by default.  Replacing the
        # engine's own script folders (sysscn/, system/) with another build's
        # modules can abort startup, so that stays opt-in per title.
        'overlay_dirs': ['scn'],
        'script_dir': script_dir,
        'script_files': dict(script_names),
        'script_out_dir': script_out_dir,
        # Some engines execute KAG's Override.tjs from a bare name
        # ("custom.tjs") while the archive stores the script under main/.
        # Keep a root copy whenever the scripts live in a subfolder so both
        # lookup paths resolve inside patch.xp3.
        'script_out_also_root': bool(script_out_dir),
        'inject_multilang': True,
        'load_multilang': False,
        'seed_uipsd_subfolder': bool(uipsd),
    }
    font_size = _message_font_size(active_texts)
    if font_size:
        # The base size of the message window.  The new language uses the
        # same size as the title's own text, so the profile carries it.
        build['message_font_size'] = font_size
        notes.append('default message font size: %d' % font_size)
    ui_hooks = _ui_asset_hooks(active_texts)
    if ui_hooks:
        # The title resolves its interface images through a global function.
        # The patch wraps it so the new language's images come from the
        # patch archive, through the game's own media.
        build['ui_asset_hooks'] = ui_hooks
        build['ui_asset_wrap'] = True
        notes.append('interface lookup: %s (served from the patch archive)'
                     % ', '.join(item.split(':')[0] for item in ui_hooks))
    ui_command = _ui_language_command(active_texts, entries, extracted)
    build['ui_language_command'] = ui_command
    notes.append('interface language command: %s' % ui_command)
    if hx_obfuscated:
        # A title whose archives are obfuscated builds a virtual media that
        # the engine cannot enumerate: a folder inside patch.xp3 never
        # reaches the auto-path table.  A file with a *new* name (the new
        # language's images and its interface INI) is therefore delivered
        # either through the interface lookup above -- the media redirects
        # the name into the patch -- or as a real file.
        if ui_hooks:
            notes.append('archives are obfuscated: the new language\'s assets '
                         'are read from patch.xp3')
        else:
            build['loose_assets'] = True
            build['loose_asset_dir'] = 'langpatch'
            notes.append('archives are obfuscated: the new language\'s assets '
                         'are delivered as loose files (langpatch/)')
    if native_multilang:
        build['native_multilang'] = True
    if tables is None:
        notes.append('multi-language tables: unknown -> leaving '
                     'wrap_language_text at the default (false)')
    else:
        build['wrap_language_text'] = not tables
        # A title with no tables ships no menu for switching them either; the
        # patch adds one (it needs the text wrapper to have an effect).  A
        # title with tables may still have no [UI] group: the native
        # text selector stays, and the patch only appends that third group.
        build['inject_language_menu'] = bool(not tables or
                                             (tables and ui_menu is False))
        notes.append('multi-language tables in shipped scripts: %s -> '
                     'wrap_language_text=%s, inject_language_menu=%s'
                     % (tables, str(not tables).lower(),
                        str(build['inject_language_menu']).lower()))
        if tables and ui_menu is False:
            notes.append('native menu has no [UI] group -> adding one')
    if native_multilang:
        # The native subsystem renders the variants and localises the menu
        # itself; the hand-written wrapper/selector must stay off.
        build['wrap_language_text'] = False
        build['inject_language_menu'] = False
        build['load_multilang'] = False
        # The native renderer scales both text columns from these settings.
        # Keep values shipped by the title when they exist; otherwise derive
        # the default ratios from the game's message font size.
        fonts = _native_font_settings(active_texts, tag=new_tag,
                                      base_tag=base_tag,
                                      extracted=extracted,
                                      font_size=font_size,
                                      has_english=('en' in
                                                   (variant_order or
                                                    derived_ui_tags)))
        build.update(fonts)
        notes.append('native font scales: text=%s select=%s single=%s '
                     'line=%s (message font %s)'
                     % (fonts.get('native_text_font_scale'),
                        fonts.get('native_select_font_scale'),
                        fonts.get('native_single_font_scale', '-'),
                        fonts.get('native_line_space_scale'),
                        fonts.get('message_font_size')))

    if name is None:
        name = new_tag
    if label is None:
        label = '&' + new_tag.capitalize()

    language = {
        'tag': new_tag,
        'name': name,
        'label': label,
        'index': slot,
        'multilang': True,
        # The text the new language is translated from.  A title that ships
        # an english text is translated from english, not from the original
        # language, so the reference is english whenever there is one.
        'base_tag': ('en' if 'en' in (variant_order or derived_ui_tags)
                     else base_tag),
    }
    if font_size:
        # The size of the new language's message text.  It is a fixed
        # number (see config.json), not a value borrowed from another
        # language: the translation is read on every title.
        language['font_size'] = font_size
    if variant_order is not None:
        language['variants'] = variant_order
    if menu_title:
        language['menu_title'] = menu_title
    if menu_labels:
        language['menu_labels'] = dict(menu_labels)
    if ui_text:
        merged_ui = {}
        for ui_tag, captions in ui_text.items():
            ui_tag = str(ui_tag)
            merged_ui[ui_tag] = dict(captions or {})
        # Legacy scalar flags describe the new language's own interface; fold
        # them into ``ui_text`` so both representations agree.
        if menu_title or menu_labels:
            own = merged_ui.setdefault(new_tag, {})
            if menu_title:
                own.setdefault('title', menu_title)
            if menu_labels:
                labels = dict(own.get('labels') or {})
                labels.update(menu_labels)
                own['labels'] = labels
        # Keep the scalar fields in sync with the new language's captions so
        # profiles stay readable (and older builders keep working).
        own = merged_ui.get(new_tag) or {}
        if own.get('title'):
            language['menu_title'] = own['title']
        if own.get('labels'):
            language['menu_labels'] = dict(own['labels'])
        language['ui_text'] = merged_ui
    if default_ui:
        language['default_ui'] = default_ui
    profile = {
        'game_root': game_root,
        'game_exe': exe_name or '',
        'scheme': scheme_title or '',
        # The archive the scenarios are read from.  A title may ship several
        # archives; the one the extraction starts with holds the main story,
        # the others add branches.  The workbook tells them apart.
        'main_archive': os.path.basename(archive_path) if archive_path else '',
        'test_dir': '',
        'stage_to_test': False,
        'extract_dir': '%s/extracted' % slug,
        'extract_exclude': ['patch'],
        'language': language,
        'ui_tags': derived_ui_tags,
        'assets': {
            'dirs': sorted(set(
                ['uipsd/_%s/' % ref for ref in uipsd]
                + ['uipsd/%s/' % ref for ref in uipsd])),
            'by_size': None,
            'seed_untagged': [],
            'seed_flat': True,
            'ui_source': '%s/extracted' % slug,
            'tag_names': sorted(ui_tag_names),
            # The title draws the speaker name as a picture.  The new text
            # language uses text names instead (see build_patch name_images).
            'name_images': name_images,
            # Folders whose images carry the game's own text (the story
            # pictures) and folders that never do (effect art).  Manual
            # choices; a regeneration keeps them.
            'text_dirs': list(asset_io.TEXT_DIRS),
            'exclude_dirs': [],
        },
        'build': build,
        'paths': {
            'scn_dir': '%s/scn_overrides' % slug,
            'build_dir': '%s/build' % slug,
            'workbook': '%s/text_export.xlsx' % slug,
            'cache_dir': '%s/cache/psb' % slug,
            'extract_output': '%s/extracted' % slug,
            'loader': 'loader',
            'filemap': '%s/filemap.json' % slug,
            'assets_dir': '%s/assets' % slug,
            'assets_work': '%s/assets_work' % slug,
            'assets_workbook': '%s/assets_export.xlsx' % slug,
        },
    }
    if extracted:
        # An extraction outside the template (a big archive on another
        # drive) is stored as an absolute path.
        try:
            extracted_rel = os.path.relpath(os.path.abspath(extracted),
                                            TEMPLATE).replace(os.sep, '/')
        except ValueError:
            extracted_rel = os.path.abspath(extracted).replace(os.sep, '/')
        profile['extract_dir'] = extracted_rel
        profile['paths']['extract_output'] = extracted_rel
        profile['assets']['ui_source'] = extracted_rel
    if filemap:
        try:
            filemap_rel = os.path.relpath(os.path.abspath(filemap),
                                          TEMPLATE).replace(os.sep, '/')
        except ValueError:
            filemap_rel = os.path.abspath(filemap).replace(os.sep, '/')
        profile['paths']['filemap'] = filemap_rel
    return profile, notes


# Build flags that describe *how discovery found the game*, not a user
# preference.  Regeneration must be able to correct them (a stale
# ``native_multilang`` used to win over the freshly detected script layout and
# silently disabled the language wrapper and menu).
_STRUCTURAL_BUILD_KEYS = frozenset((
    'patch_only', 'deploy_loose', 'assets_in_patch', 'scn_in_patch',
    'mirror_to_root', 'autopath_recursive', 'overlay_dirs',
    'script_dir', 'script_files', 'script_out_dir', 'script_out_also_root',
    'native_multilang', 'wrap_language_text', 'inject_language_menu',
    'inject_multilang', 'load_multilang', 'seed_uipsd_subfolder',
))


def merge_manual(profile, previous):
    """Fold user-authored settings from *previous* into a fresh *profile*.

    Discovery is authoritative for game structure (paths to scripts, scenario
    variants, UI tags).  Everything a generator cannot know - translated menu
    captions, native-font tuning, where the working folders live - is kept from
    the existing profile instead of being overwritten by a regeneration.
    """
    if not isinstance(previous, dict):
        return profile
    for key in ('test_dir', 'stage_to_test', 'hashing', 'extract_exclude'):
        if key in previous:
            profile[key] = previous[key]

    old_paths = previous.get('paths') or {}
    if old_paths:
        merged = dict(profile.get('paths') or {})
        merged.update(old_paths)
        profile['paths'] = merged

    old_lang = previous.get('language') or {}
    manual_language = ('name', 'label', 'menu_title', 'menu_labels',
                       'menu_groups', 'menu_none', 'menu_swap', 'ui_text',
                       'default_ui', 'name_font')
    for key in manual_language:
        if key in old_lang:
            profile['language'][key] = old_lang[key]

    old_build = previous.get('build') or {}
    build = profile.setdefault('build', {})
    for key, value in old_build.items():
        if key in _STRUCTURAL_BUILD_KEYS:
            continue
        if key not in build:
            build[key] = value
    # A title whose archive is unreadable can keep the script folder earlier
    # builds used, but only when it still exists on disk.
    old_script_dir = old_build.get('script_dir')
    new_script_dir = build.get('script_dir')
    if old_script_dir and os.path.isdir(old_script_dir) \
            and not (new_script_dir and os.path.isdir(new_script_dir)):
        build['script_dir'] = old_script_dir
        if old_build.get('script_files') and not build.get('script_files'):
            build['script_files'] = old_build['script_files']

    old_assets = previous.get('assets') or {}
    for key, value in old_assets.items():
        if key not in profile['assets'] and key != 'by_size':
            profile['assets'][key] = value
    # The folders whose images carry the game's own text and the folders
    # that never do are manual choices: the generator cannot know them.
    for key in ('text_dirs', 'exclude_dirs'):
        if not profile['assets'].get(key) and old_assets.get(key):
            profile['assets'][key] = old_assets[key]
    return profile


def write_profile(profile, out_path, force=False):
    out_path = os.path.abspath(out_path)
    if os.path.exists(out_path) and not force:
        raise SystemExit('%s exists; pass --force to overwrite' % out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(profile, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return out_path


def _extra_has_text(path):
    """Cheap check whether an extra archive can hold translatable text."""
    try:
        arch = xp3.parse_archive(path)
    except Exception:
        return False
    names = [entry.name or '' for entry in arch.entries]
    if not names:
        return False
    # Hxv4 hides every storage name behind a hash, so only a real pass can
    # tell.  Any other archive announces its text entries in the index.
    if all(name.lower().startswith('hxv4_') or not name for name in names):
        return True
    return any(name.lower().endswith(TEXT_ONLY_NEEDLES) for name in names)


def _ensure_extraction(game_root, slug, scheme=None, exe=None, out_dir=None):
    """Extract the main archive and build a filemap.

    The archives go to ``<slug>/extracted`` inside the template, or to
    ``out_dir`` when the extraction should live somewhere else (a big game
    needs more room than the checkout has).

    A game often ships its extra branches in a second archive.  Those
    archives hold
    dialogue that has to be translated as well, so they are extracted too,
    but only their text-bearing entries: copying gigabytes of artwork for
    the sake of a few scenario files is not worth it.  The rows land in the
    same filemap, which is what the scenario export reads.
    """
    archive = _pick_archive(game_root)
    if not archive:
        raise SystemExit('no *.xp3 archive found in %s' % game_root)
    if scheme is None:
        scheme, _why = _detect_scheme(archive)
    if out_dir:
        out = os.path.abspath(out_dir)
        filemap = os.path.join(out, 'filemap.json')
    else:
        out = os.path.join(TEMPLATE, slug, 'extracted')
        filemap = os.path.join(TEMPLATE, slug, 'filemap.json')
    exe_path = gamecfg.detect_exe(game_root, exe)
    cmd = [sys.executable, '-X', 'utf8',
           os.path.join(TEMPLATE, 'src', 'extract_xp3.py'),
           archive, '-o', out, '--filemap', filemap]
    if exe_path:
        cmd += ['--exe', exe_path]
    if scheme:
        cmd += ['--scheme', scheme]
    print('extracting: %s -> %s' % (os.path.basename(archive),
                                    os.path.relpath(out, TEMPLATE)))
    rc = subprocess.call(cmd, cwd=TEMPLATE)
    if rc:
        raise SystemExit('extraction failed (rc=%d)' % rc)
    for extra in _archives(game_root):
        if os.path.abspath(extra) == os.path.abspath(archive):
            continue
        if not _extra_has_text(extra):
            continue
        extra_scheme, _why = _detect_scheme(extra)
        cmd = [sys.executable, '-X', 'utf8',
               os.path.join(TEMPLATE, 'src', 'extract_xp3.py'),
               extra, '-o', out, '--filemap', filemap]
        for needle in TEXT_ONLY_NEEDLES:
            cmd += ['--only', needle]
        if exe_path:
            cmd += ['--exe', exe_path]
        if extra_scheme:
            cmd += ['--scheme', extra_scheme]
        print('extracting texts: %s -> %s' % (os.path.basename(extra),
                                               os.path.relpath(out, TEMPLATE)))
        # A missing extra archive must not stop the profile: the game may
        # ship it only in the adult package.
        subprocess.call(cmd, cwd=TEMPLATE)
    return filemap, out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', required=True, help='game installation folder')
    ap.add_argument('--tag', required=True, help='new language tag, e.g. xx')
    ap.add_argument('--font-size', type=float, default=None,
                    help='message font size of the new language '
                         '(default: the one in config.json)')
    ap.add_argument('--name', default=None, help='long language name')
    ap.add_argument('--label', default=None, help='menu caption')
    ap.add_argument('--exe', default=None, help='executable name override')
    ap.add_argument('--scheme', default=None, help='archive scheme title override')
    ap.add_argument('--slug', default=None, help='folder prefix inside the template')
    ap.add_argument('--filemap', default=None,
                    help='filemap.json from an extraction (enables Hx titles)')
    ap.add_argument('--extracted', default=None,
                    help='extraction tree that filemap paths resolve against')
    ap.add_argument('--scripts-dir', default=None,
                    help='folder of existing TJS scripts to discover languages/UI')
    ap.add_argument('--scenario-cache', default=None,
                    help='folder of .scn.json dumps to discover text variants')
    ap.add_argument('--merge-from', default=None,
                    help='existing profile whose manual settings are kept')
    ap.add_argument('--extract', action='store_true',
                    help='extract the main archive into <slug>/extracted first')
    ap.add_argument('--extract-dir', default=None,
                    help='folder for the extracted archives (default: '
                         '<slug>/extracted inside the template)')
    ap.add_argument('--variants', default=None,
                    help='comma list of text-variant tags, base first '
                         '(default: multiLangLanguageTags + new tag)')
    ap.add_argument('--index', type=int, default=None,
                    help='target text slot (default: position in --variants)')
    ap.add_argument('--ui-tag', action='append', default=None,
                    help='interface language tag; repeatable '
                         '(default: derived from the game)')
    ap.add_argument('--native-multilang', action='store_true',
                    help='use the engine multi-language tables directly')
    ap.add_argument('--menu-title', default=None,
                    help='in-game language menu caption, e.g. "&Language"')
    ap.add_argument('--menu-label', action='append', default=[], metavar='TAG=CAPTION',
                    help='caption of one language entry (repeatable)')
    ap.add_argument('--ui-text', action='append', default=[], metavar='TAG=JSON',
                    help='menu captions for one interface language, as a JSON '
                         'object (repeatable)')
    ap.add_argument('--default-ui', default=None,
                    help='interface language tag used on a fresh install')
    ap.add_argument('--out', default=None,
                    help='profile path (default: configs/<slug>.json)')
    ap.add_argument('--force', action='store_true', help='overwrite an existing profile')
    args = ap.parse_args()

    menu_labels = {}
    for item in args.menu_label:
        tag, sep, caption = item.partition('=')
        if not sep or not tag.strip() or not caption:
            raise SystemExit('bad --menu-label %r (expected TAG=CAPTION)' % item)
        menu_labels[tag.strip()] = caption

    ui_text = {}
    for item in args.ui_text:
        tag, sep, payload = item.partition('=')
        if not sep or not tag.strip():
            raise SystemExit('bad --ui-text %r (expected TAG=JSON)' % item)
        try:
            captions = json.loads(payload)
        except ValueError as error:
            raise SystemExit('bad --ui-text %r: %s' % (item, error))
        if not isinstance(captions, dict):
            raise SystemExit('bad --ui-text %r: expected a JSON object' % item)
        ui_text[tag.strip()] = captions

    slug = args.slug or _slugify(os.path.basename(os.path.abspath(args.game)))
    filemap = args.filemap
    extracted = args.extracted
    scripts_dir = args.scripts_dir
    scenario_cache = args.scenario_cache
    if filemap and not os.path.isabs(filemap):
        filemap = os.path.join(TEMPLATE, filemap)
    if extracted and not os.path.isabs(extracted):
        extracted = os.path.join(TEMPLATE, extracted)
    if scripts_dir and not os.path.isabs(scripts_dir):
        scripts_dir = os.path.join(TEMPLATE, scripts_dir)
    if scenario_cache and not os.path.isabs(scenario_cache):
        scenario_cache = os.path.join(TEMPLATE, scenario_cache)
    if args.extract and not (filemap and extracted):
        filemap, extracted = _ensure_extraction(
            args.game, slug, scheme=args.scheme, exe=args.exe,
            out_dir=args.extract_dir)
    elif filemap and not extracted:
        extracted = os.path.join(TEMPLATE, slug, 'extracted')
    elif extracted and not filemap:
        filemap = os.path.join(TEMPLATE, slug, 'filemap.json')

    variants = None
    if args.variants:
        variants = [item.strip() for item in args.variants.split(',')
                    if item.strip()]

    # The size of the new language's message text.  It is a fixed number in
    # the base profile (config.json), so every title gets the same one.
    font_size = args.font_size
    if font_size is None:
        font_size = _base_font_size()

    profile, notes = build_profile(
        args.game, args.tag, name=args.name, label=args.label, exe=args.exe,
        scheme=args.scheme, slug=slug, menu_title=args.menu_title,
        menu_labels=menu_labels or None, ui_text=ui_text or None,
        default_ui=args.default_ui, filemap=filemap, extracted=extracted,
        variants=variants, index=args.index, ui_tags=args.ui_tag,
        native_multilang=args.native_multilang, scripts_dir=scripts_dir,
        scenario_cache=scenario_cache, font_size=font_size)
    if args.merge_from:
        merge_path = args.merge_from
        if not os.path.isabs(merge_path):
            merge_path = os.path.join(TEMPLATE, merge_path)
        if os.path.isfile(merge_path):
            with open(merge_path, encoding='utf-8') as handle:
                previous = json.load(handle)
            profile = merge_manual(profile, previous)
            notes.append('manual settings kept from %s'
                         % os.path.relpath(merge_path, TEMPLATE))
    out_path = args.out or os.path.join(TEMPLATE, 'configs', slug + '.json')
    out_path = write_profile(profile, out_path, force=args.force)

    print('profile: %s' % out_path)
    print('  index : %s' % profile['language']['index'])
    print('  ui    : %s' % ', '.join(profile['ui_tags']))
    print('  build : script_out_dir=%r wrap_language_text=%s'
          % (profile['build']['script_out_dir'],
             profile['build'].get('wrap_language_text', 'default')))
    for note in notes:
        print('  - %s' % note)


if __name__ == '__main__':
    main()
