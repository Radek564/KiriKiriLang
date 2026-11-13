#!/usr/bin/env python
"""One-command driver for the KrKrZ translation patch.

Everything the day-to-day workflow needs, with paths taken from
``config.json`` (top-level keys plus an optional ``paths`` block).  The target
language, its tag and the variant index all come from ``language`` — nothing
here assumes a specific language.

The short way:

    py -X utf8 langpatch.py "D:\\Games\\Some Game"   # set everything up
    py -X utf8 langpatch.py pack                    # build patch.xp3

``new`` inspects the game, writes the profile, extracts the archives, adds
the language slot and builds the translation kit.  ``pack`` writes the kit
back into the game data and packs the result into ``<kit>/dist/``.

The individual steps:

    py -X utf8 langpatch.py status          # what exists, what is installed
    py -X utf8 langpatch.py auto            # slot -> import -> build (safe default)
    py -X utf8 langpatch.py slot            # rebuild scenario PSBs with the target slot
    py -X utf8 langpatch.py export          # scn_overrides -> workbook
    py -X utf8 langpatch.py import          # workbook -> scn_overrides
    py -X utf8 langpatch.py build           # pack patch.xp3 + stage test copy
    py -X utf8 langpatch.py images-export   # game images/PSBs -> assets_work/
    py -X utf8 langpatch.py images-import   # edited assets_work/ -> assets/
    py -X utf8 langpatch.py assets-seed     # reference-language assets -> _<tag> copies
    py -X utf8 langpatch.py all             # slot + (import|export) + build
    py -X utf8 langpatch.py extract         # list/extract XP3 archives (headless)
    py -X utf8 langpatch.py deploy          # copy build/ into the game folder
    py -X utf8 langpatch.py profile         # inspect a game -> configs/<slug>.json
    py -X utf8 langpatch.py kit create      # portable kit: text + UI + images + README
    py -X utf8 langpatch.py kit status      # show a kit and its translation state

``auto`` never overwrites an existing workbook: it imports it, then builds.
Use ``export --force`` when you really want to regenerate it from scratch.

Run ``langpatch.cmd <command>`` to avoid typing the interpreter.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

TEMPLATE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(TEMPLATE, 'src')


def _split_config_arg(argv):
    """Pull ``--config PATH`` out of *argv*, wherever it appears.

    The profile must be selected before any ``src`` module is imported, so this
    runs on raw ``sys.argv`` and the cleaned list is handed to argparse later.
    """
    kept = []
    value = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--config':
            if i + 1 < len(argv):
                value = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith('--config='):
            value = arg.split('=', 1)[1]
            i += 1
            continue
        kept.append(arg)
        i += 1
    return kept, value


ARGV, _CONFIG_ARG = _split_config_arg(sys.argv[1:])
if _CONFIG_ARG:
    _config_abs = (_CONFIG_ARG if os.path.isabs(_CONFIG_ARG)
                   else os.path.join(os.getcwd(), _CONFIG_ARG))
    os.environ['TRANSLATION_PATCH_CONFIG'] = os.path.abspath(_config_abs)

if SRC not in sys.path:
    sys.path.insert(0, SRC)
import configfile
import kit as kitmod

CONFIG = configfile.load()
CONFIG_PATH = configfile.config_path()

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
TAG = CONFIG['language']['tag']
INDEX = CONFIG['language']['index']

DEFAULT_PATHS = {
    'scn_dir': 'scn_overrides',
    'build_dir': 'build',
    'workbook': 'text_export.xlsx',
    'cache_dir': 'cache/psb',
    'extract_output': 'extracted',
    'loader': 'loader',
    'filemap': 'filemap.json',
    'assets_dir': 'assets',
    'assets_work': 'assets_work',
    'assets_workbook': 'assets_export.xlsx',
}


def path_of(key):
    """Resolve a path from config["paths"], falling back to the default."""
    raw = (CONFIG.get('paths') or {}).get(key) or DEFAULT_PATHS[key]
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def config_path(key, default=None):
    """Resolve a top-level config path relative to the template."""
    raw = CONFIG.get(key) or default
    if not raw:
        return raw
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


SCN_DIR = path_of('scn_dir')
BUILD_DIR = path_of('build_dir')
WORKBOOK = path_of('workbook')
CACHE_DIR = path_of('cache_dir')
ASSETS_DIR = path_of('assets_dir')
ASSETS_WORK = path_of('assets_work')
ASSETS_BOOK = path_of('assets_workbook')


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def rel(path):
    """Shorten an absolute path for display."""
    try:
        r = os.path.relpath(path, TEMPLATE)
    except ValueError:
        return path
    return path if r.startswith('..') else r


def count_scn():
    if not os.path.isdir(SCN_DIR):
        return 0
    return sum(1 for base, _dirs, files in os.walk(SCN_DIR)
               for n in files if n.lower().endswith('.scn'))


def newest_mtime(folder, suffix='.scn'):
    if not os.path.isdir(folder):
        return 0.0
    times = [os.path.getmtime(os.path.join(base, n))
             for base, _dirs, files in os.walk(folder)
             for n in files if n.lower().endswith(suffix)]
    return max(times) if times else 0.0


def human_time(ts):
    import time
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if ts else '-'


def run(script, args=(), title=None):
    """Run one of the src/ scripts with the same interpreter."""
    if title:
        print('\n=== %s ===' % title)
    cmd = [sys.executable, '-X', 'utf8', os.path.join(SRC, script)]
    cmd += [str(a) for a in args if a not in (None, False)]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env[configfile.ENV] = configfile.config_path()
    return subprocess.call(cmd, cwd=TEMPLATE, env=env)


def run_self(args=(), title=None, config=None):
    """Run this driver again, optionally with another profile.

    A freshly generated profile has to be picked up by a *new* process: the
    modules in ``src/`` read the profile once, when they are imported.
    """
    if title:
        print('\n=== %s ===' % title)
    cmd = [sys.executable, '-X', 'utf8', os.path.join(TEMPLATE, 'langpatch.py')]
    if config:
        cmd += ['--config', config]
    cmd += [str(a) for a in args if a not in (None, False)]
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return subprocess.call(cmd, cwd=TEMPLATE, env=env)


def need_scn_dir():
    if not os.path.isdir(SCN_DIR) or not count_scn():
        raise SystemExit('no scenarios in %s - run: langpatch slot' % rel(SCN_DIR))


def workbook_rows(path):
    """Return {sheet: row_count} or None when openpyxl is unavailable."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None
    try:
        wb = load_workbook(path, read_only=True)
    except Exception:
        return None
    out = {}
    for name in wb.sheetnames:
        if name == 'README':
            continue
        ws = wb[name]
        out[name] = max(0, (ws.max_row or 1) - 1)
    wb.close()
    return out


def copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            copy_tree(s, d)
        else:
            shutil.copy2(s, d)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_status(args):
    print('template : %s' % TEMPLATE)
    print('config   : %s' % configfile.describe())
    print('language : %s (index %d)' % (TAG, INDEX))
    print('game     : %s' % CONFIG.get('game_root', '-'))
    extract_dir = config_path('extract_dir')
    print('extract  : %s' % (extract_dir or '-'))
    print('test copy: %s' % (CONFIG.get('test_dir') or '-'))
    extracted = extract_dir
    if extracted and os.path.isdir(extracted):
        n = 0
        for _r, _d, files in os.walk(extracted):
            n += len(files)
        print('           %d extracted file(s)' % n)
    else:
        print('           not extracted (run: langpatch extract)')
    filemap = path_of('filemap')
    if os.path.isfile(filemap):
        try:
            n = len(json.load(open(filemap, encoding='utf-8')))
        except Exception:
            n = 0
        print('filemap  : %-40s %d entries' % (rel(filemap), n))
    else:
        print('filemap  : %-40s missing' % rel(filemap))
    print('')
    scn = count_scn()
    print('scn_dir  : %-40s %d .scn  newest %s'
          % (rel(SCN_DIR), scn, human_time(newest_mtime(SCN_DIR))))
    print('cache    : %-40s %d json'
          % (rel(CACHE_DIR),
             len([n for n in os.listdir(CACHE_DIR)
                  if n.endswith('.json')]) if os.path.isdir(CACHE_DIR) else 0))
    # The dialogue lives in the kit: the profile's workbook is only the
    # export target of the old flow.
    kit_workbook = os.path.join(TEMPLATE, 'kits',
                                kitmod.profile_slug(CONFIG, CONFIG_PATH),
                                TAG, 'text', 'text.xlsx')
    if os.path.isfile(kit_workbook):
        rows = workbook_rows(kit_workbook)
        if rows is None:
            detail = '%d bytes' % os.path.getsize(kit_workbook)
        else:
            detail = ', '.join('%s %d' % (k, v) for k, v in rows.items())
        print('workbook : %-40s %s (newest %s)'
              % (rel(kit_workbook), detail,
                 human_time(os.path.getmtime(kit_workbook))))
    elif os.path.isfile(WORKBOOK):
        rows = workbook_rows(WORKBOOK)
        if rows is None:
            detail = '%d bytes' % os.path.getsize(WORKBOOK)
        else:
            detail = ', '.join('%s %d' % (k, v) for k, v in rows.items())
        print('workbook : %-40s %s (newest %s)'
              % (rel(WORKBOOK), detail, human_time(os.path.getmtime(WORKBOOK))))
    else:
        print('workbook : %-40s missing (run: langpatch export)' % rel(WORKBOOK))

    patch = os.path.join(BUILD_DIR, 'patch.xp3')
    dll = os.path.join(BUILD_DIR, 'version.dll')
    if os.path.isfile(patch):
        unenc = os.path.join(BUILD_DIR, 'unencrypted')
        n = len(os.listdir(unenc)) if os.path.isdir(unenc) else 0
        print('build    : %-40s patch %d B, dll %d B, %d loose files'
              % (rel(BUILD_DIR), os.path.getsize(patch),
                 os.path.getsize(dll) if os.path.isfile(dll) else 0, n))
    else:
        print('build    : %-40s missing (run: langpatch build)' % rel(BUILD_DIR))

    game = CONFIG.get('game_root')
    if game and os.path.isdir(game):
        installed = os.path.isfile(os.path.join(game, 'patch.xp3'))
        print('game     : %-40s %s'
              % (rel(game), 'patch installed' if installed else 'patch NOT installed'))
    elif game:
        print('game     : %-40s not found' % game)

    shipped = (len([n for n in os.listdir(ASSETS_DIR)
                    if os.path.isfile(os.path.join(ASSETS_DIR, n))])
               if os.path.isdir(ASSETS_DIR) else 0)
    manifest = os.path.join(ASSETS_WORK, 'manifest.json')
    if os.path.isfile(manifest):
        try:
            data = json.load(open(manifest, encoding='utf-8'))
            n = len(data.get('assets') or [])
        except Exception:
            n = 0
        print('images   : %-40s %d in assets/, %d exported (%s)'
              % (rel(ASSETS_WORK), shipped, n, data.get('generated', '-')))
    else:
        print('images   : %-40s %d in assets/, no export yet'
              % (rel(ASSETS_DIR), shipped))
    print('')
    prefix = ''
    if os.path.abspath(CONFIG_PATH) != os.path.abspath(os.path.join(TEMPLATE, 'config.json')):
        prefix = '--config %s ' % rel(CONFIG_PATH)
    if not scn:
        print('next     : langpatch %sslot' % prefix)
    elif not os.path.isfile(WORKBOOK):
        print('next     : langpatch %sexport' % prefix)
    elif not os.path.isfile(patch):
        print('next     : langpatch %sauto' % prefix)
    else:
        print('next     : translate %s, then langpatch %simport && langpatch %sbuild'
              % (rel(WORKBOOK), prefix, prefix))
    return 0


