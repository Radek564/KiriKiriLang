"""Build patch.xp3 plus the loose asset folder for a new language slot.

Layout produced under <template>/build/:

    patch.xp3          scripts: default.tjs, storages.tjs, custom.tjs
                       (+ the assets/ tree when build.assets_in_patch is true)
    version.dll        loader that mounts patch.xp3 and serves unencrypted/
    unencrypted/       loose _<tag> assets and scenario overrides

The game keeps its original archives untouched.  Copy `patch.xp3`,
`version.dll` and `unencrypted/` into the game folder.

Why loose files?
    The loader intercepts storage opens and maps
        arc://./<path>  ->  <game>/unencrypted/<path>
                          ->  <game>/patch.xp3><path>
    Placed-path lookups (psbfile.dll for PIMG/PSB, Storages.isExistentStorage)
    only see autopaths, so custom.tjs registers <game>/unencrypted/ and the
    regionalised assets exist there as real files.

Everything language-specific comes from ``config.json`` (``language`` and
``ui_tags``); nothing here assumes a particular target language or that the
base game
already offers a language menu.  When a title has no multilingual tables at
all, the keys are injected, and the missing engine support is reported.

Usage:
    py -X utf8 src/build_patch.py [--no-stage]
"""
import json
import os
import re
import shutil
import subprocess
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import configfile
import gamecfg
import simplecrypt as SC
import xp3_pack

CONFIG = configfile.load()
GAME = CONFIG.get('game_root')
TEST = CONFIG.get('test_dir')

LANGUAGE = CONFIG.get('language') or {}
TAG = LANGUAGE.get('tag')
if not TAG:
    raise SystemExit('config.json: language.tag is required')
LANG_NAME = LANGUAGE.get('name') or TAG           # alias, e.g. "target"
LABEL = LANGUAGE.get('label') or TAG              # menu caption, e.g. "&Target"

# Text variant order inside scenario PSBs.  ``language.variants`` is
# authoritative; ``ui_tags`` is the older name for the same list.  The two are
# separate because a title can store more text variants than the kit ships
# interface captions for (a title may keep three dialogue columns but only
# two caption files).
VARIANTS = list(LANGUAGE.get('variants') or [])
UI_TAGS = list(CONFIG.get('ui_tags') or [])
if not VARIANTS:
    VARIANTS = list(UI_TAGS)
if not VARIANTS:
    VARIANTS = [t for t in ('en', 'cn') if t != TAG] + [TAG]
if TAG not in VARIANTS:
    VARIANTS.append(TAG)
if not UI_TAGS:
    UI_TAGS = list(VARIANTS)
if TAG not in UI_TAGS:
    UI_TAGS.append(TAG)
INDEX = LANGUAGE.get('index')
if INDEX is None:
    INDEX = VARIANTS.index(TAG)
REF_TAGS = VARIANTS[:INDEX]
if not REF_TAGS:
    REF_TAGS = [t for t in VARIANTS if t != TAG]
# The language whose faces and layout stand in for the new one when a title
# has no entry for it.  ``language.base_tag`` names the reference that was
# translated; the last reference tag is the fallback (usually the one the
# title is built in).
BASE_TAG = LANGUAGE.get('base_tag') or (REF_TAGS[-1] if REF_TAGS else TAG)
if BASE_TAG not in VARIANTS:
    BASE_TAG = REF_TAGS[-1] if REF_TAGS else TAG
MULTILANG = LANGUAGE.get('multilang', True)


def _path(key, default):
    raw = (CONFIG.get('paths') or {}).get(key) or default
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


TJS_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'templates')


def _tjs(name):
    """Read a TJS fragment from ``src/templates`` and normalise line endings."""
    with open(os.path.join(TJS_TEMPLATES, name), encoding='utf-8') as handle:
        source = handle.read()
    return source.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')


# version.dll is copied from loader/ into the output.  It lives outside the
# game folder until deploy so the unpatched game is never modified.
LOADER = os.path.join(_path('loader', 'loader'), 'version.dll')

BUILD = _path('build_dir', 'build')
SRC = os.path.join(BUILD, 'patch')
ASSETS = _path('assets_dir', 'assets')
SCN = _path('scn_dir', 'scn_overrides')
# The extracted archive tree.  A title's own ``syslangtext_<tag>.ini`` lives
# here and is the authoritative caption source for that language.
EXTRACT = _path('extract_output', 'extracted')

# Where the editable TJS sources live and where they go inside patch.xp3.
# The sources are the game's own scripts: every profile points at the folder
# that holds them (``build.script_dir``), and they are written back into the
# patch archive at ``build.script_out_dir``.  The kit ships no scripts of its
# own, so a profile without that setting cannot build.
BUILD_CFG = CONFIG.get('build') or {}
ASSETS_CFG = CONFIG.get('assets') or {}
SCRIPT_DIR = os.path.join(TEMPLATE, BUILD_CFG.get('script_dir') or '') \
    if BUILD_CFG.get('script_dir') else ''
SCRIPT_FILES = {
    'default': 'default.tjs',
    'storages': 'storages.tjs',
    'custom': 'custom.tjs',
}
SCRIPT_FILES.update(BUILD_CFG.get('script_files') or {})
SCRIPT_OUT = BUILD_CFG.get('script_out_dir') or ''
# Some engines resolve a patched script through the archive root even when the
# original lives in a subfolder.  Writing both copies is harmless (the engine
# opens exactly one of them) and covers both resolution styles.
SCRIPT_ALSO_ROOT = BUILD_CFG.get('script_out_also_root', False)
INJECT_MULTILANG = BUILD_CFG.get('inject_multilang', True)
# Use the engine's own sysscn/rendermsgwin.tjs + msghack.tjs + multilang.tjs
# instead of the hand-written message wrapper and menu.  The modules must be
# split around the game's custom.tjs: rendermsgwin before it (msghack hooks
# MsgwinRender while loading) and multilang after it (it replaces
# RenderMsgwinPlugin.MsgwinRender at afterInit).
NATIVE_MULTILANG = bool(BUILD_CFG.get('native_multilang', False))
# Base message font size of the game.  The native multi-language scales are
# expressed relative to it, e.g. 24/26.
MESSAGE_FONT_SIZE = BUILD_CFG.get('message_font_size')
# The speaker name plate is drawn by an ordinary NameLayer, so the native
# multi-language renderer never localises its font.  By default the plate
# copies the face the message text is drawn with.  `language.name_font`
# (or the equivalent `build.name_font_*` keys) is optional and only needed
# when the plate should deliberately use a different face.
NAME_FONT_CFG = LANGUAGE.get('name_font') or {}
if not isinstance(NAME_FONT_CFG, dict):
    NAME_FONT_CFG = {'face': str(NAME_FONT_CFG)}
NAME_FONT_FACE = (NAME_FONT_CFG.get('face')
                  or BUILD_CFG.get('name_font_face') or '')
NAME_FONT_SCALE = (NAME_FONT_CFG.get('scale')
                   if NAME_FONT_CFG.get('scale') is not None
                   else BUILD_CFG.get('name_font_scale'))
NAME_FONT_SIZE = (NAME_FONT_CFG.get('size')
                  if NAME_FONT_CFG.get('size') is not None
                  else BUILD_CFG.get('name_font_size'))
NAME_FONT_BOLD = (NAME_FONT_CFG.get('bold')
                  if NAME_FONT_CFG.get('bold') is not None
                  else BUILD_CFG.get('name_font_bold'))
# A title that draws the speaker name as a picture (image/nameimage/) keeps
# doing so for its original languages.  For the new text language the name
# image map is emptied so the name is drawn as text and can be translated.
NAME_IMAGES = bool(ASSETS_CFG.get('name_images')
                   if ASSETS_CFG.get('name_images') is not None
                   else BUILD_CFG.get('name_images'))
# False: never seed a generated INI from a full template INI.  A minimal
# ``!section menu`` file only touches the keys this patch defines, so the
# game's own menu captions stay untouched.
SYS_ING_BASE = bool(BUILD_CFG.get('syslang_ini_base', True))
# The [UI] group is an optional third block in the language submenu.  A title
# whose interface translation is incomplete is better off without it; the
# profile can switch it off.
UI_MENU_GROUP = BUILD_CFG.get('ui_menu_group')
if UI_MENU_GROUP is None:
    UI_MENU_GROUP = True
# The command the [UI] entry runs.  A title whose own interface-language
# setter offers the confirming member uses it; every other title gets
# the ported reference implementation (see
# src/templates/ui_language_confirm.tjs).
UI_LANGUAGE_COMMAND = (BUILD_CFG.get('ui_language_command')
                       or 'SetUILanguageType.WithAskAndGoToTitle')
UI_LANGUAGE_CONFIRM = UI_LANGUAGE_COMMAND == '__lpUiLanguageWithAsk'
# Optional second (sub) language row inside the full-screen backlog.  Off by
# default: most titles only have room for one row.
BACKLOG_SUB_LANGUAGE = bool(BUILD_CFG.get('backlog_sub_language', False))
CUSTOM_APPEND = BUILD_CFG.get('custom_append')
if CUSTOM_APPEND and not os.path.isabs(CUSTOM_APPEND):
    CUSTOM_APPEND = os.path.join(TEMPLATE, CUSTOM_APPEND)

# --------------------------------------------------------------------------
# Where new files live inside patch.xp3
# --------------------------------------------------------------------------
# The game's own archives stay encrypted and the loader only redirects opens
# of paths the encrypted media already knows.  A file whose *name* is new to
# the game (``syslangtext_<tag>.ini``, ``title_<tag>.pimg``) therefore has no
# placed path: the media cannot see it and the engine only finds files
# through the auto-path table, which is built from the *contents* of a
# registered directory.
#
# Loose directories are enumerable, but a patch-only deployment ships just
# the archive.  So new files are staged in a subfolder of the archive and
# that folder is registered as an auto path; overrides keep the game's own
# path so the archive merge still replaces them.
# Where the files with new names are staged inside patch.xp3.  The archive
# root is the default: the bundled loader redirects "arc://./<path>" to
# "patch.xp3><path>", so a file at the root is reachable through the game's
# own media.  A subfolder is only useful for engines whose archive folders
# can be enumerated (the auto-path table is built from a folder's contents).
ASSET_SUBDIR = (BUILD_CFG.get('asset_subdir') or '').strip('/\\')
# Assets whose names the game does not know (the new language's images) can
# only be delivered as real files: the archive layer of a title does not
# always enumerate folders, and then the names never reach the engine's
# auto-path table.  With ``build.loose_assets`` those files are staged loose
# as well, in a folder the patch registers as an auto path.
LOOSE_ASSETS = bool(BUILD_CFG.get('loose_assets', False))
LOOSE_ASSET_DIR = (BUILD_CFG.get('loose_asset_dir')
                   or 'unencrypted').strip('/\\')
# Marker written into the archive so the deploy step can recognise "our"
# patch and never overwrite a foreign one.
PATCH_MARKER = BUILD_CFG.get('patch_marker') or 'langpatch.txt'
PATCH_MARKER_TEXT = 'langpatch kit archive\n'


def warn(message):
    print('WARNING: %s' % message)


def read_plain(key):
    """Read an engine script, transparently unwrapping SimpleEncrypt.

    Extracted trees keep scripts as UTF-16 (usually with a BOM), but some
    titles store them wrapped as ``FE FE <mode> FF FE``.  Both forms are
    normalised to text here so the rest of the builder never has to care.
    """
    path = os.path.join(SCRIPT_DIR, SCRIPT_FILES[key])
    if not os.path.isfile(path):
        raise SystemExit(
            'engine script not found: %s\n'
            'the profile must point build.script_dir at the folder with the '
            'game\'s own scripts' % path)
    with open(path, 'rb') as handle:
        raw = handle.read()
    if SC.is_wrapped(raw):
        raw = SC.decrypt(raw)
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16')
    if raw[:3] == b'\xef\xbb\xbf':
        return raw[3:].decode('utf-8')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp932', 'replace')


def wrap_plain(text):
    # The game stores TJS as SimpleEncrypt mode 1 with a UTF-16LE BOM.
    return SC.encrypt(('\ufeff' + text).encode('utf-16le'), mode=1)


# --------------------------------------------------------------------------
# TJS editing helpers
# --------------------------------------------------------------------------
BLOCK_RE = r'(\.%s\s*=\s*%%?\[)(.*?)(\]\s*[,;])'


def _has_token(body, token):
    # Array entries are usually quoted (`"xx"`); accept both spellings so a
    # hand-edited block is never appended to twice.
    pattern = r'(?<![\w"])(?:"%s"|%s)(?![\w"])' % (re.escape(token),
                                                   re.escape(token))
    return re.search(pattern, body) is not None


