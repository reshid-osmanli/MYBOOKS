# -*- coding: utf-8 -*-
"""
Custom PDF text decoder for the MYBOOKS "رتل" books.

The PDFs contain Arabic text written with Type0 (Identity-H) CID fonts. The
producer (MS Word) wrote a ToUnicode CMap per font (authoritative CID->unicode)
and positioned every glyph explicitly (Tj + TD, or TJ with kerning). Glyph
streams are in VISUAL left-to-right order.

Decoding:
  CID -> unicode via the font's ToUnicode CMap (primary), with glyph-name/
  cmap/GSUB reversal as fallback.
Assembly:
  per-glyph device positions -> clusters (marks share their base letter's x)
  -> lines (by y) -> clusters ordered right-to-left -> logical Arabic text.
"""

import re
import io
import unicodedata
from collections import defaultdict

import fitz
from fontTools.ttLib import TTFont


class FontMap:
    """CID -> unicode mapping for one font, with ToUnicode as primary."""

    def __init__(self, doc, xref, to_unicode=None, override=None):
        self.doc = doc
        self.xref = xref
        self.tu = to_unicode or {}
        self.override = override or {}
        self.gid2chars = defaultdict(set)
        self.upm = 1000
        self.advances = {}
        try:
            data = doc.extract_font(xref)
            buf = data[3]
            self.ttf = TTFont(io.BytesIO(buf), lazy=True)
            self._build()
        except Exception as e:
            print(f"  !! font xref={xref} failed: {e}")
            self.ttf = None

    def _build(self):
        ttf = self.ttf
        self.upm = ttf["head"].unitsPerEm or 1000
        # advances
        order = ttf.getGlyphOrder()
        hmtx = ttf["hmtx"]
        for gid, name in enumerate(order):
            try:
                self.advances[gid] = hmtx[name][0]
            except Exception:
                pass
        # cmap
        cmap = ttf.getBestCmap() or {}
        for cp, name in cmap.items():
            try:
                gid = ttf.getGlyphID(name)
            except Exception:
                continue
            ch = chr(cp)
            self.gid2chars[gid].add(ch)
            n = unicodedata.normalize("NFKC", ch)
            if n:
                self.gid2chars[gid].add(n)
        # glyph names
        for gid, name in enumerate(order):
            m = re.match(r"^uni([0-9A-Fa-f]{4,6})$", name)
            if m:
                ch = chr(int(m.group(1), 16))
                self.gid2chars[gid].add(ch)
                n = unicodedata.normalize("NFKC", ch)
                if n:
                    self.gid2chars[gid].add(n)

    def char_for(self, cid):
        if cid in self.override:
            return self.override[cid]
        if cid in self.tu:
            return self.tu[cid]
        cands = self.gid2chars.get(cid)
        if not cands:
            return None
        for c in cands:
            if len(c) == 1 and 0x0600 <= ord(c) <= 0x06FF:
                return c
        return sorted(cands, key=lambda c: (len(c) != 1, len(c)))[0]

    def advance(self, cid):
        return self.advances.get(cid, 0)


# ---------------- content stream interpreter ----------------

def decode_string_bytes(b):
    out = []
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5C:
            n = b[i + 1] if i + 1 < len(b) else 0x5C
            if n == 0x6E: out.append(0x0A); i += 2; continue
            if n == 0x72: out.append(0x0D); i += 2; continue
            if n == 0x74: out.append(0x09); i += 2; continue
            if n == 0x62: out.append(0x08); i += 2; continue
            if n == 0x66: out.append(0x0C); i += 2; continue
            if n == 0x0A: i += 2; continue
            if n == 0x0D:
                i += 2
                if i < len(b) and b[i] == 0x0A: i += 1
                continue
            if n in (0x28, 0x29, 0x5C):
                out.append(n); i += 2; continue
            m = re.match(rb"([0-7]{1,3})", b[i + 1:i + 4])
            if m:
                out.append(int(m.group(1), 8) & 0xFF)
                i += 1 + len(m.group(1))
                continue
            out.append(n); i += 2; continue
        out.append(c)
        i += 1
    return bytes(out)


