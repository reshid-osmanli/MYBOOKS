# -*- coding: utf-8 -*-
"""
محرّك التفريغ الضوئي (OCR) لكتب PDF المصوّرة.

المشكلة التي يحلّها هذا الملف:
  الكتاب «2_علم_التجويد_دكتور_يحيى_الغوثاني» ملف PDF مصوّر: كل صفحة صورة
  واحدة بلا أي طبقة نصّية، ودقّة معظم الصفحات 96 نقطة/بوصة فقط، والنصّ
  عربي مشكول بالكامل. لذلك:

  1. نستخرج الصورة الأصلية المضمّنة (لا نعيد رسم الصفحة) لتفادي أي فقد.
  2. نرفع دقّتها ×3 بشبكة ESPCN (رفع دقّة فائق) لأن Tesseract يحتاج
     ارتفاع حرف ≈ 30–40 بكسل، والمصدر يعطي ≈ 20 بكسل فقط.
  3. نطبّق عتبة Otsu لفصل الحبر عن الورق.
  4. نمرّرها على Tesseract بخيارَي تقطيع (PSM 6 و PSM 3) ونحتفظ بمربّع كل
     كلمة وثقتها، فيمكن لاحقًا ترجيح نتيجة على أخرى ووسم المواضع المشكوك فيها.
"""

import io
import os

import cv2
import numpy as np
import pymupdf
import tesserocr
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SR_DIR = os.path.join(HERE, "sr_models")
TESSDATA = os.path.join(HERE, "refs")

PSM_MODES = (6, 3)          # single uniform block / full page segmentation
FACTOR = 3                  # معامل رفع الدقّة لشبكة ESPCN

# ----- سياسة المقاسات -------------------------------------------------------
# Tesseract يعطي أفضل نتائج عندما يكون ارتفاع الحرف ≈ 30–40 بكسل.
# مصادر الكتاب ثلاثة أنواع، ولكلٍّ منها معالجة:
#   * عرض ≤ LOW_MAX   (صفحات 96 نقطة/بوصة)  -> رفع دقّة ×3 بشبكة ESPCN
#   * LOW_MAX < w ≤ OK_MAX (صفحات 200 نقطة/بوصة) -> تُترك كما هي
#   * w > OK_MAX      (أغلفة عالية الدقّة)  -> تُصغَّر إلى OK_MAX
LOW_MAX = 1100
OK_MAX = 2300

TILE = 512                  # ضلع مربّع المعالجة عند رفع الدقّة
PAD = 16                    # هامش تداخل بين المربّعات (لتفادي خطوط الوصل)


# --------------------------------------------------------------------- الصور

def load_native(doc, pno):
    """الصورة المضمّنة في الصفحة كما هي، بتدرّج رمادي."""
    page = doc[pno]
    imgs = page.get_images(full=True)
    if not imgs:
        return None
    raw = doc.extract_image(imgs[0][0])
    img = Image.open(io.BytesIO(raw["image"])).convert("L")
    return np.array(img)