def list_entry(text, key, token, entry, what):
    """Append *entry* to the ``.key = [ ... ];`` style block.

    Returns ``(text, found)``.  ``found`` is False when the block is absent, so
    the caller can decide whether to inject a whole fallback block.
    """
    pattern = re.compile(BLOCK_RE % re.escape(key), re.S)
    match = pattern.search(text)
    if not match:
        return text, False
    body = match.group(2)
    if _has_token(body, token):
        return text, True
    new_body = body.rstrip()
    if new_body and not new_body.endswith((',', '[')):
        new_body += ','
    new_body += '\n\t\t' + entry + '\n\t'
    return (text[:match.start()] + match.group(1) + new_body
            + match.group(3) + text[match.end():]), True


def map_entry(text, key, entry):
    """Append *entry* to the outer ``.key = %[ ... ];`` dictionary.

    ``list_entry`` stops at the first ``]``; that is correct for a flat array
    but wrong for a map whose values are themselves maps (the new entry would
    land inside the last value).  This scans for the matching bracket, so
    ``multiLangParamsMap`` can grow without disturbing the title's entries.
    """
    match = re.search(r'\.%s\s*=\s*%%?\[' % re.escape(key), text)
    if not match:
        return text, False
    start = match.end() - 1
    depth = 0
    i = start
    n = len(text)
    close = -1
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            nl = text.find('\n', i)
            i = n if nl < 0 else nl + 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                close = i
                break
        i += 1
    if close < 0:
        return text, False
    body = text[start + 1:close]
    if _has_token(body, TAG):
        return text, True
    new_body = body.rstrip()
    if new_body and not new_body.endswith(','):
        new_body += ','
    new_body += '\n\t\t' + entry + '\n\t'
    return text[:start + 1] + new_body + text[close:], True


def set_scalar(text, key, value):
    """Set ``.key = <value>;`` to a new literal, if present."""
    pattern = re.compile(r'(\.%s\s*=\s*)("[^"]*"|[^;]+)(;)' % re.escape(key))
    match = pattern.search(text)
    if not match:
        return text, False
    return text[:match.start(2)] + value + text[match.end(2):], True


def params_map(word_break=0, reference_tag=None):
    """``multiLangParamsMap`` entry for the new language.

    ``word_break: 0`` wraps latin text at word boundaries; the engine default
    for tags without an entry is 1 (break anywhere), which is right for CJK
    but would split latin words mid-word.  The base tag must not get an entry
    when the title already configures the map itself: forcing ``word_break``
    on the base language would change its original wrapping.

    A title with no ``multiLangParamsMap`` at all needs the reference latin tag
    spelled out as well, otherwise the existing foreign column changes its
    wrapping.  ``reference_tag`` requests that extra entry.
    """
    if reference_tag and reference_tag != TAG:
        return (
            '\t// Per-language text layout.  word_break: 0 = wrap at word boundaries\r\n'
            '\t// (latin), 1 = break anywhere (CJK).  Tags without an entry default to\r\n'
            '\t// 1, which would wrap latin text mid-word in the new slot.\r\n'
            '\t.multiLangParamsMap = %%[\r\n'
            '\t\t%s: %%[ word_break: %d, width_time_scale: 1 ],\r\n'
            '\t\t%s: %%[ word_break: %d, width_time_scale: 1 ],\r\n'
            '\t];\r\n'
        ) % (reference_tag, word_break, TAG, word_break)
    return (
        '\t// Per-language text layout.  word_break: 0 = wrap at word boundaries\r\n'
        '\t// (latin), 1 = break anywhere (CJK).  Only the new tag is listed;\r\n'
        '\t// the base language keeps the engine default.\r\n'
        '\t.multiLangParamsMap = %%[\r\n'
        '\t\t%s: %%[ word_break: %d, width_time_scale: 1 ],\r\n'
        '\t];\r\n'
    ) % (TAG, word_break)


def _fallback_menu_labels(tags=None):
    """Tag/caption pairs shown when no syslangtext INI is available.

    The game's own interface language is the one the title is built in, so
    the fallbacks come from that entry of the profile.  Falling back to
    another language would leak it into the base interface.
    """
    tags = list(tags or VARIANTS)
    default_tag = LANGUAGE.get('default_ui') or (UI_TAGS[0] if UI_TAGS else TAG)
    data = (LANGUAGE.get('ui_text') or {}).get(default_tag) or {}
    labels = data.get('labels') or {}
    return [(t, labels.get(t) or t) for t in tags]


def _systemconfig_close(text):
    """Index of the ``}`` that closes ``with (SystemConfig) {``.

    The block is not always the last thing in the file (a title may follow it
    with ``@if (...)`` blocks), so locate the matching brace instead of
    assuming the file ends in ``}``.
    """
    match = re.search(r'with\s*\(\s*SystemConfig\s*\)\s*\{', text)
    if not match:
        return None
    i = match.end() - 1
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            nl = text.find('\n', i)
            i = n if nl < 0 else nl + 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            close = text.find('*/', i + 2)
            i = n if close < 0 else close + 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _has_font_size_var(text):
    """True when ``default.tjs`` defines the local ``fsz`` font-size variable.

    The reference script does ``var fsz = .defaultMessageFontSize = 36;``
    and derives the multi-language scales from it.  Titles that never had
    multi-language support have no such variable, and inside the surrounding
    ``with (SystemConfig)`` an unqualified ``fsz`` is a member lookup that
    throws "Member fsz does not exist" at load time.
    """
    return re.search(r'\bfsz\b', text) is not None


def _font_scales(text):
    """Return ``(double_column_scale, single_column_scale)`` expressions.

    The native modules express their scales relative to the game's base
    message font size.  ``build.message_font_size`` states that size
    explicitly; only titles without the setting fall back to probing for the
    ``fsz`` local and then to the engine default of 24.
    """
    if MESSAGE_FONT_SIZE:
        return ('24.0/%s' % MESSAGE_FONT_SIZE,
                '30.0/%s' % MESSAGE_FONT_SIZE)
    if _has_font_size_var(text):
        return '24/fsz', '30/fsz'
    # 24 is the engine's default message font size (MessageLayer.tjs).
    return '24/24', '30/24'


def inject_multilang_block(text, latin_tag):
    """Append the complete native multi-language configuration.

    The keys only do something when the engine ships the ``multilang.tjs``
    extension; the caller warns about that.  The block is placed at the end of
    the ``with (SystemConfig) { ... }`` wrapper.

    Unlike the old minimal injection, this emits *every* key the native menu
    and renderer read.  Several of them (``multiLangForceMultiLang``,
    ``multiLangMenuCapsFmt``, ``multiLangParamsMap``) are otherwise left at
    values meant for the original two-language build.
    """
    at = _systemconfig_close(text)
    if at is None:
        warn('default.tjs has no "with (SystemConfig)" block; '
             'cannot inject multi-language keys')
        return text
    default_tag = LANGUAGE.get('default_ui') or (UI_TAGS[0] if UI_TAGS else TAG)
    if default_tag not in VARIANTS:
        default_tag = VARIANTS[0] if VARIANTS else TAG
    menu_map = ', '.join('%s:"%s"' % (tag, caption)
                         for tag, caption in _fallback_menu_labels(VARIANTS))
    aliases = []
    if 'jp' in VARIANTS:
        aliases.append('"japanese" => "jp"')
    aliases.append('"%s" => "%s"' % (LANG_NAME, TAG))
    aliases.append('"" => ""')
    alias_map = ', '.join(aliases)
    word_break = int(BUILD_CFG.get('native_word_break', 0))
    # Typical values: 16/26 for two-column text and choice menus,
    # 20/26 for a single latin column.  The base language keeps the engine
    # default so the base language's wrapping and sizing are untouched.
    text_scale = (BUILD_CFG.get('native_text_font_scale')
                  or _font_scales(text)[0])
    select_scale = (BUILD_CFG.get('native_select_font_scale')
                    or text_scale)
    line_space = BUILD_CFG.get('native_line_space_scale', 0.8)
    first_tag = (BUILD_CFG.get('native_first_tag') or default_tag)
    if first_tag not in VARIANTS and first_tag != 'auto':
        first_tag = default_tag
    unknown_tag = (BUILD_CFG.get('native_unknown_tag') or first_tag)
    if unknown_tag not in VARIANTS:
        unknown_tag = default_tag
    single_scale = BUILD_CFG.get('native_single_font_scale')
    if single_scale:
        single_line = ('\t.multiLangSingleFontScaleMap = %%[ %s: %s ];\r\n'
                       % (TAG, single_scale))
    else:
        # Leave the property at its engine default (void): a single-language
        # line then renders at the game's own message font size.
        single_line = ''
    block = (
        '\r\n\t// ==== multi-language support (injected by the patch template) ====\r\n'
        '\t// Mirrors the multi-language configuration of the title.\r\n'
        '\t.multiLangLanguageTags = [ %s ];\r\n'
        '\t.multiLangTagAliasMap = [ %s ];\r\n'
        '\t.multiLangMenuMap = %%[ %s ];\r\n'
        '\t.multiLangMenuCapsFmt = "[${mainsub}] ${text}";\r\n'
        '\t.multiLangUnknownTag = "%s";\r\n'
        '\t.multiLangFirstTag = "%s";\r\n'
        '\t.multiLangTitleCaption = false;\r\n'
        '\t.multiLangForceMultiLang = true;\r\n'
        '\t.multiLangTextFontScale = %s;\r\n'
        '\t.multiLangSelectFontScale = %s;\r\n'
        '\t.multiLangLineSpaceScale = %s;\r\n'
        '%s'
        '%s'
    ) % (', '.join('"%s"' % tag for tag in VARIANTS), alias_map, menu_map,
         unknown_tag, first_tag, text_scale, select_scale, line_space,
         single_line, params_map(word_break))
    return text[:at] + block + text[at:]


# --------------------------------------------------------------------------
# 1. default.tjs: register the tag in the multi-language tables
# --------------------------------------------------------------------------
def _unpin_name_layer_font(text):
    """Remove a pin on the name plate's face.

    A title that fixes the face of the speaker name in its own config draws
    the name in a different face than the message text.  The games that offer
    the setting call it a user option ("enable this line to fix the name
    font"), so the patch comments the line out: the plate then follows the
    face of the message text, in every language.
    """
    pattern = re.compile(
        r'^([ \t]*\.[A-Za-z_]*[Nn]ame[A-Za-z_]*[Ff]ont[A-Za-z_]*\s*=\s*.*)$',
        re.M)
    for match in pattern.finditer(text):
        line = match.group(1)
        if line.strip().startswith(';'):
            continue
        return (text[:match.start()] + ';' + line + text[match.end():],
                True)
    return text, False


default = read_plain('default')
# Variant 0 is the base text.  The latin reference whose wrapping and font
# scaling the new language copies is the english one whenever the title ships
# it: the new language is a translation of the english text, so it has to
# look like the english text, not like the original language.
latin_tag = 'en' if 'en' in REF_TAGS else (REF_TAGS[0] if REF_TAGS else 'en')
# The speaker name has to be drawn in the face of the message text.  A title
# that pins the name plate's face in its own config would draw it in another
# one, so the pin goes.
default, name_pinned = _unpin_name_layer_font(default)
if name_pinned:
    warn('name plate font pin removed from default.tjs — the plate now '
         'follows the message text')
# Remember whether the title already had multi-language support before the
# injection below; the selector is only added to titles that lacked it.
HAD_MULTILANG = '.multiLangLanguageTags' in default

if not HAD_MULTILANG:
    if MULTILANG and INJECT_MULTILANG:
        default = inject_multilang_block(default, latin_tag)
        warn('game has no multi-language tables; injected them into '
             'default.tjs — the engine still needs multilang.tjs support')
    elif MULTILANG:
        warn('game has no multi-language tables; injection disabled '
             '(build.inject_multilang=false)')
    else:
        warn('game has no multi-language tables and language.multilang is '
             'false; skipping the in-game selector')
