"""Portable, per-game / per-language translation kits.

A kit is everything one translator needs for one target language, kept outside
the game folder and re-importable at any time::

    kits/<game>/<tag>/
        kit.json                      metadata (profile, tags, sources, stats)
        README.md                     how to translate and import
        text/text.xlsx                scenario dialogue with the target slot
        ui/catalog.json               every discovered menu/UI key + provenance
        ui/base/syslangtext_jp.ini    the game's original captions
        ui/<tag>/syslangtext_<tag>.ini the editable translation
        images/                       optional image / PSB texture export
        images/assets_export.xlsx     index of the image export
        images/manifest.json          hashes written by export_assets.py

``create`` discovers the captions from the game (INI files, plain-text TJS and
decompiled TJS), exports the scenario workbook and, optionally, the images.
``import`` writes the workbook back into the scenario PSBs, copies both INIs
into the profile's asset folder and repacks edited images; ``build`` then runs
the normal patch builder.

The module is language-neutral: every tag, label and path comes from the
profile, and nothing assumes a specific language.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(TEMPLATE, 'src')
KITS = os.path.join(TEMPLATE, 'kits')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import configfile
import menu_catalog


# ---------------------------------------------------------------------------
# paths and profile helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def slugify(text):
    text = re.sub(r'[^0-9A-Za-z._-]+', '_', str(text or '')).strip('._-')
    return text or 'game'


def resolve_path(config, key, default=None):
    """Resolve ``paths.<key>`` against the template folder."""
    raw = (config.get('paths') or {}).get(key) or default
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def resolve_top(config, key, default=None):
    raw = config.get(key) or default
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def profile_of(config):
    return config.get('_config_path')


def rel(path):
    try:
        value = os.path.relpath(path, TEMPLATE)
    except ValueError:
        return path
    return path if value.startswith('..') else value.replace('\\', '/')


def _env(profile):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    if profile:
        env[configfile.ENV] = os.path.abspath(profile)
    return env


def run_script(script, args, profile, log=print, title=None):
    if title:
        log('\n=== %s ===' % title)
    cmd = [sys.executable, '-X', 'utf8', os.path.join(SRC, script)]
    cmd += [str(arg) for arg in args if arg not in (None, False)]
    log('  %s' % ' '.join(os.path.basename(part) if index == 0 else part
                          for index, part in enumerate(cmd[3:], 3)))
    return subprocess.call(cmd, cwd=TEMPLATE, env=_env(profile))


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# INI rewriting
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r'^!\s*section\s+(\S+)', re.I)

# The caption kinds that mean the same in every language: the texts the
# scripts ask for.  Everything else was read from a language file and
# belongs to that language alone.
NEUTRAL_KINDS = ('langtext', 'message')


def _ini_sections(text):
    """Return an ordered list of ``(section, [(key, value), ...])``."""
    sections = [('menu', [])]
    current = sections[0]
    seen = {'menu'}
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('!'):
            match = _SECTION_RE.match(stripped)
            if match:
                name = match.group(1).strip()
                if name not in seen:
                    current = (name, [])
                    sections.append(current)
                    seen.add(name)
                else:
                    current = next(item for item in sections if item[0] == name)
            continue
        if stripped.startswith(('#', ';')):
            continue
        match = re.match(r'^([^\s]+)[\t ]+(.*)$', line)
        if match:
            current[1].append((match.group(1), match.group(2).strip()))
    return sections


def _rewrite_ini(text, captions, section='menu', keep_existing=False):
    """Update/append captions in one INI section, keeping comments intact.

    ``captions`` is a ``key -> value`` mapping.  Existing keys keep their
    position; new keys are appended at the end of the section.  When the
    section does not exist it is appended to the file.

    ``keep_existing`` leaves every value that is already present untouched and
    only adds missing keys.  This is what a translated target INI needs: a
    refresh must never replace a translated caption with the base-language
    placeholder.  The default rewrites values and is used for the reference INI.

    A value that is only the tag (``xx``, ``XX``) is a placeholder, not a
    translation: it is replaced even when existing values are kept.
    """
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = []
    in_section = False
    found = False
    seen = set()

    def is_placeholder(value, key):
        """True when the value is the tag itself instead of a caption."""
        plain = value.strip().lstrip('&').strip().lower()
        tag = key.rsplit('_', 1)[-1].lower()
        return bool(plain) and plain == tag

    def append_missing():
        for key in sorted(captions):
            if key not in seen:
                out.append('%s\t%s' % (key, captions[key]))
                seen.add(key)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('!'):
            if in_section:
                append_missing()
                in_section = False
            match = _SECTION_RE.match(stripped)
            if match and match.group(1).strip() == section:
                in_section = True
                found = True
            out.append(line)
            continue
        if in_section and stripped and not stripped.startswith(('#', ';')):
            match = re.match(r'^([^\s]+)[\t ]+', line)
            if match and match.group(1) in captions:
                key = match.group(1)
                current = re.split(r'[\t ]+', line.strip(), 1)[-1].strip()
                if keep_existing and not is_placeholder(current, key):
                    out.append(line)
                else:
                    out.append('%s\t%s' % (key, captions[key]))
                seen.add(key)
                continue
        out.append(line)
    if in_section:
        append_missing()
    if not found:
        if out and out[-1].strip():
            out.append('')
        out.append('!section %s' % section)
        for key in sorted(captions):
            out.append('%s\t%s' % (key, captions[key]))
    return '\r\n'.join(out)


def _ini_values(text):
    """Every ``section -> {key: value}`` of an INI file."""
    out = {}
    for section, entries in _ini_sections(text):
        out[section] = dict(entries)
    return out


def _has_translation(target_text, base_text):
    """True when the target file holds a caption of its own.

    A file that only repeats the base language has nothing to lose: it can
    be seeded again from the title's own caption file, so the captions the
    title ships (the window title among them) are part of the kit.
    """
    base = _ini_values(base_text)
    for section, entries in _ini_values(target_text).items():
        reference = base.get(section) or {}
        for key, value in entries.items():
            ref = reference.get(key)
            if ref is None:
                continue
            if value.strip() == ref.strip():
                continue
            # A value that is only the tag is a placeholder, not a caption.
            tag = key.rsplit('_', 1)[-1].lower()
            if value.strip().lstrip('&').strip().lower() == tag:
                continue
            return True
    return False


def _menu_captions(catalog, section='menu'):
    return {key: entry.caption
            for key, entry in catalog.entries.items()
            if entry.section == section and entry.caption}


def _game_ini_tags(config, ui_tags):
    """The interface languages the title ships a caption file for.

    A game that has more than one interface language also has a caption
    file per language (``syslangtext_<tag>.ini``); the kit keeps them, so
    every interface can be translated as well.
    """
    tags = list(ui_tags or [])
    extract = resolve_top(config, 'extract_dir')
    if extract and os.path.isdir(extract):
        for base, _dirs, files in os.walk(extract):
            for name in files:
                match = re.match(r'^syslangtext_([A-Za-z0-9_-]+)\.ini$', name)
                if match and match.group(1) not in tags:
                    tags.append(match.group(1))
    return tags


def _selector_captions(catalog):
    """The captions the selector itself owns (the group names).

    They are the tool's own, not the game's: they are refreshed on every
    run so a stale placeholder cannot survive in the base language's file.
    """
    return {key: entry.caption for key, entry in catalog.entries.items()
            if entry.kind == 'selector' and entry.caption}


def _all_captions(catalog, kinds=None, base_tag=None):
    """Every discovered caption, grouped by the section it belongs to.

    The order is stable (menu first, then the other sections in the order
    they were discovered) so a refresh does not reshuffle the INI.

    ``kinds`` restricts the result to the language-neutral texts: a caption
    that was read from an INI belongs to *that* language and must never be
    copied into another one.

    ``base_tag`` drops captions read from another language's caption file.
    The caption file of a second language often translates the character
    names; merging those into the base file would show them in the base
    interface.
    """
    sections = {}
    for key, entry in catalog.entries.items():
        if not entry.caption:
            continue
        if kinds is not None and entry.kind not in kinds:
            continue
        if base_tag and _foreign_ini(entry.source, base_tag):
            continue
        sections.setdefault(entry.section, {})[key] = entry.caption
    ordered = {}
    for name in sorted(sections, key=lambda s: (s != 'menu', s)):
        ordered[name] = sections[name]
    return ordered


def _foreign_ini(source, base_tag):
    """True when a catalogue entry was read from another language's INI."""
    match = re.search(r'syslangtext_([A-Za-z0-9_-]+)\.ini$', source or '',
                      re.IGNORECASE)
    return bool(match) and match.group(1) != base_tag


