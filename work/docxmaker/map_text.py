# -*- coding: utf-8 -*-
"""
Align the *final, proofread* transcription (partN_final.txt) with the
extracted layout, so every visual segment keeps its PDF geometry but carries
the FINAL corrected text.

Writes mapped JSON: same structure as layout, each segment gets
  "runs": [{text, font, size, color}], "text": final text of the segment.
"""
import json
import re
import sys
import difflib
import unicodedata

AR_DIAC = dict.fromkeys(
    [ord(c) for c in
     "ًٌٍَُِّْٰٕٔۖۗۘۙۚۛۜ۝۞ۣ۟۠ۡۢۤۧۥۦۨ۩۪ۭ۫۬ـﱞﱟ"
     ] + list(range(0x0610, 0x061B)) + list(range(0x08E3, 0x0900)), None)


def norm_key(s):
    """Aggressive key for matching: letters only, unified forms."""
    if s.strip() and set(s.replace(" ", "")) <= {"."}:
        return "\x00DOTS\x00"        # answer dots lines get their own key
    s = unicodedata.normalize("NFC", s)
    s = s.translate(AR_DIAC)
    s = re.sub(r"[\s\u0640{}()\[\].,،؛:؟!''\"\"«»\-–—/\\0-9٠١٢٣٤٥٦٧٨٩◆•▪√○]+", "", s)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ٱ", "ا").replace("ى", "ي").replace("ئ", "ي")
         .replace("ؤ", "و").replace("ة", "ه").replace("ء", "")
         .replace("ٮ", "ي"))
    return s


def parse_final(path):
    """final.txt -> dict page_no -> [line, ...] (keeping original order)."""
    pages = {}
    cur = None
    for raw in open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"\s*===== PAGE (\d+) =====\s*$", raw)
        if m:
            cur = int(m.group(1))
            pages[cur] = []
            continue
        if cur is None:
            continue
        line = raw.rstrip()
        if line.strip() == "":
            continue
        pages[cur].append(line)
    return pages


def seg_pdf_text(seg):
    out = ""
    for c in seg["clusters"]:
        if c.get("sp") and out and not out.endswith(" "):
            out += " "
        out += c["text"]
    return out


def line_pdf_text(line):
    # segments sorted logical (right to left)
    parts = [seg_pdf_text(s) for s in line["segments"]]
    t = " ".join(p for p in parts if p.strip())
    return re.sub(r"\s+([.,،؛:؟!])", r"\1", t).strip()


def align_chars(segs, final_text):
    """Distribute final_text over segs. Returns list of (seg, text)."""
    # flat char list with segment map
    flat = []
    smap = []
    parts = []
    for i, s in enumerate(segs):
        t = seg_pdf_text(s)
        if i:
            flat.append(" ")
            smap.append(None)
        for ch in t:
            flat.append(ch)
            smap.append(i)
    pdf_line = "".join(flat)

    result = {i: "" for i in range(len(segs))}
    sm = difflib.SequenceMatcher(None, pdf_line, final_text,
                                 autojunk=False)
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            for k in range(a0, a1):
                i = smap[k]
                if i is not None:
                    result[i] += final_text[b0 + (k - a0)]
        elif op == "replace":
            tgt = None
            for k in range(a0, a1):
                if smap[k] is not None:
                    tgt = smap[k]
                    break
            if tgt is None and a0 > 0:
                tgt = smap[a0 - 1]
            if tgt is None:
                tgt = 0
            result[tgt] += final_text[b0:b1]
        elif op == "insert":
            tgt = None
            if a0 > 0:
                # previous char's segment
                for k in range(a0 - 1, -1, -1):
                    if smap[k] is not None:
                        tgt = smap[k]
                        break
            if tgt is None:
                for k in range(a0 + 1, len(smap)):
                    if smap[k] is not None:
                        tgt = smap[k]
                        break
            if tgt is None:
                tgt = 0
            result[tgt] += final_text[b0:b1]
        # delete: drop
    return [(segs[i], result[i]) for i in range(len(segs))]