def cmd_slot(args):
    before = count_scn()
    argv = []
    if args.force:
        argv.append('--force')
    if args.only:
        argv += ['--only', args.only]
    if args.limit:
        argv += ['--limit', args.limit]
    rc = run('build_scenarios.py', argv,
             'Adding the "%s" slot to scenario PSBs' % TAG)
    after = count_scn()
    print('\nscenarios: %d file(s) (%+d)' % (after, after - before))
    if rc == 0 and args.force and os.path.isfile(WORKBOOK):
        # --force rebuilds from the untouched archives, which drops translated
        # text; replay the workbook so nothing is lost.
        print('restoring translations from %s' % rel(WORKBOOK))
        rc = run('import_text.py',
                 ['--scn-dir', SCN_DIR, '--cache-dir', CACHE_DIR,
                  '-w', WORKBOOK], None)
    return rc


def cmd_export(args):
    out = os.path.abspath(args.out or WORKBOOK)
    if os.path.isfile(out) and not args.force:
        print('refusing to overwrite %s' % out)
        print('  a translated workbook would be replaced by reference copies.')
        print('  pass --force if that is really what you want.')
        return 2
    argv = ['--scn-dir', SCN_DIR, '--cache-dir', CACHE_DIR, '-o', out]
    if args.only:
        argv += ['--only', args.only]
    if args.no_cache:
        argv.append('--no-cache')
    return run('export_text.py', argv, 'Exporting scenarios to Excel')


def cmd_import(args):
    wb = os.path.abspath(args.workbook or WORKBOOK)
    if not os.path.isfile(wb):
        print('workbook not found: %s' % wb)
        print('run: langpatch export')
        return 2
    argv = ['--scn-dir', SCN_DIR, '--cache-dir', CACHE_DIR, '-w', wb]
    if args.dry_run:
        argv.append('--dry-run')
    if args.apply_empty:
        argv.append('--apply-empty')
    if args.only:
        argv += ['--only', args.only]
    if args.no_cache:
        argv.append('--no-cache')
    return run('import_text.py', argv,
               'Importing Excel into scenarios%s'
               % (' (dry run)' if args.dry_run else ''))


def cmd_build(args):
    argv = ['--no-stage'] if args.no_stage else []
    patch_only = bool((CONFIG.get('build') or {}).get('patch_only', False))
    title = ('Building patch.xp3' if patch_only
             else 'Building patch.xp3 + unencrypted/')
    rc = run('build_patch.py', argv, title)
    if rc == 0:
        patch = os.path.join(BUILD_DIR, 'patch.xp3')
        print('\noutput: %s' % rel(BUILD_DIR))
        if os.path.isfile(patch):
            print('  patch.xp3   %d bytes' % os.path.getsize(patch))
        print('  version.dll')
        if not patch_only:
            print('  unencrypted/')
    return rc


def _import_or_export(args):
    if os.path.isfile(WORKBOOK):
        return cmd_import(argparse.Namespace(
            workbook=None, dry_run=False, apply_empty=False,
            only=getattr(args, 'only', None), no_cache=False))
    return cmd_export(argparse.Namespace(
        out=None, force=False, only=getattr(args, 'only', None), no_cache=False))


def cmd_auto(args):
    if not count_scn():
        rc = cmd_slot(argparse.Namespace(force=False, only=getattr(args, 'only', None),
                                         limit=0))
        if rc:
            return rc
    rc = _import_or_export(args)
    if rc:
        return rc
    return cmd_build(argparse.Namespace(no_stage=args.no_stage))


def cmd_all(args):
    rc = cmd_slot(argparse.Namespace(force=False, only=getattr(args, 'only', None),
                                     limit=0))
    if rc:
        return rc
    rc = _import_or_export(args)
    if rc:
        return rc
    return cmd_build(argparse.Namespace(no_stage=args.no_stage))


def cmd_extract(args):
    had_archives = bool(args.archives)
    archives = list(args.archives or [])
    if not archives:
        game = CONFIG.get('game_root')
        if not game or not os.path.isdir(game):
            print('game folder not found: %s' % (game or '<unset>'))
            print('set "game_root" in config.json or pass an .xp3 path')
            return 2
        archives = sorted(glob.glob(os.path.join(game, '*.xp3')))
        excluded = {name.lower() for name in (CONFIG.get('extract_exclude') or [])}
        archives = [p for p in archives
                    if os.path.splitext(os.path.basename(p))[0].lower() not in excluded]
        if not archives:
            print('no .xp3 archives in %s' % game)
            return 2

    argv = list(archives)
    if args.list or (not had_archives and not args.out):
        # Bare `langpatch extract` is an inventory: list every game archive.
        argv.append('--list')
    elif args.out:
        argv += ['--out', args.out]
    else:
        argv += ['--out', path_of('extract_output')]
    game = CONFIG.get('game_root')
    if game:
        sys.path.insert(0, SRC)
        import gamecfg
        exe = gamecfg.detect_exe(game, CONFIG.get('game_exe'))
        if exe:
            argv += ['--exe', exe]
    if not args.raw:
        filemap = getattr(args, 'filemap', None) or path_of('filemap')
        argv += ['--filemap', filemap]
    scheme_configured = bool(getattr(args, 'scheme', None)
                             or CONFIG.get('scheme')
                             or getattr(args, 'auto_scheme', False))
    if getattr(args, 'no_mine', False) and not scheme_configured:
        # Without mining and without a scheme the rows would only contain
        # hash names.  A scheme supplies real names, so its filemap is useful.
        argv.append('--no-filemap')
    if args.limit:
        argv += ['--limit', str(args.limit)]
    if args.raw:
        argv.append('--raw')
    if args.verify:
        argv.append('--verify')
    if getattr(args, 'scheme', None):
        argv += ['--scheme', args.scheme]
    elif CONFIG.get('scheme'):
        argv += ['--scheme', CONFIG['scheme']]
    if getattr(args, 'auto_scheme', False):
        argv.append('--auto-scheme')
    if getattr(args, 'no_mine', False):
        argv.append('--no-mine')
    for needle in (getattr(args, 'only', None) or []):
        argv += ['--only', needle]
    return run('extract_xp3.py', argv, 'Reading XP3 archives')