def _rewrite_sections(text, sections, keep_existing=False):
    """Rewrite several INI sections in one pass."""
    for name, captions in sections.items():
        text = _rewrite_ini(text, captions, section=name,
                            keep_existing=keep_existing)
    return text


def _keep_known_sysarg(catalog, known):
    """Drop discovered ``:group=value`` keys that an authoritative INI rejects.

    Value keys are reconstructed from menu-list literals; control flow in a
    compiled script can leave a stale array in a register and produce a
    plausible but non-existent key.  When the game (or a curated seed) lists
    the real keys, trust it and discard the rest.
    """
    if not known:
        return
    for key in [k for k, e in catalog.entries.items()
                if e.kind == 'sysarg' and k not in known]:
        del catalog.entries[key]


def _warn_changed_captions(source, dest, ignore=(), limit=6):
    """Report captions that differ between the title's file and the kit's.

    The kit's base file is the title's own file plus the texts the scripts
    ask for.  A caption that *changed* means the catalogue carried a caption
    of another language, which would replace the title's own interface.
    ``ignore`` holds the keys the kit owns (the selector's own captions), which
    are meant to be rewritten.
    """
    ignore = set(ignore)
    before = _ini_section_map(menu_catalog.read_text(source))
    after = _ini_section_map(menu_catalog.read_text(dest))
    changed = []
    for name, entries in before.items():
        for key, value in entries.items():
            if key in ignore:
                continue
            new = after.get(name, {}).get(key)
            if new is not None and new != value:
                changed.append((key, value, new))
    if not changed:
        return
    print('WARNING: %d caption(s) of the base language changed while the '
          'catalogue was merged in:' % len(changed))
    for key, old, new in changed[:limit]:
        print('   %-32s %r -> %r' % (key, old[:30], new[:30]))


def _ini_section_map(text):
    """Split an INI into ``{section: {key: value}}``."""
    out = {}
    name = ''
    for line in text.replace('\r\n', '\n').split('\n'):
        stripped = line.strip()
        if stripped.lower().startswith('!section'):
            name = stripped[len('!section'):].strip()
            out.setdefault(name, {})
            continue
        if not stripped or stripped.startswith(('#', ';', '!')):
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            out.setdefault(name, {})[parts[0]] = parts[1].strip()
    return out


def _reference_captions(ui_tag):
    """The captions the template's reference INI has for one language."""
    path = os.path.join(TEMPLATE, 'assets', 'syslangtext_%s.ini' % ui_tag)
    if not os.path.isfile(path):
        return {}
    try:
        return _ini_section_map(menu_catalog.read_text(path))
    except OSError:
        return {}


def _apply_language_reference(target_text, base_text, ui_tag):
    """Replace the still untranslated values by the language reference.

    A key whose value is still the source language's wording has never been
    translated; the reference knows the translation of that key, so the
    translator gets a translated file to work on.  A finished translation is
    never touched.
    """
    reference = _reference_captions(ui_tag)
    if not reference:
        return target_text
    # The engine looks a text up by its key, not by its section, so the
    # reference is matched by key across all of its sections.
    flat = {}
    for entries in reference.values():
        for key, value in entries.items():
            flat.setdefault(key, value)
    base = _ini_section_map(base_text)
    target = _ini_section_map(target_text)
    replacements = {}
    for name, entries in target.items():
        for key, value in entries.items():
            if base.get(name, {}).get(key) != value:
                continue
            translated = flat.get(key)
            if translated and translated != value:
                replacements.setdefault(name, {})[key] = translated
    if not replacements:
        return target_text
    return _rewrite_sections(target_text, replacements)


def _write_ini(path, text):
    menu_catalog.write_utf16_bom(path, text)


def _first_ini(candidates):
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _ini_in_dir(folder, tag):
    """Return the real ``syslangtext_<tag>.ini`` in *folder* (or ``.ini.txt``)."""
    if not folder or not os.path.isdir(folder):
        return None
    wanted = 'syslangtext_%s.ini' % tag
    for name in sorted(os.listdir(folder)):
        if menu_catalog.logical_name(name).lower() == wanted:
            return os.path.join(folder, name)
    return None


