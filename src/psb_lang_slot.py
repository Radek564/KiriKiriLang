"""Add / refresh a language slot inside decompiled PSB scenario JSON.

A scenario PSB (`.scn`) stores dialogue like this:

    scenes[i].texts[j] = [
        speaker_jp,
        [ [speaker_en, text_en, len], [speaker_cn, text_cn, len] ],  # variants
        voice, delay, kagParams
    ]

Variant index == index in LanguageTags.  The base variant is index 0;
`root.languages` lists the *additional* languages (variant 1..n), and
`root.llmap` holds one entry per additional language.

This tool appends a copy of a chosen source variant at a target index so the
slot exists for every dialogue line, and keeps `languages` / `llmap` in sync.

Usage:
    py -X utf8 psb_lang_slot.py --json in.json --out out.json \
        --lang xx --index 2 [--marker TEXT] [--source-index 0]
"""
import argparse
import copy
import json
import os
import re

from psb_io import text_variants


def _best_source(seq, source_index):
    """The most useful variant to copy: the chosen one, else the first real one.

    Some lines simply have no text in that language (a ``None`` hole in the
    array) even
    when the surrounding PSB does.  Falling back to the first language that is
    really there keeps the new slot from staying empty.
    """
    def usable(value):
        if not value:
            return False
        if isinstance(value, dict):
            # A choice entry whose text is empty is a hole as well.
            return bool(value.get("text"))
        return True

    if 0 <= source_index < len(seq) and usable(seq[source_index]):
        return copy.deepcopy(seq[source_index])
    for value in seq:
        if usable(value):
            return copy.deepcopy(value)
    return None


def _grow_list(seq, index, source_index, fallback=None):
    """Ensure ``seq[index]`` exists and is not empty.

    Returns True when the list was changed.  A ``None`` source (common for
    choice-language arrays, where the base text lives in ``select.text``)
    falls back to a dict built from that text.
    """
    if not isinstance(seq, list):
        return False
    src = _best_source(seq, source_index)
    if src is None:
        src = copy.deepcopy(fallback)
    changed = False
    while len(seq) <= index:
        seq.append(copy.deepcopy(src))
        changed = True
    if not seq[index] and src is not None:
        # The array was already long enough but the slot is a hole (a line
        # without that language).  Fill it instead of leaving it missing.
        seq[index] = copy.deepcopy(src)
        changed = True
    return changed


def _select_slot(langarr, index, source_index, base_text):
    """Ensure a language dict exists at ``index`` for a choice entry."""
    if not isinstance(langarr, list):
        return False
    src = _best_source(langarr, source_index)
    if isinstance(src, list):
        src = None
    if src is None:
        src = {
            "text": base_text,
            "searchtext": base_text,
            "speechtext": base_text,
            "textlength": len(base_text),
        }
    changed = False
    while len(langarr) <= index:
        langarr.append(copy.deepcopy(src))
        changed = True
    if not langarr[index]:
        langarr[index] = copy.deepcopy(src)
        changed = True
    return changed


def add_lang_slot(data, lang, index, source_index=0, marker=None):
    scenes = data.get("scenes") or []
    variant_counts = set()
    touched = 0

    for scene in scenes:
        texts = scene.get("texts")
        if isinstance(texts, list):
            for entry in texts:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                variants = text_variants(entry)
                if not variants:
                    continue
                variant_counts.add(len(variants))
                if _grow_list(variants, index, source_index):
                    touched += 1
                if marker is not None:
                    # Marker is applied to the just-created slot only.
                    slot = variants[index]
                    if isinstance(slot, list) and len(slot) > 1:
                        slot[1] = marker

        # Localised scene title: ["Chapter", "章"] -> add the new language.
        title = scene.get("title")
        if isinstance(title, list) and len(title) > 1:
            if _grow_list(title, index, source_index):
                touched += 1

        # Choice text: selects[i].language = [None, {cn}, ...].
        for sel in scene.get("selects") or []:
            if not isinstance(sel, dict):
                continue
            langarr = sel.get("language")
            if isinstance(langarr, list):
                base = str(sel.get("text") or "")
                if _select_slot(langarr, index, source_index, base):
                    touched += 1

    old_variant_count = max(variant_counts) if variant_counts else index + 1

    # ``languages`` lists the *additional* variants (1..n).  Some titles (the
    # titles among them) have no such field: the engine's variant
    # order lives in ``multiLangLanguageTags`` in default.tjs, and the PSB
    # only stores the variant arrays.  Creating ``languages`` there would
    # change engine behaviour, so only maintain metadata that already exists.
    if index < 1:
        raise ValueError("language.index must be >= 1 (variant 0 is the base text)")
    langs = data.get("languages")
    had_languages = isinstance(langs, list)
    if had_languages:
        if len(langs) < index - 1:
            raise ValueError(
                "cannot add slot %d: PSB declares only %d extra language(s); "
                "set language.index to %d or add the missing tag to 'languages'"
                % (index, len(langs), len(langs) + 1))
        if len(langs) == index - 1:
            langs.append(lang)
        elif len(langs) == index and lang not in langs:
            langs[-1] = lang
        elif lang not in langs:
            raise ValueError(
                "slot %d is already used by %r; choose another language.index"
                % (index, langs[index - 1]))

        # ``llmap`` is optional bookkeeping used by the in-game converter.
        llmap = data.get("llmap")
        if isinstance(llmap, list) and len(llmap) < len(langs):
            base = data.get("name") or ""
            if base.lower().endswith(".txt"):
                base = base[:-4]
            if llmap:
                entry = copy.deepcopy(llmap[-1])
                name = entry.get("name")
                if isinstance(name, str):
                    new_name = re.sub(r"_[A-Za-z0-9]+\.txt$", "", name)
                else:
                    new_name = base
                entry["name"] = "%s_%s.txt" % (new_name, lang)
            else:
                entry = {"name": "%s_%s.txt" % (base, lang)}
            while len(llmap) < len(langs):
                llmap.append(copy.deepcopy(entry))

    return touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", required=True)
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--source-index", type=int, default=0)
    ap.add_argument("--marker", default=None)
    args = ap.parse_args()

    with open(args.json, encoding="utf-8") as f:
        data = json.load(f)

    n = add_lang_slot(data, args.lang, args.index, args.source_index, args.marker)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print("patched %d dialogue entries -> %s" % (n, args.out))


if __name__ == "__main__":
    main()
