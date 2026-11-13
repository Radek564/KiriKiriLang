"""Shared PSB load / build helpers.

Scenarios are PSB files (``*.txt.scn``) with no embedded resources.  This
module wraps the native :mod:`psb` reader/writer and keeps a decompiled-JSON
cache keyed by the ``.scn`` modification time, which makes repeated Excel
export runs fast.
"""
import json
import os
import re

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys
SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import psb


def decompile(src, tmp=None):
    """Return the PSB JSON (kept for API compatibility; *tmp* is ignored)."""
    return psb.load_file(src).to_json()


def build(json_path, out_psb):
    """Compile a JSON file into *out_psb* (kept for API compatibility)."""
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
    psb.build_file(out_psb, data)


def load_scn(src, cache_dir=None):
    """Return the decompiled JSON dict for *src*.

    When *cache_dir* is given, a JSON copy is kept there and reused while it
    is at least as new as the `.scn`.  PSB builds are fast now, but this still
    saves parsing on every export.
    """
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache = os.path.join(cache_dir, os.path.basename(src) + '.json')
        if (os.path.isfile(cache)
                and os.path.getmtime(cache) >= os.path.getmtime(src)):
            try:
                with open(cache, encoding='utf-8') as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        data = psb.load_file(src).to_json()
        with open(cache, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False)
        return data

    return psb.load_file(src).to_json()


def drop_cache(src, cache_dir):
    if not cache_dir:
        return
    cache = os.path.join(cache_dir, os.path.basename(src) + '.json')
    try:
        os.remove(cache)
    except FileNotFoundError:
        pass


def save_and_build(data, src, cache_dir=None):
    """Write *data* to a PSB built over *src*.  Returns the new byte size."""
    parsed = psb.load_file(src)
    blob = psb.build(data, parsed.version,
                     resources=parsed.resources,
                     extra_resources=parsed.extra_resources)
    tmp = src + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, src)
    drop_cache(src, cache_dir)
    return len(blob)


def text_variants(entry):
    """Return the language-variant list inside one ``scene.texts`` entry.

    Two layouts are in use across KrKrz titles:

    * ``[speaker, variants, voice, delay, kag]`` — the common form;
    * ``[speaker_id, speaker_display, variants, voice, delay, kag]`` — the
      another form, which adds a localised speaker-name column
      before the variants.

    The variant list is the first child that is a non-empty list of lists
    (each variant is itself ``[speaker, text, ...]``).  Checking the shape
    instead of a fixed index keeps this language-neutral and works for both
    layouts without a per-title switch.
    """
    if not isinstance(entry, list):
        return None
    for value in entry:
        if not isinstance(value, list) or not value:
            continue
        if any(isinstance(v, list) for v in value) and all(
                v is None or isinstance(v, list) for v in value):
            return value
    return None


def iter_texts(data):
    """Yield (scene_index, scene_label, line_index, entry) for dialogue rows."""
    for si, scene in enumerate(data.get('scenes') or []):
        texts = scene.get('texts')
        if not isinstance(texts, list):
            continue
        label = scene.get('label') or '*scene%d' % si
        for ti, entry in enumerate(texts):
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            variants = text_variants(entry)
            if not variants:
                continue
            yield si, label, ti, entry


def iter_choices(data):
    """Yield (scene_index, scene_label, choice_index, select_dict).

    Some engines store branch metadata in the same ``selects`` array as real
    buttons (``{"block": ..., "selidx": ...}``).  Those entries have neither
    text nor language variants and must not become workbook rows.
    """
    for si, scene in enumerate(data.get('scenes') or []):
        label = scene.get('label') or '*scene%d' % si
        for ci, sel in enumerate(scene.get('selects') or []):
            if not isinstance(sel, dict):
                continue
            if not sel.get('text') and not isinstance(sel.get('language'), list):
                continue
            yield si, label, ci, sel


def iter_titles(data):
    """Yield (scene_index, scene_label, title_list) for localised titles.

    Only list titles carry per-language strings; a plain-string title is
    language-neutral and is not exported.
    """
    for si, scene in enumerate(data.get('scenes') or []):
        title = scene.get('title')
        if isinstance(title, list) and len(title) > 1:
            yield si, scene.get('label') or '*scene%d' % si, title