def _has_ini(folder):
    if not folder or not os.path.isdir(folder):
        return False
    return any(menu_catalog.logical_name(name).lower().startswith('syslangtext_')
               and menu_catalog.logical_name(name).lower().endswith('.ini')
               for name in os.listdir(folder))


def _seed_dirs(config):
    """Reference INIs for catalogue discovery.

    The template's ``assets/`` folder holds the engine-level reference
    captions (menu, dialog, system) for every Krkrz title.  Those are the only
    seeds: a title's own captions come from the game, and the profile's
    ``assets_dir`` is the *build output* of the last run - seeding from it
    would feed the previous bundle's wording back into a fresh kit.
    """
    default_assets = os.path.join(TEMPLATE, 'assets')
    return [default_assets] if _has_ini(default_assets) else []


def _reference_ini(ui_tag):
    """The template's reference INI for one interface language."""
    path = os.path.join(TEMPLATE, 'assets', 'syslangtext_%s.ini' % ui_tag)
    return path if os.path.isfile(path) else None


def _find_game_ini(config, tag):
    """Find ``syslangtext_<tag>.ini`` in the extracted tree or game folder.

    Some extraction tools append ``.txt`` to text files, so both
    ``syslangtext_jp.ini`` and ``syslangtext_jp.ini.txt`` count as the INI.
    """
    filename = 'syslangtext_%s.ini' % tag
    roots = [resolve_top(config, 'extract_dir'), config.get('game_root')]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d.lower() not in ('unencrypted', 'patch',
                                            'patch_adult', 'savedata', 'save')]
            for name in files:
                if menu_catalog.logical_name(name).lower() == filename:
                    return os.path.join(base, name)
    return None


# ---------------------------------------------------------------------------
# kit metadata
# ---------------------------------------------------------------------------

def profile_slug(config, profile=None):
    """Folder-safe kit name: profile basename, or game folder for config.json."""
    profile = profile or profile_of(config)
    name = os.path.splitext(os.path.basename(profile or ''))[0]
    if not name or name.lower() in ('config', 'config.json'):
        name = os.path.basename(
            (config.get('game_root') or '').rstrip('\\/')) or 'game'
    return slugify(name)


def kit_dir_for(config, slug=None, tag=None):
    slug = slug or profile_slug(config)
    tag = tag or (config.get('language') or {}).get('tag')
    return os.path.join(KITS, slug, tag)


def _kit_paths(kit_dir, tag, base_tag=None):
    base_tag = base_tag or tag
    return {
        'text': 'text/text.xlsx',
        'catalog': 'ui/catalog.json',
        # One caption file per interface language, exactly like the game
        # itself: the base language is the reference, the new language is
        # the file to translate.
        'base_ini': 'ui/syslangtext_%s.ini' % base_tag,
        'target_ini': 'ui/syslangtext_%s.ini' % tag,
        # The interface lives in one folder: the caption files and the
        # interface graphics belong together, and the graphics keep the
        # game's own paths (``image/…``, ``main/…``, ``psb/…``).
        'images': 'ui',
        'images_manifest': 'ui/manifest.json',
        'images_workbook': 'ui/assets_export.xlsx',
    }


def resolve_kit(value):
    """Resolve a ``--kit`` argument against the template folder."""
    if not value:
        return None
    return os.path.abspath(value if os.path.isabs(value)
                           else os.path.join(TEMPLATE, value))


def list_kits():
    """Return ``[(kit_dir, metadata)]`` for every kit under ``kits/``."""
    found = []
    if not os.path.isdir(KITS):
        return found
    for slug in sorted(os.listdir(KITS)):
        root = os.path.join(KITS, slug)
        if not os.path.isdir(root):
            continue
        for tag in sorted(os.listdir(root)):
            kit_dir = os.path.join(root, tag)
            meta = os.path.join(kit_dir, 'kit.json')
            if os.path.isfile(meta):
                found.append((kit_dir, _read_json(meta, {})))
    return found


def _load_kit(kit_dir):
    path = os.path.join(kit_dir, 'kit.json')
    if not os.path.isfile(path):
        raise SystemExit('not a kit: %s (missing kit.json)' % kit_dir)
    return _read_json(path)


def _profile_for_kit(kit_dir, data):
    """Active profile, falling back to the kit's own snapshot.

    A kit is portable: it carries ``profile.json`` so import still works after
    the template profile is renamed or the kit is moved to another checkout.
    The live profile wins while it exists, so profile edits are still picked up.
    """
    raw = data.get('profile')
    profile = os.path.join(TEMPLATE, raw) if raw else None
    if profile and os.path.isfile(profile):
        return profile
    snapshot = os.path.join(
        kit_dir, data.get('profile_snapshot') or 'profile.json')
    if os.path.isfile(snapshot):
        return snapshot
    raise SystemExit(
        'profile not found: %s (and no profile.json in the kit)' % raw)


def _write_profile_snapshot(kit_dir, config, source):
    data = {key: value for key, value in config.items()
            if not key.startswith('_')}
    data['_profile_source'] = rel(source)
    _write_json(os.path.join(kit_dir, 'profile.json'), data)
    return 'profile.json'


def _kit_base_tag(config):
    language = config.get('language') or {}
    tags = list(config.get('ui_tags') or [])
    if language.get('base_tag'):
        return language['base_tag']
    if tags:
        return tags[0]
    return language.get('tag')


def _kit_summary(catalog, config, base_tag):
    stats = menu_catalog.stats(catalog, base_tag)
    return {
        'total': stats['total'],
        'sections': stats['sections'],
        'kinds': stats['kinds'],
        'conflicts': stats['conflicts'],
        'sources': stats['sources'],
    }


def _language_meta(config, tag):
    """The language block stored in ``kit.json``.

    Rebuilt from the active profile so an export/import after a profile edit
    (for example a different text slot) never leaves stale metadata behind.
    """
    language = config.get('language') or {}
    base_tag = _kit_base_tag(config) or tag
    ui_tags = list(config.get('ui_tags') or [base_tag, tag])
    if tag not in ui_tags:
        ui_tags.append(tag)
    return {
        'tag': tag,
        'name': language.get('name') or tag,
        'label': language.get('label'),
        'index': language.get('index'),
        'base_tag': base_tag,
        'ui_tags': ui_tags,
        'native_multilang': bool(
            (config.get('build') or {}).get('native_multilang')),
    }


