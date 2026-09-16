# -*- coding: utf-8 -*-
"""
make_docx.py -- يحوّل نصّ التفريغ النهائي إلى ملف وورد منظَّم قابل للتحرير.

    python make_docx.py

يقرأ out/تفريغ_كتاب_علم_التجويد.txt ويُنتج out/كتاب_علم_التجويد_تفريغ.docx:
  * كل صفحة في مقطعها مع فاصل صفحات، وترويسة تحمل رقم الصفحة المطبوع؛
  * اتجاه الصفحة من اليمين إلى اليسار وخط عربي واضح؛
  * المواضع المشكوك فيها (⟨؟⟩) بلون مختلف ليراجعها المدقّق البشري؛
  * الشواهد القرآنية المحصورة بـ﴿﴾ بلون مميّز.
"""

import os
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SRC = os.path.join(OUT, "تفريغ_كتاب_علم_التجويد_مقروء.txt")
DEST = os.path.join(OUT, "كتاب_علم_التجويد_تفريغ.docx")

FONT = "Sakkal Majalla"
FALLBACK = "Traditional Arabic"
PAGE_RE = re.compile(r"^===== صفحة (\d+) =====$")
PRINTED_RE = re.compile(r"^\[الصفحة المطبوعة (.+)\]$")
DOUBT = "\u27e8\u061f\u27e9"          # ⟨؟⟩
VERSE_RE = re.compile(r"(\uFD3E[^\uFD3E\uFD3F]*\uFD3F)")


def rtl(paragraph):
    pPr = paragraph._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    pPr.append(bidi)


def style_run(run, size=13, color=None, bold=False):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.append(rf)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rf.set(qn(attr), FONT)
    szcs = OxmlElement("w:szCs")
    szcs.set(qn("w:val"), str(int(size * 2)))
    rPr.append(szcs)
    rtl_el = OxmlElement("w:rtl")
    rPr.append(rtl_el)


def add_line(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.space_before = Pt(0)
    rtl(p)
    # نقسّم السطر: شاهد قرآني / موضع مشكوك / نصّ عادي
    pos = 0
    for m in VERSE_RE.finditer(text):
        if m.start() > pos:
            _plain(p, text[pos:m.start()])
        r = p.add_run(m.group())
        style_run(r, color=RGBColor(0x0B, 0x61, 0x21))
        pos = m.end()
    if pos < len(text):
        _plain(p, text[pos:])
    return p


def _plain(p, text):
    if not text:
        return
    if DOUBT in text:
        for i, chunk in enumerate(text.split(DOUBT)):
            if i:
                r = p.add_run(" ⟨؟⟩ ")
                style_run(r, size=9, color=RGBColor(0xC0, 0x00, 0x00))
            if chunk:
                r = p.add_run(chunk)
                style_run(r, size=13)
    else:
        r = p.add_run(text)
        style_run(r, size=13)


def main():
    if not os.path.exists(SRC):
        print(f"لا يوجد ملف مصدر: {SRC}")
        return 1
    doc = Document()
    # الخط الافتراضي للمستند
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(13)
    st.element.rPr.rFonts.set(qn("w:cs"), FONT)

    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Pt(50)
    sec.top_margin = sec.bottom_margin = Pt(50)
    sectPr = sec._sectPr
    bidi = OxmlElement("w:bidi")
    sectPr.append(bidi)

    first = True
    for raw in open(SRC, encoding="utf-8"):
        line = raw.rstrip("\n")
        m = PAGE_RE.match(line)
        if m:
            pdf_page = int(m.group(1))
            if not first:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            first = False
            h = doc.add_paragraph()
            h.alignment = WD_ALIGN_PARAGRAPH.CENTER
            rtl(h)
            r = h.add_run(f"— {pdf_page} —")
            style_run(r, size=10, color=RGBColor(0x80, 0x80, 0x80))
            continue
        if PRINTED_RE.match(line):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            rtl(p)
            r = p.add_run(line)
            style_run(r, size=10, color=RGBColor(0x80, 0x80, 0x80))
            continue
        if not line.strip():
            continue
        add_line(doc, line)

    doc.save(DEST)
    print(f"كُتب: {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
