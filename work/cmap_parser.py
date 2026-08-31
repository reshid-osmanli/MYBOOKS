# -*- coding: utf-8 -*-
"""Parser for PDF ToUnicode CMaps (bfchar/bfrange sections)."""
import re


def _uni_from_hex(hx):
    try:
        raw = bytes.fromhex(hx)
    except Exception:
        return None
    try:
        return raw.decode("utf-16-be")
    except Exception:
        return None


def parse_cmap(data):
    """Parse CMap stream bytes -> dict int(cid) -> str(unicode)."""
    if isinstance(data, bytes):
        s = data.decode("latin-1")
    else:
        s = data
    mapping = {}

    # ---- bfchar sections ----
    for m in re.finditer(r"\d+\s+beginbfchar(.*?)endbfchar", s, re.S):
        body = m.group(1)
        hexes = re.findall(r"<([0-9A-Fa-f]+)>", body)
        for j in range(0, len(hexes) - 1, 2):
            cid = int(hexes[j], 16)
            uni = _uni_from_hex(hexes[j + 1])
            if uni is not None:
                mapping[cid] = uni

    # ---- bfrange sections ----
    for m in re.finditer(r"\d+\s+beginbfrange(.*?)endbfrange", s, re.S):
        body = m.group(1)
        # split into tokens: single <hex> or [ <hex> ... ]
        pos = 0
        toks = []
        while pos < len(body):
            c = body[pos]
            if c == "<":
                j = body.find(">", pos)
                toks.append(body[pos + 1:j])
                pos = j + 1
            elif c == "[":
                j = body.find("]", pos)
                inner = re.findall(r"<([0-9A-Fa-f]+)>", body[pos:j])
                toks.append(("arr", inner))
                pos = j + 1
            else:
                pos += 1
        j = 0
        while j + 2 < len(toks):
            lo, hi, dst = toks[j], toks[j + 1], toks[j + 2]
            if isinstance(lo, tuple) or isinstance(hi, tuple):
                j += 1
                continue
            try:
                lo_i, hi_i = int(lo, 16), int(hi, 16)
            except Exception:
                j += 1
                continue
            j += 3
            if isinstance(dst, tuple):
                vals = dst[1]
                k = 0
                for cid in range(lo_i, hi_i + 1):
                    if k >= len(vals):
                        break
                    uni = _uni_from_hex(vals[k])
                    k += 1
                    if uni is not None:
                        mapping[cid] = uni
            else:
                try:
                    base = int(dst, 16)
                except Exception:
                    continue
                if len(dst) > 4:
                    # PDF 32000-1, 9.10.3: with a *string* destination the
                    # last byte of the string is incremented for every cid,
                    # so "A" "C" <004100420043> gives A, AB, ABC, not ABC
                    # three times.  (Odd hex strings are truncated.)
                    raw = bytes.fromhex(dst[:len(dst) - len(dst) % 2])
                    for offset, cid in enumerate(range(lo_i, hi_i + 1)):
                        b = bytearray(raw)
                        if b:
                            b[-1] = (b[-1] + offset) & 0xFF
                        try:
                            mapping[cid] = b.decode("utf-16-be")
                        except Exception:
                            pass
                    continue
                for cid in range(lo_i, hi_i + 1):
                    try:
                        mapping[cid] = chr(base + (cid - lo_i))
                    except Exception:
                        pass
    return mapping
