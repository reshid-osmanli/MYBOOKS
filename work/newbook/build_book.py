# -*- coding: utf-8 -*-
"""
build_book.py -- المرحلة النهائية: يبني نصّ الكتاب المنظَّم المدقَّق من
ملفات JSON الوسيطة، ويكتب الخرج النصّي وسجلّ التصحيحات.

    python build_book.py                 # الكتاب كاملًا
    python build_book.py --limit 20      # للتجربة السريعة

المراحل بالترتيب:
  1. دمج تقطيعَي Tesseract (PSM 6 أساسًا، وما فاته من PSM 3).
  2. حذف أسطر الضجيج (بقايا تنقيط الضبط).
  3. إصلاح الشواهد القرآنية بمطابقة «المرساة» مع نصّ المصحف.
  4. تدقيق لغوي آلي: تنقية الضبط البنيوي + تصحيح حروف قاطع.
  5. ترتيب بنية الصفحة وإخراج النصّ مع وسم المواضع المشكوك فيها.

كل تبديل يُسجَّل في out/changes.json مع رقم الصفحة ونوعه، ليُراجع بشريًّا.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from proofread import Proofreader, skeleton          # noqa: E402
from quran_ref import (Quran, build_anchors, find_verse_span,  # noqa: E402
                       normalise, strip_diacritics)

JSON_DIR = os.path.join(HERE, "json")
OUT_DIR = os.path.join(HERE, "out")

ARABIC_RE = re.compile(r"[\u0621-\u064A]")
DIAC_RE = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
WORD_RE = re.compile(r"[\u0621-\u064A][\u0621-\u064A\u064b-\u065f\u0670"
                     r"\u06d6-\u06ed\u0640]*")
PAGENO_RE = re.compile(r"^[\s\(\)\u0660-\u0669\d\.\-]{1,6}$")
FOOTNOTE_RE = re.compile(r"^[\s\(\)\u0660-\u0669\d]{1,4}\s*[\]\)]")


# --------------------------------------------------------------------- أدوات

def weight_conf(words):
    tot = sum(len(w["text"]) for w in words) or 1
    return sum(w["conf"] * len(w["text"]) for w in words) / tot


def overlaps(a, b, tol=8):
    return not (a[3] < b[1] - tol or b[3] < a[1] - tol)


def merge_passes(rec):
    """يدمج الوضعين بترتيب محفوظ.

    ترتيب كلمات سطر Tesseract ترتيب *منطقي* (RTL)، وإحداثياتها تتناقص،
    فإعادة الفرز بالإحداثيات تقلب السطر. لذلك لا نفرز: نأخذ أسطر PSM 6
    كما هي (وهو الأكمل)، ونستبدل كل كلمة بنظيرتها من PSM 3 إن كان لها
    نظير في الموضع نفسه وثقتها أعلى؛ ثم نضيف من PSM 3 ما لم يجده PSM 6
    أصلًا، في موضعه من السطر.
    """
    base = rec["passes"]["6"]
    alt_words = [w for L in rec["passes"]["3"] for w in L["words"]]
    used = [False] * len(alt_words)

    merged = []
    for L in base:
        ws = []
        for w in L["words"]:
            best = w
            for i, aw in enumerate(alt_words):
                if used[i]:
                    continue
                if abs(aw["bbox"][0] - w["bbox"][0]) <= 12 and \
                        abs(aw["bbox"][2] - w["bbox"][2]) <= 12 and \
                        abs(aw["bbox"][1] - w["bbox"][1]) <= 14:
                    used[i] = True
                    if aw["conf"] > w["conf"]:
                        best = aw
                        break
            ws.append(best)
        merged.append({"bbox": list(L["bbox"]), "words": ws, "src": "6"})

    # إضافة أسطر PSM 3 التي لم يتقاطع أيٌّ من كلماتها مع كلمات PSM 6
    for L in rec["passes"]["3"]:
        fresh = [w for w in L["words"]
                 if not any(_overlaps_box(w["bbox"], b)
                            for b in _flat_boxes(merged))]
        if not fresh:
            continue
        x0 = min(w["bbox"][0] for w in fresh)
        y0 = min(w["bbox"][1] for w in fresh)
        x1 = max(w["bbox"][2] for w in fresh)
        y1 = max(w["bbox"][3] for w in fresh)
        merged.append({"bbox": [x0, y0, x1, y1], "words": fresh, "src": "3"})

    merged.sort(key=lambda L: L["bbox"][1])
    for L in merged:
        L["text"] = " ".join(w["text"] for w in L["words"])
        L["conf"] = weight_conf(L["words"])
    return merged


def _flat_boxes(lines):
    return [w["bbox"] for L in lines for w in L["words"]]


def _overlaps_box(a, b, tol=6):
    return not (a[2] < b[0] + tol or b[2] < a[0] + tol or
                a[3] < b[1] - tol or b[3] < a[1] - tol)


def is_noise(line):
    """سطر ضجيج: لا يكاد يحوي حروفًا عربية."""
    t = line["text"].strip()
    if not t:
        return True
    core = DIAC_RE.sub("", t)
    core = re.sub(r"[^\w\s]", "", core, flags=re.UNICODE).strip()
    letters = ARABIC_RE.findall(core)
    if not letters:
        return not re.fullmatch(r"[\d\u0660-\u0669]+", core.strip())
    ratio = len(letters) / max(1, len(re.sub(r"\s", "", core)))
    if ratio < 0.5 and len(letters) < 6:
        return True
    words = t.split()
    if len(words) >= 4:
        short = sum(1 for w in words if len(DIAC_RE.sub("", w)) <= 2)
        if short / len(words) > 0.75:
            return True
    return False


# ------------------------------------------------------- إصلاح الشواهد

def repair_verses(lines, quran, anchors, page, log, join_next=True):
    """يستبدل الشواهد القرآنية بنصّها الصحيح من المصحف.

    يجرّب كل سطر على حدة، ثم كل سطرين متجاورين (لأن الشاهد قد يُلفّ على
    سطرين). الاستبدال مشروط بمطابقة ≥ العتبة المحدَّدة في find_verse_span.
    """
    for i, L in enumerate(lines):
        if L.get("merged_next"):
            # سطر ابتلعه السطر السابق عند إصلاح شاهد ممتدّ على سطرين
            continue
        hit = find_verse_span(quran, anchors, L["text"])
        if hit:
            a, b, sid, ayah, sub, score = hit
            before = L["text"][a:b]
            L["text"] = L["text"][:a] + "﴿" + quran.texts[(sid, ayah)] + "﴾" \
                + L["text"][b:]
            log.append({"page": page, "kind": "شاهد قرآني",
                        "ref": f"{quran.surah_name(sid)}: {ayah}",
                        "before": before,
                        "after": quran.texts[(sid, ayah)],
                        "score": round(score, 2)})
            continue
        if join_next and i + 1 < len(lines):
            joined = L["text"].rstrip() + " " + lines[i + 1]["text"].lstrip()
            hit = find_verse_span(quran, anchors, joined)
            if hit:
                a, b, sid, ayah, sub, score = hit
                before = joined[a:b]
                fixed = joined[:a] + "﴿" + quran.texts[(sid, ayah)] + "﴾" \
                    + joined[b:]
                L["text"] = fixed
                L["merged_next"] = True
                log.append({"page": page, "kind": "شاهد قرآني (سطران)",
                            "ref": f"{quran.surah_name(sid)}: {ayah}",
                            "before": before,
                            "after": quran.texts[(sid, ayah)],
                            "score": round(score, 2)})
    return [L for L in lines if not L.get("merged_next")]


# -------------------------------------------------------------- التدقيق

TOKEN_RE = re.compile(r"[\u0621-\u064A][\u0621-\u064A\u064b-\u065f\u0670"
                      r"\u06d6-\u06ed\u0640]*")


def proofread_line(text, pr, page):
    out = []
    pos = 0
    for m in TOKEN_RE.finditer(text):
        out.append(text[pos:m.start()])
        w, kind = pr.correct_word(m.group(), page=page)
        out.append(w)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


# ------------------------------------------------------------------ العرض

# ------------------------------------------------------------------ الترقيم
# رقم الصفحة المطبوع أسفل الصفحة يُقرأ آليًّا بثقة ضعيفة (الرقم صغير جدًّا
# بالنسبة لسطوع الورق). لكن العلاقة بينه وبين موضع الصفحة ثابتة، وقد
# تُحقّقت بصريًّا على إحدى عشرة صفحة موزّعة على الكتاب كلّه:
#   PDF 45->43، 49->47، 53->51، 57->55، 61->59، 65->63، 100->98،
#   150->148، 160->158، 170->168، 193->191  ==> المطبوع = PDF − 2
PAGE_OFFSET = 2


def printed_page(pdf_page):
    n = pdf_page - PAGE_OFFSET
    return n if n >= 1 else None


def fmt_arabic_number(n):
    """يحوّل الرقم إلى أرقام عربية-هندية كما في الكتاب."""
    if n is None:
        return None
    return str(n).translate(str.maketrans("0123456789",
                                          "\u0660\u0661\u0662\u0663\u0664"
                                          "\u0665\u0666\u0667\u0668\u0669"))


CONF_OK = 62          # عتبة الثقة التي نحتفظ عندها بضبط الكلمة


def clean_line(text, words, line_conf):
    """ينظّف الضبط غير الموثوق بدل تخمينه.

    المحرّك يقرأ الحروف العربية بثقة معقولة، لكن التشكيل — وهو دقيق في هذا
    الكتاب — يقرأه بثقة أدنى بكثير. فبدل تمرير ضبط خاطئ (يُفسد المعنى
    القرآني والفقهي) أو تخمين ضبط صحيح (افتراء على النصّ)، نحذف الضبط من
    الكلمات منخفضة الثقة فقط، ونُبقي ضبط ما قرأه المحرّك بثقة عالية.
    النتيجة نصّ مقروء أمين، تُحفظ فيه معلومة الضبط حيث صحّت.
    """
    if line_conf >= CONF_OK:
        return text
    conf_by_text = {}
    for w in words:
        conf_by_text.setdefault(w["text"], []).append(w["conf"])

    def repl(m):
        w = m.group()
        cs = conf_by_text.get(w)
        if not cs:
            return w
        if max(cs) >= CONF_OK:
            return w
        return DIAC_RE.sub("", w)

    return TOKEN_RE.sub(repl, text)


def render(lines, page, pageno, clean=False):
    parts = [f"===== صفحة {page} ====="]
    for L in lines:
        t = L["text"].strip()
        if not t:
            continue
        if clean:
            t = clean_line(t, L["words"], L["conf"])
        if L["conf"] < 40 and not clean:
            t += "   ⟨؟⟩"
        parts.append(t)
    if pageno:
        parts.append(f"[الصفحة المطبوعة {fmt_arabic_number(pageno)}]")
    parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(
        OUT_DIR, "تفريغ_كتاب_علم_التجويد.txt"))
    ap.add_argument("--no-proof", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(OUT_DIR, exist_ok=True)
    quran = Quran()
    anchors = build_anchors(quran)
    print(f"المراسي القرآنية: {len(anchors):,}")

    # مفردات المجال من الكتابين السابقين (مصطلح تجويدي مشترك)
    domain = []
    for f in ("part2_final.txt", "part3_final.txt"):
        p = os.path.join(HERE, "..", f)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                domain += [skeleton(w) for w in WORD_RE.findall(fh.read())]
    domain = [w for w in set(domain) if len(w) >= 3]
    print(f"مفردات المجال: {len(domain):,}")

    pr = Proofreader(domain_words=domain, verbose=True)

    files = sorted(f for f in os.listdir(JSON_DIR)
                   if re.fullmatch(r"p\d+\.json", f))
    if args.limit:
        files = files[:args.limit]

    log, pages, clean_pages, stats = [], [], [], Counter()
    for n, fn in enumerate(files, 1):
        rec = json.load(open(os.path.join(JSON_DIR, fn), encoding="utf-8"))
        page = rec["page"]
        lines = merge_passes(rec)
        stats["أسطر خام"] += len(lines)
        kept = [L for L in lines if not is_noise(L)]
        stats["أسطر ضجيج محذوفة"] += len(lines) - len(kept)
        kept = repair_verses(kept, quran, anchors, page, log)
        if not args.no_proof:
            for L in kept:
                L["text"] = proofread_line(L["text"], pr, page)
        # رقم الصفحة: أُزيل السطر الذي يحمله من المتن (لم يُقرأ آليًّا بدقّة)
        if kept:
            t = kept[-1]["text"].strip()
            if PAGENO_RE.fullmatch(t):
                kept = kept[:-1]
        pageno = printed_page(page)
        pages.append(render(kept, page, pageno))
        clean_pages.append(render(kept, page, pageno, clean=True))
        if n % 25 == 0:
            print(f"  ... صفحة {page}", flush=True)

    text = "\n".join(pages)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    clean_path = args.out.replace(".txt", "_مقروء.txt")
    with open(clean_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(clean_pages))
    json.dump({"verse_and_proof_fixes": log,
               "proofreader": dict(pr.stats)},
              open(os.path.join(OUT_DIR, "changes.json"), "w",
                   encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print("\n— الإحصاءات —")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print(f"  شواهد قرآنية مُصلَحة: "
          f"{sum(1 for x in log if 'قرآني' in x['kind']):,}")
    for k, v in pr.stats.items():
        print(f"  تدقيق/{k}: {v:,}")
    print(f"\nالخرج: {args.out}\n       {clean_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
