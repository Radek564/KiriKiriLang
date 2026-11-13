"""Shared helpers for exporting/importing game assets.

Two kinds of assets are supported:

* **plain images** (``.png`` / ``.jpg`` / ``.webp`` / ``.bmp``) - copied as-is;
* **PSB texture containers** (``.pimg`` or any PSB that embeds image chunks) -
  unpacked with the native :mod:`psb` / :mod:`tlg` codecs into a JSON plus one
  PNG per texture, and packed back with the same native writer.

The extracted game keeps assets under hash-named folders
(``<archive>/<dirhash>/<filehash>``); ``filemap.json`` (produced by
``hash_dumper.py``) turns those into archive / directory / file names.
"""
import fnmatch
import hashlib
import json
import os
import shutil
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import psb
import psb_tjs
import tlg
import configfile

TEMPLATE = os.path.dirname(SRC)
PSB_MAGIC = b'PSB\x00'
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tlg'}
TEXT_LAYER_TYPE = 2
UI_EXTENSIONS = ('.pimg', '.uipsd', '.png', '.jpg', '.jpeg', '.webp', '.bmp')

# Where interface images live and which standalone names are language-aware.
# ``uipsd`` is always language-aware; the other folders only contribute files
# whose stem matches ``UI_PATTERNS``.  ``UI_TAG_NAMES`` lists images the game
# looks up through a language tag even when the original only ships one copy
# (for example a title that ships several variants of one picture).
UI_DIRS = ('uipsd', 'image', 'system', 'main')
UI_PATTERNS = ('attention*', 'warning*', 'aboutdialog*',
               'exchview_watermark*', 'notice*', 'caution*')
UI_TAG_NAMES = ('aboutdialog', 'warning', 'exchview_watermark',
                'attention', 'attention2', 'attention2a', 'attention2b',
                'attention2c')
# Folders that carry the game's own text (the story pictures).  Their names
# differ per title, but the folder that holds them is a convention: the
# images of the text objects.
TEXT_DIRS = ('image/tx_object/',)
# Folders that never carry text: effect and background art that live next
# to the interface.
EXCLUDE_DIRS = ('image/w_contents/',)


def load_config():
    return configfile.load()


def resolve(raw):
    """Resolve a config path relative to the template folder."""
    if not raw:
        return raw
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def config_paths():
    cfg = load_config()
    return cfg.get('paths') or {}


