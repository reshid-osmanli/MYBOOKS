# -*- coding: utf-8 -*-
"""
font_fixer.py -- resolve the glyph ids of a subsetted PDF font to real text.

Why this module exists
----------------------
The PDFs are written by MS Word with Type0 / Identity-H (CID) fonts.  The
content stream only stores *glyph ids*, so the text can only be recovered if
we can map every glyph id back to characters.  Word embeds a ``ToUnicode``
CMap next to each font, but it is only partially trustworthy:

* for **SakkalMajalla** (the body font) Word's ToUnicode is plain wrong for a
  number of glyphs (``lam`` coded as ``thaa``, ``kaf`` as a comma, ...);
* for the **KFGQPC Uthmanic** (Qur'an) fonts most of the *contextual* glyphs
  and ligatures are missing from ToUnicode or mapped to the wrong mark;
* the *bracket* glyphs are mirrored (see below), so their glyph name and their
  ToUnicode entry intentionally disagree.

The outlines of the embedded glyphs, however, are correct.  This module
rebuilds a reliable ``gid -> text`` map by combining, in this order:

1. the glyph **name** of the embedded subset (``uni0644``, ``ligature...``);
2. a full copy of the *same* font family, read at the **same glyph id**
   (glyph names such as ``afii57416.init`` / ``uni0671.zz04`` resolve to the
   base character once the contextual suffix is stripped);
3. the **composite structure** of the glyph (a stacked mark cluster such as
   ``shadda + fatha`` is a TrueType composite of two named glyphs);
4. an **outline match** against the full copy of the font (this is what the
   first version of the project did, and it is still the main source for the
   two SakkalMajalla faces);
5. the **ToUnicode** CMap written by Word, used as a last resort -- it is the
   only source that knows what the multi-character ligatures
   (``Allah`` -> ``للّ``, ``Bism`` -> ``بِسۡمِ``, ...) actually stand for, and
   it is the only source for mirrored punctuation.
"""

import os
import re
import unicodedata
from collections import defaultdict

from fontTools.ttLib import TTFont

from reverse_resolver import outline_sig, build_outline_index


# --------------------------------------------------------------------------
# glyph names
# --------------------------------------------------------------------------

