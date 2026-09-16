# -*- coding: utf-8 -*-
"""
Render each PDF page WITHOUT its text layer -> clean background images
(cream paper, golden frame, ornaments, table shading, diagram boxes...).
"""
import os
import sys
import pymupdf as fitz


def make_backgrounds(pdf_path, out_dir, dpi=150):
    os.makedirs(out_dir, exist_ok=True)
    src = fitz.open(pdf_path)
    for pno in range(len(src)):
        page = src[pno]
        # redact all text spans, keep graphics & images
        d = page.get_text("dict")
        for b in d["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    if not s["text"].strip():
                        continue
                    r = fitz.Rect(s["bbox"]) + (-1, -1, 1, 1)
                    page.add_redact_annot(r)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
        out = os.path.join(out_dir, f"bg_{pno+1:03d}.jpg")
        pix.pil_save(out, format="JPEG", quality=88)
    print("done", out_dir, len(src))


if __name__ == "__main__":
    make_backgrounds(sys.argv[1], sys.argv[2],
                     dpi=int(sys.argv[3]) if len(sys.argv) > 3 else 150)