def load_filemap(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def entry_path(entry, extracted):
    """Resolve one filemap row to a file on disk.

    Newer maps record the real relative ``path`` (extraction now writes
    resolved names), while older ones only have the hash triple.  A row that
    cannot be resolved has no file: the caller skips it.
    """
    rel = entry.get('path')
    if rel:
        candidate = os.path.join(extracted, rel.replace('/', os.sep))
        if os.path.isfile(candidate):
            return candidate
    archive = entry.get('archive')
    dirhash = entry.get('dirhash')
    filehash = entry.get('filehash')
    if not (archive and dirhash and filehash):
        return None
    return os.path.join(extracted, archive, dirhash, filehash)


def is_psb(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == PSB_MAGIC
    except OSError:
        return False


def sanitize(name):
    """Make a storage name safe as a folder name."""
    text = name.replace('\\', '/').strip('/')
    for ch in '#:*?"<>|':
        text = text.replace(ch, '_')
    return text


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def folder_hash(path):
    """Hash a decompiled PSB folder: relative names + file bytes."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path).replace('\\', '/')
            h.update(rel.encode('utf-8'))
            h.update(b'\0')
            h.update(file_hash(full).encode('ascii'))
            h.update(b'\n')
    return h.hexdigest()


def hash_path(path):
    return folder_hash(path) if os.path.isdir(path) else file_hash(path)


# ---------------------------------------------------------------------------
# UI language assets
# ---------------------------------------------------------------------------

def text_layers(root):
    """Names of PSB layers that hold editable UI text.

    A text group is marked with ``layer_type == 2``.  The group
    itself has zero width/height; its child texture layers carry the rendered
    glyphs.  That is a much safer signal than guessing from the file name.
    """
    found = []
    for layer in (root or {}).get('layers') or []:
        if layer.get('layer_type') != TEXT_LAYER_TYPE:
            continue
        name = (layer.get('name') or '').strip()
        if name:
            found.append(name)
    return found


def _norm(value):
    return (value or '').replace('\\', '/').strip('/').lower()


def _stem(value):
    return os.path.splitext(os.path.basename((value or '').replace('\\', '/')))[0].lower()


def _collision_copy(path):
    """True for duplicate names (``name_1.png``).

    Extraction renames a second archive entry with the same storage name by
    appending ``_1``.  When the canonical file sits next to it with the same
    size it is the same asset, not a new UI image, so UI discovery should not
    list it twice.
    """
    stem, ext = os.path.splitext(os.path.basename(path))
    head, sep, tail = stem.rpartition('_')
    if not sep or not tail.isdigit():
        return False
    base = os.path.join(os.path.dirname(path), head + ext)
    if not os.path.isfile(base):
        return False
    try:
        return os.path.getsize(path) == os.path.getsize(base)
    except OSError:
        return False


def _ui_roots(ui_dirs):
    """Normalised folder list.

    ``None`` means "the default folders", an empty list means "no folder
    at all" -- the difference matters for the include and exclude lists.
    """
    if ui_dirs is None:
        ui_dirs = UI_DIRS
    return tuple(_norm(item) for item in ui_dirs if _norm(item))


def _ui_names(tag_names):
    """Configured UI stems plus the language-neutral defaults.

    Profiles add discovered names (``attention2*``) but must not lose the
    common KrKrz names that discovery cannot see in every extraction.
    """
    values = list(UI_TAG_NAMES) + list(tag_names or ())
    return tuple(dict.fromkeys(item.lower() for item in values if item))


def is_uipsd(storage):
    text = _norm(storage)
    return text == 'uipsd' or text.startswith('uipsd/')


def is_ui_storage(storage, directory='', ui_dirs=(), patterns=(), tag_names=(),
                  tags=(), text_dirs=(), exclude_dirs=()):
    """True for an interface image or PSB.

    A ``uipsd`` entry is always UI.  Standalone images count when their stem is
    one of ``tag_names`` (``attention``, ``exchview_watermark``, ...) or when
    their stem matches ``patterns`` inside one of ``ui_dirs``.  A folder in
    ``text_dirs`` is always UI: those are the images that carry the game's
    own text (the story pictures), whatever their names are.  A folder in
    ``exclude_dirs`` never is: effect and background art lives next to the
    interface and is not text.
    """
    text = _norm(storage)
    folder = _norm(directory)
    if is_uipsd(text) or is_uipsd(folder):
        return True
    for root in _ui_roots(exclude_dirs):
        if text == root or text.startswith(root + '/'):
            return False
        if folder == root or folder.startswith(root + '/'):
            return False
    stem = _stem(text or folder)
    if not stem:
        return False
    for root in _ui_roots(text_dirs):
        if text == root or text.startswith(root + '/'):
            return True
        if folder == root or folder.startswith(root + '/'):
            return True
    names = _ui_names(tag_names)
    # A language suffix may sit on a configured name (``option_en.pimg``,
    # ``exchview_watermark_en.png``); strip the known tag before matching so
    # the localised copy is recognised alongside its base asset.  Using
    # the profile's tag list avoids treating ``title_bg`` as ``title``.
    match_stem = stem
    language = language_tag(text or folder, tags) if tags else None
    if language and stem.endswith('_' + language):
        match_stem = stem[:-len(language) - 1]
    if any(fnmatch.fnmatchcase(match_stem, item) for item in names):
        return True
    pats = tuple(item.lower() for item in
                 (UI_PATTERNS if patterns is None else patterns))
    if not any(fnmatch.fnmatchcase(stem, item) for item in pats):
        return False
    roots = _ui_roots(ui_dirs)
    if not roots:
        return True
    parent = os.path.dirname(text).strip('/') if text else ''
    if not parent:
        parent = folder
    return any(parent == root or parent.startswith(root + '/')
               for root in roots)


def language_tag(storage, tags):
    """Language tag encoded in a storage name, or ``None`` for the base UI.

    Both layouts used by the test titles are recognised: a suffix
    (``option_en.pimg``) and a language folder (``uipsd/_en/option_en.pimg``
    or ``uipsd/en/dialog_en.pimg``).
    """
    text = (storage or '').replace('\\', '/')
    parts = [part for part in text.split('/') if part]
    if not parts:
        return None
    stem = os.path.splitext(parts[-1])[0]
    for tag in tags:
        if tag and stem.endswith('_' + tag):
            return tag
    for part in parts[:-1]:
        for tag in tags:
            if tag and part in (tag, '_' + tag):
                return tag
    return None


def ui_key(storage, tags):
    """Group key shared by all language variants of one UI asset."""
    text = (storage or '').replace('\\', '/')
    parts = [part for part in text.split('/') if part]
    if not parts:
        return text
    stem, ext = os.path.splitext(parts[-1])
    dirs = parts[:-1]
    tag = None
    for candidate in tags:
        if candidate and stem.endswith('_' + candidate):
            tag = candidate
            stem = stem[:-len(candidate) - 1]
            break
    for index, part in enumerate(dirs):
        if any(candidate and part in (candidate, '_' + candidate)
               for candidate in tags):
            dirs = dirs[:index] + dirs[index + 1:]
            break
    return '/'.join(dirs + [stem + ext])


def _ui_score(candidate, prefer_tags, target):
    tag = candidate.get('language')
    if tag == target:
        return 1000
    if tag in prefer_tags:
        return prefer_tags.index(tag)
    if tag is None:
        return len(prefer_tags) + 1
    return len(prefer_tags)


# Screens whose untagged file is the complete picture and whose ``_<tag>``
# siblings only redefine part of it.  The title screen is the important one:
# the base ``title.pimg`` also carries the main-menu buttons, while
# ``title_en.pimg`` only swaps the logo.  A kit always starts from the
# untagged file so those buttons can be translated; choosing the language
# overlay would silently drop every picture it does not redefine.
BASE_SCREENS = ('title',)


def _tag_override(storage, language, tag):
    same = os.path.basename(storage)
    stem, ext = os.path.splitext(same)
    if language:
        suffix = '_' + language
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    return stem + '_' + tag + ext


def _choose_ui(candidates, tag, prefer_tags, override_mode='auto', by_size=None,
               tag_names=(), ui_dirs=()):
    """Pick one source per UI asset and compute its patch name/path.

    ``target`` is the path under ``assets/``.  Language-aware assets become a
    flat ``<name>_<tag>`` override because the game resolves them by bare name.
    Images the game loads by their real path (``image/attention5.png``) keep
    that path so the patch does not invent a language variant the engine never
    asks for.

    A screen listed in :data:`BASE_SCREENS` is always taken from its untagged
    file when one exists; its ``_<tag>`` copies are partial overlays and are
    ignored, so the kit never loses the pictures only the base file has.
    """
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate['key'], []).append(candidate)
    names = _ui_names(tag_names)
    out = []
    for key, group in groups.items():
        key_stem = os.path.splitext(os.path.basename(key))[0].lower()
        if key_stem in BASE_SCREENS:
            untagged = [item for item in group if not item.get('language')]
            if untagged:
                group = untagged
        chosen = min(group, key=lambda item: (
            _ui_score(item, prefer_tags, tag),
            0 if item.get('kind') in ('psb', 'image') else 1,
        ))
        if chosen.get('language') == tag:
            continue
        storage = chosen['storage']
        mapped = override_name(storage, chosen.get('size'),
                               by_size or {}, tag)
        same = os.path.basename(storage)
        stem = os.path.splitext(same)[0].lower()
        language_aware = (
            is_uipsd(storage)
            or any(fnmatch.fnmatchcase(stem, item) for item in names)
            or chosen.get('language') is not None
            or mapped != storage
        )
        if override_mode == 'same':
            override = same
            target = storage
        elif mapped != storage and chosen.get('size') in (by_size or {}):
            # Unnamed PSBs are identified by size; the table supplies the real
            # storage name (for example dialog_{tag}.pimg).
            override = os.path.basename(mapped)
            target = override
        elif override_mode == 'tag' or language_aware:
            override = _tag_override(storage, chosen.get('language'), tag)
            target = override
        else:
            override = same
            target = storage
        item = dict(chosen)
        item['override'] = override
        item['target'] = target
        out.append(item)
    # Two storages can map to one patch target (``adult/option_en.pimg`` and
    # ``uipsd/_en/option_en.pimg`` are the same duplicate; a base PSB and its
    # language overlay also share the flat ``<name>_<tag>`` target).  Keep the
    # best source per target so import never writes the same file twice and a
    # large base PSB cannot overwrite the smaller text overlay.
    ranked = {}
    for item in out:
        rank = (_ui_score(item, prefer_tags, tag),
                0 if is_uipsd(item.get('storage') or '') else 1,
                0 if item.get('language') else 1)
        previous = ranked.get(item['target'])
        if previous is None or rank < previous[0]:
            ranked[item['target']] = (rank, item)
    return [item for _rank, item in ranked.values()]


def ui_root(source):
    """Return the ``uipsd`` folder inside *source*, or ``None``."""
    if not source or not os.path.isdir(source):
        return None
    if os.path.isdir(os.path.join(source, 'uipsd')):
        return os.path.join(source, 'uipsd')
    if os.path.basename(os.path.normpath(source)).lower() == 'uipsd':
        return source
    return None


def _ui_scan_roots(source, ui_dirs):
    """Folders to scan, as ``(path, storage_prefix)``.

    Scanning ``image`` also covers ``image/sys`` and ``image/w_effect``, so a
    nested root is skipped when its parent is already in the list.
    """
    if not source or not os.path.isdir(source):
        return []
    if os.path.basename(os.path.normpath(source)).lower() == 'uipsd':
        return [(source, 'uipsd')]
    roots = []
    for folder in sorted(_ui_roots(ui_dirs), key=len):
        path = os.path.join(source, folder.replace('/', os.sep))
        if not os.path.isdir(path):
            continue
        if any(folder == prefix or folder.startswith(prefix + '/')
               for _path, prefix in roots):
            continue
        roots.append((path, folder))
    return roots


def scan_ui_tree(source, extensions=UI_EXTENSIONS, tags=(), target_tag=None,
                 prefer_tags=(), override_mode='auto', include=None,
                 ui_dirs=(), patterns=(), tag_names=(), text_dirs=(),
                 exclude_dirs=()):
    """Collect UI assets from a *named* extraction tree.

    Handles raw ``.pimg`` / ``.uipsd`` files, standalone UI images under
    ``image/`` (``attention*``, ``warning*``, ``exchview_watermark*`` ...),
    and tool dumps (``<name>.tjs`` plus a sibling ``<name>/`` folder of
    ``.tlg`` textures).  Dumps without textures are returned as
    ``psb_dump_text`` so a single-language title can at least report which
    screens contain text.
    """
    if not source or not os.path.isdir(source):
        return []
    exts = {ext.lower() for ext in (extensions or UI_EXTENSIONS)}
    tags = tuple(tags or ())
    prefer_tags = tuple(prefer_tags or ())
    dirs = tuple(ui_dirs or UI_DIRS)
    pats = tuple(UI_PATTERNS if patterns is None else patterns)
    names = tuple(tag_names or UI_TAG_NAMES)
    roots = _ui_scan_roots(source, dirs)
    if not roots:
        return []
    candidates = []
    seen = set()
    for root, prefix in roots:
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext not in exts:
                    continue
                path = os.path.join(base, name)
                if _collision_copy(path):
                    continue
                rel = os.path.relpath(path, root).replace('\\', '/')
                storage = prefix + '/' + rel if prefix else rel
                if not is_ui_storage(storage, os.path.dirname(storage),
                                     dirs, pats, names, tags, text_dirs,
                                     exclude_dirs):
                    continue
                if include and include.lower() not in storage.lower():
                    continue
                if storage in seen:
                    continue
                seen.add(storage)
                candidates.append({
                    'storage': storage,
                    'source': path,
                    'kind': 'psb' if is_psb(path) else 'image',
                    'archive': None,
                    'dir': os.path.dirname(storage).replace('\\', '/') + '/',
                    'name': name,
                    'size': os.path.getsize(path),
                    'language': language_tag(storage, tags),
                    'key': ui_key(storage, tags),
                    'override': storage,
                    'has_text': None,
                    'text_layers': None,
                })

    for root, prefix in roots:
        if not prefix.startswith('uipsd'):
            continue
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                lower = name.lower()
                if lower.endswith('.psb.tjs'):
                    stem = name[:-len('.psb.tjs')]
                elif lower.endswith('.tjs'):
                    stem = name[:-len('.tjs')]
                else:
                    continue
                texture_dir = os.path.join(base, stem)
                dump = os.path.join(base, name)
                rel = os.path.relpath(dump, root).replace('\\', '/')
                storage = prefix + '/' + os.path.splitext(rel)[0] + '.pimg'
                if include and include.lower() not in storage.lower():
                    continue
                if storage in seen:
                    continue
                try:
                    root_value = dump_root(dump)
                    text = text_layers(root_value)
                except Exception:
                    continue
                if not text:
                    continue
                has_textures = os.path.isdir(texture_dir) and any(
                    child.lower().endswith('.tlg')
                    for child in os.listdir(texture_dir))
                seen.add(storage)
                candidates.append({
                    'storage': storage,
                    'source': dump,
                    'kind': 'psb_dump' if has_textures else 'psb_dump_text',
                    'texture_dir': texture_dir if has_textures else None,
                    'archive': None,
                    'dir': os.path.dirname(storage).replace('\\', '/') + '/',
                    'name': os.path.basename(storage),
                    'size': os.path.getsize(dump),
                    'language': language_tag(storage, tags),
                    'key': ui_key(storage, tags),
                    'has_text': True,
                    'text_layers': text,
                })
    return _choose_ui(candidates, target_tag, prefer_tags,
                      override_mode=override_mode, tag_names=names,
                      ui_dirs=dirs)


def _replace_dump_resources(value):
    if isinstance(value, dict):
        return {key: _replace_dump_resources(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_dump_resources(child) for child in value]
    if isinstance(value, str) and value.startswith('Binary::'):
        return psb.RESOURCE_PREFIX + value[len('Binary::'):]
    return value


def dump_root(path):
    """Parse a ``.tjs`` PSB dump into the JSON resource model."""
    return _replace_dump_resources(psb_tjs.load_file(path))


def build_psb_from_dump(dump_path, texture_dir, out_path, version=3):
    """Rebuild a PSB/PIMG from a TJS dump plus its loose ``.tlg`` textures."""
    root = dump_root(dump_path)
    resources = {}
    for key, value in root.items():
        if not isinstance(value, str) or not value.startswith(psb.RESOURCE_PREFIX):
            continue
        index = int(value[len(psb.RESOURCE_PREFIX):])
        source = os.path.join(texture_dir, key)
        with open(source, 'rb') as handle:
            resources[index] = handle.read()
    table = ([resources.get(index, b'')
              for index in range(max(resources) + 1)] if resources else [])
    data = psb.build(root, version=version, resources=table)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'wb') as handle:
        handle.write(data)
    return len(data)


def find_json(folder):
    """Return the main decompiled JSON inside a PSB export folder."""
    candidates = []
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith('.json') and not name.endswith('.resx.json'):
            candidates.append(os.path.join(folder, name))
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def _placeholder(value):
    """Return ``('normal'|'extra', index)`` for a resource placeholder."""
    if not isinstance(value, str):
        return None
    if value.startswith(psb.EXTRA_RESOURCE_PREFIX):
        try:
            return 'extra', int(value[len(psb.EXTRA_RESOURCE_PREFIX):])
        except ValueError:
            return None
    if value.startswith(psb.RESOURCE_PREFIX):
        try:
            return 'normal', int(value[len(psb.RESOURCE_PREFIX):])
        except ValueError:
            return None
    return None


def _walk_placeholders(value, out):
    if isinstance(value, dict):
        for key, child in value.items():
            info = _placeholder(child)
            if info is not None:
                out.append((info, key))
            _walk_placeholders(child, out)
    elif isinstance(value, list):
        for child in value:
            _walk_placeholders(child, out)


def _texture_names(root):
    """Map ``(kind, index)`` to the layer name used for its PNG file."""
    found = []
    _walk_placeholders(root, found)
    names = {}
    # Prefer top-level ``<layer>.tlg`` keys.
    for key, value in root.items():
        info = _placeholder(value)
        if info is not None and key.lower().endswith('.tlg'):
            names.setdefault(info, key[:-4])
    for info, key in found:
        if info not in names:
            stem = key[:-4] if key.lower().endswith('.tlg') else str(info[1])
            names.setdefault(info, stem)
    return names


def decompile_psb(src, out_dir):
    """Unpack a PSB into ``out_dir`` (JSON + PNGs).  Returns (json, pngs)."""
    os.makedirs(out_dir, exist_ok=True)
    parsed = psb.load_file(src)
    root = parsed.to_json()
    base = os.path.basename(src)
    image_dir = os.path.join(out_dir, base)
    os.makedirs(image_dir, exist_ok=True)

    names = _texture_names(root)
    resources = {}
    colors = {}
    pngs = 0
    for (kind, index), name in names.items():
        table = parsed.extra_resources if kind == 'extra' else parsed.resources
        if index >= len(table):
            continue
        blob = table[index]
        rel = os.path.join(base, name + '.png').replace('\\', '/')
        dest = os.path.join(out_dir, base, name + '.png')
        try:
            width, height, bgra = tlg.decode(blob)
            from PIL import Image
            image = Image.frombytes('RGBA', (width, height), bgra,
                                    'raw', 'BGRA')
            image.save(dest)
            colors[name] = tlg._meta(blob)[1]
            pngs += 1
        except Exception:
            rel = os.path.join(base, name + '.bin').replace('\\', '/')
            dest = os.path.join(out_dir, base, name + '.bin')
            with open(dest, 'wb') as f:
                f.write(blob)
        resources[name] = rel

    json_path = os.path.join(out_dir, base + '.json')
    with open(json_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(root, f, ensure_ascii=False, indent=1)
        f.write('\n')

    meta = {
        'PsbVersion': parsed.version,
        'PsbType': 'Pimg',
        'Platform': 'none',
        'CryptKey': None,
        'ExternalTextures': False,
        'Context': {},
        'Resources': resources,
        'Colors': colors,
    }
    with open(os.path.join(out_dir, base + '.resx.json'), 'w',
              encoding='utf-8', newline='\n') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return json_path, pngs, text_layers(root)


def build_psb(json_path, out_path):
    """Compile a decompiled PSB folder back into a single PSB file."""
    folder = os.path.dirname(os.path.abspath(json_path))
    with open(json_path, encoding='utf-8') as f:
        root = json.load(f)
    meta_path = None
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith('.resx.json'):
            meta_path = os.path.join(folder, name)
            break
    if meta_path is None:
        raise RuntimeError('missing .resx.json in %s' % folder)
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)

    names = _texture_names(root)
    lookup = {name: info for info, name in names.items()}
    normal = {}
    extra = {}
    for name, rel in (meta.get('Resources') or {}).items():
        info = lookup.get(name)
        if info is None:
            info = _placeholder(root.get(name))
        if info is None and str(name).isdigit():
            info = ('normal', int(name))
        if info is None:
            raise RuntimeError('cannot map texture %r to a resource' % name)
        path = os.path.join(folder, rel.replace('/', os.sep))
        if not os.path.isfile(path):
            raise RuntimeError('missing texture file: %s' % path)
        if path.lower().endswith('.bin'):
            with open(path, 'rb') as f:
                blob = f.read()
        else:
            count = (meta.get('Colors') or {}).get(name, 4)
            _w, _h, blob = tlg.from_png(path, colors=count)
        kind, index = info
        (extra if kind == 'extra' else normal)[index] = blob

    def table(mapping, total):
        if not mapping:
            return []
        size = max(total, max(mapping) + 1)
        out = [b''] * size
        for index, blob in mapping.items():
            out[index] = blob
        return out

    normal_table = table(normal, 0)
    extra_table = table(extra, 0)
    data = psb.build(root, version=int(meta.get('PsbVersion') or 3),
                     resources=normal_table, extra_resources=extra_table)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'wb') as f:
        f.write(data)
    return len(data)


def override_name(storage, size, by_size, tag, tags=None):
    """Map a source storage name to the override file the patch ships.

    * unnamed ``uipsd/_en`` PSBs are recognised by size and mapped through the
      table in ``config.json``;
    * names ending in a reference tag (``_en`` / ``_cn``) switch to ``_<tag>``;
    * anything else keeps its name (the loader serves the same storage).
    """
    if size in by_size:
        return str(by_size[size]).replace('{tag}', tag)
    if not storage:
        return None
    base, ext = os.path.splitext(storage)
    for ref in (tags or ('en', 'cn', 'jp')):
        suffix = '_' + ref
        if ref and base.endswith(suffix):
            return base[:-len(suffix)] + '_' + tag + ext
    return storage


def select_assets(entries, extracted, dirs=(), extensions=(), by_size=None,
                  include=None, tag=None, ui_only=False, prefer_tags=(),
                  override_mode='auto', tags=(), ui_dirs=(), patterns=(),
                  tag_names=(), text_dirs=(), exclude_dirs=()):
    """Pick overridable assets from a filemap.

    Named files whose extension is in ``extensions`` are selected, plus every
    unnamed file whose ``dir`` is listed in ``dirs`` (the regionalised UI
    folder), plus any file whose size is a key in ``by_size`` (the explicit
    override table).  PSBs with no embedded PNGs are dropped later.

    With ``ui_only`` the result is reduced to ``uipsd`` assets and exactly one
    source per logical asset: the best reference language (``prefer_tags``)
    wins, so localised copies never overwrite each other.
    """
    if tag is None:
        import lang
        tag = lang.TAG
    by_size = by_size or {}
    exts = {e.lower() for e in extensions}
    dirs = tuple(dirs)
    tags = tuple(tags or ())
    prefer_tags = tuple(prefer_tags or ())
    candidates = []
    for entry in entries:
        name = entry.get('name') or ''
        d = entry.get('dir') or ''
        ext = os.path.splitext(name)[1].lower()
        named_image = bool(name) and ext in exts
        in_dir = bool(d) and d in dirs
        size_match = entry.get('size') in by_size
        if not (named_image or in_dir or size_match):
            continue
        if ui_only and not is_ui_storage(name, d, ui_dirs, patterns,
                                         tag_names, tags, text_dirs,
                                         exclude_dirs):
            continue
        src = entry_path(entry, extracted)
        if not os.path.isfile(src):
            continue
        size = entry.get('size') or os.path.getsize(src)
        if name:
            if ui_only and d and d != '%EmptyString%':
                storage = d.rstrip('/') + '/' + name
            else:
                storage = name
        else:
            storage = '%s%s' % (d, entry['filehash'][:12])
        if include and include.lower() not in storage.lower():
            continue
        candidates.append({
            'storage': storage,
            'source': src,
            'archive': entry.get('archive'),
            'dir': d,
            'name': name,
            'size': size,
            'kind': 'psb' if is_psb(src) else 'image',
            'language': language_tag(storage, tags),
            'key': ui_key(storage, tags) if ui_only else storage,
            'override': storage,
            'has_text': None,
            'text_layers': None,
        })
    if not ui_only:
        out = []
        seen = set()
        for item in candidates:
            if item['source'] in seen:
                continue
            seen.add(item['source'])
            out.append(item)
        return out
    return _choose_ui(candidates, tag, prefer_tags,
                      override_mode=override_mode, by_size=by_size,
                      tag_names=tag_names, ui_dirs=ui_dirs)
