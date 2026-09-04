# -*- coding: utf-8 -*-
"""
Extract precise per-line/per-cluster layout from the "رتل مصمم" PDFs.

Reuses the decoding machinery of decode_driver_v2/pdf_decoder/font_fixer so
that the *text* is the corrected Unicode text, but keeps the geometry of
every cluster (position, font, size) and attaches colors sampled from
PyMuPDF's structured text dict.

Output JSON:
{
  "src": path, "page_w": pt, "page_h": pt,
  "pages": [ { "page": 1, "lines": [ {
       "y": baseline_y_topdown, "x0": .., "x1": ..,
       "segments": [ { "x0","x1","y", "clusters": [
            {"x0","x1","text","font","size","color","bold"} ] } ] } ] } ]
}
"""
import io
import json
import os
import re
import sys
import string
from collections import defaultdict

import pymupdf as fitz

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
sys.path.insert(0, WORK)

from pdf_decoder import FontMap, PageDecoder  # noqa: E402
from decode_driver_v2 import (  # noqa: E402
    document_fonts, used_cids, build_overrides, MARK_Y_TOL, LINE_Y_TOL,
    _cluster_text, _ltr_class, _mark_overlaid, ltr_runs,
    normalize_arabic_text, _emit_cluster, LTR_CORE, LTR_PUNCT, MIRRORED_PUNCT,
)

FONT_TAG = {
    "sakkalmajallabold": "majallab",
    "sakkalmajalla": "majalla",
    "kfgqpcuthmanicscripthafs": "quran",
    "kfgqpchafsuthmanicscript": "quran",
    "kfgqpchafsuthmanicscript-regula": "quran",
    "arialmt": "arial",
    "arial-boldmt": "arialb",
    "timesnewromanpsmt": "tnr",
    "timesnewromanps-boldmt": "tnrb",
    "aptos": "aptos",
    "aptos,bold": "aptosb",
    "aptos,italic": "aptosi",
    "calibri": "calibri",
    "symbolmt": "symbol",
    "wingdings-regular": "wingdings",
    "couriernewpsmt": "courier",
    "cambriamath": "cambria",
    "frutigerltarabic-45light": "frutiger",
}


def font_tag(name):
    n = name.split("+")[-1].lower().replace(" ", "").replace("-", "")
    if n in FONT_TAG:
        return FONT_TAG[n]
    n2 = name.split("+")[-1].lower().replace(" ", "")
    if n2 in FONT_TAG:
        return FONT_TAG[n2]
    for k, v in FONT_TAG.items():
        if k.replace("-", "") in n:
            return v
    return "other:" + name.split("+")[-1]


# --------------------------------------------------------------------------
# structured version of decode_driver_v2.cluster_and_order
# --------------------------------------------------------------------------

