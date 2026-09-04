# -*- coding: utf-8 -*-
"""
Verification harness:
  - render sample pages of a built docx through a REAL docx engine
    (Aspose.Words) with the book fonts registered explicitly,
  - render the same pages of the original PDF,
  - emit side-by-side comparisons and a pixel-diff report.

Aspose evaluation truncates very long documents, so the docx is re-built
containing only the sample pages (one per page) before rendering.
"""
import os
import sys
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = "/home/user/venv_docx/bin/python"

ENV = dict(os.environ)
ENV["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
ENV["LD_LIBRARY_PATH"] = "/home/user/openssl111/lib"
FONT_DIR = "/usr/local/share/fonts/book"

HEADER = r'''
import warnings; warnings.filterwarnings("ignore")
import aspose.words as aw
import sys

src, out_pdf = sys.argv[1], sys.argv[2]
fs = aw.fonts.FontSettings()
fs.set_fonts_folders([FONT_DIR_PLACEHOLDER, "/usr/share/fonts/truetype/dejavu"], False)
opts = aw.loading.LoadOptions()
opts.font_settings = fs
doc = aw.Document(src, opts)
doc.save(out_pdf)
'''

RENDER_CODE = HEADER.replace("FONT_DIR_PLACEHOLDER", repr(FONT_DIR))


def build_subset_docx(mapped_json, bg_dir, out_docx, keep_pages,
                      orig_pdf, dpi=150):
    """Build a small docx containing only keep_pages (re-numbered)."""
    lay = json.load(open(mapped_json, encoding="utf-8"))
    keep = set(keep_pages)
    lay["pages"] = [p for p in lay["pages"] if p["page"] in keep]
    lay["pages"].sort(key=lambda p: p["page"])
    tmp_json = out_docx + ".json"
    json.dump(lay, open(tmp_json, "w", encoding="utf-8"),
              ensure_ascii=False)
    sys.path.insert(0, HERE)
    import importlib
    import build_docx
    importlib.reload(build_docx)
    build_docx.VERIFY_COMPAT = True
    build_docx.build(tmp_json, bg_dir, out_docx, embed=False)
    os.remove(tmp_json)


def render_docx(docx_path, out_pdf):
    code = RENDER_CODE
    r = subprocess.run([VENV_PY, "-c", code, docx_path, out_pdf],
                       env=ENV, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("aspose render failed")


def page_pngs(pdf_path, page_indices, out_dir, tag, dpi=100, labels=None):
    """page_indices: 0-based indices into the pdf; `labels` (1-based display
    numbers) default to page_indices+1."""
    import pymupdf as fitz
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    outs = []
    for i, pno in enumerate(page_indices):
        lab = labels[i] if labels else pno + 1
        pix = doc[pno].get_pixmap(dpi=dpi)
        out = os.path.join(out_dir, f"{tag}_{lab:03d}.png")
        pix.save(out)
        outs.append(out)
    doc.close()
    return outs


def sxs(img_a, img_b, out, labels=None):
    from PIL import Image, ImageDraw
    a = Image.open(img_a).convert("RGB")
    b = Image.open(img_b).convert("RGB")
    h = max(a.height, b.height)
    w = a.width + b.width + 30
    canvas = Image.new("RGB", (w, h + 40), "white")
    canvas.paste(a, (0, 40))
    canvas.paste(b, (a.width + 30, 40))
    d = ImageDraw.Draw(canvas)
    if labels:
        d.text((10, 10), labels[0], fill="black")
        d.text((a.width + 40, 10), labels[1], fill="black")
    canvas.save(out)


def diff_metric(img_a, img_b):
    from PIL import Image, ImageChops
    a = Image.open(img_a).convert("L").resize((620, 877))
    b = Image.open(img_b).convert("L").resize((620, 877))
    import math
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    total = sum(hist)
    sq = sum(i * i * c for i, c in enumerate(hist))
    mean = sum(i * c for i, c in enumerate(hist)) / total
    rms = math.sqrt(sq / total)
    frac = sum(c for i, c in enumerate(hist) if i > 40) / total
    return mean, rms, frac


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapped", required=True)
    ap.add_argument("--bg", required=True)
    ap.add_argument("--orig", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--pages", required=True, help="1-based page list")
    ap.add_argument("--out", default="/tmp/verify")
    args = ap.parse_args()

    pages = [int(x) for x in args.pages.split(",")]
    os.makedirs(args.out, exist_ok=True)
    sub_docx = os.path.join(args.out, f"sub_{args.tag}.docx")
    sub_pdf = os.path.join(args.out, f"sub_{args.tag}.pdf")
    build_subset_docx(args.mapped, args.bg, sub_docx, pages, args.orig)
    render_docx(sub_docx, sub_pdf)
    gen = page_pngs(sub_pdf, list(range(len(pages))), args.out, "gen",
                    dpi=100, labels=pages)
    orig = page_pngs(args.orig, [p - 1 for p in pages], args.out, "orig",
                     dpi=100, labels=pages)
    report = []
    for pno, (g, o) in zip(pages, zip(gen, orig)):
        side = os.path.join(args.out, f"side_{args.tag}_{pno:03d}.png")
        sxs(o, g, side, labels=("PDF الأصلي", "Word المعاد بناؤه"))
        mean, rms, frac = diff_metric(o, g)
        report.append((pno, mean, rms, frac))
        print(f"page {pno:3d}: meanDiff={mean:6.2f} rms={rms:6.2f} "
              f"pixels>40={frac*100:5.1f}%  -> {side}")


if __name__ == "__main__":
    main()
