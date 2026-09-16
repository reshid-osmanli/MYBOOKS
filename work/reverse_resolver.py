# -*- coding: utf-8 -*-
"""
Reverse glyph resolver: given an embedded (subset) PDF font whose glyphs may
lack cmap entries (GSUB-produced ligatures/contextual forms), resolve each
glyph id to its Unicode character sequence using:

 1. the embedded font's own cmap + glyph names (NFKC-decomposed),
 2. a FULL copy of the same font: reverse all GSUB lookups (single, ligature,
    extension-unwrapped) to find which character sequences produce a given
    output glyph. Chained-contextual rules reference lookups in the same
    LookupList, which are all processed directly, so chains need no special
    handling for the *output* mapping.
 3. outline comparison against the full font's glyphs (same glyph ids),
 4. outline comparison against other glyphs of the embedded font itself
    (duplicate glyphs).

Returns: dict gid -> set of candidate char strings.
"""
import re
import unicodedata
from collections import defaultdict

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen


def _norm(ch):
    n = unicodedata.normalize("NFKC", ch)
    return n if n else ch


class ReverseResolver:
    def __init__(self, embedded, full=None):
        self.emb = embedded
        self.full = full if full is not None else embedded
        self.gid2chars = defaultdict(set)   # gid -> set of char strings
        self._rev_single = defaultdict(set)  # out_gid -> set of in_gid
        self._rev_lig = defaultdict(set)     # out_gid -> set of tuples(input gids)
        self._build()

    # ---------- basic maps ----------
    def _add_cmap(self, font):
        cmap = font.getBestCmap() or {}
        for cp, name in cmap.items():
            try:
                gid = font.getGlyphID(name)
            except Exception:
                continue
            ch = chr(cp)
            self.gid2chars[gid].add(ch)
            self.gid2chars[gid].add(_norm(ch))

    def _add_names(self, font):
        for gid, name in enumerate(font.getGlyphOrder()):
            m = re.match(r"^uni([0-9A-Fa-f]{4,6})$", name)
            if m:
                ch = chr(int(m.group(1), 16))
                self.gid2chars[gid].add(ch)
                self.gid2chars[gid].add(_norm(ch))

    # ---------- GSUB reversal ----------
    def _unwrap_lookup(self, lk):
        out = []
        for st in lk.SubTable:
            if getattr(st, "LookupType", None) == 7:  # ExtensionSubst
                out.append(st.ExtSubTable)
            else:
                out.append(st)
        return out

    def _build(self):
        self._add_cmap(self.emb)
        if self.full is not self.emb:
            self._add_cmap(self.full)
        self._add_names(self.emb)
        if self.full is not self.emb:
            self._add_names(self.full)
        if "GSUB" in self.full:
            self._reverse_gsub(self.full)
        self._fixpoint()

    def _reverse_gsub(self, font):
        gsub = font["GSUB"].table
        lookups = gsub.LookupList.Lookup
        for lk in lookups:
            for st in self._unwrap_lookup(lk):
                lt = getattr(st, "LookupType", None)
                if lt == 1:
                    for inp, out in st.mapping.items():
                        self._rev_single[out].add(inp)
                elif lt == 4:
                    for first, ligs in st.ligatures.items():
                        for lig in ligs:
                            comps = (first,) + tuple(lig.Component)
                            self._rev_lig[lig.LigGlyph].add(comps)

    def _fixpoint(self):
        changed = True
        it = 0
        while changed and it < 12:
            changed = False
            it += 1
            # ligatures: all components resolved -> output = concat
            for out, comp_sets in self._rev_lig.items():
                for comps in comp_sets:
                    parts = [self._best(g) for g in comps]
                    if all(p is not None for p in parts):
                        c = "".join(parts)
                        if c and c not in self.gid2chars[out]:
                            self.gid2chars[out].add(c)
                            changed = True
            # singles: out takes char of in
            for out, ins in self._rev_single.items():
                for g in ins:
                    for c in list(self.gid2chars.get(g, ())):
                        if c not in self.gid2chars[out]:
                            self.gid2chars[out].add(c)
                            changed = True

    def _best(self, gid):
        cands = self.gid2chars.get(gid)
        if not cands:
            return None
        for c in cands:
            if len(c) == 1 and 0x0600 <= ord(c) <= 0x06FF:
                return c
        return sorted(cands, key=len)[0]

    def candidates(self, gid):
        return self.gid2chars.get(gid, set())

    def best(self, gid):
        cands = self.gid2chars.get(gid)
        if not cands:
            return None
        for c in cands:
            if len(c) == 1 and 0x0600 <= ord(c) <= 0x06FF:
                return c
        return sorted(cands, key=lambda c: (len(c) != 1, len(c)))[0]

    def resolve_with_outlines(self, gid, full_outline_idx=None, emb_outline_idx=None, prec=2):
        """Try outline matching for an unresolved gid. Returns list of char strings."""
        cands = set()
        sig = outline_sig(self.emb, gid, prec)
        if sig is None:
            return cands
        # match against full font glyphs (same design, same ids hopefully)
        if full_outline_idx is not None and self.full is not self.emb:
            for g in full_outline_idx.get(sig, ()):
                for c in self.gid2chars.get(g, ()):
                    cands.add(c)
        # match against embedded font glyphs (duplicates)
        if emb_outline_idx is not None:
            for g in emb_outline_idx.get(sig, ()):
                if g != gid:
                    for c in self.gid2chars.get(g, ()):
                        cands.add(c)
        return cands


# ---------------- outline comparison ----------------

def outline_sig(font, gid, prec=2):
    try:
        order = font.getGlyphOrder()
        gs = font.getGlyphSet()
        pen = DecomposingRecordingPen(gs)
        gs[order[gid]].draw(pen)
        ops = pen.value
        if not ops:
            return None
        pts = []
        for op in ops:
            for pt in op[1]:
                pts.append((round(pt[0], prec), round(pt[1], prec)))
        return tuple(pts)
    except Exception:
        return None


def build_outline_index(font, gids, prec=2):
    idx = defaultdict(list)
    for gid in gids:
        sig = outline_sig(font, gid, prec)
        if sig is not None:
            idx[sig].append(gid)
    return idx