def tokenize(data):
    i, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c in b" \t\r\n\f\x00":
            i += 1
            continue
        if c == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == b"[":
            yield ("array_begin", None); i += 1; continue
        if c == b"]":
            yield ("array_end", None); i += 1; continue
        if c == b"<":
            if data[i + 1:i + 2] == b"<":
                yield ("dict_begin", None); i += 2; continue
            j = data.find(b">", i + 1)
            hs = data[i + 1:j]
            try:
                raw_hex = bytes.fromhex(hs.decode("ascii"))
            except Exception:
                raw_hex = b""
            yield ("hexstr", raw_hex); i = j + 1; continue
        if c == b">":
            if data[i + 1:i + 2] == b">":
                yield ("dict_end", None); i += 2; continue
            i += 1; continue
        if c == b"(":
            depth, j = 1, i + 1
            while j < n and depth:
                if data[j:j + 1] == b"\\":
                    j += 2
                    continue
                if data[j:j + 1] == b"(": depth += 1
                elif data[j:j + 1] == b")": depth -= 1
                j += 1
            yield ("str", decode_string_bytes(data[i + 1:j - 1])); i = j; continue
        if c == b"/":
            m = re.match(rb"/[A-Za-z0-9_.+#*-]*", data[i:])
            yield ("name", m.group(0)[1:]); i += len(m.group(0)); continue
        m = re.match(rb"[+-]?(?:\d+\.?\d*|\.\d+)", data[i:])
        if m:
            yield ("num", float(m.group(0))); i += len(m.group(0)); continue
        m = re.match(rb"[A-Za-z*'\"]+", data[i:])
        if m:
            yield ("op", m.group(0).decode()); i += len(m.group(0)); continue
        i += 1


class GlyphItem:
    __slots__ = ("chars", "x", "y", "font", "size", "is_mark", "width", "cid",
                 "attached_marks", "_attached")

    def __init__(self, chars, x, y, font, size, is_mark, width, cid=None):
        self.chars = chars
        self.x = x
        self.y = y
        self.font = font
        self.size = size
        self.is_mark = is_mark
        self.width = width
        self.cid = cid