def cmd_images_export(args):
    argv = ['--out', ASSETS_WORK, '--workbook', ASSETS_BOOK,
            '--filemap', path_of('filemap')]
    extracted = config_path('extract_dir')
    if extracted:
        argv += ['--extracted', extracted]
    if getattr(args, 'ui', False):
        argv.append('--ui')
    if getattr(args, 'text_only', False):
        argv.append('--text-only')
    if getattr(args, 'ui_source', None):
        argv += ['--ui-source', args.ui_source]
    if args.only:
        argv += ['--only', args.only]
    if args.limit:
        argv += ['--limit', args.limit]
    if args.extensions:
        argv += ['--extensions', args.extensions]
    if args.dirs:
        argv += ['--dirs', args.dirs]
    if args.reset:
        argv.append('--reset')
    return run('export_assets.py', argv, 'Exporting images / PSB textures')


def cmd_images_import(args):
    argv = ['--work', ASSETS_WORK, '--out', ASSETS_DIR]
    if args.only:
        argv += ['--only', args.only]
    if args.all:
        argv.append('--all')
    if args.dry_run:
        argv.append('--dry-run')
    return run('import_assets.py', argv,
               'Repacking edited images / PSB textures%s'
               % (' (dry run)' if args.dry_run else ''))


def cmd_assets_seed(args):
    argv = ['--out', ASSETS_DIR, '--filemap', path_of('filemap')]
    extracted = config_path('extract_dir')
    if extracted:
        argv += ['--extracted', extracted]
    for folder in (args.from_dir or []):
        argv += ['--from', folder]
    if args.only:
        argv += ['--only', args.only]
    if args.dry_run:
        argv.append('--dry-run')
    if args.force:
        argv.append('--force')
    return run('lang_assets.py', argv,
               'Seeding _%s language assets%s'
               % (TAG, ' (dry run)' if args.dry_run else ''))


def cmd_profile(args):
    """Generate a profile by inspecting a game installation."""
    game = args.game or CONFIG.get('game_root')
    if not game or not os.path.isdir(game):
        print('game folder not found: %s' % (game or '<unset>'))
        print('pass --game DIR or set "game_root" in config.json')
        return 2
    lang = CONFIG.get('language') or {}
    tag = args.tag or lang.get('tag') or 'en'
    name = args.name if args.name is not None else lang.get('name')
    label = args.label if args.label is not None else lang.get('label')
    slug = args.slug
    if not slug and args.out:
        slug = os.path.splitext(os.path.basename(args.out))[0]
    if not slug:
        slug = re.sub(r'[^0-9A-Za-z._-]+', '_',
                      os.path.basename(os.path.abspath(game))).strip('._-')
    out = args.out or os.path.join(TEMPLATE, 'configs', (slug or 'game') + '.json')
    argv = ['--game', os.path.abspath(game), '--tag', tag, '--slug', slug,
            '--out', os.path.abspath(out)]
    if name:
        argv += ['--name', name]
    if label:
        argv += ['--label', label]
    if args.exe:
        argv += ['--exe', args.exe]
    if args.scheme:
        argv += ['--scheme', args.scheme]
    for opt, value in (('--filemap', args.filemap),
                       ('--extracted', args.extracted),
                       ('--extract-dir', getattr(args, 'extract_dir', None)),
                       ('--scripts-dir', args.scripts_dir),
                       ('--scenario-cache', args.scenario_cache),
                       ('--merge-from', args.merge_from),
                       ('--variants', args.variants)):
        if value:
            argv += [opt, value]
    if getattr(args, 'extract', True):
        argv.append('--extract')
    if args.index is not None:
        argv += ['--index', str(args.index)]
    for ui_tag in (args.ui_tag or []):
        argv += ['--ui-tag', ui_tag]
    if args.native_multilang:
        argv.append('--native-multilang')
    menu_title = args.menu_title
    if menu_title is None:
        menu_title = lang.get('menu_title')
    if menu_title:
        argv += ['--menu-title', menu_title]
    menu_labels = dict(lang.get('menu_labels') or {})
    for item in (args.menu_label or []):
        tag, sep, caption = item.partition('=')
        if not sep or not tag.strip() or not caption:
            print('bad --menu-label %r (expected TAG=CAPTION)' % item)
            return 2
        menu_labels[tag.strip()] = caption
    for tag in sorted(menu_labels):
        argv += ['--menu-label', '%s=%s' % (tag, menu_labels[tag])]
    ui_text = {tag: dict(captions or {})
               for tag, captions in (lang.get('ui_text') or {}).items()}
    for item in (args.ui_text or []):
        tag, sep, payload = item.partition('=')
        if not sep or not tag.strip():
            print('bad --ui-text %r (expected TAG=JSON)' % item)
            return 2
        try:
            captions = json.loads(payload)
        except ValueError as error:
            print('bad --ui-text %r: %s' % (item, error))
            return 2
        if not isinstance(captions, dict):
            print('bad --ui-text %r (expected a JSON object)' % item)
            return 2
        ui_text[tag.strip()] = captions
    for tag in sorted(ui_text):
        argv += ['--ui-text', '%s=%s' % (
            tag, json.dumps(ui_text[tag], ensure_ascii=False,
                            separators=(',', ':')))]
    default_ui = (args.default_ui if args.default_ui is not None
                  else lang.get('default_ui'))
    if default_ui:
        argv += ['--default-ui', default_ui]
    if args.force:
        argv.append('--force')
    rc = run('profile_gen.py', argv, 'Profiling %s' % os.path.basename(
        os.path.abspath(game)))
    if rc == 0:
        print('\nnext: langpatch --config %s status' % rel(os.path.abspath(out)))
    return rc


