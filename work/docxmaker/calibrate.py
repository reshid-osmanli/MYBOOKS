# -*- coding: utf-8 -*-
"""
Calibration probes: put text boxes with known geometry into a docx,
render via Aspose (a real OOXML layout engine), and measure where the
text baseline lands so we can predict Word's placement:

  baseline_y = box_top + dy(tag, size)

with line-rule "exact" S = size*LINE_MULT and box height size*BOX_H_MULT.
"""
import os
import re
import sys
import json
import subprocess

import docx
import docx.oxml
from docx.oxml.ns import qn

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = "/home/user/venv_docx/bin/python"
ENV = dict(os.environ)
ENV["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
ENV["LD_LIBRARY_PATH"] = "/home/user/openssl111/lib"

PROBES = [
    ("majalla",  "الحرف المشدد يتكون أصـله من حرفين", 20.0),
    ("majallab", "حكم النون والميم المشددتين", 24.0),
    ("majallab", "معنى الحرف المشدد:", 19.98),
    ("majalla",  "الحرف المشدد", 10.02),
    ("majallab", "60", 12.0),
    ("quran",    "وَيُمَنِّيهِمْ أَمَّتُكُمْ", 19.98),
    ("arial",    "[النساء: 120]", 10.02),
    ("aptos",    "3", 6.0),
]


def build_probe(path_csv, out_docx):
    import build_docx as B
    document = docx.Document()
    body = document.element.body
    for p in list(body):
        if p.tag == qn("w:p"):
            body.remove(p)
    sect = document.sections[0]
    sect.page_width = docx.shared.Emu(B.pt2emu(B.PAGE_W))
    sect.page_height = docx.shared.Emu(B.pt2emu(B.PAGE_H))
    for prop in ["top_margin", "bottom_margin", "left_margin",
                 "right_margin", "header_distance", "footer_distance"]:
        setattr(sect, prop, docx.shared.Emu(0))
    src = open(B.__file__).read()
    m = re.search(r"nsdecl = \((.*?)\)\n", src, re.S)
    nsdecl = eval("(" + m.group(1) + ")")

    y = 60.0
    docpr = 5000
    meta = []
    p_xml = (f'<w:p {nsdecl}><w:pPr><w:spacing w:before="0" w:after="0" '
             'w:line="20" w:lineRule="exact"/><w:rPr><w:sz w:val="2"/>'
             '</w:rPr></w:pPr>')
    for tag, text, size in PROBES:
        inner = B.run_xml(text, tag, size, "#000000")
        w = 260.0
        h = size * B.BOX_H_MULT
        ls = size * B.LINE_MULT
        p_xml += B.textbox_xml(docpr, 80.0, y, w, h, inner, ls, "right")
        meta.append({"tag": tag, "size": size, "text": text,
                     "x": 80.0, "top": y, "w": w, "h": h, "ls": ls})
        y += h + 24
        docpr += 1
    p_xml += '</w:p>'
    body.insert_element_before(docx.oxml.parse_xml(p_xml), 'w:sectPr')
    document.save(out_docx)
    with open(path_csv, "w") as fh:
        for m_ in meta:
            fh.write(json.dumps(m_, ensure_ascii=False) + "\n")
    print("probe docx ->", out_docx)


def measure(meta_path, pdf_path):
    import pymupdf as fitz
    metas = [json.loads(l) for l in open(meta_path, encoding="utf-8")]
    doc = fitz.open(pdf_path)
    spans = []
    for b in doc[0].get_text("rawdict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                if not s.get("chars"):
                    continue
                txt = "".join(c["c"] for c in s["chars"]).strip()
                if not txt:
                    continue
                spans.append((s["bbox"], s.get("origin"),
                              s["font"], txt))
    for m in metas:
        cy = m["top"] + m["h"] / 2
        cx = 80 + m["w"] / 2
        best, bd = None, 1e9
        for bbox, origin, font, txt in spans:
            if origin is None:
                continue
            d = abs(origin[1] - cy)
            if d < bd:
                bd, best = d, (origin, font, txt, bbox)
        if best:
            (origin, font, txt, bbox) = best
            dy = origin[1] - m["top"]
            rel = dy / m["size"]
            print(f"{m['tag']:9s} size={m['size']:6.2f} ls={m['ls']:.1f} "
                  f"top={m['top']:6.1f} -> baseline+{dy:6.2f} "
                  f"({rel:.3f}*size)  font={font:28s} txt={txt[:18]!r}")
        else:
            print(f"{m['tag']:9s} size={m['size']:6.2f} NOT FOUND")


if __name__ == "__main__":
    out_docx = "/tmp/calib/probe.docx"
    os.makedirs("/tmp/calib", exist_ok=True)
    build_probe("/tmp/calib/meta.csv", out_docx)
    r = subprocess.run(
        [VENV_PY, "-c",
         "import warnings; warnings.filterwarnings('ignore');"
         "import aspose.words as aw;"
         "fs=aw.fonts.FontSettings();"
         "fs.set_fonts_folder('/usr/local/share/fonts/book',False);"
         "o=aw.loading.LoadOptions(); o.font_settings=fs;"
         "doc=aw.Document(r'''/tmp/calib/probe.docx''', o);"
         "doc.save(r'''/tmp/calib/probe.pdf''')"],
        env=ENV, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-1500:], r.stderr[-1500:])
        sys.exit(1)
    measure("/tmp/calib/meta.csv", "/tmp/calib/probe.pdf")