class Upscaler:
    """غلّاف حول ESPCN (رفع دقّة ×3 يعمل بالتقسيط).

    التقسيم ضروري: تمرير صورة بعرض 2449 بكسل على شبكة بـ64 قناة يعني
    عشرات الجيغابايتات من الذاكرة. نعالج مربّعات 512×512 بهامش تداخل
    ثم نخيطها، والنتيجة مطابقة للمعالجة الكاملة لأن الشبكة التفافية بالكامل.
    """

    def __init__(self):
        self.sr = None
        path = os.path.join(SR_DIR, "ESPCN_x3.pb")
        if os.path.exists(path):
            self.sr = cv2.dnn_superres.DnnSuperResImpl_create()
            self.sr.readModel(path)
            self.sr.setModel("espcn", FACTOR)

    def _upscale_full(self, gray):
        return self.sr.upsample(gray)

    def _upscale_tiled(self, gray):
        h, w = gray.shape
        out = np.zeros((h * FACTOR, w * FACTOR), np.uint8)
        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                y0, x0 = max(0, y - PAD), max(0, x - PAD)
                y1, x1 = min(h, y + TILE + PAD), min(w, x + TILE + PAD)
                tile = gray[y0:y1, x0:x1]
                up = self._upscale_full(tile)
                ty0, tx0 = (y - y0) * FACTOR, (x - x0) * FACTOR
                th, tw = min(TILE, h - y) * FACTOR, min(TILE, w - x) * FACTOR
                out[y * FACTOR:y * FACTOR + th, x * FACTOR:x * FACTOR + tw] = \
                    up[ty0:ty0 + th, tx0:tx0 + tw]
        return out

    def __call__(self, gray):
        if self.sr is None:
            return cv2.resize(gray, None, fx=FACTOR, fy=FACTOR,
                              interpolation=cv2.INTER_LANCZOS4)
        try:
            if gray.shape[0] * gray.shape[1] * FACTOR ** 2 > 20_000_000:
                return self._upscale_tiled(gray)
            return self._upscale_full(gray)
        except cv2.error:
            return cv2.resize(gray, None, fx=FACTOR, fy=FACTOR,
                              interpolation=cv2.INTER_LANCZOS4)


def normalise(gray, upscaler):
    """يوحّد دقّة الصفحة وفق سياسة المقاسات أعلاه."""
    w = gray.shape[1]
    if w <= LOW_MAX:
        return upscaler(gray)
    if w > OK_MAX:
        scale = OK_MAX / w
        return cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA)
    return gray


def binarize(gray):
    _, out = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return out


def prepare(gray, upscaler):
    """السلسلة الكاملة: تطبيع الدقّة ثم عتبة. تُعيد صورة جاهزة لـ Tesseract."""
    return binarize(normalise(gray, upscaler))


# ------------------------------------------------------------------ Tesseract

def ocr_page(img, psm, tessdata=TESSDATA, lang="ara"):
    """يشغّل Tesseract ويُعيد أسطرًا: [{'bbox':..., 'words':[{text,conf,bbox}]}]."""
    api = tesserocr.PyTessBaseAPI(path=tessdata, lang=lang)
    try:
        api.SetPageSegMode(psm)
        if isinstance(img, np.ndarray):
            img = Image.fromarray(img)
        api.SetImage(img)
        tsv = api.GetTSVText(0)
    finally:
        api.End()
    return _parse_tsv(tsv)


def _parse_tsv(tsv):
    lines = {}
    if not tsv:
        return []
    for row in tsv.splitlines():
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":      # level 5 == word
            continue
        try:
            _lvl, _pg, blk, par, ln = (int(parts[i]) for i in range(5))
            left, top, w, h = (int(parts[i]) for i in range(6, 10))
            conf = float(parts[10])
            text = parts[11].strip()
        except (ValueError, IndexError):
            continue
        if not text:
            continue
        key = (blk, par, ln)
        ent = lines.setdefault(key, {"bbox": [left, top, left + w, top + h],
                                     "words": []})
        b = [left, top, left + w, top + h]
        ent["bbox"][0] = min(ent["bbox"][0], b[0])
        ent["bbox"][1] = min(ent["bbox"][1], b[1])
        ent["bbox"][2] = max(ent["bbox"][2], b[2])
        ent["bbox"][3] = max(ent["bbox"][3], b[3])
        ent["words"].append({"text": text, "conf": conf, "bbox": b})
    out = list(lines.values())
    out.sort(key=lambda e: (round(e["bbox"][1] / 8), e["bbox"][1]))
    return out


class PageOCR:
    """نتيجة صفحة كاملة: كل أوضاع التقطيع، مع مقاييس مساعدة."""

    def __init__(self, pno, native_shape, passes):
        self.pno = pno
        self.native_shape = list(native_shape) if native_shape else None
        self.passes = passes            # {"6": [...lines], "3": [...]}

    def to_json(self):
        return {"page": self.pno,
                "native_shape": self.native_shape,
                "passes": {str(k): v for k, v in self.passes.items()}}