else:
    default, found = list_entry(default, 'multiLangLanguageTags', TAG,
                                '"%s"' % TAG, 'language tag list')
    if not found:
        warn('multiLangLanguageTags block not recognised; skipped')

    default, found = list_entry(default, 'multiLangTagAliasMap', TAG,
                                '"%s" => "%s"' % (LANG_NAME, TAG),
                                'language alias map')
    if not found:
        # Some titles use an associative-array literal with ``=>``.
        default, found = list_entry(default, 'multiLangTagAliasMap', TAG,
                                    '"%s" => "%s"' % (LANG_NAME, TAG),
                                    'language alias map')
    if not found:
        warn('multiLangTagAliasMap block not recognised; skipped')

    default, found = list_entry(default, 'multiLangMenuMap', TAG,
                                '%s:"%s"' % (TAG, LABEL), 'menu caption map')
    if not found:
        warn('multiLangMenuMap block not recognised; skipped')

    default, found = set_scalar(default, 'multiLangFirstTag', '"%s"' % TAG)
    if not found:
        warn('multiLangFirstTag not found; skipped')

    if '.multiLangParamsMap' not in default:
        anchor = '\t.multiLangTagAliasMap = ['
        if anchor in default:
            default = default.replace(anchor, params_map(reference_tag=latin_tag) + anchor, 1)
        else:
            warn('multiLangTagAliasMap anchor not found; multiLangParamsMap not added')
    else:
        # The title already configures per-language layout.  A tag without an
        # entry falls back to ``word_break`` = 1 (break anywhere), which is
        # right for CJK but splits latin words in the middle.  Add only the
        # new tag, leaving the title's own entries untouched.
        default, found = map_entry(
            default, 'multiLangParamsMap',
            '%s: %%[ word_break: %d, width_time_scale: 1 ]'
            % (TAG, int(BUILD_CFG.get('native_word_break', 0))))
        if not found:
            warn('multiLangParamsMap block not recognised; word break not set')

    # Map-style line spacing.  Copy the reference language's value so the new
    # tag wraps like the existing latin text.
    # multiLangSingleFontScaleMap *is* extended: the map holds the size of a
    # single-language column, and a tag without an entry falls back to the
    # engine's base size -- which is larger than the size the title uses for
    # its own latin text.  Copying the reference value keeps the new
    # language's text exactly as big as the original english one.
    for key, default_value in (
            ('multiLangBacklogSingleLineSpaceScaleMap', '0.8'),
            ('multiLangSingleFontScaleMap', None)):
        pattern = re.compile(BLOCK_RE % re.escape(key), re.S)
        match = pattern.search(default)
        if not match:
            continue
        body = match.group(2)
        if _has_token(body, TAG):
            continue
        if key == 'multiLangSingleFontScaleMap' and BUILD_CFG.get(
                'native_single_font_scale'):
            # The size of the new language's text is a fixed number from the
            # base profile, not a value borrowed from another language.
            value = BUILD_CFG['native_single_font_scale']
        elif re.search(r'(?<![\w"])%s\s*:\s*([^,\]]+)' % re.escape(latin_tag), body):
            value = re.search(r'(?<![\w"])%s\s*:\s*([^,\]]+)'
                              % re.escape(latin_tag), body).group(1).strip()
        elif default_value is None:
            # The title ships no value for the reference language either:
            # nothing to copy.
            continue
        else:
            value = default_value
        entry = '%s:%s' % (TAG, (' ' + value) if not value.startswith(' ') else value)
        new_body = body.rstrip()
        if new_body and not new_body.endswith((',', '[')):
            new_body += ','
        new_body += ' ' + entry + ' '
        default = (default[:match.start()] + match.group(1) + new_body
                   + match.group(3) + default[match.end():])

# Variant 0 is the implicit base column.  Remove the new tag from the
# dual-language column list as well, otherwise translate mode renders a third
# column that is only a copy of the base text.
remove_re = re.compile(r'(\n([ \t]*)list\.remove\("en"\);)')
match = remove_re.search(default)
if match and not _has_token(default[match.start():], TAG):
    indent = match.group(2)
    default = (default[:match.end()] + '\r\n%slist.remove("%s");' % (indent, TAG)
               + default[match.end():])
elif not match:
    warn('GetScnConvertLangList not found; dual-language column not adjusted')

# --------------------------------------------------------------------------
# 2. storages.tjs: make uipsd/_<tag> visible
# --------------------------------------------------------------------------
storages = read_plain('storages')
SEED_UIPSD = BUILD_CFG.get('seed_uipsd_subfolder', True)
if SEED_UIPSD and '"uipsd/_%s"' % TAG not in storages:
    inserted = False
    for ref in REF_TAGS:
        needle = '\t"uipsd/_%s",' % ref
        if needle in storages:
            storages = storages.replace(
                needle,
                '\t"uipsd/_%s",\r\n\r\n%s' % (TAG, needle), 1)
            inserted = True
            break
    if not inserted:
        # A title without regionalised folders still needs the new one when it
        # ships ``uipsd/_<tag>`` assets.  Append to the existing call, or add a
        # separate call when the list uses a different spelling.
        match = re.search(r'(setupSubFolders\s*\(\s*\[)(.*?)(\]\s*\))',
                          storages, re.S)
        if match:
            body = match.group(2).rstrip()
            if body and not body.endswith((',', '[')):
                body += ','
            body += '\r\n\r\n\t"uipsd/_%s",\r\n' % TAG
            storages = (storages[:match.start(2)] + body
                        + storages[match.end(2):])
        else:
            storages = (storages.rstrip()
                        + '\r\n\r\nsetupSubFolders([ "uipsd/_%s" ]);\r\n' % TAG)

# --------------------------------------------------------------------------
# 3. custom.tjs: register the loose asset folder as an autopath
# --------------------------------------------------------------------------
AUTOPATH_FLAT = (
    '// ==== language slot: make <game>/unencrypted/ enumerable ==============\r\n'
    '// The loader intercepts storage opens, but placed-path lookups (psbfile.dll\r\n'
    '// for PIMG/PSB, Storages.isExistentStorage) only search autopaths.\r\n'
    'try { Storages.addAutoPath(System.exePath + "unencrypted/"); } catch (e) { }\r\n'
)

AUTOPATH_TREE = (
    '// ==== language slot: make <game>/unencrypted/ enumerable ==============\r\n'
    '// The loader intercepts storage opens, but placed-path lookups (psbfile.dll\r\n'
    '// for PIMG/PSB, Storages.isExistentStorage) only search autopaths.\r\n'
    '//\r\n'
    '// The engine resolves a placed path in two steps:\r\n'
    '//   1. TVPIsExistentStorageNoSearch on the fully normalized name\r\n'
    '//   2. TVPAutoPathTable[basename]  -- the table maps *bare file names* to\r\n'
    '//      an autopath prefix, built from the files each autopath directory\r\n'
    '//      contains directly.  Registering "unencrypted/" alone therefore only\r\n'
    '//      exposes the files at its top level; "unencrypted/scn/extra_01.txt.scn"\r\n'
    '//      is never indexed and the archive copy wins.\r\n'
    '// Register every directory of the tree so each loose file gets its entry.\r\n'
    'global.__lpAutoPathCount = 0;\r\n'
    'function __lpAddAutoPathTree(base, depth) {\r\n'
    '\ttry {\r\n'
    '\t\tif (depth > 8) return;\r\n'
    '\t\tif (!Storages.isExistentDirectory(base)) return;\r\n'
    '\t\tStorages.addAutoPath(base);\r\n'
    '\t\tglobal.__lpAutoPathCount = global.__lpAutoPathCount + 1;\r\n'
    '\t\tvar list = Storages.dirlist(base);\r\n'
    '\t\tfor (var i = 0; i < list.count; i++) {\r\n'
    '\t\t\tvar name = (string)list[i];\r\n'
    '\t\t\t// "." and ".." come back as "./" and "../"; following them\r\n'
    '\t\t\t// walks the same directory forever.\r\n'
    '\t\t\tif (name == "" || name.charAt(0) == ".") continue;\r\n'
    '\t\t\tif (name.charAt(name.length - 1) == "/")\r\n'
    '\t\t\t\t__lpAddAutoPathTree(base + name, depth + 1);\r\n'
    '\t\t}\r\n'
    '\t} catch (e) { }\r\n'
    '}\r\n'
    'try { __lpAddAutoPathTree(System.exePath + "unencrypted/", 0); } catch (e) { }\r\n'
)

# The game registers <<exe>/data.xp3>scn/> etc. through addAutoPathRecursive in
# Initialize.tjs.  custom.tjs loads after that, and the autopath list is a
# plain vector in which a duplicate basename keeps the *last* registered path
# (tTJSHashTable::Add overwrites).  So one flat entry per override folder is
# enough and always beats the archive copy -- no directory walk needed.
#
# Only folders listed in build.overlay_dirs are registered.  Overriding the
# engine's own script folders (sysscn/, system/) is only safe when the title
# does not already ship a working multi-language renderer: swapping its
# modules for another build's copies can abort startup.  Scenarios are always
# safe, so that is the default.
_OVERLAY_DIRS = [d for d in (BUILD_CFG.get('overlay_dirs') or ['scn']) if d]
AUTOPATH_OVERLAY = (
    '// ==== language slot: override folders win over the archive copies ======\r\n'
    + ''.join('try { Storages.addAutoPath(System.exePath + "%s/"); } catch (e) { }\r\n'
              % d.rstrip('/\\') for d in _OVERLAY_DIRS)
)

def _skip_asset(rel):
    """Assets hold UI images and captions, not engine code.

    A ``.tjs`` file under ``assets/`` is a native-subsystem override (the
    the ``sysscn/`` + ``system/`` modules).  Packing it into a patch
    that does *not* enable the native subsystem would replace the game's own
    modules and can abort startup, so only native builds stage it.
    """
    if not rel.lower().endswith('.tjs'):
        return False
    return not NATIVE_MULTILANG


def _original_storages():
    """Storage paths that already exist in the game's own archives.

    The kit's image manifest lists every exported asset together with the
    storage it came from.  A file whose path still matches such a storage is
    an *override*: the engine merges it over the original, so it keeps the
    game's own path.  Everything else is a new file and needs an auto path.
    """
    slug = os.path.splitext(
        os.path.basename(CONFIG.get('_config_path') or 'config.json'))[0]
    candidates = [_path('kit_dir', os.path.join('kits', slug, TAG))]
    for kit in candidates:
        manifest = os.path.join(kit, 'images', 'manifest.json')
        if not os.path.isfile(manifest):
            continue
        try:
            with open(manifest, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        paths = set()
        for asset in data.get('assets') or []:
            storage = asset.get('storage')
            if storage:
                paths.add(storage.replace('\\', '/').lower())
        if paths:
            return paths
    return None


_SYSLANG_INI_RE = re.compile(r'^syslangtext_([A-Za-z0-9_-]+)\.ini$')


def _stale_caption_ini(rel):
    """True for a caption INI the build does not manage and must not ship.

    ``assets/`` can hold a copy of the title's own captions.  The build
    rewrites one INI per interface language it supports, so a copy for any
    other tag is a leftover: shipping it would override the game's own file
    (and lose, for example, a store-specific title caption).  The game's
    archive already has the original, so the correct action is to drop it.
    """
    if '/' in rel:
        return False
    match = _SYSLANG_INI_RE.match(rel)
    return bool(match) and match.group(1) not in UI_TAGS


def _asset_plan():
    """Map every file under ``assets/`` to its path inside patch.xp3.

    Overrides keep the game's own path (the archive merge replaces them);
    new files -- the new language's images and the interface INIs -- are
    moved into ``ASSET_SUBDIR``, where an auto path can index them.
    """
    original = _original_storages()
    plan = {}
    if not os.path.isdir(ASSETS):
        return plan
    for base, _dirs, files in os.walk(ASSETS):
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, ASSETS).replace('\\', '/')
            if _skip_asset(rel) or _stale_caption_ini(rel):
                continue
            if original is not None:
                is_new = rel.lower() not in original
            else:
                # Without a manifest: files that sit directly in ``assets/``
                # were renamed by the export (new names), files in subfolders
                # keep the game's own path (overrides).
                is_new = '/' not in rel
            if is_new and ASSET_SUBDIR:
                plan[rel] = '%s/%s' % (ASSET_SUBDIR, rel)
            else:
                plan[rel] = rel
    return plan


# Patch-only deployments keep the overrides *inside* patch.xp3.  The game
# mounts the archive root with useArchiveIfExists("patch.xp3") only after
# Storages.tjs has already run setupSubFolders, so "<game>/patch.xp3>scn/"
# never reaches TVPAutoPathTable and the data.xp3 copy wins every lookup.
# Register every in-archive folder that holds a file before packing:
# addAutoPath appends, and a later duplicate basename overwrites the earlier
# one in tTJSHashTable::Add, so the patch copy wins.
def _patch_auto_path_dirs():
    """Archive directories (relative, with trailing '/') to register."""
    dirs = []

    def add(value):
        value = (value or '').replace('\\', '/').strip('/')
        if not value:
            return
        value += '/'
        if value not in dirs:
            dirs.append(value)

    for value in _OVERLAY_DIRS:
        add(value)
    add(SCRIPT_OUT)
    for target in _asset_plan().values():
        folder = os.path.dirname(target)
        if folder:
            add(folder)
    if os.path.isdir(SCN):
        for base, _subdirs, files in os.walk(SCN):
            if not files:
                continue
            rel = os.path.relpath(base, SCN)
            if rel == '.':
                # Root-level files are already exposed by patch.xp3> itself.
                continue
            add(rel)
    return dirs