def distribute_line(line, final_text):
    """Distribute the final line's characters over the line's clusters
    (each cluster keeps font/geometry; text comes from the final version).
    Sets cluster['text_final'] and seg['text']."""
    # flat pdf chars with cluster refs, segments separated by a space
    flat, cmap, order = [], [], []
    for si, seg in enumerate(line["segments"]):
        first = True
        for ci, c in enumerate(seg["clusters"]):
            t = ((" " if c.get("sp") else "") + c["text"]) if not first else c["text"]
            first = False
            for ch in t:
                flat.append(ch)
                cmap.append((si, ci))
        order.append(seg)
        flat.append(" ")
        cmap.append(None)
    pdf_line = "".join(flat)
    texts = {}    # (si,ci) -> final text
    sm = difflib.SequenceMatcher(None, pdf_line, final_text, autojunk=False)
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            for k in range(a0, a1):
                ref = cmap[k]
                if ref is not None:
                    texts.setdefault(ref, "")
                    texts[ref] += final_text[b0 + (k - a0)]
        elif op == "replace":
            tgt = None
            for k in range(a0, a1):
                if cmap[k] is not None:
                    tgt = cmap[k]
                    break
            if tgt is None and a0 > 0:
                for k in range(a0 - 1, -1, -1):
                    if cmap[k] is not None:
                        tgt = cmap[k]
                        break
            if tgt is None:
                for k in range(len(cmap)):
                    if cmap[k] is not None:
                        tgt = cmap[k]
                        break
            if tgt is not None:
                texts.setdefault(tgt, "")
                texts[tgt] += final_text[b0:b1]
        elif op == "insert":
            tgt = None
            for k in range(a0 - 1, -1, -1):
                if cmap[k] is not None:
                    tgt = cmap[k]
                    break
            if tgt is None:
                for k in range(a0 + 1, len(cmap)):
                    if cmap[k] is not None:
                        tgt = cmap[k]
                        break
            if tgt is not None:
                texts.setdefault(tgt, "")
                texts[tgt] += final_text[b0:b1]
    for si, seg in enumerate(line["segments"]):
        seg_text = ""
        for ci, c in enumerate(seg["clusters"]):
            c["text_final"] = texts.get((si, ci), "")
            seg_text += c["text_final"]
        if not seg_text.strip():
            # content deleted by the proofreader's final text (e.g. the
            # footnote markers now live on their own final line): keep the
            # original pdf text so nothing vanishes visually.
            for c in seg["clusters"]:
                c["text_final"] = c["text"]
            seg_text = "".join(c["text"] for c in seg["clusters"])
        seg["text"] = seg_text


def align_lines(pdf_keys, fin_keys):
    """Optimal order-preserving alignment of two line lists.

    DP maximizing sum of per-pair similarity with gap penalty.
    Returns (matched pairs list, unmatched_pdf list, unmatched_fin list).
    """
    n, m = len(pdf_keys), len(fin_keys)
    # similarity cache
    sim = {}
    for i in range(n):
        for j in range(m):
            if not pdf_keys[i] and not fin_keys[j]:
                sim[(i, j)] = 1.0
            elif not pdf_keys[i] or not fin_keys[j]:
                sim[(i, j)] = 0.0
            else:
                sim[(i, j)] = difflib.SequenceMatcher(
                    None, pdf_keys[i], fin_keys[j]).ratio()

    def match_score(i, j):
        r = sim[(i, j)]
        return 2.0 * r - 0.6

    GAP = -0.3
    NEG = -1e9
    # dp[i][j] = best score aligning first i pdf lines with first j fin lines
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = GAP * i
        back[i][0] = "up"
    for j in range(1, m + 1):
        dp[0][j] = GAP * j
        back[0][j] = "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            ms = match_score(i - 1, j - 1)
            best = dp[i - 1][j - 1] + ms
            mv = "diag"
            # prefer gaps over pairing very dissimilar lines
            if ms < -0.15:
                if dp[i - 1][j] + GAP > best or True:
                    pass
            if dp[i - 1][j] + GAP > best:
                best = dp[i - 1][j] + GAP
                mv = "up"
            if dp[i][j - 1] + GAP > best:
                best = dp[i][j - 1] + GAP
                mv = "left"
            dp[i][j] = best
            back[i][j] = mv
    matched, un_pdf, un_fin = [], [], []
    i, j = n, m
    while i > 0 or j > 0:
        mv = back[i][j]
        if mv == "diag":
            matched.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif mv == "up":
            un_pdf.append(i - 1)
            i -= 1
        else:
            un_fin.append(j - 1)
            j -= 1
    matched.sort()
    return matched, sorted(un_pdf), sorted(un_fin)


