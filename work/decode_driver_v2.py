# -*- coding: utf-8 -*-
"""
Driver: decode one of the "رتل مصمم" PDFs into clean, logically ordered text.

Usage
-----
    python work/decode_driver_v2.py "cmd/كتاب رتل مصمم ver3 - Part2.pdf" \
                                    work/part2_final.txt

What it does
------------
1. collects every Type0 font of the document and its ``ToUnicode`` CMap;
2. rebuilds a trustworthy ``gid -> text`` map for each of them with
   ``font_fixer`` (glyph names of the subset + a full copy of the same family
   + composite decomposition + outline matching);
3. walks the content streams (``pdf_decoder``) and records every glyph with
   its device position;
4. groups glyphs into clusters (base letter + its marks), clusters into lines
   and emits every line in *logical* right-to-left order -- keeping runs of
   Latin/digit characters in their own left-to-right order (see
   ``ltr_runs``);
5. normalises a handful of typographic variants that MS Word / Sakkal Majalla
   introduce (``ھ`` -> ``ه``, doubled ``يي`` produced by Word ligatures, ...).

Design notes
------------
* fonts are identified by **family name**, never by the xref number of a
  specific PDF, so the same driver works for every part of the book;
* the reference fonts live in ``work/fonts/`` (not in git) and are resolved
  relative to *this file*, so the driver can be run from any directory.
"""

import io
import os
import re
import sys
import string
from collections import defaultdict

import pymupdf as fitz
from fontTools.ttLib import TTFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_decoder import FontMap, PageDecoder          # noqa: E402
from cmap_parser import parse_cmap                    # noqa: E402
from font_fixer import build_fixed_map, inherit_from_siblings  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(HERE, "fonts")

MARK_Y_TOL = 16.0     # pt: a mark may sit this far above/below its base
LINE_Y_TOL = 3.5      # pt: baselines within this distance form one line


# --------------------------------------------------------------------------
# reference (full) copies of the embedded font families
# --------------------------------------------------------------------------

def fixture_for(name):
    """Path of the full copy of font family *name* (None if we have none)."""
    n = name.lower()
    if "sakkalmajalla" in n:
        if "bold" in n or n.endswith("b"):
            return os.path.join(FONTS_DIR, "majallab.ttf")
        return os.path.join(FONTS_DIR, "majalla.ttf")
    if ("kfgqpc" in n and "uthmanic" in n) or "uthmanic" in n:
        return os.path.join(FONTS_DIR, "UthmanicHafs1_Ver09.otf")
    return None


def get_tounicode(doc, xref):
    try:
        k = doc.xref_get_key(xref, "ToUnicode")
        if k[0] != "xref":
            return {}
        target = int(k[1].split()[0])
        data = doc.xref_stream(target)
        if not data:
            return {}
        return parse_cmap(data)
    except Exception:
        return {}


def document_fonts(doc):
    """Return ``{xref: {name, tu}}`` and ``{resource-name: xref}``."""
    info = {}
    ref2xref = {}
    for pno in range(len(doc)):
        for xref, ext, ftype, name, ref, enc in doc.get_page_fonts(pno):
            if ftype != "Type0":
                continue
            ref2xref.setdefault(ref, xref)
            if xref not in info:
                tu = get_tounicode(doc, xref)
                info[xref] = {"name": name, "tu": tu}
                print(f"font xref={xref} {name} ToUnicode entries={len(tu)}")
    return info, ref2xref


def used_cids(doc, font_info, ref2xref):
    """Every glyph id that is actually drawn, per font xref."""
    fontmaps1 = {xref: FontMap(doc, xref, to_unicode=fi["tu"])
                 for xref, fi in font_info.items()}
    used = defaultdict(set)
    for pno in range(len(doc)):
        dec = PageDecoder(doc, fontmaps1)
        dec.decode_page(pno)
        for it in dec.items:
            if it.cid is not None:
                used[it.font].add(it.cid)
    res_used = defaultdict(set)
    for pno in range(len(doc)):
        for xref, ext, ftype, name, ref, enc in doc.get_page_fonts(pno):
            if ftype == "Type0":
                res_used[ref] |= used.get(ref, set())
    out = {}
    for ref, cids in res_used.items():
        xref = ref2xref.get(ref)
        if xref is not None:
            out.setdefault(xref, set()).update(cids)
    return out