_PATCH_AUTOPATH_DIRS = _patch_auto_path_dirs()
# The game itself mounts "patch.xp3>" and then every "patchN.xp3>" that
# exists (Initialize.tjs).  The auto-path table keeps the *last* registered
# path for a base name, so registering the folders of every patch archive in
# ascending order makes the newest patch win.
AUTOPATH_PATCH = (
    '// ==== language slot: patch.xp3 override folders win over data.xp3 =====\r\n'
    '// The engine mounts "patch.xp3>" and "patchN.xp3>" itself, but an\r\n'
    '// archive root is not enumerable: only the *contents* of a folder are\r\n'
    '// added to the auto-path table.  New files (the new language\'s images,\r\n'
    '// the interface INIs) therefore live in a subfolder of the archive, and\r\n'
    '// that folder is registered here.\r\n'
    'try {\r\n'
    '\tglobal.__lpPatchList = new (global).Array();\r\n'
    '\tfor (var __lpN = 1; __lpN <= 16; __lpN++) {\r\n'
    '\t\tvar __lpName = (__lpN == 1) ? "patch.xp3" : ("patch" + __lpN + ".xp3");\r\n'
    '\t\tvar __lpPath = System.exePath + __lpName;\r\n'
    '\t\tif (!Storages.isExistentStorage(__lpPath)) continue;\r\n'
    '\t\tglobal.__lpPatchList.add(__lpPath);\r\n'
    '\t}\r\n'
    '\tglobal.__lpPatchXp3 = (global.__lpPatchList.count > 0)\r\n'
    '\t\t? global.__lpPatchList[global.__lpPatchList.count - 1] : "";\r\n'
    '\tfor (var __lpI = 0; __lpI < global.__lpPatchList.count; __lpI++) {\r\n'
    '\t\tvar __lpArc = global.__lpPatchList[__lpI];\r\n'
    '\t\tStorages.addAutoPath(__lpArc + ">");\r\n'
    + ''.join('\t\tStorages.addAutoPath(__lpArc + ">%s");\r\n'
              % d for d in _PATCH_AUTOPATH_DIRS)
    + '\t}\r\n'
    '} catch (e) { }\r\n'
)

# Some engines index only the files sitting directly in an autopath directory
# (the loose tree then needs one autopath per sub-directory); others reach the
# files through the loader hook and are happy with the root entry alone.
PATCH_ONLY_EARLY = bool((CONFIG.get('build') or {}).get('patch_only', False))
AUTOPATH = AUTOPATH_TREE if BUILD_CFG.get('autopath_recursive') else AUTOPATH_FLAT
if PATCH_ONLY_EARLY:
    AUTOPATH += AUTOPATH_PATCH
elif BUILD_CFG.get('mirror_to_root') and not BUILD_CFG.get('autopath_recursive'):
    AUTOPATH += AUTOPATH_OVERLAY
if LOOSE_ASSETS and LOOSE_ASSET_DIR != 'unencrypted':
    # The loose assets live in a folder of their own; register it so the
    # engine finds the files by their bare names.
    AUTOPATH += (
        '// ==== language slot: the loose language assets ==================\r\n'
        'try { Storages.addAutoPath(System.exePath + "%s/"); } catch (e) { }\r\n'
        % LOOSE_ASSET_DIR)


def text_wrapper(index, tags):
    """TJS that makes the plain message layer draw the patched column.

    ``MessageLayer``/``msghack`` never look at ``d.language``; they only draw
    ``d.text``/``d.disp``.  ``convertSceneText`` is the single place where a
    scenario row becomes that dict, so copying the chosen column here makes the
    slot visible without ``rendermsgwin``.

    The column follows ``kag.languageType`` (the value the engine itself
    saves/restores in ``kag.scflags.languageType``).  Until a language has been
    chosen explicitly, the configured index is used, so a fresh install shows
    the translation immediately.

    A secondary column is appended on a new line when the player picked one in
    the ``[Sub]`` group, mirroring the engine's dual-language message renderer.
    ``subLanguageType < 0`` means "none" and leaves the text untouched.
    """
    tag_list = ', '.join('"%s"' % t for t in tags)
    source = _tjs('text_wrapper.tjs')
    return (source.replace('@TAGS@', tag_list)
            .replace('@INDEX@', str(index)))


def _tjs_string(value):
    return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')


def _tjs_dict(mapping):
    """TJS ``%[ "key" => "value", ... ]`` literal for a string map.

    A quoted key must use ``=>``; ``"key": value`` is not valid TJS2 and makes
    the compiler crash.  Unquoted identifier keys would use ``:``.
    """
    return '%%[ %s ]' % ', '.join(
        '%s => %s' % (_tjs_string(str(key)), _tjs_string(str(value)))
        for key, value in mapping.items())



def language_menu(index, tags, labels, title, default_ui=0,
                  default_ui_tag=None, group_names=None, none_text='None',
                  swap_text='Switch Main and Sub', caption_tables=None,
                  with_ui=True, backlog_sub=False):
    """TJS that adds the missing in-game language selector.

    Titles without native multi-language support ship neither a language menu
    nor ``syslangtext`` tables.  This module is self-contained: the captions
    are emitted from ``syslangtext_<tag>.ini`` (see ``_menu_captions``) and a
    menu-bar walker applies them to every item registered on ``kag``.

    The layout mirrors the engine's own ``multilang.tjs``:

        [Main] <language> ...   (radio group 1, drives kag.languageType)
        -
        [Sub] <language> ...    (radio group 2, drives subLanguageType)
        [Sub] None
        -
        Switch Main and Sub
        -
        [UI] <language> ...     (radio group 3, drives CurrentLanguageUITag)

    ``with_ui=False`` drops the third group and its separator.
    ``backlog_sub`` is accepted for call-site compatibility; the generic
    backlog getter shows the selected main language only.
    """
    tag_list = ', '.join(_tjs_string(t) for t in tags)
    label_list = ', '.join(_tjs_string(l) for l in labels)
    title_js = _tjs_string(title)
    if not default_ui_tag:
        default_ui_tag = tags[default_ui] if 0 <= default_ui < len(tags) else ''
    default_tag_js = _tjs_string(default_ui_tag)
    groups = group_names or ['Main', 'Sub', 'UI']
    group_list = ', '.join(_tjs_string(g) for g in groups)
    none_js = _tjs_string(none_text)
    swap_js = _tjs_string(swap_text)

    # Per-UI-language caption tables.  They are emitted verbatim so the
    # patched title does not need syslangtext.tjs or an INI loader.
    if caption_tables is None:
        caption_tables = {}
    table_js = []
    menu_keys = []
    selector_keys = {
        'textLanguageMenu', 'textLanguageMenuItem', 'subLanguageMenuItem',
        'uiLanguageMenuItem', 'subLanguageMenuItem_none',
        'swapMainSubLangMenuItem',
    }
    for tag in tags:
        table = caption_tables.get(tag) or {}
        table_js.append('%s => %s' % (_tjs_string(tag), _tjs_dict(table)))
        for key in table:
            if key in selector_keys:
                continue
            if key.startswith(('textLanguageMenuItem_',
                               'subLanguageMenuItem_',
                               'uiLanguageMenuItem_')):
                continue
            if key not in menu_keys:
                menu_keys.append(key)
    captions_js = '%%[ %s ]' % ', '.join(table_js)
    menu_keys_js = ', '.join(_tjs_string(k) for k in menu_keys)
    with_ui_js = 'true' if with_ui else 'false'

    source = _tjs('language_menu.tjs')
    source = (source.replace('@TAGS@', tag_list)
              .replace('@LABELS@', label_list)
              .replace('@GROUPS@', group_list)
              .replace('@TITLE@', title_js)
              .replace('@NONE@', none_js)
              .replace('@SWAP@', swap_js)
              .replace('@INDEX@', str(index))
              .replace('@DEFAULT_UI_TAG@', default_tag_js)
              .replace('@DEFAULT_UI@', str(default_ui))
              .replace('@CAPTIONS@', captions_js)
              .replace('@MENU_KEYS@', menu_keys_js)
              .replace('@WITH_UI@', with_ui_js))
    # Top-level ``var`` is script-local in TJS; KAG calls the menu wrapper and
    # the after-init callbacks from other script contexts, where those names
    # are invisible ("Member ... does not exist").  Keep every piece of menu
    # state on ``global`` so the closures resolve it from any context.
    for _name in ('__lpMTags', '__lpMLabels', '__lpMGroupNames',
                  '__lpMMenuTitle', '__lpMNone', '__lpMSwap',
                  '__lpMDefaultTag', '__lpMMenuText', '__lpMMenuKeys',
                  '__lpMOrig', '__lpMOrigCreateMenus'):
        source = source.replace('var ' + _name, 'global.' + _name)
        source = re.sub(r'(?<![\w.])' + _name + r'\b',
                        'global.' + _name, source)
    return source


def multilang_preload(index):
    """TJS that activates the engine's multilingual selector.

    A title can ship ``sysscn/rendermsgwin.tjs``, ``syslangtext.tjs`` and
    ``multilang.tjs`` without ever loading them.  They add the in-game language
    menu and the ``getLangInfo`` renderer.  Loading them is optional: the text
    wrapper above already makes the slot visible.
    """
    return _tjs('multilang_preload.tjs').replace('@@INDEX@@', str(index))


def native_preload():
    """TJS loaded *before* the game's own ``custom.tjs``.

    ``sysscn/rendermsgwin.tjs`` defines ``MsgwinRender``/``RenderMsgwinPlugin``
    and pulls in ``LangRender.tjs`` + ``TagTextConverterEx.tjs``.  The game's
    own ``custom.tjs`` loads ``msghack.tjs``, which subclasses those classes,
    so the renderer has to exist first.
    """
    return _tjs('native_preload.tjs')


def _text_workbook():
    """``kits/<game>/<tag>/text/text.xlsx`` of this profile (may be absent)."""
    return os.path.join(_kit_dir(), 'text', 'text.xlsx')


def name_table():
    """Runtime map ``name_<base> -> name_<tag>`` from the ``Names`` sheet.

    A speaker name doubles as the lookup key for the voice table, the alias
    table and the name plate, so the scenario keeps the base-language name in
    every language column.  Only the string the renderer finally draws is
    translated (``dispNameFilter``); this map feeds that hook.
    """
    path = _text_workbook()
    if not os.path.isfile(path):
        return {}
    try:
        import names_sheet
    except ImportError:
        warn('text workbook present but openpyxl is missing; names stay untranslated')
        return {}
    return names_sheet.read_map(path)


def names_filter(index):
    """TJS that translates the drawn speaker name from the ``Names`` sheet.

    Native multilang builds get the same hook from ``native_activate``; this
    variant is for titles that keep the engine's own name drawing.
    """
    source = _tjs('names_filter.tjs')
    source = source.replace('@@NAME_TABLE@@', _tjs_dict(name_table()))
    return source.replace('@@NAME_INDEX@@', str(index))


def native_activate(index):
    """TJS loaded *after* the game's own ``custom.tjs``.

    ``multilang.tjs`` registers the ``onKAGSetupMenu`` hook that builds the
    ``[Main]``/``[Sub]`` submenu, replaces the message-window renderer at
    afterInit and loads ``syslangtext.tjs`` itself.  Its own afterInit callback
    runs first and seeds ``kag.languageType``/``scflags.uiLanguageType`` from
    ``multiLangFirstTag``; ours then moves the text column to the patched
    variant on the very first launch.
    """
    fragments = {
        '@@LANGUAGE_SWITCH@@': _tjs('language_switch.tjs'),
    }
    source = _tjs('native_activate.tjs')
    for token, fragment in fragments.items():
        source = source.replace(token + '\r\n', fragment)
    source = source.replace('@@NAME_TABLE@@', _tjs_dict(name_table()))
    source = source.replace('@@NAME_INDEX@@', str(index))
    return source.replace('@@INDEX@@', str(index))