MARKER_RE = re.compile(r"^[\s()ۖۗ◦()\d0-9٠١٢٣٤٥٦٧٨٩﴾﴿۔.ًٌٍَُِّْ]*$")


def is_marker_line(text, max_len=10):
    """Tiny line made only of digits/parens/harakat -> superscript row."""
    t = text.strip()
    if not t or len(t) > max_len:
        return False
    if not MARKER_RE.match(t):
        return False
    return any(ch.isdigit() for ch in t)


def absorb_marker_lines(page):
    """Merge marker-only lines into the nearest normal line on the PDF side
    (as trailing segments; geometry is preserved).  Page numbers at the very
    bottom of the page are left alone."""
    lines = page["lines"]
    page["lines"] = lines
    absorbed = set()
    n = len(lines)
    line_y = [min(s["y"] for s in l["segments"]) for l in lines]
    for i, l in enumerate(lines):
        txt = line_pdf_text(l)
        if not is_marker_line(txt):
            continue
        if line_y[i] > 735:     # bottom strip: page number
            continue
        # nearest other line vertically
        best, bd = None, 1e9
        for k in range(n):
            if k == i or k in absorbed:
                continue
            if is_marker_line(line_pdf_text(lines[k])) and k != i:
                continue
            d = abs(line_y[k] - line_y[i])
            if d < bd:
                bd, best = d, k
        if best is not None and bd <= 40.0:
            lines[best]["segments"].extend(l["segments"])
            absorbed.add(i)
    if absorbed:
        page["lines"] = [l for i, l in enumerate(lines) if i not in absorbed]


def _digit_sig(t):
    import collections
    return "".join(sorted(ch for ch in t if ch.isdigit()))


