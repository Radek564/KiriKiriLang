"""Repack edited asset exports into the patch ``assets/`` folder.

Reads ``assets_work/manifest.json`` written by ``export_assets.py``, compares
every export with the hash recorded at export time, and only processes the ones
you actually changed:

* plain image   -> copied to ``assets/<target>``
* PSB textures  -> recompiled with the native PSB writer into ``assets/<target>``

``target`` is the path recorded in the manifest: a language override stays at
the assets root (``attention_xx.png``), while an image the engine loads by its
real path keeps its folder (``image/attention5.png``).

The patch build ships everything in ``assets/`` as loose files under
``unencrypted/``, so a rebuild is all that is needed afterwards.

Usage:
    py -X utf8 src/import_assets.py
    py -X utf8 src/import_assets.py --dry-run
    py -X utf8 src/import_assets.py --only title --all
"""
import argparse
import json
import os
import shutil
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import asset_io

CONFIG = asset_io.load_config()
PATHS = CONFIG.get('paths') or {}


def _path(key, default):
    return asset_io.resolve(PATHS.get(key) or default)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', default=None,
                    help='export folder (default: config paths.assets_work)')
    ap.add_argument('--out', default=None,
                    help='patch assets folder (default: config paths.assets_dir)')
    ap.add_argument('--only', default=None, help='substring filter on storage name')
    ap.add_argument('--all', action='store_true',
                    help='process every export, not only edited ones')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    work = os.path.abspath(args.work or _path('assets_work', 'assets_work'))
    out = os.path.abspath(args.out or _path('assets_dir', 'assets'))
    manifest_path = os.path.join(work, 'manifest.json')
    if not os.path.isfile(manifest_path):
        raise SystemExit('manifest not found: %s\nrun: langpatch images-export' % manifest_path)

    with open(manifest_path, encoding='utf-8') as f:
        manifest = json.load(f)
    assets = manifest.get('assets') or []
    if not assets:
        raise SystemExit('manifest is empty: %s' % manifest_path)

    os.makedirs(out, exist_ok=True)

    changed = unchanged = written = errors = 0
    for asset in assets:
        storage = asset['storage']
        if args.only and args.only.lower() not in storage.lower():
            continue
        path = os.path.join(work, asset['path'].replace('/', os.sep))
        if not os.path.exists(path):
            print('MISS %-44s %s' % (storage, asset['path']))
            errors += 1
            continue
        digest = asset_io.hash_path(path)
        override = asset.get('override')
        target = asset.get('target') or override
        if not target:
            print('WARN %-44s cannot be overridden (unknown storage name)' % storage)
            continue
        # Only the assets that were really edited are written.  An export
        # that still carries the original text is shipped unchanged by the
        # game, and the rebuilt PSB of an untouched export is not the same
        # picture (the text layers are placeholders).
        edited = digest != asset.get('sha256')
        if not (edited or args.all):
            # An export that was not edited is not shipped: the rebuilt PSB
            # of an untouched export is not the same picture (the text
            # layers are placeholders), and the game still has the original.
            stale = os.path.join(out, target.replace('/', os.sep))
            if os.path.isfile(stale) and not args.dry_run:
                os.remove(stale)
            unchanged += 1
            continue
        changed += 1

        dest = os.path.join(out, target.replace('/', os.sep))
        if args.dry_run:
            print('DRY  %-44s -> %s' % (storage, target))
            written += 1
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if asset['kind'] == 'image':
                shutil.copyfile(path, dest)
                size = os.path.getsize(dest)
            else:
                json_path = asset_io.find_json(path)
                if json_path is None:
                    raise RuntimeError('no .json in %s' % path)
                size = asset_io.build_psb(json_path, dest)
            written += 1
            print('OK   %-44s -> %-32s %8d bytes' % (storage, target, size))
        except Exception as exc:
            errors += 1
            print('ERR  %-44s %s: %s' % (storage, type(exc).__name__, exc))

    print('')
    print('%s: %d edited, %d unchanged, %d written, %d error(s)'
          % ('would import' if args.dry_run else 'imported',
             changed, unchanged, written, errors))
    if written and not args.dry_run:
        print('next: langpatch build')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
