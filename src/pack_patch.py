"""Pack a folder into a standard XP3 archive.

    py -X utf8 src/pack_patch.py <folder> [-o patch.xp3] [--scheme TITLE]

By default the archive is plain: the loader only needs a standard XP3 and
no Cxdec/Hxv4 encryption is involved.  For titles that read an encrypted patch,
``--scheme`` applies the matching filter to every body (adler32 of the
plaintext body is stored in ``adlr``, exactly like a game-made archive).

``<folder>.xp3`` is written next to the folder; ``-o`` optionally redirects it.
"""
import argparse
import os
import shutil
import sys

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TEMPLATE, 'src'))
import xp3_crypt
import xp3_pack


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder')
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--scheme', default=None,
                    help='archive filter title or unique fragment')
    ap.add_argument('--list-schemes', action='store_true',
                    help='print every known XP3 scheme title and exit')
    args = ap.parse_args()

    if args.list_schemes:
        for title in xp3_crypt.scheme_names():
            print(title)
        return 0

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        raise SystemExit('not a folder: %s' % folder)

    crypt = None
    if args.scheme:
        try:
            crypt = xp3_crypt.resolve_scheme(args.scheme)
        except KeyError as exc:
            raise SystemExit(str(exc))

    produced = folder.rstrip('\\/') + '.xp3'
    size = xp3_pack.pack_folder(folder, produced, crypt=crypt)

    if args.out:
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.move(produced, out)
        produced = out
    print('%s (%d bytes)' % (produced, size))


if __name__ == '__main__':
    sys.exit(main())