def build_overrides(doc, font_info, used):
    """Rebuild a correct ``gid -> text`` map for every font we can check."""
    overrides = {}
    for xref, fi in font_info.items():
        fullpath = fixture_for(fi["name"])
        if not fullpath:
            continue
        if not os.path.exists(fullpath):
            print(f"  !! reference font missing: {fullpath} "
                  f"(run work/fetch_fonts.py)")
            continue
        # Sakkal Majalla: the bold subset stores its contextual glyphs under
        # generic names, so borrow names from the regular copy of the family.
        fallback = None
        if "sakkalmajalla" in fi["name"].lower():
            candidate = os.path.join(FONTS_DIR, "majalla.ttf")
            if os.path.exists(candidate):
                fallback = candidate
        try:
            emb_buf = doc.extract_font(xref)[3]
            if not emb_buf:
                print(f"  !! font xref={xref} has no embedded data")
                continue
            emb_ttf = TTFont(io.BytesIO(emb_buf), lazy=False)
            cids = used.get(xref, set())
            fixed, stats = build_fixed_map(emb_ttf, fullpath, cids, fallback)
            inherited = inherit_from_siblings(emb_ttf, cids, fixed)
            print(f"font xref={xref} {fi['name']} resolved: {stats} "
                  f"inherited={len(inherited)} fallback={bool(fallback)}")
            fixed.update(inherited)
            overrides[xref] = fixed
        except Exception as e:
            print(f"  !! font xref={xref} fix failed: {e}")
    return overrides


def decode_doc(path, out_path, debug=0):
    doc = fitz.open(path)
    font_info, ref2xref = document_fonts(doc)
    used = used_cids(doc, font_info, ref2xref)
    overrides = build_overrides(doc, font_info, used)

    fontmaps = {xref: FontMap(doc, xref, to_unicode=fi["tu"],
                              override=overrides.get(xref))
                for xref, fi in font_info.items()}

    pages_items = {}
    all_unknown = defaultdict(int)
    for pno in range(len(doc)):
        dec = PageDecoder(doc, fontmaps)
        dec.decode_page(pno)
        pages_items[pno] = dec.items
        for k, v in dec.unknown.items():
            all_unknown[k] += v
    if debug:
        print("\nglyphs with no text at all:")
        for (xref, cid), cnt in sorted(all_unknown.items(),
                                       key=lambda kv: -kv[1])[:60]:
            print(f"  font {font_info.get(xref, {}).get('name')} "
                  f"(xref {xref}) cid {cid} (0x{cid:04X}) x{cnt}")

    out = []
    for pno in sorted(pages_items):
        out.append(f"\n===== PAGE {pno + 1} =====\n")
        items = pages_items[pno]
        if not items:
            continue
        for line in cluster_and_order(items):
            out.append(line + "\n")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    print("wrote", out_path)


# --------------------------------------------------------------------------
# text assembly
# --------------------------------------------------------------------------

# Characters that always keep their left-to-right order, even inside an
# Arabic (right-to-left) paragraph.  Word lays them out visually left to
# right, so reading them right to left - as the rest of the line - would
# mirror every number in the book ("120" -> "021").
LTR_CORE = set("0123456789٠١٢٣٤٥٦٧٨٩") | set(string.ascii_letters)
# punctuation drawn *inside* numbers (1.5, 12-14, 3/4, ...)
LTR_PUNCT = set(".,:;/\\-+%=*^$#@!?\"'`~|&_")
# paired punctuation: Word draws the mirrored shape, so the character that
# comes out of the font already carries the right direction -- never reorder.
MIRRORED_PUNCT = set("()[]{}<>«»")


def _cluster_text(cluster):
    return "".join(g.chars for g in cluster)