def _refresh_missing(kit_dir, data):
    """Recompute the untranslated-caption list from the kit's own INIs."""
    paths = data['paths']
    base_path = os.path.join(kit_dir, paths['base_ini'])
    target_path = os.path.join(kit_dir, paths['target_ini'])
    if not (os.path.isfile(base_path) and os.path.isfile(target_path)):
        return data.get('missing_translations') or []
    return _missing_translations(menu_catalog.read_text(base_path),
                                 menu_catalog.read_text(target_path))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def create(profile, *, game=None, tag=None, slug=None, images='none',
           image_only=None, text=True, ensure_slot=True, force=False,
           reset=False, decompiler=None, scan_compiled=True, log=print):
    """Create or refresh ``kits/<slug>/<tag>/`` from a profile."""
    config = configfile.load(profile)
    language = config.get('language') or {}
    tag = tag or language.get('tag')
    if not tag:
        raise SystemExit('profile has no language.tag')
    base_tag = _kit_base_tag(config) or tag
    ui_tags = list(config.get('ui_tags') or [base_tag, tag])
    if tag not in ui_tags:
        ui_tags.append(tag)

    if not slug:
        slug = profile_slug(config, profile)
    kit_dir = os.path.join(KITS, slug, tag)
    if reset and os.path.isdir(kit_dir):
        shutil.rmtree(kit_dir)
    if os.path.isdir(kit_dir) and not (force or reset):
        raise SystemExit('kit already exists: %s\n'
                         'use --force to refresh it or --reset to start over'
                         % rel(kit_dir))
    os.makedirs(kit_dir, exist_ok=True)
    profile_snapshot = _write_profile_snapshot(kit_dir, config, profile)
    paths = _kit_paths(kit_dir, tag, base_tag)
    for relpath in (paths['text'], paths['catalog'], paths['base_ini'],
                    paths['target_ini'], paths['images']):
        os.makedirs(os.path.dirname(os.path.join(kit_dir, relpath))
                    or kit_dir, exist_ok=True)

    assets_dir = resolve_path(config, 'assets_dir', 'assets')
    seed_dirs = _seed_dirs(config)

    log('profile : %s' % rel(profile))
    log('kit     : %s' % rel(kit_dir))
    log('language: %s (base %s, ui %s)' % (tag, base_tag, ', '.join(ui_tags)))

    catalog = menu_catalog.discover(
        TEMPLATE, config, profile_path=profile,
        seed_dirs=seed_dirs, base_tag=base_tag, ui_tags=ui_tags,
        decompiler=decompiler, scan_compiled=scan_compiled, log=log)

    # Authoritative INIs decide which reconstructed sysarg value keys are real.
    # ``:group=value`` entries discovered from an INI are the voted truth; script
    # reconstruction only fills gaps when the title ships no INI at all.  Only
    # the title's *own* INIs vote: the template reference is a different engine
    # build and lists sysargs this title does not have (and vice versa).
    known_sysarg = {key for key, entry in catalog.entries.items()
                    if entry.kind == 'ini' and key.startswith(':')
                    and (entry.source or '').startswith('ini:')}
    _keep_known_sysarg(catalog, known_sysarg)

    catalog_path = os.path.join(kit_dir, paths['catalog'])
    catalog.write(catalog_path)
    log('catalog : %s (%d keys, %d conflict(s))'
        % (rel(catalog_path), len(catalog.entries), len(catalog.conflicts)))

    # --- base INI -----------------------------------------------------
    # The title's own captions, else the template's reference file for that
    # language.  ``paths.assets_dir`` is build output, never a source: seeding
    # from it would feed the last bundle's wording (and its mistakes) back in.
    base_src = _first_ini([
        _find_game_ini(config, base_tag),
        _reference_ini(base_tag),
    ])
    base_dest = os.path.join(kit_dir, paths['base_ini'])
    base_text = menu_catalog.read_text(base_src) if base_src \
        else '!section menu'
    # The title's own captions win; the catalogue only adds what the file
    # does not mention yet.  The selector's own captions (the group names)
    # are refreshed: they belong to the tool, not to the game.
    base_text = _rewrite_sections(base_text,
                                  _all_captions(catalog, base_tag=base_tag),
                                  keep_existing=True)
    base_text = _rewrite_sections(base_text,
                                  {'menu': _selector_captions(catalog)})
    _write_ini(base_dest, base_text)
    # A caption that changed while the catalogue was merged in means the
    # catalogue carried a caption of another language -- that would replace
    # the title's own interface.
    if base_src:
        _warn_changed_captions(base_src, base_dest,
                               ignore=_selector_captions(catalog))
    log('base ini: %s%s' % (rel(base_dest),
                            ' (from %s)' % rel(base_src) if base_src else ''))

    # --- the other interface languages --------------------------------
    # The build regenerates the INI of every interface language from the
    # title's own file, so the kit keeps a copy of each of them: without
    # it the other interface languages would ship with nothing but the menu
    # captions.
    for ui_tag in _game_ini_tags(config, ui_tags):
        if ui_tag in (base_tag, tag):
            continue
        ui_src = _first_ini([
            _find_game_ini(config, ui_tag),
            _reference_ini(ui_tag),
        ])
        source_note = 'from %s' % rel(ui_src) if ui_src else None
        if not ui_src:
            continue
        ui_dest = os.path.join(kit_dir, 'ui',
                               'syslangtext_%s.ini' % ui_tag)
        # Only the texts the scripts ask for are language-neutral; a caption
        # read from another language's INI must not leak into this one.
        ui_text = _rewrite_sections(
            menu_catalog.read_text(ui_src),
            _all_captions(catalog, NEUTRAL_KINDS), keep_existing=True)
        # The selector's own captions belong to this language: every caption
        # file names the interface language in its own wording.
        ui_text = _rewrite_sections(
            ui_text,
            {'menu': menu_catalog.reference_menu_captions(ui_tag)},
            keep_existing=True)
        _write_ini(ui_dest, ui_text)
        log('ui ini  : %s (%s)' % (rel(ui_dest), source_note or 'empty'))

    # --- target INI ---------------------------------------------------
    # The kit's own file is the translation in progress: refreshing must never
    # discard it.  On a fresh kit the title's caption file or the template
    # reference seeds it.
    target_src = _first_ini([
        _ini_in_dir(os.path.join(kit_dir, 'ui'), tag),
        _find_game_ini(config, tag),
        _reference_ini(tag),
    ])
    target_dest = os.path.join(kit_dir, paths['target_ini'])
    if target_src:
        target_text = menu_catalog.read_text(target_src)
        note = 'existing translation from %s' % rel(target_src)
        if base_text and not _has_translation(target_text, base_text):
            # The file still holds the base language's words: seed it again
            # from the title's own caption file, so the captions the title
            # ships (the window title among them) are part of the kit.
            target_text = base_text
            note = 'seeded from the title\'s own captions'
    else:
        target_text = base_text
        note = 'seeded from the base captions - translate the values'
    # Every base key must exist in the target INI.  Existing translated values
    # are preserved; only missing keys are added, with the base caption as a
    # placeholder.  A refresh must never overwrite finished translations.
    target_text = _rewrite_sections(target_text,
                                    _all_captions(catalog, NEUTRAL_KINDS),
                                    keep_existing=True)
    # A key that is still in the source language takes the translation of the
    # language reference, so the translator starts from a translated file
    # instead of a wall of placeholders.
    target_text = _apply_language_reference(target_text, base_text, tag)
    _write_ini(target_dest, target_text)
    log('target  : %s (%s)' % (rel(target_dest), note))

    missing = _missing_translations(
        menu_catalog.read_text(base_dest), menu_catalog.read_text(target_dest))

    # --- text ---------------------------------------------------------
    scn_dir = resolve_path(config, 'scn_dir', 'scn_overrides')
    cache_dir = resolve_path(config, 'cache_dir', 'cache/psb')
    workbook = os.path.join(kit_dir, paths['text'])
    text_notes = []
    if text:
        if not _has_files(scn_dir, '.scn') and ensure_slot:
            rc = run_script('build_scenarios.py', [], profile, log,
                            'Adding the %s language slot to scenario PSBs' % tag)
            if rc:
                text_notes.append('scenario slot creation failed (rc=%d)' % rc)
        if _has_files(scn_dir, '.scn'):
            if os.path.isfile(workbook) and not (force or reset):
                text_notes.append('workbook kept (pass --force to re-export)')
            else:
                args = ['--scn-dir', scn_dir, '--cache-dir', cache_dir,
                        '-o', workbook]
                if os.path.isfile(workbook):
                    # A re-export re-reads every scenario; finished
                    # translations of the rows that still exist are carried
                    # over, so a newly extracted archive only adds rows.
                    args += ['--merge-from', workbook]
                rc = run_script(
                    'export_text.py', args, profile, log,
                    'Exporting scenario text to the kit')
                if rc:
                    text_notes.append('scenario export failed (rc=%d)' % rc)
        else:
            text_notes.append('no scenario PSBs; run: langpatch slot')
    else:
        text_notes.append('text export skipped (--no-text)')
    if not os.path.isfile(workbook):
        _write_text_stub(kit_dir, paths['text'], tag)

    # --- images -------------------------------------------------------
    image_notes = _export_images(config, kit_dir, paths, images, image_only,
                                 force, log)

    if missing:
        log('note    : %d target caption(s) still equal the base language'
            % len(missing))

    data = {
        'kit_version': 1,
        'created': _now(),
        'updated': _now(),
        'game': {
            'root': config.get('game_root'),
            'exe': config.get('game_exe'),
            'slug': slug,
        },
        'profile': rel(profile),
        'profile_snapshot': profile_snapshot,
        'language': _language_meta(config, tag),
        'paths': paths,
        'catalog': _kit_summary(catalog, config, base_tag),
        'missing_translations': missing,
        'text_notes': text_notes,
        'image_notes': image_notes,
        'last_import': None,
        'last_build': None,
    }
    _write_json(os.path.join(kit_dir, 'kit.json'), data)
    _write_readme(kit_dir, data)
    log('')
    log('kit ready: %s' % rel(kit_dir))
    log('  translate : %s' % rel(target_dest))
    if os.path.isfile(workbook):
        log('  dialogue  : %s' % rel(workbook))
    log('  import    : langpatch --config %s kit import --kit %s'
        % (rel(profile), rel(kit_dir)))
    return kit_dir


