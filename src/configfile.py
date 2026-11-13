"""Resolve which ``config.json`` profile the patch tools use.

A single template checkout can host several games at once.  The default config
stays in ``config.json`` next to this template; a profile such as
``configs/<game>.json`` only needs to list what differs.

Selection order:

* ``TRANSLATION_PATCH_CONFIG`` - an absolute path, or a path relative to the
  template folder (set by ``langpatch.py --config`` for every child
  process);
* ``config.json`` in the template folder.

A profile is merged *on top of* the base config, dictionary by dictionary, so
shared blocks (``language``, ``loader``, ``hashing``) do not have to be copied.
Relative paths in ``paths`` still resolve against the template folder, which
keeps ``loader/`` and ``src/`` shared; a profile that must not disturb another
game's build simply points at its own folder names.
"""
import json
import os

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(TEMPLATE, 'config.json')
ENV = 'TRANSLATION_PATCH_CONFIG'


def config_path():
    """Absolute path of the active config file."""
    raw = os.environ.get(ENV)
    if not raw:
        return BASE
    return raw if os.path.isabs(raw) else os.path.join(TEMPLATE, raw)


def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _merge(out[key], value) if key in out else value
        return out
    return override


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def load(path=None):
    """Load the active config, merging a profile over the base file.

    ``config.json`` is the language-neutral base.  It may name a
    ``default_profile`` (a checkout-wide default game); running the tools
    without ``--config`` then resolves to that profile, while every profile
    still overrides the same shared defaults.  A default profile that does not
    exist (yet) is ignored, so the base file alone is enough to start.
    """
    path = path or config_path()
    base = _read(BASE) if os.path.isfile(BASE) else {}
    default_profile = base.pop('default_profile', None)
    active = os.path.abspath(path)
    if active == os.path.abspath(BASE) and default_profile:
        candidate = (default_profile if os.path.isabs(default_profile)
                     else os.path.join(TEMPLATE, default_profile))
        if os.path.isfile(candidate):
            path = candidate
            active = os.path.abspath(path)
    data = _read(path)
    if active != os.path.abspath(BASE) and base:
        data = _merge(base, data)
    data['_config_path'] = active
    return data


def describe():
    """One-line description used by ``status``."""
    active = config_path()
    base = _read(BASE) if os.path.isfile(BASE) else {}
    default_profile = base.get('default_profile')
    if os.path.abspath(active) == os.path.abspath(BASE) and default_profile:
        candidate = (default_profile if os.path.isabs(default_profile)
                     else os.path.join(TEMPLATE, default_profile))
        if os.path.isfile(candidate):
            active = candidate
    if os.path.abspath(active) == os.path.abspath(BASE):
        return 'config.json'
    return os.path.relpath(active, TEMPLATE)