def _profile_for_game(game):
    """Find the profile that already describes *game*.

    A folder that was set up before keeps its profile, its kit and its
    extracted data: pointing at the same game again refreshes them instead
    of creating a second copy under a new name.
    """
    target = os.path.normcase(os.path.abspath(game)).rstrip('\\/')
    configs = os.path.join(TEMPLATE, 'configs')
    if not os.path.isdir(configs):
        return None
    for name in sorted(os.listdir(configs)):
        if not name.lower().endswith('.json'):
            continue
        path = os.path.join(configs, name)
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception:
            continue
        root = data.get('game_root')
        if not root:
            continue
        if os.path.normcase(os.path.abspath(root)).rstrip('\\/') == target:
            return path
    return None


def _default_slug(game, exe=None):
    """The folder name a new game gets inside the template.

    The executable names the game better than the installation folder does:
    a folder may be called "Game v1.04" while the executable is the one
    name that identifies the title.  When the folder holds several
    executables, the one that owns the archives is picked.
    """
    stem = ''
    try:
        import gamecfg
        exe_path = gamecfg.detect_exe(game, exe)
    except Exception:
        exe_path = None
    if exe_path:
        stem = os.path.splitext(os.path.basename(exe_path))[0]
    if not stem:
        stem = os.path.basename(game)
    return re.sub(r'[^0-9A-Za-z._-]+', '_', stem).strip('._-') or 'game'


def cmd_new(args):
    """Set everything up from a game folder.

    One command does the whole preparation: read the installation, write the
    profile, extract the archives, add the language slot to the scenarios and
    build the translation kit.  The folder the user points at is the only
    thing that has to be known.
    """
    game = os.path.abspath(args.game) if args.game else ''
    if not game or not os.path.isdir(game):
        print('game folder not found: %s' % (game or '<unset>'))
        print('usage: langpatch new "D:\\Games\\Some Game"')
        return 2
    lang = CONFIG.get('language') or {}
    tag = args.tag or lang.get('tag')
    if not tag:
        print('no target language tag is configured')
        print('add "language": {"tag": "..."} to config.json')
        return 2
    known = _profile_for_game(game)
    slug = args.slug or (os.path.splitext(os.path.basename(known))[0]
                         if known else _default_slug(game, args.exe))
    profile = os.path.abspath(
        args.profile or known or os.path.join(TEMPLATE, 'configs',
                                              slug + '.json'))
    exists = os.path.isfile(profile)

    print('game    : %s' % game)
    print('profile : %s%s' % (profile, ' (refresh)' if exists else ''))
    print('tag     : %s' % tag)

    # An existing extraction is reused: re-reading gigabytes of archives for
    # a refresh is a waste of time, and the filemap is all the profile needs.
    existing = {}
    if exists:
        try:
            with open(profile, encoding='utf-8') as handle:
                existing = json.load(handle)
        except Exception:
            existing = {}
    old_paths = existing.get('paths') or {}
    extracted = (args.extracted or old_paths.get('extract_dir')
                 or existing.get('extract_dir'))
    filemap = args.filemap or old_paths.get('filemap') or existing.get('filemap')
    if args.extract_dir:
        # The caller wants the archives somewhere else: the old extraction
        # is not reused.
        extracted = filemap = None
    have_extraction = bool(extracted and os.path.isdir(extracted)
                           and filemap and os.path.isfile(filemap))
    if have_extraction:
        print('extract : %s (reused)' % extracted)
    elif not args.no_extract:
        print('extract : the archives are read (this can take a while)')

    # 1. the profile: the game is inspected, the archives are extracted
    profile_args = argparse.Namespace(
        game=game, tag=tag, name=args.name, label=args.label,
        slug=slug, out=profile, exe=args.exe, scheme=args.scheme,
        filemap=filemap if have_extraction else None,
        extracted=extracted if have_extraction else None,
        extract_dir=args.extract_dir,
        scripts_dir=None, scenario_cache=None,
        merge_from=profile if exists else None, variants=None,
        extract=not (have_extraction or args.no_extract),
        index=None, ui_tag=None, native_multilang=False, menu_title=None,
        menu_label=None, ui_text=None, default_ui=None,
        force=bool(exists))
    rc = cmd_profile(profile_args)
    if rc:
        return rc

    # 2. the kit: the scenario slot, the dialogue workbook and the interface
    rc = run_self(['kit', 'create', '--images', args.images, '--force'],
                  title='Creating the translation kit', config=profile)
    if rc:
        return rc

    kit_dir = os.path.join(TEMPLATE, 'kits', slug, tag)
    workbook = os.path.join(kit_dir, 'text', 'text.xlsx')
    ini = os.path.join(kit_dir, 'ui', 'syslangtext_%s.ini' % tag)
    print('\nready')
    print('  profile : %s' % rel(profile))
    print('  kit     : %s' % rel(kit_dir))
    print('  dialogue: %s' % rel(workbook))
    print('  interface: %s' % rel(ini))
    print('\ntranslate the two files above, then run:')
    print('  langpatch --config %s pack' % rel(profile))
    return 0


def cmd_pack(args):
    """Pack the kit into a ready-to-install patch archive.

    The workbook and the interface captions are written back into the game
    data, the patch is built and the result is copied into ``<kit>/dist/``:
    ``patch.xp3`` and ``version.dll`` are all a player needs.
    """
    kit_dir = kitmod.resolve_kit(getattr(args, 'kit', None))
    if not kit_dir:
        kit_dir = kitmod.kit_dir_for(CONFIG)
    if not os.path.isfile(os.path.join(kit_dir, 'kit.json')):
        print('kit not found: %s' % rel(kit_dir))
        print('run: langpatch new "D:\\Games\\Some Game"')
        return 2
    rc = kitmod.pack(kit_dir, text=not args.no_text, images=not args.no_images,
                     log=print)
    if rc:
        return rc
    print('\ninstall it with:')
    print('  langpatch --config %s deploy -y' % rel(CONFIG_PATH))
    return 0