def collect_lines(items, ref2fam):
    """Return structured lines. Each line:
    {y, x0, x1, clusters:[{x0,x1,y_med,text,font,size}], }
    clusters in *logical* emission order with geometry attached.
    """
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
        line_info.append((ys[len(ys) // 2], bs))

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
        tokens.sort(key=lambda t: t[0])

        merged = []
        for tok in tokens:
            if tok[1] == "s" and merged and merged[-1][1] == "s":
                continue
            merged.append(tok)

        # ---- emit clusters in logical order, remembering geometry ----
        # out_clusters = list of [cl, space_before]
        out_clusters = []
        pending_space = False
        for is_run, group in reversed(ltr_runs(merged)):
            for x, kind, cl in group:
                if kind == "s":
                    pending_space = True
                    continue
                text = normalize_arabic_text(_emit_cluster(cl))
                if not text:
                    continue
                out_clusters.append([cl, pending_space])
                pending_space = False

        line_clusters = []
        for cl, sp_before in out_clusters:
            base = cl[0]
            fam = ref2fam.get(base.font, "")
            xs = [g.x for g in cl]
            wx = [g.x + (g.width if g.width > 0 else 0) for g in cl]
            x0, x1 = min(xs), max(wx if wx else xs)
            ys = sorted(g.y for g in cl)
            line_clusters.append({
                "x0": round(x0, 2), "x1": round(x1, 2),
                "y": round(ys[len(ys) // 2], 2),
                "text": normalize_arabic_text(_emit_cluster(cl)),
                "sp": bool(sp_before),
                "font": font_tag(fam),
                "family": fam.split("+")[-1],
                "size": round(base.size, 2),
            })
        if not line_clusters:
            continue
        lines.append({
            "y": round(med, 2),
            "x0": round(min(c["x0"] for c in line_clusters), 2),
            "x1": round(max(c["x1"] for c in line_clusters), 2),
            "clusters": line_clusters,
        })
    lines.sort(key=lambda l: l["y"])
    return lines


# --------------------------------------------------------------------------
# segmentation: split each line's logical clusters into visual segments
# --------------------------------------------------------------------------

GAP_SPLIT = 6.0      # pt: horizontal gap that starts a new segment
Y_SPLIT = 2.4        # pt: baseline deviation (only together with a gap)


def segment_lines(lines):
    """Assign each cluster a segment; segments get exact geometry.

    A new segment starts only at a real horizontal separation; diacritic
    clusters raised inside a word (`دًا` and friends) stay inside their
    word's segment so that shaping still sees complete words.
    """
    for line in lines:
        cl_sorted = sorted(line["clusters"], key=lambda c: c["x0"])
        segs = []
        cur = None
        for c in cl_sorted:
            if cur is not None:
                gap = c["x0"] - cur["x1"]
                dy = abs(c["y"] - cur["y"])
                if gap > GAP_SPLIT or (gap > 1.2 and dy > Y_SPLIT):
                    cur = None
            if cur is None:
                cur = {"x0": c["x0"], "x1": c["x1"], "y": c["y"],
                       "clusters": []}
                segs.append(cur)
            cur["clusters"].append(c)
            cur["x0"] = min(cur["x0"], c["x0"])
            cur["x1"] = max(cur["x1"], c["x1"])
        # logical order of segments inside a line: right to left
        segs.sort(key=lambda s: -s["x0"])
        # within segment, clusters must be in logical (emission) order
        order = {id(c): i for i, c in enumerate(line["clusters"])}
        for s in segs:
            s["clusters"].sort(key=lambda c: order[id(c)])
            # segment dominant font/size/color come later
            s["size"] = max(c["size"] for c in s["clusters"])
            s["y"] = sorted(c["y"] for c in s["clusters"])[
                len(s["clusters"]) // 2]
        line["segments"] = segs
        del line["clusters"]
    return lines


# --------------------------------------------------------------------------

def attach_colors(doc, pages):
    """Sample colors from get_text('dict') spans by position."""
    for pno, page in enumerate(pages):
        spans = []
        for b in doc[pno].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    if not s["text"].strip():
                        continue
                    spans.append((fitz.Rect(s["bbox"]), s["color"], s["size"]))
        for line in page["lines"]:
            for seg in line["segments"]:
                for c in seg["clusters"]:
                    r = fitz.Rect(c["x0"] - 1, c["y"] - c["size"] - 2,
                                  c["x1"] + 1, c["y"] + 3)
                    best, best_a = None, 0.0
                    for sr, col, sz in spans:
                        inter = r & sr
                        a = inter.get_area() if not inter.is_empty else 0
                        if a > best_a:
                            best, best_a = (col, sz), a
                    col, sz = best if best else (0, c["size"])
                    c["color"] = "#{:06X}".format(col & 0xFFFFFF)
                    c["size"] = round(sz, 2) if best else c["size"]
    return pages


def fix_y_coordinates(doc, pages):
    """Convert decoder y (bottom-up PDF space) to top-down.

    The flip decision is taken ONCE per document from pooled evidence of
    all pages, so every page is transformed consistently.
    """
    import statistics
    diffs_direct, diffs_flip = [], []
    for pno, page in enumerate(pages):
        ph = doc[pno].rect.height
        origins = []
        for b in doc[pno].get_text("rawdict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    if "origin" in s and s.get("chars"):
                        origins.append((s["origin"], s["bbox"]))
        for line in page["lines"]:
            for seg in line["segments"]:
                cx = (seg["x0"] + seg["x1"]) / 2
                cy = seg["y"]
                best, bd = None, 1e9
                for (ox, oy), bb in origins:
                    d = abs(ox - cx)
                    if d < bd:
                        bd, best = d, oy
                if best is not None and bd < 60:
                    diffs_direct.append(abs(cy - best))
                    diffs_flip.append(abs(ph - cy - best))
    if diffs_direct:
        flip = statistics.median(diffs_flip) < statistics.median(diffs_direct)
    else:
        flip = True
    print("  global y-flip:", flip,
          "med_direct=%.2f med_flip=%.2f" % (
              statistics.median(diffs_direct) if diffs_direct else -1,
              statistics.median(diffs_flip) if diffs_flip else -1))
    for pno, page in enumerate(pages):
        ph = doc[pno].rect.height
        for line in page["lines"]:
            line["y_raw"] = line["y"]
            for seg in line["segments"]:
                if flip:
                    seg["y"] = round(ph - seg["y"], 2)
                    for c in seg["clusters"]:
                        c["y"] = round(ph - c["y"], 2)
        page["flip"] = flip
        lines_sorted = sorted(page["lines"],
                              key=lambda l: min(s["y"] for s in l["segments"]))
        page["lines"] = lines_sorted
    return pages


def extract(pdf_path, out_json):
    doc = fitz.open(pdf_path)
    font_info, ref2xref = document_fonts(doc)
    used = used_cids(doc, font_info, ref2xref)
    overrides = build_overrides(doc, font_info, used)
    fontmaps = {xref: FontMap(doc, xref, to_unicode=fi["tu"],
                              override=overrides.get(xref))
                for xref, fi in font_info.items()}

    # resource ref -> family basefont
    ref2fam = {}
    for pno in range(len(doc)):
        for xref, ext, ftype, name, ref, enc in doc.get_page_fonts(pno):
            ref2fam.setdefault(ref, name)

    pages = []
    for pno in range(len(doc)):
        dec = PageDecoder(doc, fontmaps)
        dec.decode_page(pno)
        # base font size: PageDecoder stores raw fs in text space; the page
        # CTM is identity for these Word exports, so size is already in pt.
        lines = collect_lines(dec.items, ref2fam)
        segment_lines(lines)
        pages.append({"page": pno + 1, "lines": lines})

    pages = fix_y_coordinates(doc, pages)
    pages = attach_colors(doc, pages)

    out = {"src": os.path.basename(pdf_path),
           "page_w": doc[0].rect.width, "page_h": doc[0].rect.height,
           "pages": pages}
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("wrote", out_json)


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
