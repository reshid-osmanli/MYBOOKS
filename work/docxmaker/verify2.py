# -*- coding: utf-8 -*-
"""
Engine-independent DOCX renderer/verifier.

Parses the built .docx (anchored pictures + textboxes), lays every text run
out with HarfBuzz (same shaper Word uses) and rasterizes glyphs with
FreeType, then diffs the result page-by-page against the original PDF.

This bypasses Aspose entirely, so KFGQPC glyph chains can be checked
faithfully.  Fonts that the docx references but that are not present
(Arial / Times / Symbol / Wingdings) use DejaVu Sans as outline source,
which is good enough for positional verification.

    python verify2.py <docx> <origpdf> --pages 3,16,31 --out /tmp/v2
"""
import argparse
import io
import json
import math
import os
import re
import sys
import zipfile

import numpy as np
from PIL import Image
import freetype as FT
import uharfbuzz as hb
import fitz  # pymupdf, only for rendering the ORIGINAL pdf pages

EMU_PER_PT = 12700.0
NS = {
    "w":   "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp":  "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a":   "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "r":   "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

BASE_FRACTION = 0.8
LINE_MULT = 1.30

HERE = os.path.dirname(os.path.abspath(__file__))
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# docx family+bold -> outline file actually used for rasterization
def family_file(family, bold, fonts_dir):
    f = (family or "").lower()
    if "kfgqpc" in f:
        return os.path.join(fonts_dir, "UthmanicHafs1_Ver09.ttf")
    if "sakkal" in f:
        return os.path.join(fonts_dir,
                            "majallab.ttf" if bold else "majalla.ttf")
    if bold:
        return DEJAVU_B
    return DEJAVU

SYM_UNI = {"F0B7": "\u2022", "F0A8": "\u25C6", "F06F": "\u25CB",
           "F0D6": "\u221A", "F020": " "}
JC_FLIP = {"right": "left", "left": "right", "center": "center",
           "both": "right", None: "right"}


class GlyphLayer(object):
    """One FT face + hb font pair at a fixed pixel size; caches renders."""
    def __init__(self, path, px):
        self.ft_face = FT.Face(path)
        self.ft_face.set_pixel_sizes(0, max(1, int(round(px))))
        data = open(path, "rb").read()
        hb_face = hb.Face(data)
        hb_font = hb.Font(hb_face)
        u = hb_face.upem
        hb_font.scale = (u, u)
        self.upem = u
        self.px = px
        # hb font for shaping at exact pixel size (positions then in px*? ->
        # use upem units and divide by upem, multiply by px)
        buf = hb.Buffer()
        self._buf = buf
        self.hb_font = hb_font
        self._bmp_cache = {}

    def shape(self, text, rtl=True):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        buf.direction = "rtl" if rtl else "ltr"
        buf.script = "arab" if rtl else "latn"
        buf.language = "ar" if rtl else "en"
        hb.shape(self.hb_font, buf, {})
        infos = buf.glyph_infos
        poss = buf.glyph_positions
        out = []
        for inf, pos in zip(infos, poss):
            out.append((inf.codepoint,
                        pos.x_offset / self.upem * self.px,
                        pos.y_offset / self.upem * self.px,
                        pos.x_advance / self.upem * self.px))
        return out

    def bitmap(self, gid):
        bm = self._bmp_cache.get(gid)
        if bm is None:
            self.ft_face.load_glyph(gid, FT.FT_LOAD_DEFAULT)
            slot = self.ft_face.glyph
            slot.render(FT.FT_RENDER_MODE_NORMAL)
            bmp = slot.bitmap
            arr = np.frombuffer(bytes(bmp.buffer), dtype=np.uint8)
            if arr.size:
                arr = arr.reshape(bmp.rows, bmp.pitch)[:, :bmp.width].copy()
            else:
                arr = np.zeros((0, 0), dtype=np.uint8)
            bm = (arr, slot.bitmap_left, slot.bitmap_top)
            self._bmp_cache[gid] = bm
        return bm


_LAYER_CACHE = {}
def get_layer(path, px):
    key = (path, int(round(px)))
    if key not in _LAYER_CACHE:
        _LAYER_CACHE[key] = GlyphLayer(path, px)
    return _LAYER_CACHE[key]


def blend(canvas, bmp, left, top, rgb):
    """alpha-blend 8bit glyph bitmap onto RGB canvas at (left,top)."""
    h, w = bmp.shape
    if h == 0 or w == 0:
        return
    x0, y0 = int(round(left)), int(round(top))
    x1, y1 = x0 + w, y0 + h
    H, W = canvas.shape[:2]
    if x1 <= 0 or y1 <= 0 or x0 >= W or y0 >= H:
        return
    bx0, by0 = max(0, -x0), max(0, -y0)
    bx1 = bx0 + min(x1, W) - max(x0, 0)
    by1 = by0 + min(y1, H) - max(y0, 0)
    sx0, sy0 = max(x0, 0), max(y0, 0)
    reg = canvas[sy0:sy0 + (by1 - by0), sx0:sx0 + (bx1 - bx0)]
    a = bmp[by0:by1, bx0:bx1].astype(np.float32)[:, :, None] / 255.0
    c = np.array(rgb, dtype=np.float32)[None, None, :]
    reg[:] = (reg.astype(np.float32) * (1.0 - a) + c * a).astype(np.uint8)


_RE_AR = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
def is_arabic(s):
    return bool(_RE_AR.search(s))

# bidi mirroring for RTL runs (Word's pipeline does this before cmap)
_MIRROR = {0x0028: 0x0029, 0x0029: 0x0028, 0x005B: 0x005D, 0x005D: 0x005B,
           0x007B: 0x007D, 0x007D: 0x007B, 0x003C: 0x003E, 0x003E: 0x003C,
           0x00AB: 0x00BB, 0x00BB: 0x00AB, 0x2039: 0x203A, 0x203A: 0x2039,
           0x2264: 0x2265, 0x2265: 0x2264}
_MIRROR_TAB = str.maketrans(_MIRROR)
def mirror_rtl(s):
    return s.translate(_MIRROR_TAB)


def parse_docx(path):
    z = zipfile.ZipFile(path)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(z.read("word/document.xml"))
    rels = {}
    rroot = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    for r in rroot:
        rels[r.get("Id")] = r.get("Target")
    media = {n: z.read(n) for n in z.namelist() if n.startswith("word/media/")}
    sect = root.find(".//w:sectPr", NS)
    sz = sect.find("w:pgSz", NS)
    page_w = int(sz.get(qn_hack("w", "w"))) / 20.0
    page_h = int(sz.get(qn_hack("w", "h"))) / 20.0
    return root, rels, media, page_w, page_h


def qn_hack(ns, tag):
    return "{%s}%s" % (NS[ns], tag)


def anchors_of_paragraph(p):
    out = []
    for d in p.findall(".//w:drawing", NS):
        for anch in d.findall(".//wp:anchor", NS):
            # position
            ph = anch.find("wp:positionH", NS)
            pv = anch.find("wp:positionV", NS)
            x = int(ph.find("wp:posOffset", NS).text) / EMU_PER_PT
            y = int(pv.find("wp:posOffset", NS).text) / EMU_PER_PT
            ext = anch.find("wp:extent", NS)
            cx = int(ext.get("cx")) / EMU_PER_PT
            cy = int(ext.get("cy")) / EMU_PER_PT
            pic = anch.find(".//pic:pic", NS)
            if pic is not None:
                blip = pic.find(".//a:blip", NS)
                rid = blip.get(qn_hack("r", "embed"))
                out.append(("pic", x, y, cx, cy, rid))
                continue
            txbx = anch.find(".//wps:txbx", NS)
            if txbx is not None:
                bodypr = anch.find(".//wps:bodyPr", NS)
                lIns = int(bodypr.get("lIns", "91440")) / EMU_PER_PT
                rIns = int(bodypr.get("rIns", "91440")) / EMU_PER_PT
                paras = []
                for wp in txbx.findall(".//w:p", NS):
                    ppr = wp.find("w:pPr", NS)
                    jc = None
                    if ppr is not None:
                        jc_el = ppr.find("w:jc", NS)
                        if jc_el is not None:
                            jc = jc_el.get(qn_hack("w", "val"))
                    runs = []
                    for wr in wp.findall("w:r", NS):
                        rpr = wr.find("w:rPr", NS)
                        fam, bold, size, color = None, False, 10.0, "000000"
                        if rpr is not None:
                            rf = rpr.find("w:rFonts", NS)
                            if rf is not None:
                                fam = rf.get(qn_hack("w", "ascii"))
                            b = rpr.find("w:b", NS)
                            if b is not None:
                                bold = b.get(qn_hack("w", "val"), "1") not in ("0", "false")
                            sz = rpr.find("w:sz", NS)
                            if sz is not None:
                                size = int(sz.get(qn_hack("w", "val"))) / 2.0
                            col = rpr.find("w:color", NS)
                            if col is not None:
                                color = col.get(qn_hack("w", "val"))
                        t = wr.find("w:t", NS)
                        if t is not None and t.text:
                            runs.append(("t", t.text, fam, bold, size, color))
                        sym = wr.find("w:sym", NS)
                        if sym is not None:
                            ch = sym.get(qn_hack("w", "char"))
                            sf = sym.get(qn_hack("w", "font"))
                            txt = SYM_UNI.get(ch, "?")
                            runs.append(("t", txt, sf + " SYM", bold, size, color))
                    paras.append({"jc": jc, "runs": runs})
                out.append(("txbx", x, y, cx, cy, lIns, rIns, paras))
    return out


def p_has_break(p):
    ppr = p.find("w:pPr", NS)
    if ppr is None:
        return False
    return ppr.find("w:pageBreakBefore", NS) is not None


def render_pages(docx_path, scale, fonts_dir):
    root, rels, media, page_w, page_h = parse_docx(docx_path)
    body = root.find(".//w:body", NS)
    pages = []
    cur = []
    for p in body.findall("w:p", NS):
        if p_has_break(p):
            pages.append(cur)
            cur = []
        anchp = anchors_of_paragraph(p)
        cur.extend(anchp)
    pages.append(cur)
    out = []
    S = scale
    W = int(round(page_w * S))
    H = int(round(page_h * S))
    for anchs in pages:
        canvas = np.full((H, W, 3), 255, dtype=np.uint8)
        for a in anchs:
            if a[0] == "pic":
                _, x, y, cx, cy, rid = a
                target = rels.get(rid, "")
                data = media.get("word/" + target)
                if not data:
                    continue
                im = Image.open(io.BytesIO(data)).convert("RGB")
                pw, ph = max(1, int(round(cx * S))), max(1, int(round(cy * S)))
                im = im.resize((pw, ph), Image.LANCZOS)
                arr = np.array(im)
                px, py = int(round(x * S)), int(round(y * S))
                canvas[py:py + ph, px:px + pw] = arr[:H - py, :W - px]
            else:
                _, x, y, cx, cy, lIns, rIns, paras = a
                for para in paras:
                    runs = para["runs"]
                    if not runs:
                        continue
                    jc_vis = JC_FLIP.get(para["jc"], "right")
                    # shape every run
                    shaped = []
                    total = 0.0
                    for _, txt, fam, bold, size, color in runs:
                        fm = fam or ""
                        use_file = family_file(fm.replace(" SYM", ""), bold,
                                               fonts_dir)
                        lay = get_layer(use_file, size * S)
                        rtl = is_arabic(txt)
                        lay_txt = mirror_rtl(txt) if rtl else txt
                        gl = lay.shape(lay_txt, rtl=rtl)
                        wsum = sum(g[3] for g in gl)
                        shaped.append((lay, gl, rtl, wsum,
                                       size, color))
                        total += wsum
                    xL = (x + lIns) * S
                    xR = (x + cx - rIns) * S
                    if jc_vis == "right":
                        pen = xR - total
                    elif jc_vis == "left":
                        pen = xL
                    else:
                        pen = xL + ((xR - xL) - total) / 2.0
                    # baseline of first line
                    size0 = runs[0][4]
                    base = (y + BASE_FRACTION * size0 * LINE_MULT) * S
                    # HarfBuzz emits glyphs in VISUAL order even for RTL:
                    # draw left->right, pen advances by +x_advance.
                    # XML runs are in logical order, so for RTL lines walk
                    # the runs in reverse to get visual left-to-right flow.
                    for lay, gl, rtl, wsum, size, color in reversed(shaped):
                        rgb = tuple(int(color[i:i + 2], 16)
                                    for i in (0, 2, 4))
                        for gid, xo, yo, adv in gl:
                            arr, gl_l, gl_t = lay.bitmap(gid)
                            blend(canvas, arr, pen + xo + gl_l,
                                  base - yo - gl_t, rgb)
                            pen += adv
        out.append(Image.fromarray(canvas))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("docx")
    ap.add_argument("origpdf")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--out", default="/tmp/v2")
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--fonts-dir",
                    default=os.path.join(HERE, "..", "fonts"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    pages = [int(t) for t in args.pages.split(",")]
    renders = render_pages(args.docx, args.scale,
                           os.path.abspath(args.fonts_dir))
    orig = fitz.open(args.origpdf)
    zoom = fitz.Matrix(args.scale, args.scale)
    for pn in pages:
        gen = renders[pn - 1]
        gp = os.path.join(args.out, "gen_%03d.png" % pn)
        gen.save(gp)
        opix = orig.load_page(pn - 1).get_pixmap(matrix=zoom)
        op = Image.frombytes("RGB", (opix.width, opix.height),
                             opix.samples)
        op = op.resize(gen.size) if op.size != gen.size else op
        o = np.array(op).astype(np.int16)
        g = np.array(gen).astype(np.int16)
        d = np.abs(o - g).mean()
        big = int((np.abs(o - g).max(axis=2) > 40).sum())
        side = Image.new("RGB", (gen.width * 2 + 8, gen.height), "white")
        side.paste(op, (0, 0))
        side.paste(gen, (gen.width + 8, 0))
        sp = os.path.join(args.out, "side_%03d.png" % pn)
        side.save(sp)
        print("page %3d: meanDiff=%6.2f  pixels>40=%6d (%.1f%%)  -> %s"
              % (pn, d, big, 100.0 * big / (gen.width * gen.height), sp))


if __name__ == "__main__":
    main()