def _ltr_class(cluster):
    """'core' (digits/Latin), 'punct' (Latin punctuation) or None."""
    t = _cluster_text(cluster) if cluster else ""
    if not t:
        return None
    if any(c in MIRRORED_PUNCT for c in t):
        return None                 # never reorder paired punctuation
    if all(c in LTR_CORE for c in t):
        return "core"
    if all(c in LTR_CORE or c in LTR_PUNCT for c in t):
        return "punct"
    return None


def _mark_overlaid(tokens):
    """Flag tokens that were drawn *inside* the previous glyph.

    Word emits the closing bracket of ``[البلد: 17]`` with a tiny offset and
    without advancing the pen, so it lands on top of the first digit.
    """
    # An insertion is drawn while the pen is still (almost) where the previous
    # glyph started: Word emits the closing bracket of "[البلد: 17]" that way.
    # A normal kerning artefact moves the pen most of the advance, so compare
    # the offset against a fraction of the previous glyph's own width.
    flags = [False] * len(tokens)
    prev_x = prev_w = None
    for i, (x, kind, cl) in enumerate(tokens):
        width = cl[0].width if (kind == "c" and cl) else 0.0
        if prev_x is not None and x - prev_x < max(0.3, 0.25 * prev_w):
            flags[i] = True          # keep comparing against the same glyph
            continue
        prev_x, prev_w = x, width
    return flags


def ltr_runs(tokens):
    """Split *tokens* (ascending x) into segments; LTR runs stay LTR.

    Returns a list of ``(is_run, [tokens])`` in ascending-x order.  Tokens
    inside a run are re-ordered so that they read left to right, which is how
    Word laid them out; a token drawn inside the previous glyph (no advance)
    is moved to the end of the run, i.e. to the left, which is where the
    closing bracket of ``[البلد: 17]`` belongs in the logical text.
    """
    overlaid = _mark_overlaid(tokens)
    segments = []
    i, n = 0, len(tokens)
    while i < n:
        cls = _ltr_class(tokens[i][2]) if tokens[i][1] == "c" else None
        if cls != "core":
            segments.append((False, [tokens[i]]))
            i += 1
            continue
        j = i
        while j + 1 < n:
            nxt = tokens[j + 1]
            if overlaid[j + 1]:
                j += 1               # zero-advance insertion: keep in the run
                continue
            if nxt[1] != "c" or _ltr_class(nxt[2]) is None:
                break
            j += 1
        while j > i and _ltr_class(tokens[j][2]) != "core" and not overlaid[j]:
            j -= 1                   # drop trailing punctuation
        run = tokens[i:j + 1]
        flags = overlaid[i:j + 1]
        ordered = [t for t, f in zip(run, flags) if not f]
        extra = [t for t, f in zip(run, flags) if f]
        segments.append((True, ordered + extra))
        i = j + 1
    return segments


def normalize_arabic_text(text):
    """Unify typographic variants introduced by MS Word / Sakkal Majalla."""
    if not text:
        return text
    table = {
        "\u06be": "\u0647",   # ھ -> ه  (Urdu heh, used by Word for Arabic heh)
        "\u06cc": "\u064a",   # ی -> ي  (Persian yeh)
        "\u066e": "\u064a",   # ٮ -> ي  (dotless yeh)
        "\u06a9": "\u0643",   # ک -> ك  (Keheh)
        "\u06d5": "\u0647",   # ە -> ه
        "\u06d2": "\u064a",   # ے -> ي
    }
    text = "".join(table.get(ch, ch) for ch in text)
    # A handful of Word ligatures are encoded as an extra yeh.  Standard
    # Arabic never writes two yehs in a row without a shadda in between, so
    # the doubled letter is always an artefact and can be collapsed safely.
    while "\u064a\u064a" in text:
        text = text.replace("\u064a\u064a", "\u064a")
    return text


