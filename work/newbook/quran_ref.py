# -*- coding: utf-8 -*-
"""
quran_ref.py -- مرجع نصّ القرآن الكريم للتصحيح الآلي للشواهد القرآنية.

لماذا هذا الملف؟
    تفريغ كتاب تجويد بالـOCR ينتج آيات مشوّهة (الرسم العثماني بخطّ دقيق
    وضبط كامل يصعب على Tesseract). لكن الشاهد القرآني نصّه معلوم قطعًا،
    فيمكن *استبداله* بنصّه الصحيح بدل تركه مشوّهًا. هذا أكبر مكسب جودة
    في المشروع كله.

المصدر: dist/quran.json من مستودع risan/quran-json (الرسم العثماني
مع الضبط الكامل) -- يُنزَّل عبر fetch_refs.py.
"""

import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
QURAN_JSON = os.path.join(HERE, "refs", "quran.json")

# ------------------------------------------------------------------ التطبيع
# علامات الضبط والرموز القرآنية التي تُحذف قبل المقارنة
_DIACRITICS = (
    "\u064b-\u065f"      # تنوين وحركات
    "\u0670"             # ألف خنجرية
    "\u06d6-\u06ed"      # رموز الوقف والضبط العثماني (ۖ ۡ ۚ ...)
    "\u0640"             # تطويل
    "\u0653-\u0655"      # مدّة، همزة فوق/تحت
    "\u200c-\u200f"      # محارف اتجاه
)
# تنبيه: ألف الوصل \u0671 ليست هنا. كانت تُحذف خطأً فينتج «بلنفس» من
# «بِٱلنَّفْسِ»، وهو ما أفسد فهرس المراسي وصيّر مطابقة الشواهد عاجزة.
_DIAC_RE = re.compile(f"[{_DIACRITICS}]")
_SPACE_RE = re.compile(r"\s+")

# توحيد الأشكال المتقاربة رسومًا
_UNIFY = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ى": "ي", "ئ": "ي", "ي": "ي",
    "ؤ": "و", "ة": "ه", "ۀ": "ه",
    "ک": "ك", "ی": "ي",
    # الهمزة المفردة تُحذف: الرسم العثماني يكتب «ءَايَةٍ» والكتاب «آيَةٍ»،
    # فحذفها من الطرفين يجعلهما «ايه» ويتيح المطابقة.
    "ء": "",
}

# ما يُحذف كليًّا من نصّ الـOCR لأنه ضجيج رموز لا حروف
_STRIP_OCR = re.compile(r"[\"“”«»\(\)\[\]\{\}<>#*_~^|/\\+=؛،,%$&@!?؟:،\u200e\u200f\u202a-\u202e]")


