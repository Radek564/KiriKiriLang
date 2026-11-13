"""Language configuration shared by the export/import/build tools.

Nothing in the template may hard-code a language tag (``xx``) or language name
(``target``).  Everything is read from ``config.json``.  The example uses a
fictional target language::

    "language": {
        "tag":      "xx",          # variant tag written into PSB ``languages``
        "name":     "target",      # internal alias used by the engine tables
        "label":    "&Target",     # caption shown in the language menu
        "index":    2,             # variant slot inside scenario PSBs
        "variants": ["base", "xx"] # text variant order (optional)
    },
    "ui_tags": ["base", "xx"]      # interface languages the kit ships

``language.variants`` is the order of the language columns inside scenario
PSBs.  It is separate from ``ui_tags`` because a title can store more text
variants than the kit translates interface captions for: a title that can keep
dialogue in several languages, while the kit only needs the base INI and the
new target INI.  When ``variants`` is omitted it falls back to ``ui_tags``,
so older profiles keep working.

Variant 0 is the original text; the new language is appended at ``index``.
For a game with no multilingual support at all, set ``variants`` to
``["jp", "<tag>"]`` and ``index`` to 1.
"""
import os

import configfile

TEMPLATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path=None):
    return configfile.load(path)


CONFIG = load_config()
LANGUAGE = CONFIG.get('language') or {}
TAG = LANGUAGE.get('tag')
if not TAG:
    raise SystemExit('config.json: language.tag is required')
NAME = LANGUAGE.get('name') or TAG
LABEL = LANGUAGE.get('label') or TAG

# Text variant order inside scenario PSBs.  ``language.variants`` is
# authoritative; ``ui_tags`` is the older name for the same list.
VARIANTS = list(LANGUAGE.get('variants') or [])
UI_TAGS = list(CONFIG.get('ui_tags') or [])
if not VARIANTS:
    VARIANTS = list(UI_TAGS)
if not VARIANTS:
    VARIANTS = [t for t in ('en', 'cn') if t != TAG] + [TAG]
if TAG not in VARIANTS:
    VARIANTS.append(TAG)

# Interface languages whose captions the kit ships.  A game may have more text
# variants than translated interfaces (see ``language.variants`` above).
if not UI_TAGS:
    UI_TAGS = list(VARIANTS)
if TAG not in UI_TAGS:
    UI_TAGS.append(TAG)

INDEX = LANGUAGE.get('index')
if INDEX is None:
    INDEX = VARIANTS.index(TAG)
if not 0 <= INDEX < len(VARIANTS):
    raise SystemExit(
        'config.json: language.index %d is outside language.variants %r'
        % (INDEX, VARIANTS))
if VARIANTS[INDEX] != TAG:
    raise SystemExit(
        'config.json: language.index %d points at %r, not %r (variants: %r)'
        % (INDEX, VARIANTS[INDEX], TAG, VARIANTS))

# Languages already present in the base game (everything before the new slot).
REF_TAGS = VARIANTS[:INDEX]
if not REF_TAGS:
    REF_TAGS = [t for t in VARIANTS if t != TAG] or ['en']

# The reference the new slot is seeded from.  The best text the game already
# ships is used: the base language when it exists, otherwise the first slot
# (the original language).  ``base_tag`` in the profile overrides it.
BASE_TAG = (LANGUAGE.get('base_tag') or '').strip()
if BASE_TAG not in VARIANTS or BASE_TAG == TAG:
    BASE_TAG = REF_TAGS[0]
BASE_INDEX = VARIANTS.index(BASE_TAG)

# Excel column suffixes, e.g. text_<tag> / speaker_<tag> / title_<tag>.
TEXT_COL = 'text_%s' % TAG
SPEAKER_COL = 'speaker_%s' % TAG
TITLE_COL = 'title_%s' % TAG


def ref_column(kind, ref_tag):
    """Column name for a reference language, e.g. ``text_en``."""
    return '%s_%s' % (kind, ref_tag)


def variant_index(tag):
    """Index of *tag* in the text-variant list, or ``None`` when absent."""
    try:
        return VARIANTS.index(tag)
    except ValueError:
        return None


def multilang_enabled():
    """True when the config asks for an in-game language selector."""
    return bool((CONFIG.get('language') or {}).get('multilang', True))
