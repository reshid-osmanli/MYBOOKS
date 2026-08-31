# -*- coding: utf-8 -*-
"""
fetch_fonts.py -- download the *reference* fonts used to decode the PDFs.

The PDFs embed subsetted fonts whose glyph names/ToUnicode maps are
incomplete, so ``decode_driver_v2.py`` compares them against a full copy of
the same font family.  Those copies are not part of the repository (they are
not ours to redistribute); this script downloads them into ``work/fonts/``.

    python work/fetch_fonts.py            # download what is missing
    python work/fetch_fonts.py --force    # re-download everything

Sources (all three are the canonical upstream repositories of the fonts):

* Sakkal Majalla (regular + bold) -- github.com/Aya-Ibrahim261/majalla-font
* KFGQPC Uthmanic Script Hafs      -- github.com/mustafa0x/qpc-fonts
"""

import argparse
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(HERE, "fonts")

UA = {"User-Agent": "MYBOOKS-rtl-text-extraction/1.0"}

FONTS = [
    {
        "file": "majalla.ttf",
        "repo": "Aya-Ibrahim261/majalla-font",
        "path": "majalla.ttf",
        "size": 370084,
    },
    {
        "file": "majallab.ttf",
        "repo": "Aya-Ibrahim261/majalla-font",
        "path": "majallab.ttf",
        "size": 294564,
    },
    {
        "file": "UthmanicHafs1_Ver09.otf",
        "repo": "mustafa0x/qpc-fonts",
        "path": "various/UthmanicHafs1 Ver09.otf",
        "size": 246428,
    },
]


def _urls(repo, path):
    """Raw download first, the GitHub API as a fallback (proxies/firewalls)."""
    quoted = path.replace(" ", "%20")
    return [
        f"https://raw.githubusercontent.com/{repo}/HEAD/{quoted}",
        f"https://api.github.com/repos/{repo}/contents/{quoted}",
    ]


def _download(repo, path, dest, force=False):
    if os.path.exists(dest) and not force:
        print(f"  = {os.path.basename(dest)} already present")
        return True
    last_error = None
    for url in _urls(repo, path):
        headers = dict(UA)
        if "api.github.com" in url:
            headers["Accept"] = "application/vnd.github.raw"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            continue
        if len(data) < 1024 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO",
                                                b"true", b"ttcf", b"wOFF"):
            last_error = f"not a font file ({len(data)} bytes from {url})"
            continue
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"  + {os.path.basename(dest)} ({len(data)} bytes)")
        return True
    print(f"  ! failed: {os.path.basename(dest)} -- {last_error}")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="download again even if the file exists")
    args = ap.parse_args(argv)

    os.makedirs(FONTS_DIR, exist_ok=True)
    print(f"reference fonts -> {FONTS_DIR}")
    ok = True
    for spec in FONTS:
        ok &= _download(spec["repo"], spec["path"],
                        os.path.join(FONTS_DIR, spec["file"]), args.force)
    if not ok:
        print("\nSome fonts could not be downloaded.  The driver still runs, "
              "but the extracted text will be less accurate.")
        return 1
    print("\nAll reference fonts are in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