def map_page(page, final_lines, report):
    absorb_marker_lines(page)
    lines = page["lines"]
    pdf_texts = [line_pdf_text(l) for l in lines]
    pdf_keys = [norm_key(t) for t in pdf_texts]
    fin_keys = [norm_key(t) for t in final_lines]

    # pre-pair long marker rows (several "(n)" items on one visual row):
    # they only pair with rows sharing the exact same digit multiset
    pre_pairs = []
    pmask = [False] * len(lines)
    fmask = [False] * len(final_lines)
    p_marks = [i for i, t in enumerate(pdf_texts)
               if is_marker_line(t, max_len=40) and len(t) > 10]
    f_marks = [j for j, t in enumerate(final_lines)
               if is_marker_line(t, max_len=40) and len(t) > 10]
    if p_marks and f_marks:
        # pair in order when digit signatures agree
        fi_pool = list(f_marks)
        for i in p_marks:
            sig = _digit_sig(pdf_texts[i])
            for j in list(fi_pool):
                if _digit_sig(final_lines[j]) == sig:
                    pre_pairs.append((i, j))
                    pmask[i] = fmask[j] = True
                    fi_pool.remove(j)
                    break

    pk2 = [pdf_keys[i] for i in range(len(lines)) if not pmask[i]]
    fk2 = [fin_keys[j] for j in range(len(final_lines)) if not fmask[j]]
    idx_p = [i for i in range(len(lines)) if not pmask[i]]
    idx_f = [j for j in range(len(final_lines)) if not fmask[j]]
    matched2, un_pdf2, un_fin2 = align_lines(pk2, fk2)
    matched = [(idx_p[i], idx_f[j]) for i, j in matched2] + pre_pairs
    un_pdf = sorted(idx_p[i] for i in un_pdf2)
    un_fin = sorted(idx_f[j] for j in un_fin2)
    for i, j in matched:
        r = difflib.SequenceMatcher(None, pdf_keys[i], fin_keys[j]).ratio()
        if r < 0.45:
            report.append(f"  p{page['page']}: WEAK match r={r:.2f} "
                          f"pdf={pdf_texts[i][:45]!r} fin={final_lines[j][:45]!r}")
    # second chance: free (unordered) pairing for table/diagram regions
    still_fin = set(un_fin)
    extra_pairs = []
    used_pdf = set()
    for j in sorted(un_fin):
        best, br = None, 0.0
        for i in un_pdf:
            if i in used_pdf:
                continue
            if not pdf_keys[i] or not fin_keys[j]:
                continue
            r = difflib.SequenceMatcher(None, pdf_keys[i],
                                        fin_keys[j]).ratio()
            if r > br:
                best, br = i, r
        if best is not None and br >= 0.55:
            extra_pairs.append((best, j))
            used_pdf.add(best)
            still_fin.discard(j)
    matched.extend(extra_pairs)
    un_pdf = [i for i in un_pdf if i not in used_pdf]
    un_fin = sorted(still_fin)

    for i in un_pdf:
        if not is_marker_line(pdf_texts[i]) and pdf_keys[i]:
            report.append(f"  p{page['page']}: pdf-only line kept: "
                          f"{pdf_texts[i][:60]!r}")
    for j in un_fin:
        # a final-only line whose normalized content is already present in
        # some pdf segment row nearby (typical: footnote markers that were
        # absorbed) needs no extra box; only report genuinely new content.
        key = norm_key(final_lines[j])
        raw = final_lines[j].strip()
        # stray punctuation-only or digits-only fragments: the underlying
        # pdf text already renders that glyph at this position
        if not re.search(r"[A-Za-z0-9٠-٩ض-ي]", raw) and len(raw) <= 3:
            continue
        covered = False
        if key:
            for l in lines:
                if key and key in norm_key(line_pdf_text(l)):
                    covered = True
                    break
        # markers: every digit marker exists as its own pdf segment
        if not covered and is_marker_line(final_lines[j]):
            nums = [ch for ch in final_lines[j] if ch.isdigit()]
            hay = "".join(seg_pdf_text(s)
                          for l in lines for s in l["segments"])
            if all(ch in hay for ch in nums):
                covered = True
        if not covered:
            report.append(f"  p{page['page']}: UNMAPPED final line: "
                          f"{final_lines[j][:70]!r}")

    matched.sort()
    for li, fi in matched:
        line = lines[li]
        distribute_line(line, final_lines[fi])
    # lines never matched at all: keep pdf text
    matched_li = {m[0] for m in matched}
    for i, line in enumerate(lines):
        if i not in matched_li:
            for s in line["segments"]:
                for c in s["clusters"]:
                    c["text_final"] = c["text"]
                s["text"] = "".join(c["text"] for c in s["clusters"])
    return matched


def map_all(layout_json, final_txt, out_json, report_path):
    lay = json.load(open(layout_json, encoding="utf-8"))
    final_pages = parse_final(final_txt)
    report = []
    for page in lay["pages"]:
        fl = final_pages.get(page["page"], [])
        matched = map_page(page, fl, report)
        page["n_final_lines"] = len(fl)
        page["n_matched"] = len(matched)
    json.dump(lay, open(out_json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))
    print(f"mapped; report lines: {len(report)} -> {report_path}")


if __name__ == "__main__":
    map_all(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