class PageDecoder:
    def __init__(self, doc, fontmaps):
        self.doc = doc
        self.fontmaps = fontmaps
        self.items = []
        self.unknown = defaultdict(int)

    def decode_page(self, pno):
        page = self.doc[pno]
        fres = {}
        for xref, ext, ftype, name, ref, enc in page.get_fonts():
            fres[ref] = {"xref": xref, "type": ftype}
        for xref in page.get_contents():
            data = self.doc.xref_stream(xref)
            if not data:
                continue
            self._walk(data, fres)

    def _walk(self, data, fres):
        toks = list(tokenize(data))
        n = len(toks)
        pos = 0
        stack = []
        tstate = {"tm": None, "tlm": None, "size": 12, "leading": 0, "font": None, "tpos": None}
        cur_ctm = (1, 0, 0, 1, 0, 0)
        while pos < n:
            kind = toks[pos][0]
            if kind in ("name", "num", "str", "hexstr", "array_begin"):
                operands = []
                while pos < n:
                    kk = toks[pos][0]
                    if kk in ("name", "num", "str", "hexstr"):
                        operands.append(toks[pos]); pos += 1; continue
                    if kk == "array_begin":
                        depth = 1
                        arr = []
                        pos += 1
                        while pos < n and depth:
                            k2, v2 = toks[pos]
                            if k2 == "array_begin": depth += 1
                            elif k2 == "array_end":
                                depth -= 1
                                if depth == 0:
                                    pos += 1
                                    break
                            arr.append((k2, v2))
                            pos += 1
                        operands.append(("array", arr))
                        continue
                    if kk == "dict_begin":
                        depth = 1
                        pos += 1
                        while pos < n and depth:
                            if toks[pos][0] == "dict_begin": depth += 1
                            elif toks[pos][0] == "dict_end":
                                depth -= 1
                                if depth == 0:
                                    pos += 1
                                    break
                            pos += 1
                        continue
                    break
                if pos >= n:
                    break
                k2, v2 = toks[pos]
                if k2 != "op":
                    pos += 1
                    continue
                self._apply_op(v2, operands, fres, stack, tstate, cur_ctm)
                pos += 1
                continue
            if kind == "dict_begin":
                depth = 1
                pos += 1
                while pos < n and depth:
                    if toks[pos][0] == "dict_begin": depth += 1
                    elif toks[pos][0] == "dict_end":
                        depth -= 1
                    pos += 1
                continue
            if kind == "array_begin":
                depth = 1
                pos += 1
                while pos < n and depth:
                    if toks[pos][0] == "array_begin": depth += 1
                    elif toks[pos][0] == "array_end":
                        depth -= 1
                    pos += 1
                continue
            if kind == "op":
                self._apply_op(toks[pos][1], [], fres, stack, tstate, cur_ctm)
                pos += 1
                continue
            pos += 1

    def _apply_op(self, op, ops, fres, stack, tstate, cur_ctm):
        if op == "q":
            stack.append((dict(tstate), cur_ctm))
        elif op == "Q":
            if stack:
                t, c = stack.pop()
                tstate.update(t)
                cur_ctm = c
        elif op == "cm":
            if len(ops) == 6:
                m = tuple(float(o[1]) for o in ops)
                cur_ctm = self._mm(m, cur_ctm)
        elif op == "Tf":
            fname = ops[0][1].decode() if ops[0][0] == "name" else ops[0][1]
            tstate["font"] = fname
            tstate["size"] = float(ops[1][1])
        elif op == "Tm":
            if len(ops) == 6:
                m = tuple(float(o[1]) for o in ops)
                tstate["tm"] = m
                tstate["tlm"] = m
        elif op == "Td":
            if len(ops) == 2:
                tx, ty = float(ops[0][1]), float(ops[1][1])
                tstate["tlm"] = self._translate(tstate["tlm"], tx, ty)
                tstate["tm"] = tstate["tlm"]
        elif op == "TD":
            if len(ops) == 2:
                tx, ty = float(ops[0][1]), float(ops[1][1])
                tstate["leading"] = -ty
                tstate["tlm"] = self._translate(tstate["tlm"], tx, ty)
                tstate["tm"] = tstate["tlm"]
        elif op == "T*":
            tstate["tlm"] = self._translate(tstate["tlm"], 0, -tstate["leading"])
            tstate["tm"] = tstate["tlm"]
        elif op in ("Tj", "TJ", "'", '"'):
            if op == "'":
                tstate["tlm"] = self._translate(tstate["tlm"], 0, -tstate["leading"])
                tstate["tm"] = tstate["tlm"]
                self._emit_text(ops, tstate, fres, cur_ctm)
            elif op == '"':
                tstate["tlm"] = self._translate(tstate["tlm"], 0, -tstate["leading"])
                tstate["tm"] = tstate["tlm"]
                self._emit_text(ops, tstate, fres, cur_ctm)
            else:
                self._emit_text(ops, tstate, fres, cur_ctm)

    def _translate(self, tm, tx, ty):
        if tm is None:
            return (1, 0, 0, 1, tx, ty)
        a, b, c, d, e, f = tm
        return (a, b, c, d, e + a * tx + c * ty, f + b * tx + d * ty)

    @staticmethod
    def _mm(m1, m2):
        a1, b1, c1, d1, e1, f1 = m1
        a2, b2, c2, d2, e2, f2 = m2
        return (
            a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2,
        )

    def _emit_text(self, ops, tstate, fres, cur_ctm):
        font = tstate["font"]
        if font is None:
            return
        fd = fres.get(font)
        if fd is None:
            return
        xref = fd["xref"]
        is_type0 = fd["type"] == "Type0"
        fm = self.fontmaps.get(xref)

        items = []
        if ops and ops[0][0] == "array":
            for k, v in ops[0][1]:
                if k in ("str", "hexstr"):
                    items.append(("s", v))
                elif k == "num":
                    items.append(("k", v))
        else:
            for k, v in ops:
                if k in ("str", "hexstr"):
                    items.append(("s", v))

        tm = tstate["tm"]
        if tm is None:
            tm = (1, 0, 0, 1, 0, 0)
        a, b, c, d, e, f = tm
        ca, cb, cc, cd, ce, cf = cur_ctm
        # text-space start (0,0) transformed to device
        ox = ca * e + cc * f + ce
        oy = cb * e + cd * f + cf
        fs = tstate["size"]
        # advance scaling: text unit (1pt) -> device
        sx = (ca * a + cc * b)
        sy = (cb * a + cd * b)
        sxx = abs(sx) if abs(sx) > 1e-6 else abs(sy)
        tx = 0.0  # per-op text position: Tm is authoritative between ops
        for kind, v in items:
            if kind == "k":
                tx -= float(v) / 1000.0 * fs * sxx  # kerning in thousandths of text units
                continue
            if is_type0:
                for i in range(0, len(v) - 1, 2):
                    cid = (v[i] << 8) | v[i + 1]
                    ch = fm.char_for(cid) if fm else None
                    if ch is None:
                        ch = "\uFFFD"
                        self.unknown[(xref, cid)] += 1
                    # glyph device position
                    gx = ox + (ca * a + cc * b) * (tx / fs) if False else ox + sx * tx + sy * 0
                    gy = oy + (cb * a + cd * b) * tx
                    w = (fm.advance(cid) if fm else 0) / fm.upm * fs if fm else 0
                    is_mark = w < 0.5
                    self.items.append(GlyphItem(ch, gx, gy, font, fs, is_mark, w, cid=cid))
                    tx += w
            else:
                for i in range(len(v)):
                    ch = chr(v[i])
                    gx = ox + sx * tx
                    gy = oy + (cb * a + cd * b) * tx
                    w = fs * 0.5
                    self.items.append(GlyphItem(ch, gx, gy, font, fs, False, w))
                    tx += w
