# -*- coding: utf-8 -*-
"""Driver: decode a PDF into clean logical-order text."""
import sys, os, re
from collections import defaultdict
import fitz
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_decoder import FontMap, PageDecoder
from cmap_parser import parse_cmap

MARK_Y_TOL = 16.0  # pt: ... and this y distance (same line only)
LINE_Y_TOL = 3.5   # pt: line grouping tolerance


def get_tounicode(doc, xref):
    try:
        k = doc.xref_get_key(xref, "ToUnicode")
        if k[0] != 'xref':
            return {}
        target = int(k[1].split()[0])
        data = doc.xref_stream(target)
        if not data:
            return {}
        return parse_cmap(data)
    except Exception as e:
        return {}


def decode_doc(path, out_path, debug=0):
    doc = fitz.open(path)
    # ---- collect font info ----
    font_info = {}
    for pno in range(len(doc)):
        for xref, ext, ftype, name, ref, enc in doc.get_page_fonts(pno):
            if ftype == "Type0" and xref not in font_info:
                tu = get_tounicode(doc, xref)
                print(f"font xref={xref} {name} ToUnicode entries={len(tu)}")
                font_info[xref] = {"tu": tu, "name": name}

    # ---- pass 1: decode to collect used CIDs ----
    fontmaps1 = {xref: FontMap(doc, xref, to_unicode=fi["tu"])
                 for xref, fi in font_info.items()}
    used = defaultdict(set)
    for pno in range(len(doc)):
        dec = PageDecoder(doc, fontmaps1)
        dec.decode_page(pno)
        for it in dec.items:
            if it.cid is not None:
                used[it.font].add(it.cid)
    ref2xref = {}
    for pno in range(len(doc)):
        for xref, ext, ftype, name, ref, enc in doc.get_page_fonts(pno):
            if ftype == "Type0":
                ref2xref.setdefault(ref, xref)

    # ---- outline-based fixes for SakkalMajalla ----
    from font_fixer import build_fixed_map, inherit_from_siblings
    import io as _io
    from fontTools.ttLib import TTFont as _TTF
    overrides = {}
    FIXTURES = {
        12: "work/fonts/majallab.ttf",
        34: "work/fonts/majalla.ttf",
    }
    MANUAL = {
        12: {0x08AD: "ف"},
        34: {0x08AD: "في", 0x08AE: "قا", 0x08BC: "ص", 0x272: "ن", 0x74F: "ل"},
    }
    for xref, fullpath in FIXTURES.items():
        if xref not in font_info:
            continue
        try:
            emb_buf = doc.extract_font(xref)[3]
            emb_ttf = _TTF(_io.BytesIO(emb_buf), lazy=True)
            cids = set()
            for ref, xx in ref2xref.items():
                if xx == xref:
                    cids |= used.get(ref, set())
            fixed, stats = build_fixed_map(emb_ttf, fullpath, cids)
            inherited = inherit_from_siblings(emb_ttf, cids, fixed)
            print(f"font xref={xref} outline fix: {stats} inherited={len(inherited)}")
            fixed.update(inherited)
            fixed.update(MANUAL.get(xref, {}))
            overrides[xref] = fixed
        except Exception as e:
            print(f"font xref={xref} fix failed: {e}")

    # ---- pass 2: decode with fixes ----
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
        for (xref, cid), cnt in sorted(all_unknown.items(), key=lambda kv: -kv[1])[:40]:
            print(f"  font {xref} cid {cid} (0x{cid:04X}) x{cnt}")

    # ---- assemble: clusters -> lines -> RTL ----
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


def cluster_and_order(items):
    """Assemble glyph items into logical Arabic lines.

    - letters (advance>0, non-space) become clusters, ordered right-to-left
    - marks (zero-advance glyphs) attach to the nearest letter on their line
    - space glyphs separate clusters; consecutive spaces collapse to one
    - lines group by baseline y
    """
    letters = [it for it in items if not it.is_mark and it.chars.strip() != ""]
    marks = [it for it in items if it.is_mark and it.chars.strip() != ""]
    spaces = [it for it in items if it.chars.strip() == ""]

    # ---- 1. group letters into lines by baseline y ----
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

    # ---- 2. attach marks to nearest letter on the same line ----
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

    # ---- 3. clusters + spaces -> tokens, x ascending ----
    lines = []
    for med, bs in line_info:
        tokens = []
        for b in bs:
            cl = [b] + sorted(getattr(b, "attached_marks", []), key=lambda g: g.x)
            tokens.append((b.x, "c", cl))
        # unattached marks nearest to this line
        for m in marks:
            if not getattr(m, "_attached", False) and abs(m.y - med) <= MARK_Y_TOL:
                tokens.append((m.x, "c", [m]))
                m._attached = True
        # space tokens on this line (baseline within tol)
        letter_xs = [x for x, _, _ in tokens]
        for sp in spaces:
            if abs(sp.y - med) <= LINE_Y_TOL + 3.0:
                # phantom space: overlaps a letter's position -> drop
                if letter_xs and any(abs(xx - sp.x) <= 2.5 for xx in letter_xs):
                    continue
                tokens.append((sp.x, "s", None))
        tokens.sort(key=lambda t: t[0])
        # collapse consecutive space tokens
        merged = []
        for x, kind, cl in tokens:
            if kind == "s":
                if merged and merged[-1][1] == "s":
                    continue
                merged.append((x, "s", None))
            else:
                merged.append((x, "c", cl))
        # emit right-to-left
        text = ""
        for x, kind, cl in reversed(merged):
            if kind == "s":
                if text and not text.endswith(" "):
                    text += " "
                continue
            base = cl[0]
            rest = cl[1:]
            # shadda first, then other marks by x
            rest = sorted(rest, key=lambda g: (0 if "\u0651" in g.chars else 1, g.x))
            text += base.chars + "".join(g.chars for g in rest)
        # tidy: no space before punctuation
        text = re.sub(r"\s+([.,،؛:؟!])", r"\1", text)
        text = text.strip()
        lines.append((med, text))
    return [t for _, t in lines]


if __name__ == "__main__":
    decode_doc(sys.argv[1], sys.argv[2], debug=1)
