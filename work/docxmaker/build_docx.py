# -*- coding: utf-8 -*-
"""
Build a pixel-faithful DOCX replica from the mapped layout JSON.

Per PDF page: one (nearly empty) body paragraph carrying
  * a behind-text full-page background picture (the text-free page render),
  * one anchored text-box per text segment, exact position/size/font/color.

Fonts (Sakkal Majalla regular+bold, KFGQPC Quran font) are embedded in the
package so the document looks the same on any machine.
"""
import json
import os
import sys
import shutil
import zipfile

import docx
import docx.oxml
from docx.oxml.ns import qn, nsmap
from docx.image.image import Image


EMU_PER_PT = 12700
PAGE_W = 595.28
PAGE_H = 841.92

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "fonts")

# tag -> (docx family, bold flag, embed role)
FONT_MAP = {
    "majalla":   ("Sakkal Majalla", False, "embedRegular"),
    "majallab":  ("Sakkal Majalla", True,  "embedBold"),
    "quran":     ("KFGQPC Uthmanic Script HAFS", False, "embedRegular"),
    "arial":     ("Arial", False, None),
    "arialb":    ("Arial", True, None),
    "tnr":       ("Times New Roman", False, None),
    "tnrb":      ("Times New Roman", True, None),
    "aptos":     ("Arial", False, None),
    "aptosb":    ("Arial", True, None),
    "aptosi":    ("Arial", False, None),
    "calibri":   ("Calibri", False, None),
    "courier":   ("Courier New", False, None),
    "cambria":   ("Cambria Math", False, None),
    "frutiger":  ("Arial", False, None),
    "wingdings": ("Wingdings", False, None),
    "symbol":    ("Symbol", False, None),
}

# legacy symbol-font char code for w:sym (PUA F0xx)
SYM_CODE = {
    "•": "F0B7", "◆": "F0A8", "\uf0a8": "F0A8", "\uf0b7": "F0B7",
    "√": "F0D6", "○": "F06F",
}

VERIFY_COMPAT = False   # swap fonts that Linux renderers can't shape/find

# vertical calibration: baseline offset from box top = a*size + b  (pt)
DY_MAP = {
    "majalla":  (0.860, 0.0),
    "majallab": (0.860, 0.0),
    "quran":    (0.980, 0.0),
}
DY_DEFAULT = (0.78, 0.0)     # arial & friends: ascent ~75-80%

LINE_MULT = 1.30             # exact line-spacing = size * LINE_MULT
BOX_H_MULT = 2.20            # box height = size * BOX_H_MULT (overflows shown)
BASE_FRACTION = 0.8          # baseline sits at top + BASE_FRACTION * lineExact


def esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def pt2emu(v):
    return int(round(v * EMU_PER_PT))


def map_tag(tag):
    fam, bold, role = FONT_MAP.get(tag, ("Sakkal Majalla", False, None))
    if VERIFY_COMPAT and tag == "quran":
        fam = "Amiri Quran"
    return fam, bold, role


def run_props_xml(tag, size, color, extra_italic=False):
    fam, bold, _ = map_tag(tag)
    sz = int(round(size * 2))
    col = (color or "#000000").lstrip("#").upper()
    x = [
        '<w:rFonts w:ascii="%s" w:hAnsi="%s" w:cs="%s"/>' % (fam, fam, fam),
        '<w:color w:val="%s"/>' % col,
        '<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (sz, sz),
        '<w:lang w:bidi="ar-SA"/>',
    ]
    if bold:
        x.insert(1, '<w:b/><w:bCs/>')
    if extra_italic:
        x.insert(1, '<w:i/><w:iCs/>')
    return "".join(x)


SYM_COMPAT = {"\uf0a8": "◆", "F0A8": "◆", "•": "•", "F0B7": "•",
              "\uf0b7": "•", "o": "○", "√": "√", "○": "○"}