def ui_language_menu(with_ui=True, backlog_sub=False, ui_tags=None):
    """TJS that rebuilds the language submenu in the standard layout.

    ``multilang.tjs`` builds only the Main/Sub groups and, unless forced
    multi-language mode is on, hides the swap entry.  The full menu has three
    groups and explicit separators::

        [Main] <language> ...
        -
        [Sub] <language> ...
        [Sub] None
        -
        Switch Main and Sub
        -
        [UI] <language> ...

    ``KAGWindow_createMenus`` calls a global ``KAGWindow_createCustomMenus``
    (``this`` = kag) after the native menu exists, so the whole children array
    is rebuilt there.  Captions come from ``syslangtext_<ui>.ini`` and ordinary
    ``ResetAllMenuLangText`` keeps them translated when the UI changes; the
    native module re-applies the ``[Main]``/``[Sub]`` prefixes itself, this hook
    does the same for ``[UI]``.

    ``with_ui=False`` drops the third group and its separator; profiles use it
    when the interface translation is incomplete.  ``backlog_sub=False`` leaves
    the backlog one-row (the stock layout); enabling it adds a second row from
    the sub language.
    """
    # The [UI] block is optional; a title with a partial interface translation
    # can leave it out and keep Main/Sub only.
    ui_group = (
        '\tout.add("-");\r\n'
        '\t__lpAddLangItems(out, 2, used, bases);\r\n'
    ) if with_ui else (
        '\t// [UI] group disabled by the profile (build.ui_menu_group=false).\r\n'
    )

    # Optional second row in each backlog block.  Off by default: the stock
    # block height was designed for one row.
    backlog_sub_block = ''
    if backlog_sub:
        backlog_sub_block = (
            '\t\t\t\tif (subType >= 0) {\r\n'
            '\t\t\t\t\tvar subText = __lpBacklogText(info, subType);\r\n'
            '\t\t\t\t\tif (subText != "") {\r\n'
            '\t\t\t\t\t\tvar subH = 0;\r\n'
            '\t\t\t\t\t\ttry { subH = rnd.getRect().height; } catch (e) { subH = 0; }\r\n'
            '\t\t\t\t\t\ttry { this.updateRenderOption(subType); } catch (e) { }\r\n'
            '\t\t\t\t\t\trnd.clear();\r\n'
            '\t\t\t\t\t\twith (tr) rnd.draw(lay, .ox, .oy + rnd.defaultRubySize + subH + rnd.defaultLineSpacing, .w, .h, subText, indent);\r\n'
            '\t\t\t\t\t}\r\n'
            '\t\t\t\t}\r\n'
        )
    source = _tjs('ui_language_menu.tjs')
    # The interface languages with the index the engine uses for them: the
    # engine numbers them by their place in the text list.
    ui_map = {}
    for tag in (ui_tags or UI_TAGS):
        if tag in VARIANTS:
            ui_map[tag] = VARIANTS.index(tag)
    ui_map_js = '%%[ %s ]' % ', '.join(
        '%s => %d' % (_tjs_string(tag), index)
        for tag, index in sorted(ui_map.items()))
    return (source.replace('@@BACKLOG_SUB@@', backlog_sub_block)
                  .replace('@@BACKLOG_SUB_ENABLED@@', 'true' if backlog_sub else 'false')
                  .replace('@@UI_GROUP@@', ui_group)
                  .replace('@@UI_TAGS@@', ui_map_js)
                  .replace('@@UI_COMMAND@@', UI_LANGUAGE_COMMAND))


def start_menu_safe():
    """TJS that makes the stock "Show Start Menu" item safe.

    KAG's ``onBootStrapKicked`` calls ``Storages.getLocalName()`` and
    ``System.shellExecute()`` with values that can be ``void`` when the game
    runs from a loose folder; both natives convert their argument to String
    and throw ``Cannot convert the variable type ((void) to string)``.  The
    replacement resolves the local path defensively, tries both known
    ``shellExecute`` signatures and reports failure instead of aborting.
    """
    return _tjs('start_menu_safe.tjs')


LOAD_MULTILANG = BUILD_CFG.get('load_multilang', False)
# Titles whose plain message layer ignores ``d.language`` need the wrapper.
# Titles with a working ``getLangInfo`` renderer must not get it, or the
# in-game selector would stop working (the wrapper pins one column).
# A title without any multi-language tables has, by definition, a plain
# message layer, so the wrapper is the correct default there; profiles that
# ship their own renderer can still turn it off explicitly.
WRAP_TEXT = BUILD_CFG.get('wrap_language_text')
if WRAP_TEXT is None:
    WRAP_TEXT = not HAD_MULTILANG
# Titles that ship no multi-language tables also ship no language menu.  The
# selector is added for them unless the profile says otherwise; it only makes
# sense together with the wrapper, because that is what switches the text.
INJECT_MENU = BUILD_CFG.get('inject_language_menu')
if INJECT_MENU is None:
    INJECT_MENU = not HAD_MULTILANG
# The native subsystem is a complete alternative: it renders the variants,
# builds the menu and localises it.  Mixing it with the hand-written wrapper
# and menu would install two selectors and two message renderers.
if NATIVE_MULTILANG:
    if WRAP_TEXT or INJECT_MENU or LOAD_MULTILANG:
        warn('build.native_multilang replaces the wrapper/menu; '
             'ignoring build.wrap_language_text/inject_language_menu/'
             'load_multilang')
    WRAP_TEXT = False
    INJECT_MENU = False
    LOAD_MULTILANG = False
# A title with native multi-language tables but no UI-language group (the
# titles) already render and switch the text themselves.  In that case
# the hand-written wrapper must stay off, but the native menu still needs the
# [UI] group: append the native-menu reconciler instead of the full selector.
UI_ONLY_MENU = bool(INJECT_MENU and not WRAP_TEXT)

UI_TEXT = LANGUAGE.get('ui_text') or {}

# Last-resort captions.  They are the engine defaults of the base game, which
# is the one the title is built in; a profile should always carry its own
# ``ui_text``.  Falling back
# to the new language here would leak it into every untranslated interface.
UI_TEXT_DEFAULTS = {
    'title': 'テキスト言語(&L)',
    'main': 'メイン',
    'sub': 'サブ',
    'ui': 'UI',
    'none': 'なし',
    'swap': 'メイン/サブを入れ替え',
}
UI_TEXT_LIST_KEYS = ('labels', 'sub_labels', 'ui_labels')


def _merge_ui_text(base, extra):
    """Shallow merge with per-language label maps merged key by key."""
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if key in UI_TEXT_LIST_KEYS:
            labels = dict(merged.get(key) or {})
            labels.update(value or {})
            merged[key] = labels
        elif value not in (None, ''):
            merged[key] = value
    return merged


def _profile_ui_text(ui_tag):
    """Caption set a profile stores for one interface language.

    ``language.ui_text`` is authoritative.  The legacy scalar fields describe
    the new language's own interface and are merged in for backward
    compatibility with profiles generated before ``ui_text`` existed.
    """
    data = _merge_ui_text({}, UI_TEXT.get(ui_tag) or {})
    if ui_tag == TAG:
        legacy = {}
        if LANGUAGE.get('menu_title'):
            legacy['title'] = LANGUAGE['menu_title']
        labels = dict(LANGUAGE.get('menu_labels') or {})
        if labels:
            legacy['labels'] = labels
        groups = LANGUAGE.get('menu_groups') or []
        for _i, _key in enumerate(('main', 'sub', 'ui')):
            if _i < len(groups):
                legacy[_key] = groups[_i]
        if LANGUAGE.get('menu_none'):
            legacy['none'] = LANGUAGE['menu_none']
        if LANGUAGE.get('menu_swap'):
            legacy['swap'] = LANGUAGE['menu_swap']
        data = _merge_ui_text(data, legacy)
    return data


def _strip_accel(text):
    return text.replace('&', '')


def _plain_label(text):
    """A caption without its accelerator hint.

    Two styles are in use: ``&Target`` (the letter is underlined) and
    ``テキスト言語(&L)`` (the letter is shown in brackets).  The label of a
    language is used without the hint, so both are stripped.
    """
    value = re.sub(r'\(\s*&?[^()]*\)', '', text or '')
    return value.replace('&', '').strip()


def _language_name(text_tag, ui_tag):
    """The caption of a language, as the interface language names it.

    The profile knows the new language's name; the other languages are named
    by the caption file of the interface: every interface calls the other
    languages by their own name.  When the interface file does not know the
    language, its own file is asked, so the name is at least the language's
    own name and never the bare tag.
    """
    data = _profile_ui_text(ui_tag)
    labels = data.get('labels') or {}
    name = labels.get(text_tag)
    if name:
        return name
    for reference_tag in (ui_tag, text_tag):
        reference = _template_ini(reference_tag)
        if not reference:
            continue
        entries = _ini_sections(reference).get('menu', {})
        value = entries.get('textLanguageMenuItem_' + text_tag)
        if value:
            return value
    return text_tag.upper()


DEFAULT_UI_TAG = LANGUAGE.get('default_ui') or (UI_TAGS[0] if UI_TAGS else TAG)
if DEFAULT_UI_TAG not in UI_TAGS:
    DEFAULT_UI_TAG = UI_TAGS[0] if UI_TAGS else TAG
DEFAULT_UI_INDEX = UI_TAGS.index(DEFAULT_UI_TAG) if DEFAULT_UI_TAG in UI_TAGS else 0
# The base interface is the game's own language.  Every other interface
# inherits its captions, so a missing INI shows the base language, never the
# target language.
BASE_UI = _merge_ui_text(UI_TEXT_DEFAULTS,
                         _profile_ui_text(DEFAULT_UI_TAG))


def _ui_text(ui_tag):
    """Caption set for one interface language, with base-UI fallbacks."""
    return _merge_ui_text(BASE_UI, _profile_ui_text(ui_tag))


# Fallbacks are taken from the base UI language (the game's main language), so
# a missing INI never turns the selector into placeholders of another
# language.
MENU_TITLE = BASE_UI.get('title')
MENU_GROUPS = [BASE_UI.get('main'), BASE_UI.get('sub'), BASE_UI.get('ui')]
MENU_NONE = BASE_UI.get('none')
MENU_SWAP = BASE_UI.get('swap')


def _ini_sections(path):
    """Parse a ``syslangtext`` INI into ``{section: {key: value}}``.

    Every section is kept: the menu captions are only one part of the file,
    the dialog and system texts live in their own sections and the engine
    looks them up by key.
    """
    try:
        text = _read_ini_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        warn('%s: %s' % (path, exc))
        return {}
    sections = {}
    current = sections.setdefault('', {})
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped.startswith('!'):
            parts = stripped[1:].split(None, 1)
            if len(parts) == 2 and parts[0].lower() == 'section':
                current = sections.setdefault(parts[1].strip(), {})
            continue
        if not stripped or stripped.startswith(('#', ';')):
            continue
        match = re.match(r'^(\S+)[\t ]+(.*)$', line)
        if not match:
            continue
        current[match.group(1)] = match.group(2).strip()
    return sections


def _ini_menu_entries(path):
    """Parse ``!section menu`` from a ``syslangtext`` INI.

    The INI *is* the menu-caption database: the engine's
    ``ResetAllMenuLangText`` resolves every key as ``kag[key]``, so the keys
    are exactly the internal menu item names.  Value-keyed submenu entries
    (``:fsres=auto``) are kept too: the native menu does not name them, so the
    generic walker matches them by ``group``/``value`` instead.
    """
    return _ini_sections(path).get('menu', {})


def _menu_item_text(ui_tag):
    """Menu-bar captions for one interface language.

    The source is ``syslangtext_<tag>.ini`` — the same file the engine reads.
    The template ships reference files under ``assets/`` (one per interface
    language) and a title's own ``assets/`` folder wins.  The profile's
    ``language.ui_menu`` can still override or extend the table for menus
    whose internal names differ from stock KAG.
    """
    table = {}
    base = _find_ini_base(ui_tag)
    if base:
        table.update(_ini_menu_entries(base))
    extra = LANGUAGE.get('ui_menu') or {}
    per_tag = extra.get(ui_tag)
    if isinstance(per_tag, dict):
        table.update(per_tag)
    if ui_tag == TAG:
        for key, value in extra.items():
            if isinstance(value, str):
                table[key] = value
    return {key: value for key, value in table.items()
            if isinstance(value, str) and value != ''}


def _reference_group_label(ui_tag, key, fallback):
    """The group's own caption from the language reference.

    A title that has no language menu of its own still names the groups
    the way its language does: every reference file names the interface
    language in its own wording.
    """
    reference = _template_ini(ui_tag)
    if reference:
        value = _ini_sections(reference).get('menu', {}).get(key)
        if value:
            return value
    return fallback