def cmd_kit(args):
    """Portable per-language kit commands (create/export/import/status/list)."""
    action = getattr(args, 'kit_cmd', None)
    if not action:
        print('usage: langpatch kit {create,export,import,status,list}')
        return 2
    if action == 'list':
        return kitmod.cmd_list(args)
    if action == 'create':
        return kitmod.cmd_create(args, CONFIG_PATH)
    kit_dir = kitmod.resolve_kit(getattr(args, 'kit', None))
    if not kit_dir:
        kit_dir = kitmod.kit_dir_for(CONFIG)
    if not os.path.isfile(os.path.join(kit_dir, 'kit.json')):
        print('kit not found: %s' % kit_dir)
        print('pass --kit DIR or run: langpatch kit create')
        return 2
    if action == 'status':
        return kitmod.cmd_status(kit_dir, args)
    if action == 'export':
        return kitmod.cmd_export(kit_dir, args)
    if action == 'import':
        return kitmod.cmd_import(kit_dir, args)
    return 2


PATCH_MARKER = 'langpatch.txt'


def _is_our_patch(path):
    """True when the archive carries the patch template's marker file.

    The marker lets the deploy step tell "our" patch from a foreign one, so
    a translation patch that is already in the game folder is never
    overwritten.
    """
    try:
        import xp3
        archive = xp3.parse_archive(path)
    except Exception:
        return False
    for entry in archive.entries:
        name = (entry.name or '').replace('\\', '/').lower()
        if name == PATCH_MARKER:
            return True
    return False


def _patch_archive_name(folder):
    """Name of the archive the patch is deployed as.

    The game mounts "patch.xp3" and then every "patchN.xp3" that exists, so
    several translation patches can live next to each other.  A foreign
    patch.xp3 is never overwritten: the new patch becomes patch2.xp3,
    patch3.xp3 and so on.  An archive of our own is replaced in place.
    """
    first = os.path.join(folder, 'patch.xp3')
    if not os.path.isfile(first) or _is_our_patch(first):
        return 'patch.xp3'
    index = 2
    while index < 100:
        name = 'patch%d.xp3' % index
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or _is_our_patch(path):
            return name
        index += 1
    return 'patch.xp3'


