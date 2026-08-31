# -*- coding: utf-8 -*-
"""Driver v2: decode a PDF into clean logical-order Arabic text.

Improvements over decode_driver.py:
  * identifies embedded Type0 fonts by *family name* instead of hard-coded
    xref numbers, so it works for every part of the book;
  * outline-fixes every SakkalMajalla and KFGQPC Uthmanic font with a full
    copy of the same font;
  * uses the embedded font's own cmap / glyph names as a fallback when there
    is no ToUnicode entry, and prints diagnostics.
"""
import sys, os, re, io, unicodedata
from collections import defaultdict

import pymupdf as fitz
from fontTools.ttLib import TTFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_decoder import FontMap, PageDecoder
from cmap_parser import parse_cmap
from font_fixer import build_fixed_map, inherit_from_siblings

MARK_Y_TOL = 16.0
LINE_Y_TOL = 3.5

# full copies of the same font families (downloaded once, in work/fonts/)
def fixture_for(name):
    n = name.lower()
    if "sakkalmajalla-bold" in n or "sakkalmajalla_bold" in n:
        return "work/fonts/majallab.ttf"
    if "sakkalmajalla" in n:
        return "work/fonts/majalla.ttf"
    if "kfgqpc" in n and "uthmanic" in n:
        return "work/fonts/UthmanicHafs1_Ver09.otf"
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
    """xref -> {name, tu}; also ref->xref."""
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
    fontmaps1 = {xref: FontMap(doc, xref, to_unicode=fi["tu"])
                 for xref, fi in font_info.items()}
    used = defaultdict(set)
    for pno in range(len(doc)):
        dec = PageDecoder(doc, fontmaps1)
        dec.decode_page(pno)
        for it in dec.items:
            if it.cid is not None:
                used[it.font].add(it.cid)
    # map ref (font resource name) -> font xref
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
    overrides = {}
    for xref, fi in font_info.items():
        fullpath = fixture_for(fi["name"])
        if not fullpath:
            continue
        if not os.path.exists(fullpath):
            print(f"  !! fixture missing {fullpath}")
            continue
        # For every SakkalMajalla copy, borrow human-readable glyph names from
        # the regular version at the same glyph id.  This fixes Word subsets
        # whose contextual glyphs are named glyphNNNN and whose outlines do
        # not match the available full copy.
        fallback = None
        if "sakkalmajalla" in fi["name"].lower():
            candidate = "work/fonts/majalla.ttf"
            if os.path.exists(candidate):
                fallback = candidate
        try:
            emb_buf = doc.extract_font(xref)[3]
            if not emb_buf:
                print(f"  !! font xref={xref} has no embedded data")
                continue
            emb_ttf = TTFont(io.BytesIO(emb_buf), lazy=True)
            cids = used.get(xref, set())
            fixed, stats = build_fixed_map(emb_ttf, fullpath, cids, fallback)
            inherited = inherit_from_siblings(emb_ttf, cids, fixed)
            print(f"font xref={xref} {fi['name']} outline fix: {stats} inherited={len(inherited)} fallback={bool(fallback)}")
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
        print("\nunknown CIDs across doc:")
        for (xref, cid), cnt in sorted(all_unknown.items(), key=lambda kv: -kv[1])[:60]:
            print(f"  font {font_info.get(xref,{}).get('name')} (xref {xref}) cid {cid} (0x{cid:04X}) x{cnt}")

    out = []
    for pno in sorted(pages_items):
        out.append(f"\n===== PAGE {pno+1} =====\n")
        items = pages_items[pno]
        if not items:
            continue
        lines = cluster_and_order(items)
        for line in lines:
            out.append(line + "\n")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    print("wrote", out_path)


def normalize_arabic_text(text):
    """Unify typographic variants introduced by MS Word / Sakkal Majalla."""
    if not text:
        return text
    table = {
        "\u06BE": "\u0647",  # ھ -> ه
        "\u06CC": "\u064A",  # ی -> ي
        "\u066E": "\u064A",  # ٮ (dotless yeh) -> ي
        "\u06A9": "\u0643",  # ک -> ك
        "\u06D5": "\u0647",  # ە -> ه
        "\u06D2": "\u064A",  # ے -> ي
    }
    out = []
    for ch in text:
        out.append(table.get(ch, ch))
    text = "".join(out)
    # The subset font encodes a handful of Word ligatures as an extra ي.
    # In standard Arabic a word never contains two consecutive ي without a
    # shadda, so collapse the artifact.
    while "يي" in text:
        text = text.replace("يي", "ي")
    text = text.replace("يءي", "ياء") if False else text
    return text


def cluster_and_order(items):
    """Same assembly as decode_driver.py, kept here so v1 stays untouched."""
    letters = [it for it in items if not it.is_mark and it.chars.strip() != ""]
    marks = [it for it in items if it.is_mark and it.chars.strip() != ""]
    spaces = [it for it in items if it.chars.strip() == ""]

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
        med = ys[len(ys) // 2]
        line_info.append((med, bs))

    for m in marks:
        best = None
        best_d = None
        for med, bs in line_info:
            for b in bs:
                dy = abs(m.y - b.y)
                dx = abs(m.x - b.x)
                if dy <= MARK_Y_TOL and dx <= 30.0:
                    d = dx + dy * 0.1
                    if best_d is None or d < best_d:
                        best_d = d
                        best = b
        if best is not None:
            best.attached_marks = getattr(best, "attached_marks", [])
            best.attached_marks.append(m)
            m._attached = True
        else:
            m._attached = False

    lines = []
    for med, bs in line_info:
        tokens = []
        for b in bs:
            cl = [b] + sorted(getattr(b, "attached_marks", []), key=lambda g: g.x)
            tokens.append((b.x, "c", cl))
        for m in marks:
            if not getattr(m, "_attached", False) and abs(m.y - med) <= MARK_Y_TOL:
                tokens.append((m.x, "c", [m]))
                m._attached = True
        letter_xs = [x for x, _, _ in tokens]
        for sp in spaces:
            if abs(sp.y - med) <= LINE_Y_TOL + 3.0:
                if letter_xs and any(abs(xx - sp.x) <= 2.5 for xx in letter_xs):
                    continue
                tokens.append((sp.x, "s", None))
        tokens.sort(key=lambda t: t[0])
        merged = []
        for x, kind, cl in tokens:
            if kind == "s":
                if merged and merged[-1][1] == "s":
                    continue
                merged.append((x, "s", None))
            else:
                merged.append((x, "c", cl))
        text = ""
        for x, kind, cl in reversed(merged):
            if kind == "s":
                if text and not text.endswith(" "):
                    text += " "
                continue
            base = cl[0]
            rest = cl[1:]
            rest = sorted(rest, key=lambda g: (0 if "\u0651" in g.chars else 1, g.x))
            text += base.chars + "".join(g.chars for g in rest)
        text = re.sub(r"\s+([.,،؛:؟!])", r"\1", text)
        text = text.strip()
        text = normalize_arabic_text(text)
        lines.append((med, text))
    return [t for _, t in lines]


if __name__ == "__main__":
    decode_doc(sys.argv[1], sys.argv[2], debug=1)
