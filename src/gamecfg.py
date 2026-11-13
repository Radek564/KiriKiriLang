"""Locate the game and the executable that owns its archives.

The template must work for any title, so nothing may assume a particular
executable name.  ``detect_exe`` picks the game's main executable from the
folder that contains the script archive:

* explicit ``game_exe`` from ``config.json`` wins;
* an ``.exe`` whose name matches the folder / a ``data.xp3`` archive wins;
* otherwise the largest ``.exe`` that is not a known helper.

The executable matters because KrKrz stores the per-game archive key material
inside it (``src/hxv4.py`` reads it at runtime); the filename hash suffix
(``xp3hnp``) is a KrKrz constant.
"""
import os

import configfile

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HELPER_EXES = {
    'bootstrap.exe', 'uninstall.exe', 'setup.exe', 'install.exe',
    'update.exe', 'launcher.exe', 'crashreport.exe', 'config.exe',
}


def load_config(path=None):
    return configfile.load(path)


def _archives(directory):
    try:
        return [n for n in os.listdir(directory) if n.lower().endswith('.xp3')]
    except OSError:
        return []


def detect_exe(game_root, configured=None):
    """Return the path of the game executable for *game_root*.

    ``configured`` is used verbatim when it names an existing file; it may be
    just a filename (resolved against *game_root*) or an absolute path.
    """
    if configured:
        candidate = configured
        if not os.path.isabs(candidate):
            candidate = os.path.join(game_root, candidate)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    if not game_root or not os.path.isdir(game_root):
        return None

    archives = [a.lower() for a in _archives(game_root)]
    folder = os.path.basename(os.path.normpath(game_root)).lower()
    exes = []
    for name in os.listdir(game_root):
        if not name.lower().endswith('.exe'):
            continue
        path = os.path.join(game_root, name)
        if not os.path.isfile(path):
            continue
        exes.append((name, path, os.path.getsize(path)))

    def score(item):
        name, _path, size = item
        stem = os.path.splitext(name)[0].lower()
        points = 0
        if name.lower() in HELPER_EXES:
            points -= 1000
        if stem in folder or folder.startswith(stem):
            points += 100
        if any(a.startswith(stem) or stem in a for a in archives):
            points += 50
        # KrKrz games usually have one large engine exe next to the archives.
        points += min(size // 1048576, 20)
        return points

    if not exes:
        return None
    exes.sort(key=score, reverse=True)
    return os.path.abspath(exes[0][1])


def multilang_seed_path(game_root, configured=None):
    """Path used by ``hxv4`` to read the per-game archive key material."""
    return detect_exe(game_root, configured)