def _menu_captions(ui_tag):
    """Full ``!section menu`` key map for one interface language."""
    data = _ui_text(ui_tag)
    labels = data.get('labels') or {}
    sub_labels = data.get('sub_labels') or {}
    ui_labels = data.get('ui_labels') or {}
    # The group names come from the profile, then from the language
    # reference: every language calls the groups in its own words.
    ui_group = (data.get('ui')
                or _reference_group_label(ui_tag, 'uiLanguageMenuItem', 'UI'))
    # The INI is authoritative for menu-bar items.  Selector keys that the
    # INI does not mention fall back to the profile's ``ui_text``.
    captions = _menu_item_text(ui_tag)
    defaults = {
        'textLanguageMenu': (data.get('title')
                             or _reference_group_label(ui_tag,
                                                      'textLanguageMenu',
                                                      'テキスト言語(&L)')),
        'textLanguageMenuItem': (data.get('main')
                                 or _reference_group_label(
                                     ui_tag, 'textLanguageMenuItem', 'Main')),
        'subLanguageMenuItem': (data.get('sub')
                                or _reference_group_label(
                                    ui_tag, 'subLanguageMenuItem', 'Sub')),
        'uiLanguageMenuItem': ui_group,
        'subLanguageMenuItem_none': (data.get('none')
                                     or _reference_group_label(
                                         ui_tag, 'subLanguageMenuItem_none',
                                         'None')),
        'swapMainSubLangMenuItem': (data.get('swap')
                                    or _reference_group_label(
                                        ui_tag, 'swapMainSubLangMenuItem',
                                        'Switch Main and Sub')),
    }
    for key, value in defaults.items():
        if value and key not in captions:
            captions[key] = value
    defaults_for_tag = {}
    for text_tag in UI_TAGS:
        if text_tag == TAG:
            # The new language names itself: the profile's caption is the
            # language's own name, not its tag.
            base = labels.get(text_tag) or LABEL
        else:
            # The other languages are named by their own caption file, so
            # the original language is called "日本語" and not "JP".
            base = labels.get(text_tag) or _language_name(text_tag, ui_tag)
        captions.setdefault('textLanguageMenuItem_' + text_tag, base)
        captions.setdefault('subLanguageMenuItem_' + text_tag,
                            sub_labels.get(text_tag) or _plain_label(base))
        # Keep raw labels here, exactly like the engine's INI: the ``[Main]``,
        # ``[Sub]`` and ``[UI]`` prefixes are added at menu-build time (the
        # native module formats Main/Sub, our hook formats UI).  Putting the
        # prefix in the INI would double it after ResetAllMenuLangText.
        captions.setdefault('uiLanguageMenuItem_' + text_tag,
                            ui_labels.get(text_tag) or _plain_label(base))
        defaults_for_tag[text_tag] = (
            base,
            sub_labels.get(text_tag) or _plain_label(base),
            ui_labels.get(text_tag) or _plain_label(base),
        )
    # A value that is only the tag is a placeholder, not a translation: the
    # new language's name has to be the language's own name.
    for text_tag, (main, sub, ui) in defaults_for_tag.items():
        for key, value in (('textLanguageMenuItem_', main),
                           ('subLanguageMenuItem_', sub),
                           ('uiLanguageMenuItem_', ui)):
            name = key + text_tag
            plain = str(captions.get(name, '')).lstrip('&').strip().lower()
            if plain and plain == text_tag.lower():
                captions[name] = value
    return captions


def _read_ini_text(path):
    raw = open(path, 'rb').read()
    # Extracted trees keep some INIs wrapped in the engine's SimpleEncrypt
    # container (``FE FE <mode> FF FE``); unwrap before decoding.
    if SC.is_wrapped(raw):
        raw = SC.decrypt(raw)
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'):
        text = raw.decode('utf-16')
    else:
        text = raw.decode('utf-8-sig')
    return text.lstrip('\ufeff')


_EXTRACTED_INI_CACHE = {}


def _extracted_ini(ui_tag):
    """The title's own ``syslangtext_<tag>.ini`` in the extracted tree.

    The real location depends on the engine build (``main/``, ``locale/``,
    ``%EmptyString%/``, or the archive root), so the known folders are tried
    first and a single cached walk covers everything else.
    """
    name = 'syslangtext_%s.ini' % ui_tag
    for sub in ('main', 'locale', '%EmptyString%', 'system', ''):
        path = (os.path.join(EXTRACT, sub, name) if sub
                else os.path.join(EXTRACT, name))
        if os.path.isfile(path):
            return path
    if name in _EXTRACTED_INI_CACHE:
        return _EXTRACTED_INI_CACHE[name] or None
    found = ''
    try:
        for root, _dirs, files in os.walk(EXTRACT):
            if name in files:
                found = os.path.join(root, name)
                break
    except OSError:
        pass
    _EXTRACTED_INI_CACHE[name] = found
    return found or None


def _title_ini(ui_tag):
    """The caption file the title/kit owns for one interface language.

    For the new language the kit's editable copy is authoritative: it is the
    file the translator edits, and a build that ignored it would silently
    ship the reference captions instead.  For the languages the title ships,
    the extracted file wins so the game's own wording is preserved.
    """
    name = 'syslangtext_%s.ini' % ui_tag
    kit = os.path.join(_kit_dir(), 'ui', name)
    # ``ASSETS`` can be a copy of the kit made by ``kit import``; it is still
    # title-owned, just one step further from the source.
    loose = os.path.join(ASSETS, name)
    extracted = _extracted_ini(ui_tag)
    order = ([kit, extracted, loose] if ui_tag == TAG
             else [extracted, loose, kit])
    for path in order:
        if path and os.path.isfile(path):
            return path
    return None


def _find_ini_base(ui_tag):
    """Reference INI for one interface language.

    The title's own file (or the kit's editable copy) always wins; the
    template reference is only a last resort for a language neither the game
    nor the kit has.
    """
    return _title_ini(ui_tag) or _template_ini(ui_tag)



def _template_ini(ui_tag):
    """The template's own reference INI for one interface language.

    It holds the translated captions of the engine-level texts (menu,
    dialog, system).  A title that has no translation of its own inherits
    them, so a new interface language is never a wall of untranslated
    placeholders.
    """
    path = os.path.join(TEMPLATE, 'assets', 'syslangtext_%s.ini' % ui_tag)
    return path if os.path.isfile(path) else None


def _ini_translations(ui_tag, sections):
    """Translations the template's reference INI provides for *sections*.

    Only keys the title knows are returned: the reference is a caption
    dictionary, not a file the title would read.
    """
    reference = _template_ini(ui_tag)
    if not reference:
        return {}
    ref = _ini_sections(reference)
    out = {}
    for name, entries in sections.items():
        values = {}
        for key, value in ref.get(name, {}).items():
            if key in entries and value:
                values[key] = value
        if values:
            out[name] = values
    return out


def _reference_sections(ui_tag):
    """Every section of the template's reference INI for one language.

    ``assets/syslangtext_<tag>.extra.json`` adds captions the reference INI
    does not carry: the texts of an engine that only some titles use.  The
    file is a plain ``{section: {key: caption}}`` map.
    """
    reference = _template_ini(ui_tag)
    sections = _ini_sections(reference) if reference else {}
    extra = os.path.join(TEMPLATE, 'assets',
                         'syslangtext_%s.extra.json' % ui_tag)
    if os.path.isfile(extra):
        try:
            with open(extra, encoding='utf-8') as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError) as exc:
            warn('reference %s: %s' % (extra, exc))
            data = {}
        for name, entries in data.items():
            if not isinstance(entries, dict):
                continue
            target = sections.setdefault(name, {})
            for key, value in entries.items():
                if isinstance(value, str) and value:
                    target[key] = value
    return sections


def _script_defaults():
    """What the kit's catalogue knows about the title.

    Two things are needed: the texts the scripts use as their own default
    (often the reference wording) and the keys the title really has.  A
    reference caption is only inherited when the title knows the key --
    otherwise a language would gain entries the game never asks for.
    """
    kit = _kit_dir()
    if not kit:
        return {}, set()
    path = os.path.join(kit, 'ui', 'catalog.json')
    if not os.path.isfile(path):
        return {}, set()
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        warn('catalogue %s: %s' % (path, exc))
        return {}, set()
    defaults = {}
    keys = set()
    for entry in data.get('entries') or []:
        key = entry.get('key')
        if not key:
            continue
        keys.add(key)
        default = entry.get('default')
        if default:
            defaults[key] = default
    # Keys the scripts ask for without a literal default.
    keys.update(data.get('requested') or [])
    return defaults, keys


def _kit_dir():
    """The kit of this profile (``kits/<slug>/<tag>``)."""
    slug = os.path.splitext(
        os.path.basename(CONFIG.get('_config_path') or 'config.json'))[0]
    return os.path.join(TEMPLATE, 'kits', slug, TAG)


def _build_syslang_ini(ui_tag):
    """Return the UTF-16 text of ``syslangtext_<ui_tag>.ini``.

    The title's own file is the base.  The template reference may only fill
    keys the title does not have; it never replaces a value the title (or the
    kit) wrote.  A language the title does not ship is derived from the base
    text language, and only values still in that language are translated.
    """
    own = _title_ini(ui_tag) if SYS_ING_BASE else None
    base = own
    # Languages the title does not ship start from the base text language
    # (the base language when available), not from an empty file.
    source_tag = BASE_TAG if (BASE_TAG and BASE_TAG != TAG) else DEFAULT_UI_TAG
    if not base and SYS_ING_BASE:
        base = _find_ini_base(source_tag)
    sections = _ini_sections(base) if base else {}
    if not sections:
        sections = {'menu': {}}
    captions = {}
    for name, entries in sections.items():
        values = dict(entries)
        if name == 'menu':
            values.update(_menu_captions(ui_tag))
        captions[name] = values
    reference = _reference_sections(ui_tag)
    flat = {}
    for entries in reference.values():
        for key, value in entries.items():
            flat.setdefault(key, value)
    defaults, known = _script_defaults()
    # The names of the languages are the title's own business: they are
    # filled by the selector captions, not by the reference.
    _is_lang_name = re.compile(
        r'^(?:text|sub|ui)LanguageMenuItem_[^_]+$')

    def present():
        keys = set()
        for entries in captions.values():
            keys.update(entries)
        return keys

    def fill_missing():
        have = present()
        for name, entries in reference.items():
            for key, value in entries.items():
                if _is_lang_name.match(key) or key in have:
                    continue
                captions.setdefault(name, {})[key] = value
                have.add(key)

    if own:
        # A real file for this language exists: keep every value it carries.
        fill_missing()
    elif ui_tag == DEFAULT_UI_TAG:
        # No title file at all: the reference is the only source.  Script
        # defaults are often the reference wording, so they may be translated.
        fill_missing()
        for entries in captions.values():
            for key, value in list(entries.items()):
                if defaults.get(key) == value and key in flat:
                    entries[key] = flat[key]
    else:
        # Derived from the base text language.  Only a value that is still
        # identical to the base file's value is replaced; a real translation
        # already present always wins.
        source_path = _find_ini_base(source_tag)
        source = _ini_sections(source_path) if source_path else {}
        source_flat = {}
        for entries in source.values():
            for key, value in entries.items():
                source_flat.setdefault(key, value)
        for entries in captions.values():
            for key, value in entries.items():
                if key in flat and value == source_flat.get(key):
                    entries[key] = flat[key]
        fill_missing()
    if base:
        return _replace_sections(_read_ini_text(base), captions)
    lines = []
    for name, values in captions.items():
        lines.append('!section %s' % name)
        for key in sorted(values):
            lines.append('%s\t%s' % (key, values[key]))
    return '\r\n'.join(lines)



def _split_ini_blocks(text):
    """Split an INI into ``(section, lines)`` blocks.

    The head of the file (everything before the first ``!section``) is the
    block with the empty name; comments and ``!ignore`` lines stay where they
    are.
    """
    blocks = []
    name = ''
    lines = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped.lower().startswith('!section'):
            blocks.append((name, lines))
            name = stripped[len('!section'):].strip()
            lines = [line]
            continue
        lines.append(line)
    blocks.append((name, lines))
    return blocks


def _rewrite_ini_block(lines, captions):
    """Replace the known keys of one INI block and append the missing ones."""
    out = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(('#', ';', '!')):
            match = re.match(r'^([^\s]+)[\t ]+', line)
            if match:
                key = match.group(1)
                if key in captions:
                    out.append('%s\t%s' % (key, captions[key]))
                    seen.add(key)
                    continue
        out.append(line)
    missing = [key for key in captions if key not in seen]
    if missing:
        while out and not out[-1].strip():
            out.pop()
        for key in missing:
            out.append('%s\t%s' % (key, captions[key]))
    return out


