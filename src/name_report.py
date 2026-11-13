"""Report naming coverage of ``filemap.json``.

Prints, per archive, how many files got a verified name and what kind of data
the unnamed remainder holds.  Used to decide which naming scheme to attack
next; it never modifies the map.

Usage:
    py -X utf8 src/name_report.py --filemap filemap.json --extracted extracted
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_all

MAGICS = (
    (b'\x89PNG', 'png'),
    (b'OggS', 'ogg'),
    (b'PSB\x00', 'psb'),
    (b'TLG', 'tlg'),
    (b'RIFF', 'wav'),
    (b'TJS', 'tjs'),
    (b'\xfe\xfe', 'simplecrypt'),
    (b'#2.00', 'sli'),
    (b'AJPM', 'ajpm'),
    (b'\x30\x26\xb2\x75', 'asf/wmv'),
    (b'OTTO', 'otf'),
    (b'\x00\x01\x00\x00', 'ttf'),
    (b'BM', 'bmp'),
    (b'\xff\xd8\xff', 'jpg'),
)


def magic_of(path):
    try:
        with open(path, 'rb') as handle:
            head = handle.read(16)
    except OSError:
        return 'missing'
    for signature, label in MAGICS:
        if head.startswith(signature):
            return label
    return head[:6].hex()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--filemap', default='filemap.json')
    ap.add_argument('--extracted', default='extracted')
    args = ap.parse_args()

    with open(args.filemap, encoding='utf-8') as handle:
        rows = json.load(handle)

    by_archive = collections.defaultdict(list)
    for row in rows:
        by_archive[row['archive']].append(row)

    for archive in sorted(by_archive):
        entries = by_archive[archive]
        named = [row for row in entries if row.get('name')]
        unnamed = [row for row in entries if not row.get('name')]
        kinds = collections.Counter()
        for row in unnamed:
            path = row.get('path')
            if path:
                path = os.path.join(args.extracted, path.replace('/', os.sep))
            else:
                path = os.path.join(args.extracted, row['archive'],
                                    row['dirhash'], row['filehash'])
            kinds[magic_of(path)] += 1
        print('%-8s named %4d / %4d   unnamed %4d  %s'
              % (archive, len(named), len(entries), len(unnamed),
                 dict(kinds.most_common())))
    dirs = {row['dirhash'] for row in rows if row.get('dir')}
    all_dirs = {row['dirhash'] for row in rows}
    print('directories named: %d/%d' % (len(dirs), len(all_dirs)))
    total = len(rows)
    named = sum(1 for row in rows if row.get('name'))
    print('files named: %d/%d' % (named, total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