#: names that carry no ``uniXXXX`` pattern but still denote a known character
BASIC_NAMES = {
    "space": " ",
    "nonmarkingreturn": " ",
    "nbspace": " ",
    "period": ".", "comma": ",", "colon": ":", "semicolon": ";",
    "hyphen": "-", "endash": "–", "emdash": "—",
    "slash": "/", "backslash": "\\", "bar": "|", "underscore": "_",
    "question": "?", "exclam": "!", "percent": "%", "plus": "+",
    "equal": "=", "asterisk": "*", "numbersign": "#", "ampersand": "&",
    "at": "@", "dollar": "$", "asciicircum": "^", "asciitilde": "~",
    "grave": "`", "quotesingle": "'", "quotedbl": '"',
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

#: Multi-character ligatures whose text cannot be read from a glyph name.
#: They *are* in the PDF's ToUnicode CMap, but Word split the words differently
#: from one Part of the book to the next ("للّ" here, "لله" there), which is
#: inconsistent -- the ligature itself is always the plain sequence below and
#: the harakat are separate glyphs that follow it.
LIGATURE_TEXT = {
    "Allah": "\u0644\u0644\u0647",        # لله  (word of majesty)
}

#: glyph names of *bidi mirrored* punctuation.  Word draws the mirrored shape
#: but keeps the logical character, so the glyph name and the text disagree:
#: a "{" typed in an Arabic paragraph is drawn with the ``}``-looking glyph.
#: These glyphs are deliberately NOT resolved here -- the PDF's own ToUnicode
#: CMap is the only source that knows which of the pair was typed.
MIRRORED_CHARS = set("()[]{}<>«»")

MIRRORED_NAMES = {
    "parenleft", "parenright", "bracketleft", "bracketright",
    "braceleft", "braceright", "less", "greater",
    "angleleft", "angleright", "guilsinglleft", "guilsinglright",
}


#: second half of a compound glyph name that is a *feature tag* rather than a
#: component: ``afii57450.zz03_zz21``, ``afii57443.init_calt``, ...
TAG_RE = re.compile(r"^(?:zz\d*|calt|init|medi|fina|isol|alt\d*|liga|rlig|"
                    r"swsh|ss\d+|salt|ccmp|locl|kern|mkmk|curs|tnum|onum)$",
                    re.IGNORECASE)


def strip_context_suffix(name):
    """``uni0644.init.alt1`` -> ``uni0644``; ``afii57416.zz03`` -> ``afii57416``.

    Font vendors (and Word's subsetter) add contextual suffixes to mark the
    positional forms.  They never change the *character* a glyph stands for,
    so dropping everything from the first dot is safe -- with the single
    exception of ``liga.XXXX`` names, which are handled by ``name_to_chars``.
    """
    if "." not in name:
        return name
    return name.split(".", 1)[0]


def _chars_from_hex(hexes):
    try:
        out = "".join(chr(int(h, 16)) for h in hexes)
    except (ValueError, OverflowError):
        return None
    norm = unicodedata.normalize("NFKC", out)
    return (norm or out).replace("\u200b", "")


def _not_mirrored(chars):
    """Mirrored punctuation is drawn with the *other* glyph of the pair."""
    if chars and all(c in MIRRORED_CHARS for c in chars):
        return None
    return chars


def name_to_chars(name):
    """Best-effort ``glyph name -> text`` conversion (no external data)."""
    if not name:
        return None
    if name.startswith("."):          # .notdef, .null, .notdef.14 ...
        return None
    if strip_context_suffix(name) in MIRRORED_NAMES:
        return None                   # see MIRRORED_NAMES
    if name in BASIC_NAMES:
        return BASIC_NAMES[name]

    # ligature glyphs: liga.0628.init -> ب   (handled before suffix stripping)
    m = re.match(r"^liga\.([0-9A-Fa-f]{4,})(?:\..*)?$", name)
    if m:
        return _not_mirrored(_chars_from_hex(
            [m.group(1)[i:i + 4] for i in range(0, len(m.group(1)), 4)]))

    base = strip_context_suffix(name)

    m = re.match(r"^uni([0-9A-Fa-f]{4,})$", base)
    if m:
        return _not_mirrored(_chars_from_hex(
            [m.group(1)[i:i + 4] for i in range(0, len(m.group(1)), 4)]))
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", base)
    if m:
        return _not_mirrored(_chars_from_hex([m.group(1)]))
    if base in BASIC_NAMES:
        return BASIC_NAMES[base]
    return None


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

class GlyphResolver:
    """Resolve the glyph ids of one embedded (subset) font to text."""

    def __init__(self, embedded_ttf, reference_path=None,
                 name_fallback_path=None, verify_outlines=True):
        self.emb = embedded_ttf
        self.order = embedded_ttf.getGlyphOrder()
        self.glyf = embedded_ttf["glyf"] if "glyf" in embedded_ttf else None
        self.verify_outlines = verify_outlines

        self.ref = None
        self.ref_order = None
        self.ref_gid2char = {}
        self.name2cps = defaultdict(list)
        self._ref_index = None
        if reference_path and os.path.exists(reference_path):
            try:
                self.ref = TTFont(reference_path, lazy=True)
                self.ref_order = self.ref.getGlyphOrder()
                cmap = self.ref.getBestCmap() or {}
                for cp, nm in cmap.items():
                    self.name2cps[nm].append(cp)
                    try:
                        self.ref_gid2char[self.ref.getGlyphID(nm)] = chr(cp)
                    except Exception:
                        pass
            except Exception:
                self.ref = None

        self.fallback_names = None
        if name_fallback_path and os.path.exists(name_fallback_path):
            try:
                self.fallback_names = TTFont(
                    name_fallback_path, lazy=True).getGlyphOrder()
            except Exception:
                self.fallback_names = None

        self._memo = {}
        self.stats = defaultdict(int)

    # ---------------- helpers ----------------

    def glyph_id(self, name):
        return self.order.index(name)

    def _ref_name_chars(self, name):
        """Resolve a *reference font* glyph name (afiiNNNNN / uniXXXX / ...)."""
        if not name or name.startswith("."):
            return None
        if strip_context_suffix(name) in MIRRORED_NAMES:
            return None            # drawn mirrored -> ToUnicode decides
        direct = name_to_chars(name)
        if direct:
            return direct
        if "_" in name:                      # lam.init + alef -> لأ
            parts = []
            for comp in name.split("_"):
                c = self._ref_name_chars(comp)
                if c:
                    parts.append(c)
                elif not TAG_RE.match(comp):
                    return None     # a real component we cannot read
            return _not_mirrored("".join(parts)) if parts else None
        for candidate in (name, strip_context_suffix(name)):
            cps = self.name2cps.get(candidate)
            if cps:
                return _not_mirrored(chr(min(cps)))
        return None

    def _ref_same_gid(self, gid):
        """Text of the reference-font glyph with the *same* glyph id."""
        if self.ref is None or gid >= len(self.ref_order):
            return None
        name = self.ref_order[gid]
        if strip_context_suffix(name) in MIRRORED_NAMES:
            return None            # drawn mirrored -> ToUnicode decides
        ch = self.ref_gid2char.get(gid)
        if ch:
            return _not_mirrored(ch)
        return self._ref_name_chars(name)

    def _outline_equal(self, gid):
        """True when the embedded glyph is pixel-identical to the reference."""
        if self.ref is None or gid >= len(self.ref_order):
            return False
        a = outline_sig(self.emb, gid)
        if a is None:
            return False
        return a == outline_sig(self.ref, gid)

    def _outline_match(self, gid):
        """Find the reference glyph with an identical outline (any glyph id)."""
        if self.ref is None:
            return None
        if self._ref_index is None:
            self._ref_index = build_outline_index(
                self.ref, range(len(self.ref_order)))
        sig = outline_sig(self.emb, gid)
        if sig is None:
            return None
        for match in self._ref_index.get(sig, ()):
            ch = _not_mirrored(self.ref_gid2char.get(match)) or \
                self._ref_name_chars(self.ref_order[match])
            if ch:
                return ch
        return None

    def _decompose(self, gid, depth):
        """Text of a TrueType composite glyph (base glyph + stacked marks)."""
        if self.glyf is None or gid >= len(self.order):
            return None
        try:
            glyph = self.glyf[self.order[gid]]
        except Exception:
            return None
        if glyph.numberOfContours <= 0 or not glyph.isComposite():
            return None
        parts = []
        for comp in glyph.getComponentNames(self.glyf):
            try:
                cgid = self.glyph_id(comp)
            except ValueError:
                return None
            ch = self.resolve(cgid, depth + 1)
            if not ch:
                return None
            parts.append(ch)
        return "".join(parts) if parts else None

    def _known_ligature(self, gid, own_name):
        """Text of a ligature we know by name (see ``LIGATURE_TEXT``)."""
        names = [own_name]
        if self.ref is not None and gid < len(self.ref_order):
            names.append(self.ref_order[gid])
        if self.fallback_names and gid < len(self.fallback_names):
            names.append(self.fallback_names[gid])
        for name in names:
            text = LIGATURE_TEXT.get(strip_context_suffix(name))
            if text:
                return text
        return None

    # ---------------- main entry point ----------------

    def resolve(self, gid, depth=0):
        if gid in self._memo:
            return self._memo[gid]
        self._memo[gid] = None               # cycle guard
        if depth > 6 or not (0 <= gid < len(self.order)):
            return None

        own = self.order[gid]
        steps = (
            ("name", lambda: name_to_chars(own)),
            # 2. a full copy of the same family, read at the same glyph id
            ("ref_gid", lambda: self._ref_same_gid(gid)
             if self.ref is not None and
             (not self.verify_outlines or self._outline_equal(gid))
             else None),
            # 3. the composite structure of the glyph (stacked marks)
            ("composite", lambda: self._decompose(gid, depth)),
            # 4. an identical outline anywhere in the reference font
            ("outline", lambda: self._outline_match(gid)
             if self.ref is not None else None),
            # 5. the same glyph id in a sibling version of the family
            ("sibling", lambda: name_to_chars(self.fallback_names[gid])
             if self.fallback_names and gid < len(self.fallback_names)
             else None),
            # 6. the *name* of the reference glyph: names are version
            #    independent, so this still works when the two copies of the
            #    family are not the same release (v0.13 vs v0.09) and their
            #    outlines therefore differ.
            ("ref_name", lambda: self._ref_name_chars(self.ref_order[gid])
             if self.ref is not None and gid < len(self.ref_order) else None),
            # 7. known multi-character ligatures (see LIGATURE_TEXT)
            ("ligature", lambda: self._known_ligature(gid, own)),
        )
        for source, step in steps:
            ch = step()
            if ch:
                self._memo[gid] = ch
                self.stats[source] += 1
                return ch

        self._memo[gid] = None
        return None

    def resolve_many(self, cids):
        return {cid: self.resolve(cid) for cid in cids}


# --------------------------------------------------------------------------
# driver-facing API (kept compatible with the first version of the project)
# --------------------------------------------------------------------------

def build_fixed_map(embedded_ttf, full_path, used_cids, name_fallback_path=None):
    """Return ``(gid -> text, stats)`` for every glyph id in *used_cids*.

    Glyph ids that cannot be resolved are *not* included: the caller then
    falls back to the PDF's own ToUnicode CMap, which is the only source for
    the multi-character ligatures (``Allah``, ``Bism``, ``laa03``, ...).
    """
    resolver = GlyphResolver(embedded_ttf, full_path, name_fallback_path)
    fixed = {}
    for cid in sorted(used_cids):
        ch = resolver.resolve(cid)
        if ch:
            fixed[cid] = ch
    stats = dict(resolver.stats)
    stats["unresolved"] = len(used_cids) - len(fixed)
    stats["used"] = len(used_cids)
    return fixed, stats


def inherit_from_siblings(embedded_ttf, used_cids, fixed, tu=None):
    """Give an unresolved glyph the character of an identical outline.

    Word ships several copies of the same shape (contextual variants,
    duplicate composites).  Once one of them is resolved the others can
    inherit its text, which avoids falling back to ToUnicode for them.
    """
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
