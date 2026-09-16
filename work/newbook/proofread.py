# -*- coding: utf-8 -*-
"""
proofread.py -- التدقيق اللغوي الآلي لنصّ التفريغ الضوئي.

الفكرة
------
الـOCR يخطئ في ثلاثة أنماط، ولكلٍّ علاج مختلف:

1. **خطأ في الحروف** (احذز ← احذر، الشون ← النون):
   يُكشف بمقارنة «الهيكل» (الكلمة بعد حذف الضبط) بمعجم عربي واسع.
   إن لم يكن الهيكل كلمةً معروفة، وكانت هناك كلمة واحدة فقط تقاربه
   بمسافة تحرير صغيرة، فالترجيح قويّ ونُصلح. وإن تعدّدت المرشّحات
   نترك الكلمة ونوسمها، لأن التخمين قد يُفسد معنى فقهيًّا أو قرآنيًّا.

2. **خطأ في الضبط وحده** (الملاحَظّة ← الملاحظة):
   لا معجم يحكم على الضبط، فنتناول فقط ما هو خطأ بنيويّ قطعيّ:
   شدّة مكرّرة، أو حركتان متنافيتان على حرف واحد.

3. **رموز وضجيج** (بقايا تنقيط الضبط): تُنظَّف أو تُوسَم.

المعاجم (تُنزَّل عبر fetch_refs.py):
 * arwiki.freqlist        -- 1.7 مليون كلمة عربية بتكراراتها (الأساس)
 * quran.vocalized.wordlist -- مفردات القرآن بالضبط (لهذا الكتاب خاصّة)
 * ar.dic                 -- قاموس جذور، احتياطيًا

نأخذ من قائمة التكرارات ما تكراره ≥ min_freq (افتراضًا 20) فيبقى ~220 ألف
كلمة: يكفي لتغطية اللغة العامّة، ويُبقي فهرس الترشيح صغيرًا في الذاكرة.
"""

import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from quran_ref import normalise  # noqa: E402

REFS = os.path.join(HERE, "refs")

DIAC_RE = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed\u0640\u0653-\u0655]")
ARABIC_RE = re.compile(r"[\u0621-\u064A]")
TATWEEL_RE = re.compile(r"\u0640")


def skeleton(word):
    """الكلمة مجرّدة من الضبط ومطبَّعة الأشكال، للمقارنة."""
    if not word:
        return ""
    w = unicodedata.normalize("NFC", word)
    w = DIAC_RE.sub("", w)
    w = TATWEEL_RE.sub("", w)
    w = normalise(w)
    return w.replace(" ", "")


def edit_distance(a, b, cap):
    """مسافة ليفنشتاين مقيّدة؛ تُعيد None إن تجاوزت cap."""
    if abs(len(a) - len(b)) > cap:
        return None
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = prev[j] + 1
            w = cur[j - 1] + 1
            u = prev[j - 1] + cost
            if w < v:
                v = w
            if u < v:
                v = u
            cur[j] = v
            if v < best:
                best = v
        if best > cap:
            return None
        prev = cur
    d = prev[lb]
    return d if d <= cap else None


# =============================================================== المعجم

def _gram(w, n):
    if len(w) < n:
        return {w}
    return {w[i:i + n] for i in range(len(w) - n + 1)}


class Lexicon:
    """معجم عربي مع فهرس n-grams لاسترجاع المرشّحات بسرعة."""

    def __init__(self, min_freq=20, extra_words=(), quran_words=True,
                 hunspell=True, verbose=False):
        self.freq = Counter()
        self._load_freqlist(os.path.join(REFS, "arwiki.freqlist"), min_freq)
        if quran_words:
            self._load_plain(os.path.join(REFS, "quran.vocalized.wordlist"),
                             weight=2000)
        if hunspell:
            self._load_hunspell(os.path.join(REFS, "ar.dic"))
        for w in extra_words:
            s = skeleton(w)
            if len(s) >= 2:
                self.freq[s] += 1000
        self.words = list(self.freq)
        self.gram_index = defaultdict(set)
        for i, w in enumerate(self.words):
            for g in _gram(w, 3 if len(w) >= 4 else 2):
                self.gram_index[g].add(i)
        if verbose:
            print(f"المعجم: {len(self.words):,} كلمة، "
                  f"{len(self.gram_index):,} فهرس n-gram", flush=True)

    # ------------------------------------------------------------- التحميل
    def _load_freqlist(self, path, min_freq):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                i = line.rfind(" ")
                if i < 0:
                    continue
                w, c = line[:i].strip(), line[i + 1:].strip()
                if not c.isdigit() or int(c) < min_freq:
                    continue
                s = skeleton(w)
                if 2 <= len(s) <= 30:
                    self.freq[s] += int(c)

    def _load_plain(self, path, weight=1000):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                s = skeleton(line.strip())
                if 2 <= len(s) <= 30:
                    self.freq[s] += weight

    def _load_hunspell(self, path):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="ignore") as fh:
            fh.readline()
            for line in fh:
                s = skeleton(line.split("/")[0].strip())
                if 2 <= len(s) <= 30:
                    self.freq.setdefault(s, 1)

    # ------------------------------------------------------------- الاستعلام
    def known(self, word):
        return skeleton(word) in self.freq

    def suggest(self, word, maxdist=1, limit=5, pool=400):
        """مرشّحات المعجم مرتّبة بالمسافة ثم التكرار."""
        s = skeleton(word)
        if len(s) < 3:
            return []
        grams = _gram(s, 3 if len(s) >= 4 else 2)
        shared = Counter()
        for g in grams:
            for i in self.gram_index.get(g, ()):
                shared[i] += 1
        if not shared:
            return []
        cand = [i for i, _ in shared.most_common(pool)]
        out = []
        for i in cand:
            w = self.words[i]
            d = edit_distance(s, w, maxdist)
            if d is not None and d > 0:
                out.append((d, -self.freq[w], w))
        out.sort()
        return [(d, w) for d, _f, w in out[:limit]]