def _replace_sections(text, sections):
    """Replace/add captions in every section of an INI.

    A section the file does not have yet is appended, so a title whose own
    INI only knows the menu still receives the dialog texts.
    """
    blocks = _split_ini_blocks(text)
    out = []
    written = set()
    for name, lines in blocks:
        if name in sections:
            written.add(name)
            out.extend(_rewrite_ini_block(lines, sections[name]))
        else:
            out.extend(lines)
    for name, captions in sections.items():
        if name in written:
            continue
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append('')
        out.append('!section %s' % name)
        for key in sorted(captions):
            out.append('%s\t%s' % (key, captions[key]))
    return '\r\n'.join(out)


def _replace_menu_section(text, captions):
    """Replace/add menu captions inside the ``!section menu`` block."""
    return _replace_sections(text, {'menu': captions})


def _write_utf16_bom(path, text):
    with open(path, 'wb') as handle:
        handle.write(('\ufeff' + text).encode('utf-16le'))


MENU_TAG_LABELS = []
for _tag in (VARIANTS if MULTILANG else []):
    _label = (BASE_UI.get('labels') or {}).get(_tag)
    if not _label:
        _label = LABEL if _tag == TAG else _tag.upper()
    MENU_TAG_LABELS.append(_label)

def name_font():
    """TJS that makes the speaker name plate use the message text's face.

    The message text is re-rendered by the multi-language engine, but the
    name plate is a plain ``NameLayer`` and copies ``userFace`` -- the
    interface font.  Wrapping ``MessageLayer.resetNameLayerFont`` lets the
    plate copy the face the text was just given, so the two always match.
    ``language.name_font.face`` is optional and overrides that default.
    """
    if not MULTILANG:
        return ''
    size = ''
    if NAME_FONT_SIZE not in (None, ''):
        size = ('\ttry { font.height = int(%s); } catch (e) { }\r\n'
                % NAME_FONT_SIZE)
    elif NAME_FONT_SCALE not in (None, ''):
        size = ('\ttry { font.height = int(layer.defaultFontSize * %s + 0.5); }'
                ' catch (e) { }\r\n' % NAME_FONT_SCALE)
    bold = ''
    if NAME_FONT_BOLD is not None:
        bold = ('\ttry { font.bold = %s; } catch (e) { }\r\n'
                % ('true' if NAME_FONT_BOLD else 'false'))
    return (_tjs('name_font.tjs')
            .replace('@@TAG@@', _tjs_string(TAG))
            .replace('@@BASE_TAG@@', _tjs_string(BASE_TAG))
            .replace('@@FACE@@', _tjs_string(str(NAME_FONT_FACE)))
            .replace('@@SIZE@@', size.rstrip('\r\n'))
            .replace('@@BOLD@@', bold.rstrip('\r\n')))


custom_tjs = AUTOPATH
if MULTILANG and NATIVE_MULTILANG:
    # rendermsgwin.tjs must exist before the game loads msghack.tjs, which
    # subclasses MsgwinRender/RenderMsgwinPlugin.
    custom_tjs += native_preload()
if MULTILANG and WRAP_TEXT:
    custom_tjs += text_wrapper(LANGUAGE.get('index', 1), VARIANTS)
if MULTILANG and INJECT_MENU and not UI_ONLY_MENU:
    _menu_default_index = (VARIANTS.index(DEFAULT_UI_TAG)
                           if DEFAULT_UI_TAG in VARIANTS else 0)
    custom_tjs += language_menu(LANGUAGE.get('index', 1), VARIANTS,
                                MENU_TAG_LABELS, MENU_TITLE,
                                default_ui=_menu_default_index,
                                default_ui_tag=DEFAULT_UI_TAG,
                                group_names=MENU_GROUPS,
                                none_text=MENU_NONE, swap_text=MENU_SWAP,
                                caption_tables={t: _menu_captions(t)
                                                for t in VARIANTS},
                                with_ui=UI_MENU_GROUP,
                                backlog_sub=BACKLOG_SUB_LANGUAGE)
if MULTILANG and LOAD_MULTILANG:
    custom_tjs += multilang_preload(LANGUAGE.get('index', 1))
def _ui_lookup_wrapper(name, kind):
    """TJS that serves the new language's interface images from the patch.

    The title resolves its interface images through a global function; the
    wrapper hands it the new language's file.  The file lives in the patch
    archive, and the archive is reached through the game's own media: the
    bundled loader redirects ``arc://./<path>`` to ``patch.xp3><path>``, so
    the name is passed on in that form.

    A graphic that was never edited is not shipped (a rebuilt but untouched
    PSB is not the same picture), so the wrapper falls back to the title's
    reference graphic -- and to the title's own graphic when it has only one
    language.

    The function name is written out literally: TJS2 has no closures, so a
    generated wrapper cannot see the name of the function it replaces.
    """
    quoted = _tjs_string(name)
    if kind == 'psd':
        body = (
            '\t\tglobal.%s = function (psd, lang) {\r\n'
            '\t\t\tif (lang === void) lang = global.CurrentLanguageUITag;\r\n'
            '\t\t\tvar r = global.__lpLangState.lookups[%s](psd, lang);\r\n'
            '\t\t\t__lpBridgeLog("lookup %s(" + psd + ", " + lang + ") -> "\r\n'
            '\t\t\t\t+ r);\r\n'
            '\t\t\t// Only the new language is served from the patch; every\r\n'
            '\t\t\t// other language keeps the title\'s own graphic.\r\n'
            '\t\t\tif (lang != %s) return r;\r\n'
            '\t\t\tvar candidate = psd + "_" + lang + ".pimg";\r\n'
            '\t\t\tvar path = __lpLangAssetPath(candidate);\r\n'
            '\t\t\t// The patch root is registered as an auto path, so the\r\n'
            '\t\t\t// engine resolves the bare name exactly like the\r\n'
            '\t\t\t// title\'s own <psd>_<lang>.  A "|" composite only\r\n'
            '\t\t\t// accepts storage names, not "<archive>><name>".\r\n'
            '\t\t\tif (path != "") return psd + "|" + psd + "_" + lang;\r\n'
            '\t\t\t// Not edited: use the english graphic instead of the\r\n'
            '\t\t\t// title\'s own one.\r\n'
            '\t\t\tvar en = global.__lpLangState.lookups[%s](psd, "en");\r\n'
            '\t\t\tif (en != psd) return en;\r\n'
            '\t\t\treturn r;\r\n'
            '\t\t};\r\n'
        ) % (name, quoted, name, _tjs_string(TAG), quoted)
    else:
        body = (
            '\t\tglobal.%s = function (fname, defext, tag) {\r\n'
            '\t\t\tif (tag === void) tag = global.CurrentLanguageUITag;\r\n'
            '\t\t\tvar r = global.__lpLangState.lookups[%s](fname, defext, tag);\r\n'
            '\t\t\t__lpBridgeLog("lookup %s(" + fname + ", " + defext\r\n'
            '\t\t\t\t+ ", " + tag + ") -> " + r);\r\n'
            '\t\t\t// Only the new language is served from the patch; every\r\n'
            '\t\t\t// other language keeps the title\'s own graphic.\r\n'
            '\t\t\tif (tag != %s) return r;\r\n'
            '\t\t\tvar chop = "";\r\n'
            '\t\t\tvar ext = "";\r\n'
            '\t\t\ttry { chop = Storages.chopStorageExt(fname); }\r\n'
            '\t\t\tcatch (e) { chop = fname; }\r\n'
            '\t\t\ttry { ext = Storages.extractStorageExt(fname); }\r\n'
            '\t\t\tcatch (e) { ext = ""; }\r\n'
            '\t\t\tif (ext == "") ext = defext;\r\n'
            '\t\t\tif (typeof chop == "String" && typeof ext == "String") {\r\n'
            '\t\t\t\tvar candidate = chop + "_" + tag + ext;\r\n'
            '\t\t\t\tvar path = __lpLangAssetPath(candidate);\r\n'
            '\t\t\t\t// Bare name: the patch root is an auto path and the\r\n'
            '\t\t\t\t// engine opens it through Storages like any other\r\n'
            '\t\t\t\t// interface picture.\r\n'
            '\t\t\t\tif (path != "") return candidate;\r\n'
            '\t\t\t}\r\n'
            '\t\t\t// Not edited: use the english graphic instead of the\r\n'
            '\t\t\t// title\'s own one.\r\n'
            '\t\t\tvar en = global.__lpLangState.lookups[%s](fname, defext, "en");\r\n'
            '\t\t\tif (en != fname) return en;\r\n'
            '\t\t\treturn r;\r\n'
            '\t\t};\r\n'
        ) % (name, quoted, name, _tjs_string(TAG), quoted)
    return (
        'function __lpInstallLookup_%s() {\r\n'
        '\tvar orig = void;\r\n'
        '\ttry { orig = global.%s; } catch (e) { orig = void; }\r\n'
        '\tif (typeof orig != "Object") return false;\r\n'
        '\tglobal.__lpLangState.lookups[%s] = orig;\r\n'
        '%s'
        '\treturn true;\r\n'
        '}\r\n'
    ) % (name, name, quoted, body)


def language_assets():
    """TJS that loads the interface files the engine cannot see itself.

    The patch archive carries the new language's ``syslangtext_<tag>.ini``.
    Its name is unknown to the game, so the game's media cannot see it; the
    archive path does.  This module merges the INI into SystemLanguageText --
    the dictionary the engine's own LoadSysLangText() fills -- so the
    captions and the text-to-speech language name behave as if the game
    could read the file.
    """
    tags = ', '.join(_tjs_string(t) for t in UI_TAGS)
    hooks = BUILD_CFG.get('ui_asset_hooks') or []
    if not BUILD_CFG.get('ui_asset_wrap', False):
        hooks = []
    blocks = []
    calls = []
    for spec in hooks:
        name, _sep, kind = str(spec).partition(':')
        if kind != 'psd':
            kind = 'file'
        blocks.append(_ui_lookup_wrapper(name, kind))
        calls.append('\ttry { __lpInstallLookup_%s(); } catch (e) { }\r\n'
                     % name)
    source = _tjs('language_assets.tjs')
    return (source
            .replace('@@ASSET_DIR@@',
                     _tjs_string((ASSET_SUBDIR + '/') if ASSET_SUBDIR else ''))
            .replace('@@UI_TAGS@@', '[%s]' % tags)
            .replace('@@UI_LOOKUPS@@', ''.join(blocks))
            .replace('@@UI_LOOKUP_CALLS@@', ''.join(calls)))


custom_tjs += read_plain('custom')
if MULTILANG:
    # The name plate keeps the base language's font unless it is replaced;
    # it is drawn outside the multi-language text renderer.
    custom_tjs += name_font()
if MULTILANG and NAME_IMAGES:
    # The title draws speaker names as pictures.  The new text language uses
    # ordinary text names instead, so the name can be translated and uses the
    # same face as the message text.
    custom_tjs += _tjs('name_images.tjs').replace('@@TAG@@', _tjs_string(TAG))
if MULTILANG and BUILD_CFG.get('dialog_captions', True):
    # The engine's message boxes carry a caption that is hard-coded in C++
    # ("Information").  Wrapping the two entry points gives the dialog a
    # caption from the language table.
    custom_tjs += _tjs('dialog_caption.tjs')
if MULTILANG and (INJECT_MENU or NATIVE_MULTILANG) \
        and BUILD_CFG.get('language_assets', True):
    # The interface INIs live in the patch archive.  The engine finds them
    # itself when the archive folder is registered as an auto path; this
    # module adds the ones it cannot see.
    custom_tjs += language_assets()
if MULTILANG and BUILD_CFG.get('ui_storage_wrap'):
    # Titles without GetUIImage()/GetUIFile() ask for the original graphic
    # everywhere.  The patch ships the translated copies beside it under a
    # language suffix; these two loaders are where every interface and scene
    # image passes, so the name is rewritten there when the variant exists.
    custom_tjs += (_tjs('ui_storage_wrap.tjs')
                   .replace('@@TAG@@', _tjs_string(TAG))
                   .replace('@@BASE_TAG@@', _tjs_string(BASE_TAG)))
if MULTILANG and (INJECT_MENU or NATIVE_MULTILANG):
    # Language-neutral UI apply/reload helper.  Shared by the injected menu
    # and the native-menu reconciliation; defines global.__lpApplyUILanguage.
    custom_tjs += _tjs('ui_language_apply.tjs').replace(
        '@@TAG@@', _tjs_string(TAG))
if MULTILANG and NATIVE_MULTILANG:
    # multilang.tjs hooks the menu and the afterInit phase; it has to come
    # after the game body so the game's own handlers are already registered.
    custom_tjs += native_activate(LANGUAGE.get('index', 1))
elif MULTILANG:
    # Titles that draw names with the engine's own path still get the
    # translated label from the workbook's Names sheet.
    custom_tjs += names_filter(LANGUAGE.get('index', 1))
