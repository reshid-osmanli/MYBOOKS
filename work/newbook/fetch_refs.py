# -*- coding: utf-8 -*-
"""
fetch_refs.py -- ينزّل المراجع الخارجية التي يحتاجها خط أنابيب التفريغ.

    python fetch_refs.py            # ينزّل ما هو ناقص
    python fetch_refs.py --force    # يعيد التنزيل كاملًا

المراجع:
  refs/ara.traineddata            نموذج Tesseract العربي (الأدقّ) -- 12.6MB
  refs/quran.json                 نصّ المصحف بالرسم العثماني -- 1.4MB
  refs/quran.vocalized.wordlist   مفردات القرآن مشكولة -- 0.4MB
  refs/arwiki.freqlist            تكرارات كلمات ويكيبيديا العربية -- 29MB
  refs/ar.dic                     قاموس جذور عربي (احتياطي) -- 7MB
  sr_models/ESPCN_x3.pb           شبكة رفع الدقّة ×3 -- 0.1MB

تُنزَّل من GitHub، فخط الأنابيب قابل لإعادة الإنتاج على أي جهاز. وهي خارج
git (انظر ../.gitignore) لأنها ملك أصحابها ولا يصحّ إلحاقها بالمستودع.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(HERE, "refs")
SR = os.path.join(HERE, "sr_models")

# (الوجهة, المستودع, المسار, أصغر حجم مقبول)
FILES = [
    (REFS, "tesseract-ocr/tessdata_best", "ara.traineddata", 5_000_000),
    (REFS, "risan/quran-json", "dist/quran.json", 500_000),
    (REFS, "a3f/arabic-wordlists", "quran.vocalized.wordlist", 100_000),
    (REFS, "a3f/arabic-wordlists", "arwiki.freqlist", 5_000_000),
    (REFS, "LibreOffice/dictionaries", "ar/ar.dic", 2_000_000),
    (SR, "fannymonori/TF-ESPCN", "export/ESPCN_x3.pb", 50_000),
]

UA = {"User-Agent": "MYBOOKS-ocr-pipeline/1.0"}


def _urls(repo, path):
    """واجهة GitHub أولًا (تعمل خلف الحواجز)، ثم التنزيل المباشر."""
    quoted = path.replace(" ", "%20")
    return [
        f"https://api.github.com/repos/{repo}/contents/{quoted}",
        f"https://raw.githubusercontent.com/{repo}/HEAD/{quoted}",
    ]


def download(repo, path, dest, force=False):
    if os.path.exists(dest) and not force:
        print(f"  = {os.path.basename(dest)} موجود")
        return True
    last = None
    for url in _urls(repo, path):
        headers = dict(UA)
        if "api.github.com" in url:
            headers["Accept"] = "application/vnd.github.raw"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            continue
        # ملف كبير عبر LFS يصل مؤشّرًا نصّيًّا لا محتوى
        if data[:7] == b"version":
            last = "ملف مخزَّن بـGit-LFS (غير قابل للتنزيل المباشر)"
            continue
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"  + {os.path.basename(dest)} ({len(data):,} بايت)")
        return True
    print(f"  ! فشل {os.path.basename(dest)}: {last}")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    os.makedirs(REFS, exist_ok=True)
    os.makedirs(SR, exist_ok=True)

    ok = True
    for dest_dir, repo, path, min_size in FILES:
        dest = os.path.join(dest_dir, os.path.basename(path))
        got = download(repo, path, dest, args.force)
        if got and os.path.getsize(dest) < min_size:
            print(f"  ! {os.path.basename(dest)} أصغر من المتوقّع — تحقّق منه")
            got = False
        ok &= got

    if not ok:
        print("\nبعض المراجع ناقصة. يمكن الحصول عليها يدويًّا ووضعها في "
              "المجلدين المذكورين.")
        return 1
    print("\nكل المراجع في مكانها.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