# ============================================ تنقية الضبط البنيوي

def fix_shadda(word):
    """يحذف الشدّة المكرّرة (الملاحَظّة ← الملاحظة)."""
    return re.sub(r"(\u0651)\1+", r"\1", word)


def fix_conflicting_marks(word):
    """يُبقي حركة واحدة على كل حرف عند تنافس الحركات."""
    FATHA, DAMMA, KASRA, SUKUN = "\u064e", "\u064f", "\u0650", "\u0652"
    VOWELS = FATHA + DAMMA + KASRA + SUKUN + "\u064b\u064c\u064d"
    out = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        out.append(ch)
        if ch == "\u0651":
            i += 1
            continue
        if ch in VOWELS:
            j = i + 1
            while j < n and word[j] in VOWELS:
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


STRUCT_RULES = (fix_shadda, fix_conflicting_marks)


def transfer_marks(original, target):
    """ينقل ضبط الكلمة الأصلية إلى الهيكل المصحَّح.

    الأخطاء غالبًا حرف واحد، فالنقل يحفظ الضبط الصحيح للحروف المتطابقة
    بدل إسقاطه كله. الحروف المستبدلة تُترك بلا ضبط (لا نخترعه).
    """
    import difflib
    src = original
    dst = target
    sm = difflib.SequenceMatcher(None, src, dst, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(src[i1:i2])
        elif tag in ("replace", "insert"):
            # نأخذ ما بعد كل حرف من الأصل إن تطابق عدد الحروف
            if (i2 - i1) == (j2 - j1):
                out.append(src[i1:i2])
            else:
                out.append(dst[j1:j2])
        # delete: نُسقط الحرف الزائد
    return "".join(out)


# ============================================================= المدقّق

class Proofreader:
    def __init__(self, lex=None, domain_words=(), verbose=False):
        self.lex = lex or Lexicon(extra_words=domain_words, verbose=verbose)
        self.domain = {skeleton(w) for w in domain_words if skeleton(w)}
        self.stats = Counter()
        self.changes = []

    def correct_word(self, word, page=None):
        """يُصلح كلمة واحدة إن كان الإصلاح قاطعًا.

        يُعيد (الكلمة الجديدة, نوع الإصلاح أو None).
        """
        if not ARABIC_RE.search(word):
            return word, None
        orig = word
        for rule in STRUCT_RULES:
            word = rule(word)
        if word != orig:
            self.stats["ضبط بنيوي"] += 1
            self.changes.append({"page": page, "kind": "ضبط بنيوي",
                                 "before": orig, "after": word})
            return word, "ضبط بنيوي"

        s = skeleton(word)
        if len(s) < 3 or s in self.domain or self.lex.known(word):
            return word, None

        maxdist = 1 if len(s) <= 5 else 2
        sug = self.lex.suggest(word, maxdist=maxdist, limit=3)
        if not sug:
            self.stats["مجهول"] += 1
            return word, None
        best_d = sug[0][0]
        tied = [w for d, w in sug if d == best_d]
        if len(tied) > 1:
            self.stats["غامض"] += 1
            return word, None
        fixed = transfer_marks(word, sug[0][1])
        if not fixed:
            return word, None
        self.stats["حروف مصلَحة"] += 1
        self.changes.append({"page": page, "kind": "حروف",
                             "before": orig, "after": fixed})
        return fixed, "حروف"
