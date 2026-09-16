# -*- coding: utf-8 -*-
"""
run_ocr.py -- يفرّغ كل صفحات الكتاب إلى ملفات JSON وسيطة (صفحة لكل ملف).

    python run_ocr.py                      # كل الصفحات
    python run_ocr.py --pages 1-5          # صفحات محدّدة (ترقيم PDF يبدأ من 1)
    python run_ocr.py --force              # إعادة التفريغ حتى لو كان الملف موجودًا

الخرج: json/pNNN.json ، كل ملف يحوي كل كلمات الصفحة بمربّعاتها وثقتها لكل
وضع تقطيع (PSM 6 و PSM 3). هذه الوسائط هي مدخل مرحلة التجميع والتدقيق.
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ocr_engine import PSM_MODES, PageOCR, load_native, ocr_page, prepare, Upscaler  # noqa: E402

PDF = os.path.join(HERE, "..", "..", "new book",
                   "2_علم_التجويد_دكتور_يحى_الغوثانى_.pdf")
OUT = os.path.join(HERE, "json")

_UP = None


def _one(args):
    pno, pdf, out, force = args
    dest = os.path.join(out, f"p{pno + 1:03d}.json")
    if os.path.exists(dest) and not force:
        return pno, "skip", 0.0
    global _UP
    if _UP is None:
        _UP = Upscaler()
    t0 = time.time()
    doc = pymupdf.open(pdf)
    try:
        native = load_native(doc, pno)
        if native is None:
            return pno, "no-image", time.time() - t0
        ready = prepare(native, _UP)
        passes = {}
        for psm in PSM_MODES:
            passes[psm] = ocr_page(ready, psm)
    finally:
        doc.close()
    rec = PageOCR(pno + 1, native.shape, passes)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(rec.to_json(), fh, ensure_ascii=False)
    return pno, "ok", time.time() - t0


def parse_pages(spec, total):
    if not spec:
        return list(range(total))
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-")
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(chunk) - 1)
    return [p for p in out if 0 <= p < total]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", default=PDF)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--pages", default=None,
                    help="مثال: 1-5 أو 1,3,7 (ترقيم PDF من 1)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--jobs", type=int, default=2)
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    pdf = os.path.abspath(args.pdf)
    doc = pymupdf.open(pdf)
    total = doc.page_count
    doc.close()
    pages = parse_pages(args.pages, total)
    print(f"PDF: {pdf}\nصفحات: {len(pages)} من {total}", flush=True)

    work = [(p, pdf, args.out, args.force) for p in pages]
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for pno, status, dt in ex.map(_one, work):
            done += 1
            print(f"[{done:3d}/{len(pages)}] صفحة {pno + 1:3d}  {status:8s} "
                  f"{dt:5.1f}s  (إجمالي {time.time() - t0:6.0f}s)", flush=True)
    print(f"انتهى في {time.time() - t0:.0f} ثانية.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