def cluster_and_order(items):
    """Assemble glyph items into logical Arabic lines.

    * letters (advance > 0, non-space) become clusters ordered right-to-left
    * marks (zero-advance glyphs) attach to the nearest letter of their line
    * space glyphs separate clusters; consecutive spaces collapse into one
    * runs of digits / Latin characters keep their left-to-right order
    * lines are grouped by baseline y and emitted top to bottom
    """
    letters = [it for it in items if not it.is_mark and it.chars.strip() != ""]
    marks = [it for it in items if it.is_mark and it.chars.strip() != ""]
    spaces = [it for it in items if it.chars.strip() == ""]

    # ---- 1. group letters into lines by baseline y -------------------
    base_lines = []
    for b in sorted(letters, key=lambda i: i.y):
        placed = False
        for line in base_lines:
            if abs(line[0] - b.y) <= LINE_Y_TOL:
                line[1].append(b)
                placed = True
                break
        if not placed:
            base_lines.append([b.y, [b]])
    base_lines.sort(key=lambda l: -l[0])
    line_info = []
    for y, bs in base_lines:
        ys = sorted(i.y for i in bs)
        line_info.append((ys[len(ys) // 2], bs))

    # ---- 2. attach marks to the nearest letter of their line ---------
    for m in marks:
        best, best_d = None, None
        for med, bs in line_info:
            for b in bs:
                dy, dx = abs(m.y - b.y), abs(m.x - b.x)
                if dy <= MARK_Y_TOL and dx <= 30.0:
                    d = dx + dy * 0.1
                    if best_d is None or d < best_d:
                        best_d, best = d, b
        if best is not None:
            best.attached_marks.append(m)
            m.attached = True
        else:
            m.attached = False

    # ---- 3. clusters + spaces -> tokens (x ascending) ----------------
    lines = []
    for med, bs in line_info:
        tokens = []
        for b in bs:
            cl = [b] + sorted(b.attached_marks, key=lambda g: g.x)
            tokens.append((b.x, "c", cl))
        for m in marks:
            if not m.attached and abs(m.y - med) <= MARK_Y_TOL:
                tokens.append((m.x, "c", [m]))
                m.attached = True
        letter_xs = [x for x, _, _ in tokens]
        for sp in spaces:
            if abs(sp.y - med) <= LINE_Y_TOL + 3.0:
                # a "space" sitting on top of a letter is a phantom: drop it
                if letter_xs and any(abs(xx - sp.x) <= 2.5 for xx in letter_xs):
                    continue
                tokens.append((sp.x, "s", [sp]))
        tokens.sort(key=lambda t: t[0])

        merged = []
        for tok in tokens:
            if tok[1] == "s" and merged and merged[-1][1] == "s":
                continue                      # collapse repeated spaces
            merged.append(tok)

        # ---- 4. emit right to left (LTR runs stay left to right) -----
        text = ""
        for is_run, group in reversed(ltr_runs(merged)):
            if not is_run:
                x, kind, cl = group[0]
                if kind == "s":
                    if text and not text.endswith(" "):
                        text += " "
                    continue
                text += _emit_cluster(cl)
            else:
                for x, kind, cl in group:
                    text += _emit_cluster(cl)
        text = re.sub(r"\s+([.,،؛:؟!])", r"\1", text)
        text = text.strip()
        text = normalize_arabic_text(text)
        lines.append((med, text))
    return [t for _, t in lines]


def _emit_cluster(cl):
    """Text of one cluster: the base glyph followed by its marks."""
    base, rest = cl[0], cl[1:]
    if not rest:
        return base.chars
    # a shadda always precedes the vowel that sits on top of it
    rest = sorted(rest, key=lambda g: (0 if "\u0651" in g.chars else 1, g.x))
    if len(base.chars) == 1:
        return base.chars + "".join(g.chars for g in rest)
    # A ligature glyph (the word الله, مج, لم, ...) carries several letters in
    # one outline.  Spread its characters over the glyph's advance -- the
    # rightmost character is the first one -- and merge the marks back in at
    # the position where they were actually drawn, so that "لله" + shadda +
    # fatha + kasra becomes "للَّهِ" and not "للهَِّ".
    n = len(base.chars)
    step = base.width / n
    pieces = [(base.x + base.width - step * (i + 0.5), ch)
              for i, ch in enumerate(base.chars)]
    pieces += [(g.x, g.chars) for g in rest]
    pieces.sort(key=lambda p: -p[0])
    return "".join(ch for _, ch in pieces)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    debug = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    decode_doc(sys.argv[1], sys.argv[2], debug=debug)