def run_xml(text, tag, size, color):
    fam, bold, _ = map_tag(tag)
    if VERIFY_COMPAT and tag in ("symbol", "wingdings"):
        props = run_props_xml("arial", size, color)
        out = []
        for ch in text:
            code = SYM_CODE.get(ch)
            key = code if code is not None else ch
            g = SYM_COMPAT.get(key, SYM_COMPAT.get(ch, ch))
            out.append('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
                       % (props, esc(g)))
        return "".join(out)
    props = run_props_xml(tag, size, color)
    if tag in ("symbol", "wingdings"):
        out = []
        for ch in text:
            code = SYM_CODE.get(ch)
            if code is None:
                o = ord(ch)
                if 0xF000 <= o <= 0xF0FF:
                    code = "%04X" % o
                elif o <= 0xFF:
                    code = "F0%02X" % o
                else:
                    # not representable in legacy font: emit as plain run
                    out.append('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
                               % (run_props_xml("arial", size, color), esc(ch)))
                    continue
            out.append('<w:r><w:rPr>%s</w:rPr><w:sym w:font="%s" w:char="%s"/></w:r>'
                       % (props, fam, code))
        return "".join(out)
    return ('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % (props, esc(text)))


def segment_runs(seg):
    """Merge consecutive clusters with identical style into runs."""
    out_runs = []
    prev_key = None
    buf = ""
    for c in seg["clusters"]:
        key = (c["font"], c["size"], c.get("color"))
        if key != prev_key and buf:
            out_runs.append((buf, *prev_key))
            buf = ""
        buf += c.get("text_final", c["text"])
        prev_key = key
    if buf:
        out_runs.append((buf, *prev_key))
    xml = []
    for text, tag, size, color in out_runs:
        if not text:
            continue
        xml.append(run_xml(text, tag, size, color))
    return "".join(xml)


JC_RTL_FLIP = {"right": "left", "left": "right", "center": "center",
               "both": "both"}


def textbox_xml(docpr_id, x, y, w, h, inner_xml, line_spacing_exact_pt,
                jc="right"):
    # with <w:bidi/> the jc value is read in logical order: jc="left" then
    # means *visual* right.  Callers pass VISUAL alignment.
    jc = JC_RTL_FLIP.get(jc, jc)
    line_twips = int(round(line_spacing_exact_pt * 20))
    return f"""
<w:r>
 <w:drawing>
  <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0"
     relativeHeight="{5000 + docpr_id}" behindDoc="0" locked="0"
     layoutInCell="1" allowOverlap="1">
   <wp:simplePos x="0" y="0"/>
   <wp:positionH relativeFrom="page"><wp:posOffset>{pt2emu(x)}</wp:posOffset></wp:positionH>
   <wp:positionV relativeFrom="page"><wp:posOffset>{pt2emu(y)}</wp:posOffset></wp:positionV>
   <wp:extent cx="{pt2emu(w)}" cy="{pt2emu(h)}"/>
   <wp:effectExtent l="0" t="0" r="0" b="0"/>
   <wp:wrapNone/>
   <wp:docPr id="{docpr_id}" name="tb{docpr_id}"/>
   <wp:cNvGraphicFramePr/>
   <a:graphic>
    <a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
     <wps:wsp>
      <wps:cNvSpPr txBox="1"/>
      <wps:spPr>
       <a:xfrm><a:off x="0" y="0"/><a:ext cx="{pt2emu(w)}" cy="{pt2emu(h)}"/></a:xfrm>
       <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
       <a:noFill/>
       <a:ln><a:noFill/></a:ln>
      </wps:spPr>
      <wps:txbx>
       <w:txbxContent>
        <w:p>
         <w:pPr>
          <w:bidi w:val="1"/>
          <w:spacing w:before="0" w:after="0" w:line="{line_twips}" w:lineRule="exact"/>
          <w:jc w:val="{jc}"/>
         </w:pPr>
         {inner_xml}
        </w:p>
       </w:txbxContent>
      </wps:txbx>
      <wps:bodyPr rot="0" spcFirstLastPara="0" vertOverflow="overflow"
        horzOverflow="overflow" vert="horz" wrap="none"
        lIns="0" tIns="0" rIns="0" bIns="0" numCol="1" spcCol="0"
        rtlCol="0" anchor="t"/>
     </wps:wsp>
    </a:graphicData>
   </a:graphic>
  </wp:anchor>
 </w:drawing>
</w:r>"""


def bg_image_xml(docpr_id, rid, name):
    return f"""
<w:r>
 <w:drawing>
  <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0"
     relativeHeight="1" behindDoc="1" locked="0" layoutInCell="1"
     allowOverlap="1">
   <wp:simplePos x="0" y="0"/>
   <wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>
   <wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>
   <wp:extent cx="{pt2emu(PAGE_W)}" cy="{pt2emu(PAGE_H)}"/>
   <wp:effectExtent l="0" t="0" r="0" b="0"/>
   <wp:wrapNone/>
   <wp:docPr id="{docpr_id}" name="{name}"/>
   <wp:cNvGraphicFramePr/>
   <a:graphic>
    <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
     <pic:pic>
      <pic:nvPicPr>
       <pic:cNvPr id="{docpr_id}" name="{name}"/>
       <pic:cNvPicPr/>
      </pic:nvPicPr>
      <pic:blipFill>
       <a:blip r:embed="{rid}"/>
       <a:stretch><a:fillRect/></a:stretch>
      </pic:blipFill>
      <pic:spPr>
       <a:xfrm><a:off x="0" y="0"/><a:ext cx="{pt2emu(PAGE_W)}" cy="{pt2emu(PAGE_H)}"/></a:xfrm>
       <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
      </pic:spPr>
     </pic:pic>
    </a:graphicData>
   </a:graphic>
  </wp:anchor>
 </w:drawing>
</w:r>"""


def build(mapped_json, bg_dir, out_docx, embed=True):
    lay = json.load(open(mapped_json, encoding="utf-8"))

    document = docx.Document()
    # default docx opens with an empty paragraph; strip it
    body = document.element.body
    for p in list(body):
        if p.tag == qn("w:p"):
            body.remove(p)

    # section: A4, minimal margins (content is page-anchored anyway)
    sect = document.sections[0]
    sect.page_width = docx.shared.Emu(pt2emu(PAGE_W))
    sect.page_height = docx.shared.Emu(pt2emu(PAGE_H))
    sect.top_margin = docx.shared.Emu(pt2emu(0))
    sect.bottom_margin = docx.shared.Emu(pt2emu(0))
    sect.left_margin = docx.shared.Emu(pt2emu(0))
    sect.right_margin = docx.shared.Emu(pt2emu(0))
    sect.header_distance = docx.shared.Emu(pt2emu(0))
    sect.footer_distance = docx.shared.Emu(pt2emu(0))

    docpr = 100
    nsdecl = ('xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
              'xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" '
              'xmlns:cx1="http://schemas.microsoft.com/office/drawing/2015/9/8/chartex" '
              'xmlns:cx2="http://schemas.microsoft.com/office/drawing/2015/10/21/chartex" '
              'xmlns:cx3="http://schemas.microsoft.com/office/drawing/2016/5/9/chartex" '
              'xmlns:cx4="http://schemas.microsoft.com/office/drawing/2016/5/10/chartex" '
              'xmlns:cx5="http://schemas.microsoft.com/office/drawing/2016/5/11/chartex" '
              'xmlns:cx6="http://schemas.microsoft.com/office/drawing/2016/5/12/chartex" '
              'xmlns:cx7="http://schemas.microsoft.com/office/drawing/2016/5/13/chartex" '
              'xmlns:cx8="http://schemas.microsoft.com/office/drawing/2016/5/14/chartex" '
              'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
              'xmlns:aink="http://schemas.microsoft.com/office/drawing/2016/ink" '
              'xmlns:am3d="http://schemas.microsoft.com/office/drawing/2017/model3d" '
              'xmlns:o="urn:schemas-microsoft-com:office:office" '
              'xmlns:oel="http://schemas.microsoft.com/office/2019/extlst" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
              'xmlns:v="urn:schemas-microsoft-com:vml" '
              'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
              'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
              'xmlns:w10="urn:schemas-microsoft-com:office:word" '
              'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
              'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
              'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
              'xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex" '
              'xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid" '
              'xmlns:w16="http://schemas.microsoft.com/office/word/2018/wordml" '
              'xmlns:w16du="http://schemas.microsoft.com/office/word/2023/wordml/word16du" '
              'xmlns:w16sdtdh="http://schemas.microsoft.com/office/word/2020/wordml/sdtdatahash" '
              'xmlns:w16se="http://schemas.microsoft.com/office/word/2015/wordml/symex" '
              'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
              'xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" '
              'xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" '
              'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
              'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" ')

    for i, page in enumerate(lay["pages"]):
        pbb = '<w:pageBreakBefore/>' if i > 0 else ''
        p_xml = (
            f'<w:p {nsdecl}>'
            '<w:pPr>'
            f'{pbb}'
            '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
            '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="2"/></w:rPr>'
            '</w:pPr>')
        # background image
        bg_path = os.path.join(bg_dir, f"bg_{page['page']:03d}.jpg")
        if os.path.exists(bg_path):
            rid, image = document.part.get_or_add_image(bg_path)
            docpr += 1
            p_xml += bg_image_xml(docpr, rid, f"bg{page['page']}")
        # text segments
        for line in page["lines"]:
            for seg in line["segments"]:
                inner = segment_runs(seg)
                if not inner:
                    continue
                size = seg["size"]
                dy = BASE_FRACTION * size * LINE_MULT
                x0, x1, yb = seg["x0"], seg["x1"], seg["y"]
                pw = x1 - x0
                cx = (x0 + x1) / 2.0
                centered = (abs(cx - PAGE_W / 2) < 28.0 and pw > 90)
                tiny = pw < 26.0
                h = size * BOX_H_MULT
                top = yb - dy
                if centered:
                    w = pw + 16
                    x = cx - w / 2
                    jc = "center"
                elif tiny:
                    w = pw + 10
                    x = cx - w / 2
                    jc = "center"
                else:
                    w = pw + 10
                    x = x1 + 1.2 - w
                    jc = "right"
                if x < 0:
                    x = 0.0
                if x + w > PAGE_W:
                    w = PAGE_W - x
                if top < 0:
                    top = 0.0
                ls = size * LINE_MULT
                docpr += 1
                p_xml += textbox_xml(docpr, x, top, w, h, inner, ls,
                                     jc)
        p_xml += '</w:p>'
        el = docx.oxml.parse_xml(p_xml)
        body.insert_element_before(el, 'w:sectPr')

    core = document.core_properties
    core.title = "رتل مصمم — نسخة Word مطابقة للـ PDF"
    core.comments = "Generated: pixel-faithful layout replica"

    document.save(out_docx)
    if embed:
        embed_fonts(out_docx)
    print("wrote", out_docx)


def embed_fonts(docx_path):
    """Post-process the docx zip: add embedded font parts."""
    fonts = [
        ("Sakkal Majalla",
         os.path.join(FONTS_DIR, "majalla.ttf"),
         os.path.join(FONTS_DIR, "majallab.ttf")),
        ("KFGQPC Uthmanic Script HAFS",
         os.path.join(FONTS_DIR, "UthmanicHafs1_Ver09.ttf"), None),
    ]
    tmp = docx_path + ".tmp"
    with zipfile.ZipFile(docx_path, "r") as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        font_entries = []   # (arcname, data)
        rel_entries = []    # (rid, arcname)
        ridno = 100
        # read + patch [Content_Types].xml
        ct = zin.read("[Content_Types].xml").decode("utf-8")
        if "fntdata" not in ct:
            ct = ct.replace("</Types>",
                            '<Default Extension="fntdata" ContentType='
                            '"application/vnd.openxmlformats-officedocument.'
                            'obfuscatedFont"/></Types>')
        # fontTable
        ft = zin.read("word/fontTable.xml").decode("utf-8")
        add = ""
        for idx, (fam, reg, bold) in enumerate(fonts):
            rreg = None
            rbold = None
            if reg and os.path.exists(reg):
                arc = f"fonts/font{2*idx+1}.fntdata"
                font_entries.append((f"word/fonts/font{2*idx+1}.fntdata",
                                     open(reg, "rb").read()))
                ridno += 1
                rel_entries.append((f"rId{ridno}",
                                    f"fonts/font{2*idx+1}.fntdata"))
                rreg = f"rId{ridno}"
            if bold and os.path.exists(bold):
                font_entries.append((f"word/fonts/font{2*idx+2}.fntdata",
                                     open(bold, "rb").read()))
                ridno += 1
                rel_entries.append((f"rId{ridno}",
                                    f"fonts/font{2*idx+2}.fntdata"))
                rbold = f"rId{ridno}"
            block = f'<w:font w:name="{fam}"><w:family w:val="auto"/>' \
                    '<w:pitch w:val="variable"/><w:charset w:val="B2"/>'
            if rreg:
                block += f'<w:embedRegular r:id="{rreg}"/>'
            if rbold:
                block += f'<w:embedBold r:id="{rbold}"/>'
            block += '</w:font>'
            add += block
        if add:
            ft = ft.replace("</w:fonts>", add + "</w:fonts>")
        # fontTable rels
        frels = ('<?xml version="1.0" encoding="UTF-8" '
                 'standalone="yes"?>\n'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/'
                 'package/2006/relationships">'
                 + "".join(
                     f'<Relationship Id="{rid}" Type="http://schemas.'
                     'openxmlformats.org/officeDocument/2006/relationships/'
                     f'font" Target="{arc}"/>'
                     for rid, arc in rel_entries)
                 + '</Relationships>')
        # settings: enable embedding
        st = zin.read("word/settings.xml").decode("utf-8")
        if "embedTrueTypeFonts" not in st:
            st = st.replace("<w:zoom",
                            '<w:embedTrueTypeFonts/>'
                            '<w:saveSubsetFonts w:val="0"/><w:zoom', 1)
        for name in names:
            data = zin.read(name)
            if name == "[Content_Types].xml":
                data = ct.encode("utf-8")
            elif name == "word/fontTable.xml":
                data = ft.encode("utf-8")
            elif name == "word/settings.xml":
                data = st.encode("utf-8")
            zout.writestr(name, data)
        zout.writestr("word/_rels/fontTable.xml.rels", frels)
        for arc, data in font_entries:
            zout.writestr(arc, data)
    shutil.move(tmp, docx_path)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], sys.argv[3])