if MULTILANG and (NATIVE_MULTILANG or UI_ONLY_MENU):
    # Reconcile the native language submenu with the standard layout.  UI-only
    # profiles skip native_activate because the game's own custom.tjs already
    # loaded its multilang.tjs; only the missing [UI] group is added.
    custom_tjs += ui_language_menu(UI_MENU_GROUP,
                                   BACKLOG_SUB_LANGUAGE,
                                   ui_tags=LANGUAGE.get('ui_languages')
                                   or UI_TAGS)
if MULTILANG and (NATIVE_MULTILANG or INJECT_MENU):
    # KAG's stock "Show Start Menu" handler can pass void to native String
    # arguments; replace only that one command, never the whole menu.
    custom_tjs += start_menu_safe()
if CUSTOM_APPEND and os.path.isfile(CUSTOM_APPEND):
    with open(CUSTOM_APPEND, encoding='utf-8') as handle:
        custom_tjs = custom_tjs.rstrip() + '\r\n\r\n' + handle.read()

if MULTILANG and UI_LANGUAGE_CONFIRM:
    # The title has no confirming member of its own; the reference
    # implementation is ported here.  It goes last: the function is only
    # called when the player picks the interface language.
    custom_tjs += _tjs('ui_language_confirm.tjs')

# --------------------------------------------------------------------------
# 4. pack scripts and stage the loose files
# --------------------------------------------------------------------------
# A patch.xp3 is a normal archive: it may contain folders and arbitrary paths.
# Some titles only see assets inside a mounted archive (or the opposite), so
# ``build.assets_in_patch`` chooses where the language assets live.  The
# default keeps them loose, matching the long-verified layout.
PACK_ASSETS = bool((CONFIG.get('build') or {}).get('assets_in_patch', False))
# Patch-only deployment: scripts, interface INIs and scenario overrides all
# live inside patch.xp3, and no unencrypted/ folder is produced.  This is the
# safest layout for titles whose engine already mounts patch.xp3.
PATCH_ONLY = bool((CONFIG.get('build') or {}).get('patch_only', False))
PACK_SCN = PATCH_ONLY or bool((CONFIG.get('build') or {}).get(
    'scn_in_patch', PACK_ASSETS))

shutil.rmtree(BUILD, ignore_errors=True)
os.makedirs(SRC)
os.makedirs(os.path.join(SRC, SCRIPT_OUT), exist_ok=True)
loose = os.path.join(BUILD, 'unencrypted')
if not PATCH_ONLY:
    os.makedirs(loose)

open(os.path.join(SRC, SCRIPT_OUT, os.path.basename(SCRIPT_FILES['default'])), 'wb').write(wrap_plain(default))
open(os.path.join(SRC, SCRIPT_OUT, os.path.basename(SCRIPT_FILES['storages'])), 'wb').write(wrap_plain(storages))
open(os.path.join(SRC, SCRIPT_OUT, os.path.basename(SCRIPT_FILES['custom'])), 'wb').write(wrap_plain(custom_tjs))

if SCRIPT_ALSO_ROOT and SCRIPT_OUT:
    for key, text in (('default', default), ('storages', storages),
                      ('custom', custom_tjs)):
        name = os.path.basename(SCRIPT_FILES[key])
        open(os.path.join(SRC, name), 'wb').write(wrap_plain(text))

def _work_file(rel_name):
    """True for the bookkeeping files that never belong into the archive."""
    name = rel_name.rsplit('/', 1)[-1]
    return name.startswith('.') or name.endswith('.tmp')


def stage_tree(folder, target_root, skip=None):
    """Copy *folder* into *target_root*, keeping every subdirectory."""
    count = 0
    for base, _dirs, files in os.walk(folder):
        rel = os.path.relpath(base, folder)
        target_dir = target_root if rel == '.' else os.path.join(target_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(files):
            if skip is not None:
                rel_name = name if rel == '.' else os.path.join(rel, name)
                if skip(rel_name.replace(os.sep, '/')):
                    continue
            shutil.copyfile(os.path.join(base, name),
                            os.path.join(target_dir, name))
            count += 1
    return count


def _stage_assets(target_root, plan):
    """Copy the planned assets into *target_root*."""
    count = 0
    for rel, target in sorted(plan.items()):
        source = os.path.join(ASSETS, rel.replace('/', os.sep))
        dest = os.path.join(target_root, target.replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(source, dest)
        count += 1
    return count


staged = 0
assets_in_patch = PACK_ASSETS or PATCH_ONLY
asset_root = SRC if assets_in_patch else loose
_asset_plan_files = _asset_plan()
if os.path.isdir(ASSETS):
    if assets_in_patch:
        staged += _stage_assets(SRC, _asset_plan_files)
    else:
        # A loose folder is enumerated by the auto-path walk, so the assets
        # keep the game's own paths there.
        staged += _stage_assets(
            loose, {rel: rel for rel in _asset_plan_files})
        # Interface INIs are opened through Storages, which also searches
        # mounted archives.  Keep a copy inside patch.xp3 so a
        # patch.xp3-only deployment still resolves
        # ``syslangtext_<tag>.ini``.
        for _name in sorted(os.listdir(ASSETS)):
            _low = _name.lower()
            if _low.startswith('syslangtext_') and _low.endswith('.ini'):
                _dest_dir = os.path.join(SRC, ASSET_SUBDIR) if ASSET_SUBDIR else SRC
                os.makedirs(_dest_dir, exist_ok=True)
                shutil.copyfile(os.path.join(ASSETS, _name),
                                os.path.join(_dest_dir, _name))
if LOOSE_ASSETS:
    # The files whose names the game does not know are delivered as real
    # files: the engine finds them by their bare names through the auto-path
    # table, which is built from the contents of a folder.
    loose_root = os.path.join(BUILD, LOOSE_ASSET_DIR)
    os.makedirs(loose_root, exist_ok=True)
    for _rel, _target in sorted(_asset_plan_files.items()):
        if not (ASSET_SUBDIR and _target.startswith(ASSET_SUBDIR + '/')):
            continue
        shutil.copyfile(os.path.join(ASSETS, _rel.replace('/', os.sep)),
                        os.path.join(loose_root,
                                     os.path.basename(_target)))
    for _ui_tag in UI_TAGS:
        _ini = os.path.join(SRC, ASSET_SUBDIR, 'syslangtext_%s.ini' % _ui_tag)
        if os.path.isfile(_ini):
            shutil.copy2(_ini, os.path.join(
                loose_root, 'syslangtext_%s.ini' % _ui_tag))
    print('staged %d loose language asset(s) into %s/'
          % (len(os.listdir(loose_root)), LOOSE_ASSET_DIR))
scn_target = SRC if PACK_SCN else loose
scn_staged = stage_tree(SCN, scn_target, skip=_work_file) \
    if os.path.isdir(SCN) else 0

# Per-interface-language menu captions.  syslangtext.tjs reads these through
# Storages, which only finds a file when the engine can build a placed path
# for it.  A name the game does not know needs the auto-path table, so the
# INIs are written into the asset subfolder (see ASSET_SUBDIR); loose
# deployments keep them in the loose folder instead, whose files are
# enumerated by the auto-path walk.
#
# The INIs are written *after* the assets, so the generated captions win
# over the untouched copy that ships in assets/.
if MULTILANG and (INJECT_MENU or NATIVE_MULTILANG):
    _lang_names = {}
    for _ui_tag in UI_TAGS:
        _ini_text = _build_syslang_ini(_ui_tag)
        if not PATCH_ONLY:
            _write_utf16_bom(
                os.path.join(loose, 'syslangtext_%s.ini' % _ui_tag),
                _ini_text)
        _ini_dir = os.path.join(SRC, ASSET_SUBDIR) if ASSET_SUBDIR else SRC
        os.makedirs(_ini_dir, exist_ok=True)
        _write_utf16_bom(os.path.join(_ini_dir, 'syslangtext_%s.ini' % _ui_tag),
                         _ini_text)
        _lang_names[_ui_tag] = {}
        for _entries in _ini_sections(
                os.path.join(_ini_dir, 'syslangtext_%s.ini' % _ui_tag)
        ).values():
            for _key in _entries:
                _match = re.match(r'^(?:text|sub|ui)LanguageMenuItem_(.+)$', _key)
                if _match:
                    _lang_names[_ui_tag].setdefault(
                        _match.group(1), set()).add(_key)
    # Every interface language has to name every language, otherwise the
    # selector of one interface shows an entry the other one does not have.
    for _ui_tag, _entries in _lang_names.items():
        for _tag in sorted(_entries):
            _expected = set()
            for _other in _lang_names.values():
                _expected |= _other.get(_tag, set())
            _missing = sorted(_expected - _entries[_tag])
            if _missing:
                warn('syslangtext_%s.ini does not name %s: %s'
                     % (_ui_tag, _tag, ', '.join(_missing)))
    print('staged syslangtext INI for interface language(s): %s%s'
          % (', '.join(UI_TAGS),
             ' (in %s/)' % ASSET_SUBDIR if ASSET_SUBDIR else ''))
print('staged %d asset file(s), %d scenario override(s) into %s'
      % (staged, scn_staged,
         'patch.xp3' if PACK_SCN else 'unencrypted/'))

# A loose file at its *real* path under the game folder is resolved by the
# engine's first placed-path step (the fully normalized name exists), which
# always beats every archive copy.  Only folders that are registered as an
# overlay autopath are mirrored, so no inert copy of the engine's own script
# folders is left next to the executable.
MIRROR_TO_ROOT = bool(BUILD_CFG.get('mirror_to_root', False)) \
    and not PATCH_ONLY
if MIRROR_TO_ROOT:
    mirror_root = os.path.join(BUILD, 'mirror')
    os.makedirs(mirror_root, exist_ok=True)
    m_scn = stage_tree(SCN, mirror_root, skip=_work_file) \
        if os.path.isdir(SCN) else 0
    m_assets = 0
    if os.path.isdir(ASSETS):
        for name in sorted(os.listdir(ASSETS)):
            src_dir = os.path.join(ASSETS, name)
            if os.path.isdir(src_dir) and name in _OVERLAY_DIRS:
                m_assets += stage_tree(src_dir, os.path.join(mirror_root, name))
    print('mirrored %d scenario + %d asset override(s) to the game root'
          % (m_scn, m_assets))

patch_out = os.path.join(BUILD, 'patch.xp3')
# Marker file: the deploy step looks for it to tell "our" archive from a
# foreign patch.xp3 that must not be overwritten.
with open(os.path.join(SRC, PATCH_MARKER), 'w', encoding='utf-8') as handle:
    handle.write(PATCH_MARKER_TEXT)
# The index is flat and sorted by the full path, which is what the engine
# expects: it binary-searches the archive names for a folder prefix when it
# builds the auto-path table.
xp3_pack.pack_folder(SRC, patch_out)
print('built %s (%d bytes)' % (patch_out, os.path.getsize(patch_out)))

# A patch whose language assets are all reference copies is valid but the new
# slot has nothing of its own; make that visible instead of shipping silently.
tagged_assets = []
for root in (SRC, loose):
    if not os.path.isdir(root):
        continue
    for base, _dirs, files in os.walk(root):
        for name in files:
            stem, ext = os.path.splitext(name)
            if stem.lower().endswith('_' + TAG):
                tagged_assets.append(os.path.relpath(os.path.join(base, name),
                                                     root))
if not tagged_assets:
    warn('no _%s asset in %s; run: langpatch assets-seed'
         % (TAG, os.path.relpath(ASSETS, TEMPLATE)))

shutil.copyfile(LOADER, os.path.join(BUILD, 'version.dll'))

# --------------------------------------------------------------------------
# 5. optional: refresh the isolated test copy
# --------------------------------------------------------------------------
if TEST and CONFIG.get('stage_to_test', True) and '--no-stage' not in sys.argv:
    exe = gamecfg.detect_exe(GAME, CONFIG.get('game_exe')) if GAME else None
    exe_name = os.path.basename(exe) if exe else 'game.exe'
    subprocess.run(['taskkill', '/F', '/IM', exe_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for f in ('patch.xp3', 'version.dll'):
        try:
            os.remove(os.path.join(TEST, f))
        except FileNotFoundError:
            pass
    shutil.rmtree(os.path.join(TEST, 'unencrypted'), ignore_errors=True)
    shutil.copyfile(patch_out, os.path.join(TEST, 'patch.xp3'))
    shutil.copyfile(LOADER, os.path.join(TEST, 'version.dll'))
    if os.path.isdir(loose):
        shutil.copytree(loose, os.path.join(TEST, 'unencrypted'))
    print('staged into %s' % TEST)
