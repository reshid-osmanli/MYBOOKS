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

SUFFIXES = (".initlow.tall", ".initlow", ".init.tall", ".medi.tall",
            ".fina.tall", ".fina.short", ".medi.narrow", ".fina.narrow",
            ".init", ".medi", ".fina", ".isol", ".alt1", ".alt2", ".alt3",
            ".alt4", ".alt5", ".alt6", ".alt7", ".alt8", ".alt9", ".alt10")


def name_to_chars(name):
    """glyph name -> logical char string (base forms, ligatures expanded)."""
    if name is None:
        return None
    base = name
    # strip contextual suffixes iteratively (e.g. uni0644.init.alt1)
    changed = True
    while changed:
        changed = False
        for suf in SUFFIXES:
            if base.endswith(suf):
                base = base[: -len(suf)]
                changed = True
                break
    for suf in (".initlow.tall", ".fina.short", ".medi.narrow"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break

    # uniXXXX / uniXXXXXXXX / uniXXXXYYYY...  (also presentation forms)
    m = re.match(r"^uni([0-9A-Fa-f]{4,})?$", base)
    if m and m.group(1):
        nums = m.group(1)
        parts = []
        # group by 4 hex digits; if a codepoint is >FFFF it may appear as >4
        for j in range(0, len(nums), 4):
            part = nums[j:j+4]
            if len(part) == 4:
                parts.append(chr(int(part, 16)))
        if parts:
            return unicodedata.normalize("NFKC", "".join(parts)).replace(" ", "")

    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", base)
    if m:
        return unicodedata.normalize("NFKC", chr(int(m.group(1), 16))).replace(" ", "")

    # ligature glyphs: liga.0628.init / liga.0647.medi etc.
    m = re.match(r"^liga\.([0-9A-Fa-f]{4,})?$", base)
    if m and m.group(1):
        nums = m.group(1)
        parts = [chr(int(nums[j:j+4], 16)) for j in range(0, len(nums), 4) if len(nums[j:j+4]) == 4]
        if parts:
            return unicodedata.normalize("NFKC", "".join(parts)).replace(" ", "")

    # ligature names like afii57415_afii57416 -> component chars via full cmap
    return None


def build_fixed_map(embedded_ttf, full_path, used_cids, name_fallback_path=None):
    """Return dict cid -> chars for CIDs whose outline matched the full font.

    ``name_fallback_path`` is used for font families whose "full" copy stores
    contextual glyphs under generic names (glyph00632, ...). For those glyphs
    we inherit the human-readable name from another copy of the same family at
    the same glyph id (e.g. majalla.ttf v5.01 for majallab.ttf v6.81).
    """
    full = TTFont(full_path)
    idx = build_outline_index(full, range(full["maxp"].numGlyphs))
    names = full.getGlyphOrder()
    # full font cmap for afii-name resolution
    cmap = full.getBestCmap() or {}
    name2cps = {}
    for cp, nm in cmap.items():
        name2cps.setdefault(nm, []).append(cp)

    fallback_names = None
    if name_fallback_path:
        fallback = TTFont(name_fallback_path)
        fallback_names = fallback.getGlyphOrder()

    def resolve_name(nm, gid=None):
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
        # generic name such as glyph00632: borrow the name from a sibling
        # version of the same font at the same glyph id.
        if fallback_names and gid is not None and gid < len(fallback_names):
            return name_to_chars(fallback_names[gid])
        return None

    fixed = {}
    stats = {"matched": 0, "no_outline": 0, "no_match": 0}

    # Direct glyph-id based fallback: modern Word subsets often keep the full
    # glyph id in a generic name (glyph02221 ...) even though the outline does
    # not match the available full copy.  The logical name can then be read
    # from the human-readable copy of the same family at the same glyph index.
    if fallback_names:
        for cid in used_cids:
            if cid < len(fallback_names):
                ch = name_to_chars(fallback_names[cid])
                if ch:
                    fixed[cid] = ch

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
        ch = resolve_name(names[gid], gid)
        if ch:
            if fixed.get(cid) != ch:
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
