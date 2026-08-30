# -*- coding: utf-8 -*-
"""Build corrected CID->char maps for the embedded SakkalMajalla fonts by
matching glyph outlines against the full majalla.ttf / majallab.ttf fonts.

MS Word wrote partially WRONG ToUnicode CMaps for the regular SakkalMajalla
(e.g. lam encoded as ث, kaf as ،). The outlines of the embedded glyphs are
correct, so outline matching against the full fonts (whose glyph names encode
the character) yields the true characters.
"""
import re
import unicodedata

from fontTools.ttLib import TTFont

from reverse_resolver import outline_sig, build_outline_index

SUFFIXES = (".init", ".medi", ".fina", ".isol", ".alt1", ".alt2", ".alt3",
            ".alt4", ".alt5", ".alt6", ".alt7", ".alt8", ".alt9", ".alt10")


def name_to_chars(name):
    """glyph name -> logical char string (base forms, ligatures expanded)."""
    if name is None:
        return None
    base = name
    for suf in SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    m = re.match(r"^uni([0-9A-Fa-f]{4,6})([0-9A-Fa-f]{4,6})?$", base)
    if m:
        parts = []
        for g in m.groups():
            if g:
                parts.append(chr(int(g, 16)))
        if parts:
            return unicodedata.normalize("NFKC", "".join(parts)).replace(" ", "")
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", base)
    if m:
        return unicodedata.normalize("NFKC", chr(int(m.group(1), 16))).replace(" ", "")
    # ligature names like afii57415_afii57416 -> component chars via full cmap
    return None


def build_fixed_map(embedded_ttf, full_path, used_cids):
    """Return dict cid -> chars for CIDs whose outline matched the full font."""
    full = TTFont(full_path)
    idx = build_outline_index(full, range(full["maxp"].numGlyphs))
    names = full.getGlyphOrder()
    # full font cmap for afii-name resolution
    cmap = full.getBestCmap() or {}
    name2cps = {}
    for cp, nm in cmap.items():
        name2cps.setdefault(nm, []).append(cp)

    def resolve_name(nm):
        ch = name_to_chars(nm)
        if ch:
            return ch
        if "_" in nm:
            parts = []
            for comp in nm.split("_"):
                cps = name2cps.get(comp, [])
                if cps:
                    parts.append(chr(min(cps)))
            if parts and len(parts) == nm.count("_") + 1:
                return "".join(parts)
        return None

    fixed = {}
    stats = {"matched": 0, "no_outline": 0, "no_match": 0}
    for cid in used_cids:
        sig = outline_sig(embedded_ttf, cid)
        if sig is None:
            stats["no_outline"] += 1
            continue
        matches = idx.get(sig)
        if not matches:
            stats["no_match"] += 1
            continue
        gid = matches[0]
        ch = resolve_name(names[gid])
        if ch:
            fixed[cid] = ch
            stats["matched"] += 1
        else:
            stats["no_match"] += 1
    return fixed, stats


def inherit_from_siblings(embedded_ttf, used_cids, fixed, tu=None):
    """For cids not in `fixed`, if their outline is identical (within the
    embedded font) to a fixed cid, inherit the fixed character. Handles
    Word's duplicate/contextual glyph variants."""
    order = embedded_ttf.getGlyphOrder()
    # index fixed cids by signature
    sig2char = {}
    for cid, ch in fixed.items():
        sig = outline_sig(embedded_ttf, cid)
        if sig is not None:
            sig2char.setdefault(sig, ch)
    inherited = {}
    for cid in used_cids:
        if cid in fixed:
            continue
        sig = outline_sig(embedded_ttf, cid)
        if sig is None:
            continue
        ch = sig2char.get(sig)
        if ch:
            inherited[cid] = ch
    return inherited