def _has_files(folder, suffix):
    if not folder or not os.path.isdir(folder):
        return False
    for _base, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(suffix):
                return True
    return False


def _missing_translations(base_text, target_text):
    """Menu keys whose target value is still byte-identical to the base."""
    base = dict(_ini_sections(base_text)[0][1])
    target = dict(_ini_sections(target_text)[0][1])
    return sorted(key for key, value in base.items()
                  if target.get(key, value) == value)


def _write_text_stub(kit_dir, relpath, tag):
    path = os.path.join(kit_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        return
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(
            'No scenario workbook was exported.\n\n'
            'Run the scenario slot step for the %s language first:\n'
            '    langpatch --config <profile> slot\n'
            '    langpatch --config <profile> kit export --kit %s\n'
            % (tag, rel(kit_dir)))


def _export_images(config, kit_dir, paths, mode, only, force, log):
    notes = []
    images_dir = os.path.join(kit_dir, paths['images'])
    manifest = os.path.join(images_dir, 'manifest.json')
    if mode in (None, '', 'none', False):
        _write_images_readme(kit_dir, paths, 'No images were exported.')
        return ['image export skipped (--images none)']
    if mode == 'translated':
        source = resolve_path(config, 'assets_work', 'assets_work')
        source_manifest = os.path.join(source, 'manifest.json') \
            if source else None
        if not source_manifest or not os.path.isfile(source_manifest):
            _write_images_readme(
                kit_dir, paths,
                'No existing assets_work export was found.  Run:\n'
                '    langpatch --config <profile> kit export '
                '--kit <kit> --images all')
            return ['no assets_work/ to copy; use --images all']
        data = _read_json(source_manifest, {})
        assets_dir = resolve_path(config, 'assets_dir', 'assets') or ''
        keep = []
        for asset in data.get('assets') or []:
            target = asset.get('target') or asset.get('override') or ''
            if target and os.path.isfile(
                    os.path.join(assets_dir, target.replace('/', os.sep))):
                keep.append(asset)
        if not keep:
            return ['no translated asset matched a file in %s' % rel(assets_dir)]
        _copy_image_subset(source, images_dir, data, keep)
        _write_images_readme(kit_dir, paths,
                             'Copied %d translated asset(s).' % len(keep))
        notes.append('copied %d translated asset(s)' % len(keep))
        return notes

    if os.path.isfile(manifest) and not (force or mode == 'refresh'):
        notes.append('image export kept (pass --force to re-export)')
        return notes
    args = ['--out', images_dir,
            '--workbook', os.path.join(kit_dir, paths['images_workbook']),
            '--filemap', resolve_path(config, 'filemap', 'filemap.json')]
    if mode in ('ui', 'ui-text', 'refresh'):
        # ``refresh`` re-exports the interface after a reset: it is the UI
        # selection, only with an empty folder first.
        args.append('--ui')
        if mode == 'ui-text':
            args.append('--text-only')
    extracted = resolve_top(config, 'extract_dir')
    if extracted:
        args += ['--extracted', extracted]
    if only:
        for needle in str(only).split(','):
            if needle.strip():
                args += ['--only', needle.strip()]
    if mode == 'refresh' or force:
        # A forced export starts from an empty folder: files of an older
        # selection would otherwise stay behind without a manifest row and
        # show up in the kit as assets that have no workbook entry.
        args.append('--reset')
    rc = run_script('export_assets.py', args,
                    profile_of(config), log,
                    'Exporting images / PSB textures to the kit')
    if rc:
        notes.append('image export failed (rc=%d)' % rc)
        _write_images_readme(
            kit_dir, paths,
            'Image export failed.  Check paths.filemap / extract_dir in the '
            'profile, then rerun:\n    langpatch --config <profile> kit export '
            '--kit <kit> --images all')
    else:
        notes.append('images exported')
    return notes


def _copy_image_subset(source, dest, data, keep):
    os.makedirs(os.path.join(dest, 'files'), exist_ok=True)
    os.makedirs(os.path.join(dest, 'psb'), exist_ok=True)
    assets = []
    for asset in keep:
        relpath = asset.get('path') or ''
        src = os.path.join(source, relpath.replace('/', os.sep))
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest, relpath.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        assets.append(asset)
    _write_json(os.path.join(dest, 'manifest.json'), {
        'generated': _now(),
        'tag': data.get('tag'),
        'out': dest,
        'assets': assets,
    })


def _write_images_readme(kit_dir, paths, message):
    path = os.path.join(kit_dir, paths['images'], 'README.md')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(
            '# Images\n\n%s\n\n'
            'An image export is optional.  To create a full one:\n\n'
            '    langpatch --config <profile> kit export '
            '--kit <kit> --images all\n\n'
            'For interface images only (uipsd screens plus attention / warning '
            '/ watermark pictures):\n\n'
            '    langpatch --config <profile> kit export '
            '--kit <kit> --images ui\n\n'
            '`--images ui-text` keeps the screens the game loads in a language '
            'variant plus the PSBs that have text layers; a pure decoration\n'
            'image is left out.\n'
            'Only files you actually edit are written back by '
            '`kit import`; unedited exports are skipped by hash.\n' % message)


# ---------------------------------------------------------------------------
# readme
# ---------------------------------------------------------------------------

def _write_readme(kit_dir, data):
    language = data['language']
    tag = language['tag']
    base = language['base_tag']
    profile = data['profile']
    kit = rel(kit_dir)
    name_col = 'name_%s' % tag
    if language.get('native_multilang'):
        speaker_hint = [
            '   A speaker name is also the key into the voice table and the',
            '   name plate, so leave the speaker columns alone and translate',
            '   the `%s` column of the `Names` sheet instead.' % name_col,
        ]
    else:
        speaker_hint = [
            '   Speaker names already translated in the speaker column keep',
            '   working; a name still in the base language can be',
            '   translated in the `%s` column of the `Names` sheet.' % name_col,
        ]
    lines = [
        '# Translation kit: %s (%s)' % (data['game'].get('slug'), tag),
        '',
        'Generated %s from `%s`.' % (data['created'], profile),
        '',
        '| | |',
        '|---|---|',
        '| Game | `%s` |' % data['game'].get('root'),
        '| Language | `%s` (%s) |' % (tag, language.get('name')),
        '| Base language | `%s` |' % base,
        '| UI variants | %s |' % ', '.join(language.get('ui_tags') or []),
        '| Text slot | %s |' % language.get('index'),
        '| Menu keys | %s |' % data['catalog'].get('total'),
        '| Profile snapshot | `%s` |' % (data.get('profile_snapshot') or '-'),
        '',
        '## What to translate',
        '',
        '1. **Dialogue** - `%s`.' % data['paths']['text'],
        '   Fill the `_%s` text columns and the `_%s` columns in the' % (tag, tag),
        '   `Choices` / `Titles` sheets.  Leave a cell empty to keep the',
        '   current value.',
    ] + speaker_hint + [
        '2. **Menus / UI** - `%s`.' % data['paths']['target_ini'],
        '   Edit the values after the tab.  Every key must stay; missing keys',
        '   make the menu fall back to the base language.',
        '3. **Images** - `%s/` when present.  Edit the PNGs in place and' %
        os.path.dirname(data['paths']['images_manifest']),
        '   keep the file names; `kit import` recompiles only changed exports.',
        '',
        '## Import and build',
        '',
        '```',
        'langpatch --config %s kit import --kit %s' % (profile, kit),
        'langpatch --config %s build' % profile,
        '```',
        '',
        '`kit import` writes the workbook into the scenario PSBs, copies both',
        'INIs into the profile asset folder and repacks edited images.  Add',
        '`--build` to run the patch builder afterwards in one step.',
        '',
        'Everything needed to translate is inside this folder.  `profile.json`',
        'is a snapshot of the game profile, so import still works if the',
        'template profile is renamed.  Image sources and their manifest live in',
        '`%s/`; no external `assets_work/` is required.'
        % os.path.dirname(data['paths']['images_manifest']),
        '',
        '## Notes',
        '',
        '- The base INI (`%s`) is the reference.  It is regenerated from the'
        % data['paths']['base_ini'],
        '  game and the discovered catalogue; do not translate it.',
        '- `ui/catalog.json` records where each caption came from and the',
        '  discovery conflicts, so a wrong caption can be traced to its source.',
        '- `%d` target caption(s) still equal the base language.'
        % len(data.get('missing_translations') or []),
        '',
    ]
    for note in (data.get('text_notes') or []) + (data.get('image_notes') or []):
        lines.append('- %s' % note)
    lines.append('')
    with open(os.path.join(kit_dir, 'README.md'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines))


# ---------------------------------------------------------------------------
# export / import / status
# ---------------------------------------------------------------------------

def export(kit_dir, *, text=False, images=None, image_only=None,
           refresh_ui=False, force=False, log=print):
    data = _load_kit(kit_dir)
    profile = _profile_for_kit(kit_dir, data)
    config = configfile.load(profile)
    snapshot = os.path.join(
        kit_dir, data.get('profile_snapshot') or 'profile.json')
    changed = False
    if not os.path.isfile(snapshot):
        data['profile_snapshot'] = _write_profile_snapshot(
            kit_dir, config, profile)
        changed = True
    tag = data['language']['tag']
    # Pick up profile edits (text slot, UI tags, native-multilang flag) so the
    # kit metadata and README never describe an older profile.
    data['language'] = _language_meta(config, tag)
    base_tag = data['language']['base_tag']
    paths = _kit_paths(kit_dir, tag, base_tag)
    data['paths'] = paths

    if text:
        workbook = os.path.join(kit_dir, paths['text'])
        if os.path.isfile(workbook) and not force:
            log('workbook exists: %s (use --force to re-export)' % rel(workbook))
            data['text_notes'] = ['scenario workbook present']
            changed = True
        else:
            scn_dir = resolve_path(config, 'scn_dir', 'scn_overrides')
            cache_dir = resolve_path(config, 'cache_dir', 'cache/psb')
            rc = run_script('export_text.py',
                            ['--scn-dir', scn_dir, '--cache-dir', cache_dir,
                             '-o', workbook], profile, log,
                            'Exporting scenario text')
            if rc:
                data['text_notes'] = ['scenario export failed (rc=%d)' % rc]
                _write_json(os.path.join(kit_dir, 'kit.json'), data)
                return rc
            data['text_notes'] = ['scenario workbook exported']
            changed = True
    if refresh_ui:
        catalog = menu_catalog.discover(
            TEMPLATE, config, profile_path=profile,
            seed_dirs=_seed_dirs(config),
            base_tag=base_tag, ui_tags=data['language'].get('ui_tags'),
            log=log)
        catalog.write(os.path.join(kit_dir, paths['catalog']))
        base_src = _first_ini([
            _find_game_ini(config, base_tag),
            _reference_ini(base_tag)])
        base_text = menu_catalog.read_text(base_src) if base_src else '!section menu'
        base_text = _rewrite_ini(base_text, _menu_captions(catalog))
        _write_ini(os.path.join(kit_dir, paths['base_ini']), base_text)
        target = os.path.join(kit_dir, paths['target_ini'])
        target_text = menu_catalog.read_text(target) if os.path.isfile(target) \
            else base_text
        target_text = _rewrite_ini(target_text, _menu_captions(catalog),
                                   keep_existing=True)
        _write_ini(target, target_text)
        data['catalog'] = _kit_summary(catalog, config, base_tag)
        changed = True
        log('catalogue refreshed')
    if images:
        notes = _export_images(config, kit_dir, paths, images, image_only,
                               force, log)
        data['image_notes'] = notes
        changed = True
    if changed:
        data['missing_translations'] = _refresh_missing(kit_dir, data)
        data['updated'] = _now()
        _write_json(os.path.join(kit_dir, 'kit.json'), data)
        _write_readme(kit_dir, data)
    return 0


def import_kit(kit_dir, *, text=True, images=True, build=False,
               dry_run=False, apply_empty=False, all_images=False,
               log=print):
    data = _load_kit(kit_dir)
    profile = _profile_for_kit(kit_dir, data)
    if not os.path.isfile(profile):
        raise SystemExit('profile not found: %s' % profile)
    config = configfile.load(profile)
    snapshot = os.path.join(
        kit_dir, data.get('profile_snapshot') or 'profile.json')
    if not os.path.isfile(snapshot):
        data['profile_snapshot'] = _write_profile_snapshot(
            kit_dir, config, profile)
    tag = data['language']['tag']
    data['language'] = _language_meta(config, tag)
    base_tag = data['language']['base_tag']
    paths = _kit_paths(kit_dir, tag, base_tag)
    data['paths'] = paths
    scn_dir = resolve_path(config, 'scn_dir', 'scn_overrides')
    cache_dir = resolve_path(config, 'cache_dir', 'cache/psb')
    assets_dir = resolve_path(config, 'assets_dir', 'assets')
    rc = 0

    if text:
        workbook = os.path.join(kit_dir, paths['text'])
        if os.path.isfile(workbook) and _has_files(scn_dir, '.scn'):
            args = ['--scn-dir', scn_dir, '--cache-dir', cache_dir,
                    '-w', workbook]
            if dry_run:
                args.append('--dry-run')
            if apply_empty:
                args.append('--apply-empty')
            rc = run_script('import_text.py', args, profile, log,
                            'Importing dialogue into scenario PSBs%s'
                            % (' (dry run)' if dry_run else ''))
            if rc:
                return rc
        elif not os.path.isfile(workbook):
            log('text    : no workbook in the kit (skipped)')
        else:
            log('text    : no scenarios in %s (run: langpatch slot)' % rel(scn_dir))

    if assets_dir:
        os.makedirs(assets_dir, exist_ok=True)
        # Every interface language has one caption file in the kit: the
        # base language's own captions (the reference), the languages the
        # title ships and the new language's translation.
        ui_dir = os.path.join(kit_dir, 'ui')
        if os.path.isdir(ui_dir):
            for name in sorted(os.listdir(ui_dir)):
                if not name.startswith('syslangtext_') \
                        or not name.endswith('.ini'):
                    continue
                source = os.path.join(ui_dir, name)
                dest = os.path.join(assets_dir, name)
                if dry_run:
                    log('DRY ini : %s -> %s' % (rel(source), rel(dest)))
                else:
                    shutil.copy2(source, dest)
                    log('ini     : %s -> %s' % (rel(source), rel(dest)))

    manifest = os.path.join(kit_dir, paths['images_manifest'])
    if images and os.path.isfile(manifest):
        args = ['--work', os.path.join(kit_dir, paths['images']),
                '--out', assets_dir]
        if dry_run:
            args.append('--dry-run')
        if all_images:
            args.append('--all')
        rc = run_script('import_assets.py', args, profile, log,
                        'Repacking edited images / PSB textures%s'
                        % (' (dry run)' if dry_run else ''))
        if rc:
            return rc
    elif images:
        log('images  : no manifest in the kit (skipped)')

    if not dry_run:
        data['missing_translations'] = _refresh_missing(kit_dir, data)
        data['last_import'] = _now()
        data['updated'] = _now()
        _write_json(os.path.join(kit_dir, 'kit.json'), data)
        _write_readme(kit_dir, data)

    if build and not dry_run:
        rc = run_script('build_patch.py', [], profile, log,
                        'Building patch.xp3 + unencrypted/')
        if rc == 0:
            data['last_build'] = _now()
            _write_json(os.path.join(kit_dir, 'kit.json'), data)
    return rc


def pack(kit_dir, *, text=True, images=True, dry_run=False, log=print):
    """Import the kit and pack a ready-to-install patch.

    The result is written to ``<kit>/dist/``: ``patch.xp3`` and
    ``version.dll`` are everything a player needs, so the kit folder can be
    handed over as it is.
    """
    data = _load_kit(kit_dir)
    profile = _profile_for_kit(kit_dir, data)
    rc = import_kit(kit_dir, text=text, images=images, build=True,
                    dry_run=dry_run, log=log)
    if rc or dry_run:
        return rc
    config = configfile.load(profile)
    build_dir = resolve_path(config, 'build_dir', 'build')
    dist = os.path.join(kit_dir, 'dist')
    os.makedirs(dist, exist_ok=True)
    copied = []
    for name in ('patch.xp3', 'version.dll'):
        source = os.path.join(build_dir, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(dist, name))
            copied.append(name)
    data = _load_kit(kit_dir)
    data['last_pack'] = _now()
    _write_json(os.path.join(kit_dir, 'kit.json'), data)
    log('dist    : %s' % rel(dist))
    for name in copied:
        log('         %s (%d bytes)'
            % (name, os.path.getsize(os.path.join(dist, name))))
    if not copied:
        log('         nothing was built - run the build again')
    return 0


def status(kit_dir, log=print):
    data = _load_kit(kit_dir)
    paths = data['paths']
    tag = data['language']['tag']
    base_tag = data['language']['base_tag']
    log('kit      : %s' % rel(kit_dir))
    log('profile  : %s%s' % (
        data['profile'],
        '' if os.path.isfile(os.path.join(TEMPLATE, data['profile']))
        else ' (using kit snapshot)'))
    log('snapshot : %s' % (data.get('profile_snapshot') or '-'))
    log('game     : %s' % data['game'].get('root'))
    log('language : %s (%s), slot %s, base %s, ui %s'
        % (tag, data['language'].get('name'), data['language'].get('index'),
           base_tag, ', '.join(data['language'].get('ui_tags') or [])))
    catalog = data.get('catalog') or {}
    log('catalog  : %s keys %s' % (catalog.get('total'),
                                   catalog.get('sections') or {}))
    log('sources  :')
    for source, count in (catalog.get('sources') or {}).items():
        log('           %-48s %d' % (source, count))
    base_path = os.path.join(kit_dir, paths['base_ini'])
    target_path = os.path.join(kit_dir, paths['target_ini'])
    if os.path.isfile(base_path) and os.path.isfile(target_path):
        base_text = menu_catalog.read_text(base_path)
        target_text = menu_catalog.read_text(target_path)
        base = dict(_ini_sections(base_text)[0][1])
        target = dict(_ini_sections(target_text)[0][1])
        missing = [key for key in base if key not in target]
        untranslated = _missing_translations(base_text, target_text)
        log('ini      : base %d keys, target %d keys, missing %d, '
            'untranslated %d'
            % (len(base), len(target), len(missing), len(untranslated)))
    for key in ('text', 'base_ini', 'target_ini'):
        path = os.path.join(kit_dir, paths[key])
        log('%-9s: %-48s %s' % (key, rel(path),
                                'ok' if os.path.isfile(path) else 'missing'))
    manifest = os.path.join(kit_dir, paths['images_manifest'])
    if os.path.isfile(manifest):
        images = _read_json(manifest, {})
        log('images   : %d asset(s), exported %s'
            % (len(images.get('assets') or []), images.get('generated')))
    else:
        log('images   : no export')
    log('imported : %s' % (data.get('last_import') or '-'))
    log('built    : %s' % (data.get('last_build') or '-'))
    return 0


def cmd_list(args=None, log=print):
    kits = list_kits()
    if not kits:
        log('no kits yet - create one with: langpatch kit create')
        return 0
    log('%-34s %-5s %-6s %-8s %s'
        % ('kit', 'lang', 'keys', 'images', 'profile'))
    for kit_dir, data in kits:
        language = data.get('language') or {}
        catalog = data.get('catalog') or {}
        manifest = os.path.join(kit_dir,
                                (data.get('paths') or {}).get(
                                    'images_manifest',
                                    'ui/images/manifest.json'))
        image_count = 0
        if os.path.isfile(manifest):
            image_count = len(_read_json(manifest, {}).get('assets') or [])
        log('%-34s %-5s %-6s %-8s %s'
            % (rel(kit_dir), language.get('tag'), catalog.get('total', '-'),
               image_count, data.get('profile', '-')))
    return 0


# ---------------------------------------------------------------------------
# CLI glue used by langpatch.py
# ---------------------------------------------------------------------------

def cmd_create(args, profile, log=print):
    create(
        profile, game=args.game, tag=args.tag, slug=args.slug,
        images=args.images, image_only=args.only, text=not args.no_text,
        ensure_slot=not args.no_slot, force=args.force, reset=args.reset,
        decompiler=args.decompiler,
        scan_compiled=not args.no_compiled, log=log)
    return 0


def cmd_export(kit_dir, args, log=print):
    return export(kit_dir, text=args.text, images=args.images,
                  image_only=args.only, refresh_ui=args.ui,
                  force=args.force, log=log)


def cmd_import(kit_dir, args, log=print):
    return import_kit(kit_dir, text=not args.no_text, images=not args.no_images,
                      build=args.build, dry_run=args.dry_run,
                      apply_empty=args.apply_empty, all_images=args.all,
                      log=log)


def cmd_status(kit_dir, args, log=print):
    return status(kit_dir, log=log)