def choice_text(sel):
    return str(sel.get('text') or '') if isinstance(sel, dict) else ''


def choice_lang(sel, index):
    lang = sel.get('language') if isinstance(sel, dict) else None
    if isinstance(lang, list) and len(lang) > index and isinstance(lang[index], dict):
        return lang[index]
    return None


def choice_lang_text(sel, index):
    info = choice_lang(sel, index)
    return str(info.get('text') or '') if info else ''


def title_at(scene, index):
    title = scene.get('title')
    if isinstance(title, list) and len(title) > index:
        value = title[index]
        return str(value) if value is not None else ''
    if isinstance(title, str) and index == 0:
        return title
    return ''


def slot(variants, index):
    """Return variants[index] as a list, or None when it does not exist."""
    if not isinstance(variants, list) or len(variants) <= index:
        return None
    value = variants[index]
    return value if isinstance(value, list) else None


def slot_text(variants, index):
    """The text of one language column, without the game's markup.

    A title may store the raw text (with the inline markup of the engine)
    and a plain mirror behind it.  The mirror is what a reader sees, so it
    is the one that belongs into the workbook.
    """
    value = slot(variants, index)
    if not value or len(value) < 2 or value[1] is None:
        return ''
    if len(value) > 3 and isinstance(value[3], str) and value[3]:
        return value[3]
    return str(value[1])


# The inline markup of the engine: a percent tag closed by a semicolon
# (``%p-1;``, ``%fuser;``).  It is not text, so it never belongs into the
# workbook -- but it has to survive the round trip.
MARKUP_RE = re.compile(r'%[^%;]*;')


def markup_segments(raw, plain):
    """The markup of a raw text and where it sits in the plain text.

    Returns a list of ``(markup, offset)``; the offset counts the plain
    characters, so a translation of another length keeps the markup in the
    same place.
    """
    segments = []
    index = 0
    offset = 0
    while index < len(raw):
        match = MARKUP_RE.match(raw, index)
        if match:
            segments.append((match.group(0), offset))
            index = match.end()
            continue
        if offset < len(plain):
            offset += 1
        index += 1
    return segments


def apply_markup(plain, segments, old_length):
    """Put the markup of the original text back into a translated one."""
    if not segments:
        return plain
    out = []
    pos = 0
    for markup, offset in segments:
        if old_length:
            at = int(round(offset * len(plain) / old_length))
        else:
            at = offset
        at = max(pos, min(at, len(plain)))
        out.append(plain[pos:at])
        out.append(markup)
        pos = at
    out.append(plain[pos:])
    return ''.join(out)


def slot_speaker(variants, index):
    value = slot(variants, index)
    if not value or not value or value[0] is None:
        return ''
    return str(value[0])


def speaker_name(entry):
    """Return the base-language display name for a dialogue entry.

    The name can live in the base variant (``variants[0][0]``) or in a
    dedicated field before the variants: one layout stores
    ``[speaker_id, display_name, variants, voice, delay, kag]``.  The variant's
    own speaker is preferred because it follows the base language; when it is
    blank (some titles draw the name as a texture and leave the field empty)
    the last non-empty string before the variant list is used.
    """
    variants = text_variants(entry)
    base = slot_speaker(variants, 0).strip()
    if base:
        return base
    prefix = []
    for value in entry:
        if value is variants:
            break
        if isinstance(value, str) and value.strip():
            prefix.append(value.strip())
    return prefix[-1] if prefix else ''


def voice_name(entry):
    """Return the voice clip name(s) stored in a dialogue entry.

    The field position differs between titles, so find it by shape:
    a dict with ``voice``, or a list
    of such dicts.  A language-variant list is a list of lists and is skipped.
    """
    if not isinstance(entry, list):
        return ''
    for value in entry:
        if isinstance(value, dict) and value.get('voice'):
            return str(value.get('voice'))
        if (isinstance(value, list) and value
                and all(isinstance(item, dict) for item in value)):
            names = [str(item.get('voice')) for item in value
                     if item.get('voice')]
            if names:
                return ', '.join(names)
    return ''