def normalise(text, strip_punct=True):
    """يُعيد صورة قابلة للمقارنة: بلا ضبط، وبأشكال موحّدة."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    if strip_punct:
        text = _STRIP_OCR.sub(" ", text)
    text = _DIAC_RE.sub("", text)
    text = "".join(_UNIFY.get(ch, ch) for ch in text)
    # أرقام عربية-هندية -> لاتينية
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    return _SPACE_RE.sub(" ", text).strip()


def strip_diacritics(text):
    """يحذف الضبط فقط، ويُبقي الحروف وعلامات الترقيم."""
    if not text:
        return ""
    return _DIAC_RE.sub("", text)


# ------------------------------------------------------------------ التحميل

class Quran:
    def __init__(self, path=QURAN_JSON):
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.surahs = []          # [{id, name, verses:[{id,text}]}]
        self.by_name = {}         # اسم منطبَع -> رقم السورة
        self.texts = {}           # (surah, ayah) -> النص بالرسم العثماني
        for s in raw:
            sid = s["id"]
            name = s["name"]
            self.surahs.append(s)
            for key in _name_keys(name):
                self.by_name.setdefault(key, sid)
            for v in s["verses"]:
                self.texts[(sid, v["id"])] = v["text"]
        # فهرس معكوس: رمز -> مجموعة أرقام الآيات (للقفز السريع)
        self._index = {}
        for (sid, ayah), txt in self.texts.items():
            for tok in set(normalise(txt).split()):
                if len(tok) >= 3:
                    self._index.setdefault(tok, set()).add((sid, ayah))

    # ------------------------------------------------------------- استعلامات
    def ayah(self, sid, num):
        return self.texts.get((sid, num))

    def surah_name(self, sid):
        return self.surahs[sid - 1]["name"]

    def resolve_surah(self, name):
        """يحوّل اسم سورة (كما ورد في الهامش) إلى رقمها."""
        key = normalise(name)
        key = key.replace("سوره ", "").replace("سورة ", "").strip()
        if key in self.by_name:
            return self.by_name[key]
        # مطابقة جزئية: «الفاتحه» داخل «الفاتحه»
        for k, sid in self.by_name.items():
            if k and (k in key or key in k):
                return sid
        return None

    def candidates(self, query_norm, min_hits=1):
        """أرقام الآيات المرشّحة بحسب الرموز المشتركة."""
        toks = [t for t in query_norm.split() if len(t) >= 3]
        if not toks:
            return []
        score = {}
        for t in toks:
            for key in self._index.get(t, ()):
                score[key] = score.get(key, 0) + 1
        return [k for k, c in score.items() if c >= min_hits]


def _name_keys(name):
    """مفاتيح بحث لاسم السورة: بالنصّ كما هو وبأشكاله المطبَّعة."""
    base = normalise(name)
    keys = {base}
    # إزالة «ال» التعريف في البداية ليتقبّل «الفاتحة/فاتحة»
    if base.startswith("ال") and len(base) > 4:
        keys.add(base[2:])
    return {k for k in keys if k}


# ------------------------------------------------------- البحث عن الآية من نصّ

def _similarity(a, b):
    """نسبة تشابه بسيطة مبنية على رموز الكلمتين المتقابلين."""
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _score(q, vn):
    """درجة تشابه بين نصّ OCR مطبَّع وآية مطبَّعة.

    نجمع إشارتين مستقلّتين لأن كلًّا منهما تفشل وحدها:
      * تشابه المحارف: يمسك الأخطاء الإملائية الصغيرة.
      * تقاطع الرموز: يمسك الاختلاف الكبير في الضبط/الرسم.
    ونضيف مكافأة الاستيعاب حين يكون أحد النصّين جزءًا من الآخر،
    وهي الحالة الغالبة (الشاهد يُقتبَس ناقصًا أو مقرونًا بسياق).
    """
    if not q or not vn:
        return 0.0
    char = _similarity(q, vn)
    qt, vt = set(q.split()), set(vn.split())
    tok = len(qt & vt) / max(1, len(qt | vt))
    s = 0.6 * char + 0.4 * tok
    if q in vn or vn in q:
        s = max(s, 0.92)
    # استيعاب جزئي: أغلب كلمات الاستعلام موجودة في الآية
    if qt:
        cover = len(qt & vt) / len(qt)
        if cover >= 0.8 and len(qt) >= 3:
            s = max(s, 0.75 + 0.2 * cover)
    return s


def rank_verses(quran, ocr_text, threshold=0.60, min_len=5, top=6):
    """يرتّب الآيات المرشّحة تنازليًّا بالتشابه.

    يُعيد قائمة [(score, surah_id, ayah_no, verse_text)].
    """
    q = normalise(ocr_text)
    if len(q.replace(" ", "")) < min_len:
        return []
    cands = quran.candidates(q, min_hits=1)
    if not cands:
        return []
    scored = []
    for (sid, ayah) in cands:
        v = quran.texts[(sid, ayah)]
        s = _score(q, normalise(v))
        if s >= threshold:
            scored.append((s, sid, ayah, v))
    scored.sort(key=lambda e: -e[0])
    return scored[:top]


def unique_verse(quran, ocr_text, threshold=0.60, margin=0.06, min_len=5):
    """يُرجع (sid, ayah, score, text) فقط إذا كان الفائز *واضحًا*.

    الشرط: أفضل درجة ≥ threshold، والفرق عن الوصيف ≥ margin. وجود آيتين
    متقاربتين يعني أن الشاهد القصير يقع في أكثر من موضع (مثل «ينطقون»)،
    ولا يجوز الجزم حينها؛ يُترك للمراجع البشري أو لقرينة الهامش.
    """
    ranked = rank_verses(quran, ocr_text, threshold=threshold, min_len=min_len)
    if not ranked:
        return None
    top = ranked[0]
    if len(ranked) == 1 or (top[0] - ranked[1][0]) >= margin:
        return top[1], top[2], top[0], top[3]
    # نتعافى إذا كان الفائز *يحتوي* الشاهد حرفيًّا والبقية لا
    q = normalise(ocr_text)
    holders = [r for r in ranked if q and q in normalise(r[3])]
    if len(holders) == 1:
        h = holders[0]
        return h[1], h[2], h[0], h[3]
    return None


def best_verse(quran, ocr_text, threshold=0.60, min_len=5):
    """أقرب آية (بلا شرط التفرّد) -- للعرض والتشخيص فقط."""
    ranked = rank_verses(quran, ocr_text, threshold=threshold, min_len=min_len)
    return ranked[0] if ranked else None


def verses_in_range(quran, sid, a_from, a_to):
    """نصوص آيات متتابعة (للشواهد الممتدة على أكثر من آية)."""
    return [(n, quran.texts[(sid, n)]) for n in range(a_from, a_to + 1)
            if (sid, n) in quran.texts]


# ------------------------------------------------- إصلاح الشواهد بالمرساة
#
# المسح الشامل لكل النوافذ بطيء جدًّا (يتجاوز 25 دقيقة على 193 صفحة).
# الحلّ: لا نبحث في كل موضع، بل في المواضع التي تحوي كلمة *لا توجد إلا في
# آية واحدة* من القرآن. هذه «المرساة» تُحدِّد الآية تحديدًا قاطعًا، ثم
# نتحقّق بمقارنة النافذة المحيطة بها بالآية كاملة.

def build_anchors(quran, min_len=5):
    """رمز -> (سورة, آية) لكل رمز يقع في آية واحدة فقط."""
    inv = {}
    for key, txt in quran.texts.items():
        for tok in set(normalise(txt).split()):
            if len(tok) >= min_len:
                inv.setdefault(tok, set()).add(key)
    return {t: next(iter(s)) for t, s in inv.items() if len(s) == 1}


def _tokens_with_spans(line):
    return [(m.start(), m.end(), normalise(m.group()))
            for m in re.finditer(r"\S+", line)]


def find_verse_span(quran, anchors, line, threshold=0.80, min_tokens=3,
                    slack=2, max_probe=60):
    """يجد أفضل مقطع في السطر يطابق آية مطابقة قاطعة.

    يُعيد (start, end, sid, ayah, matched_verse_text, score) أو None.

    الكتب التجويدية تقتبس *أجزاءً* من الآية لا الآية كاملة، فنخالف ما سبق:
    لا نفترض أن الشاهد يساوي الآية كلّها. المرساة تعطينا الآية وموقعها
    داخلها؛ ثم نجرّب كل مقطع من الآية يشمل المرساة (من كلمتين إلى الآية
    كاملة) ونحاذيه بما يقابله في السطر، فنأخذ أعلى تشابه.
    """
    toks = _tokens_with_spans(line)
    if len(toks) < min_tokens:
        return None
    norm_tokens = [t[2] for t in toks]
    best = None
    for idx, nt in enumerate(norm_tokens):
        key = anchors.get(nt)
        if key is None:
            continue
        sid, ayah = key
        vt = normalise(quran.texts[(sid, ayah)]).split()
        if nt not in vt:
            continue
        pos = vt.index(nt)
        probes = 0
        for p1 in range(pos, max(-1, pos - 14), -1):
            for p2 in range(pos, min(len(vt), pos + 14)):
                probes += 1
                if probes > max_probe * 6:
                    break
                sub = vt[p1:p2 + 1]
                if len(sub) < min_tokens:
                    continue
                base = idx - (pos - p1)
                for shift in range(-slack, slack + 1):
                    start = base + shift
                    for extra in (0, 1, -1, 2, -2):
                        a, b = start, start + len(sub) + extra
                        if a < 0 or b > len(toks) or b - a < min_tokens:
                            continue
                        window = " ".join(norm_tokens[a:b])
                        subj = " ".join(sub)
                        score = 1.0 if window == subj else _score(window, subj)
                        cover = min(len(window), len(subj)) / max(len(window),
                                                                 len(subj))
                        if cover < 0.8:
                            continue
                        if score >= threshold and (best is None or
                                                   score > best[5]):
                            best = (toks[a][0], toks[b - 1][1], sid, ayah,
                                    subj, score)
    return best