def cmd_deploy(args):
    target = os.path.abspath(args.to or CONFIG.get('game_root') or '')
    if not target or not os.path.isdir(target):
        print('game folder not found: %s' % (target or '<unset>'))
        print('set "game_root" in config.json or pass --to DIR')
        return 2
    patch = os.path.join(BUILD_DIR, 'patch.xp3')
    dll = os.path.join(BUILD_DIR, 'version.dll')
    build_cfg = CONFIG.get('build') or {}
    loose_name = (build_cfg.get('loose_asset_dir') or 'unencrypted').strip('/\\')
    unenc = os.path.join(BUILD_DIR, loose_name)
    if not (os.path.isfile(patch) and os.path.isfile(dll)):
        print('nothing to deploy - run: langpatch build')
        return 2
    patch_only = bool(build_cfg.get('patch_only', False))
    archive = _patch_archive_name(target)
    print('copy into %s:' % target)
    deploy_loose = bool(build_cfg.get('deploy_loose', not patch_only)) \
        or bool(build_cfg.get('loose_assets', False))
    if patch_only and not deploy_loose:
        print('  %s, version.dll (patch-only)' % archive)
    else:
        print('  %s, version.dll, %s/' % (archive, loose_name))
    if not args.yes:
        try:
            answer = input('continue? [y/N] ').strip().lower()
        except EOFError:
            answer = ''
        if answer not in ('y', 'yes'):
            print('cancelled')
            return 0
    shutil.copy2(patch, os.path.join(target, archive))
    shutil.copy2(dll, os.path.join(target, 'version.dll'))
    if patch_only:
        deploy_loose = deploy_loose and bool(build_cfg.get('loose_assets', False))
    if deploy_loose and os.path.isdir(unenc) and os.listdir(unenc):
        copy_tree(unenc, os.path.join(target, loose_name))
        n = len(os.listdir(os.path.join(target, loose_name)))
        print('deployed %s + version.dll (%d loose files in %s/)'
              % (archive, n, loose_name))
    else:
        print('deployed %s + version.dll (patch-only)' % archive)
    mirror = os.path.join(BUILD_DIR, 'mirror')
    if not patch_only and os.path.isdir(mirror):
        copy_tree(mirror, target)
        print('deployed mirrored overrides into %s' % target)
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog='langpatch',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__.split('\n\n')[0],
        epilog='paths come from config.json: game_root, extract_dir, test_dir, '
               'language, paths.{scn_dir,build_dir,workbook,cache_dir}. '
               'Use --config configs/<game>.json for another game profile.')
    ap.add_argument('--config', default=None, metavar='FILE',
                    help='config profile (default: config.json); may appear '
                         'before or after the command')
    sub = ap.add_subparsers(dest='cmd', metavar='command')

    p = sub.add_parser('status', help='show folders, counts and install state')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('auto', help='slot if needed -> import/export -> build')
    p.add_argument('--only', default=None, help='substring filter on file names')
    p.add_argument('--no-stage', action='store_true', help='do not refresh test_dir')
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser('all', help='slot -> import/export -> build')
    p.add_argument('--only', default=None)
    p.add_argument('--no-stage', action='store_true')
    p.set_defaults(func=cmd_all)

    p = sub.add_parser('slot', help='rebuild scenario PSBs with the %s slot' % TAG)
    p.add_argument('--force', action='store_true',
                   help='re-add the slot from the untouched archives '
                        '(translations are replayed from the workbook)')
    p.add_argument('--only', default=None)
    p.add_argument('--limit', type=int, default=0)
    p.set_defaults(func=cmd_slot)

    p = sub.add_parser('export', help='scenarios -> Excel workbook')
    p.add_argument('-o', '--out', default=None)
    p.add_argument('--force', action='store_true',
                   help='overwrite an existing workbook')
    p.add_argument('--only', default=None)
    p.add_argument('--no-cache', action='store_true')
    p.set_defaults(func=cmd_export)

    p = sub.add_parser('import', help='Excel workbook -> scenarios')
    p.add_argument('-w', '--workbook', default=None)
    p.add_argument('--dry-run', action='store_true', help='preview only')
    p.add_argument('--apply-empty', action='store_true',
                   help='also write rows whose target-language text is empty')
    p.add_argument('--only', default=None)
    p.add_argument('--no-cache', action='store_true')
    p.set_defaults(func=cmd_import)

    p = sub.add_parser('build', help='pack patch.xp3 + version.dll')
    p.add_argument('--no-stage', action='store_true')
    p.set_defaults(func=cmd_build)

    p = sub.add_parser('images-export', aliases=['assets-export'],
                       help='game assets -> editable assets_work/ tree')
    p.add_argument('--only', default=None, help='substring filter on storage name')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--extensions', default=None,
                   help='comma list, e.g. .png,.pimg')
    p.add_argument('--dirs', default=None,
                   help='comma list of unnamed asset dirs')
    p.add_argument('--ui', action='store_true',
                   help='export interface images only (uipsd + attention / '
                        'warning / watermark pictures)')
    p.add_argument('--text-only', action='store_true',
                   help='with --ui, keep only PSBs that contain text layers')
    p.add_argument('--ui-source', default=None,
                   help='folder containing uipsd/ (default: assets.ui_source)')
    p.add_argument('--reset', action='store_true', help='wipe assets_work/ first')
    p.set_defaults(func=cmd_images_export)

    p = sub.add_parser('images-import', aliases=['assets-import'],
                       help='edited assets_work/ -> assets/ (shipped)')
    p.add_argument('--only', default=None)
    p.add_argument('--all', action='store_true', help='repack even unedited exports')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_images_import)

    p = sub.add_parser('assets-seed', aliases=['seed-assets'],
                       help='copy reference-language assets to _%s variants' % TAG)
    p.add_argument('--from', dest='from_dir', action='append', default=[],
                   metavar='DIR', help='extra folder with reference assets')
    p.add_argument('--only', default=None, help='substring filter on target name')
    p.add_argument('--dry-run', action='store_true', help='preview only')
    p.add_argument('--force', action='store_true', help='overwrite existing files')
    p.set_defaults(func=cmd_assets_seed)

    p = sub.add_parser('extract', help='list or extract XP3 archives (headless)')
    p.add_argument('archives', nargs='*',
                   help='.xp3 files (default: every *.xp3 in game_root)')
    p.add_argument('-o', '--out', default=None,
                   help='extract under this folder (default: build/extracted)')
    p.add_argument('--list', action='store_true', help='show the index only')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--raw', action='store_true',
                   help='dump stored bytes even for encrypted Hxv4 entries')
    p.add_argument('--verify', action='store_true',
                   help='check adler32 of every extracted body')
    p.add_argument('--scheme', default=None,
                   help='archive filter title or unique fragment')
    p.add_argument('--auto-scheme', action='store_true',
                   help='detect the XP3 crypt scheme automatically')
    p.add_argument('--no-mine', action='store_true',
                   help='skip hash-name discovery (fast; mine later)')
    p.add_argument('--filemap', default=None,
                   help='index file to write (default: the profile\'s paths.filemap)')
    p.add_argument('--only', action='append', default=None, metavar='TEXT',
                   help='extract only entries whose name contains TEXT '
                        '(repeatable, case-insensitive)')
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser('kit',
                       help='portable per-language translation kits')
    ksub = p.add_subparsers(dest='kit_cmd', metavar='action')

    kp = ksub.add_parser('create',
                         help='build kits/<slug>/<tag>/ from the active profile')
    kp.add_argument('--game', default=None,
                    help='game folder override (default: profile game_root)')
    kp.add_argument('--tag', default=None,
                    help='target language tag (default: language.tag)')
    kp.add_argument('--slug', default=None,
                    help='kit folder name (default: profile name)')
    kp.add_argument('--images', default='none',
                    choices=['none', 'all', 'translated', 'refresh', 'ui',
                             'ui-text'],
                    help='image export mode (default: none)')
    kp.add_argument('--only', default=None,
                    help='comma list of image storage names to export')
    kp.add_argument('--no-text', action='store_true',
                    help='skip the scenario workbook')
    kp.add_argument('--no-slot', action='store_true',
                    help='do not add the target slot when none exists')
    kp.add_argument('--decompiler', default=None,
                    help='path to an external script decompiler')
    kp.add_argument('--no-compiled', action='store_true',
                    help='do not scan compiled .tjs scripts')
    kp.add_argument('--force', action='store_true',
                    help='refresh an existing kit')
    kp.add_argument('--reset', action='store_true',
                    help='delete and rebuild the kit folder')
    kp.set_defaults(func=cmd_kit, kit=None)

    kp = ksub.add_parser('export', help='refresh parts of an existing kit')
    kp.add_argument('--kit', default=None,
                    help='kit folder (default: from the active profile)')
    kp.add_argument('--text', action='store_true',
                    help='re-export the scenario workbook')
    kp.add_argument('--ui', action='store_true',
                    help='rediscover menu/UI captions and refresh both INIs')
    kp.add_argument('--images', default=None,
                    choices=['all', 'translated', 'refresh', 'ui', 'ui-text'],
                    help='image export mode')
    kp.add_argument('--only', default=None,
                    help='comma list of image storage names')
    kp.add_argument('--force', action='store_true',
                    help='overwrite existing exports')
    kp.set_defaults(func=cmd_kit)

    kp = ksub.add_parser('import',
                         help='write the kit back into scenarios/assets/build')
    kp.add_argument('--kit', default=None,
                    help='kit folder (default: from the active profile)')
    kp.add_argument('--no-text', action='store_true',
                    help='skip the scenario workbook')
    kp.add_argument('--no-images', action='store_true',
                    help='skip image repacking')
    kp.add_argument('--build', action='store_true',
                    help='run the patch builder afterwards')
    kp.add_argument('--dry-run', action='store_true', help='preview only')
    kp.add_argument('--apply-empty', action='store_true',
                    help='also write empty target cells')
    kp.add_argument('--all', action='store_true',
                    help='repack even unedited image exports')
    kp.set_defaults(func=cmd_kit)

    kp = ksub.add_parser('status', help='show one kit and its state')
    kp.add_argument('--kit', default=None,
                    help='kit folder (default: from the active profile)')
    kp.set_defaults(func=cmd_kit)

    kp = ksub.add_parser('list', help='list every kit under kits/')
    kp.set_defaults(func=cmd_kit)

    p = sub.add_parser('new', aliases=['setup'],
                       help='set everything up from a game folder')
    p.add_argument('game', nargs='?', default=None,
                   help='the game folder (the only thing that is needed)')
    p.add_argument('--tag', default=None,
                   help='target language tag (default: the configured one)')
    p.add_argument('--name', default=None,
                   help='long language name (default: the configured one)')
    p.add_argument('--label', default=None,
                   help='menu caption (default: the configured one)')
    p.add_argument('--slug', default=None,
                   help='profile/kit folder name (default: the game folder name)')
    p.add_argument('--profile', default=None,
                   help='profile path (default: configs/<slug>.json)')
    p.add_argument('--exe', default=None, help='executable name override')
    p.add_argument('--scheme', default=None, help='archive scheme title override')
    p.add_argument('--images', default='ui-text',
                   choices=['none', 'all', 'translated', 'refresh', 'ui',
                            'ui-text'],
                   help='which images go into the kit (default: ui-text)')
    p.add_argument('--no-extract', action='store_true',
                   help='do not read the archives (an existing extraction is '
                        'reused either way)')
    p.add_argument('--extracted', default=None,
                   help='an existing extraction tree (the archives are not read '
                        'again)')
    p.add_argument('--filemap', default=None,
                   help='the index of that extraction')
    p.add_argument('--extract-dir', default=None,
                   help='folder for the extracted archives (default: '
                        '<slug>/extracted inside the template)')
    p.set_defaults(func=cmd_new)

    p = sub.add_parser('pack', aliases=['patch'],
                       help='pack the kit into patch.xp3 + version.dll')
    p.add_argument('--kit', default=None,
                   help='kit folder (default: from the active profile)')
    p.add_argument('--no-text', action='store_true',
                   help='do not write the dialogue back')
    p.add_argument('--no-images', action='store_true',
                   help='do not repack the interface images')
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser('deploy', help='copy build/ into the game folder')
    p.add_argument('--to', default=None, help='target game folder')
    p.add_argument('-y', '--yes', action='store_true', help='do not ask')
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser('profile',
                       help='inspect a game folder and write a config profile')
    p.add_argument('--game', default=None,
                   help='game installation folder (default: game_root)')
    p.add_argument('--tag', default=None,
                   help='new language tag, e.g. xx (default: language.tag)')
    p.add_argument('--name', default=None,
                   help='long language name (default: language.name)')
    p.add_argument('--label', default=None,
                   help='menu caption (default: language.label)')
    p.add_argument('--exe', default=None, help='executable name override')
    p.add_argument('--scheme', default=None,
                   help='archive scheme title override')
    p.add_argument('--slug', default=None,
                   help='folder prefix inside the template (default: --out name '
                        'or the game folder)')
    p.add_argument('--filemap', default=None,
                   help='filemap.json from an extraction (enables Hx titles)')
    p.add_argument('--extracted', default=None,
                   help='extraction tree that filemap paths resolve against')
    p.add_argument('--extract-dir', default=None,
                   help='folder for the extracted archives (default: '
                        '<slug>/extracted inside the template)')
    p.add_argument('--scripts-dir', default=None,
                   help='folder of existing TJS scripts for discovery')
    p.add_argument('--scenario-cache', default=None,
                   help='folder of .scn.json dumps for text-variant discovery')
    p.add_argument('--merge-from', default=None,
                   help='existing profile whose manual settings are kept')
    p.add_argument('--extract', dest='extract', action='store_true',
                   default=True,
                   help='extract the game archives when no --filemap/--extracted '
                        'is given (default: on)')
    p.add_argument('--no-extract', dest='extract', action='store_false',
                   help='write the profile only; never touch the archives')
    p.add_argument('--variants', default=None,
                   help='comma list of text-variant tags, base first')
    p.add_argument('--index', type=int, default=None,
                   help='target text slot (default: position in variants)')
    p.add_argument('--ui-tag', action='append', default=None,
                   help='interface language tag; repeatable')
    p.add_argument('--native-multilang', action='store_true',
                   help='use the engine multi-language tables directly')
    p.add_argument('--menu-title', default=None,
                   help='in-game language menu caption (default: language.menu_title)')
    p.add_argument('--menu-label', action='append', default=None,
                   metavar='TAG=CAPTION',
                   help='caption of one language entry; repeatable '
                        '(default: language.menu_labels plus overrides)')
    p.add_argument('--ui-text', action='append', default=None,
                   metavar='TAG=JSON',
                   help='menu captions for one interface language, as a JSON '
                        'object; repeatable (default: language.ui_text plus '
                        'overrides)')
    p.add_argument('--default-ui', default=None,
                   help='interface language tag used on a fresh install '
                        '(default: language.default_ui)')
    p.add_argument('-o', '--out', default=None,
                   help='profile path (default: configs/<slug>.json)')
    p.add_argument('--force', action='store_true',
                   help='overwrite an existing profile')
    p.set_defaults(func=cmd_profile)

    return ap


def main(argv=None):
    ap = build_parser()
    if argv is None:
        argv = list(ARGV)
    else:
        argv, config_arg = _split_config_arg(list(argv))
        if config_arg:
            config_abs = (config_arg if os.path.isabs(config_arg)
                          else os.path.join(os.getcwd(), config_arg))
            os.environ['TRANSLATION_PATCH_CONFIG'] = os.path.abspath(config_abs)
    # A folder on the command line is a game: set it up.
    if argv and os.path.isdir(argv[0]):
        argv = ['new', argv[0]] + argv[1:]
    args = ap.parse_args(argv)
    if not getattr(args, 'cmd', None):
        ap.print_help()
        print('')
        cmd_status(args)
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print('\ninterrupted')
        return 130


if __name__ == '__main__':
    sys.exit(main())
