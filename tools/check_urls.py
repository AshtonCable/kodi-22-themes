#!/usr/bin/env python3
"""Check that every URL this project publishes actually resolves.

Two places hard-code raw.githubusercontent URLs: the repository add-on's <dir>
block, and the download links in README.md. A typo in either is invisible until
somebody tries to install something, and an unreachable <datadir> is the single
most likely reason a Kodi repository silently shows nothing.

While the GitHub repository is private, none of these URLs can resolve for an
unauthenticated client such as Kodi. That is a fact about the repository, not a
defect in this project, so the script detects it and passes with a notice
instead of failing.

Usage: python3 tools/check_urls.py
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_RE = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"'()<>\]]+")
# Resolves only when the repository is public; used to tell "private" apart from
# "broken link".
SENTINEL = ("https://raw.githubusercontent.com/AshtonCable/kodi-22-themes/"
            "main/README.md")


def status(url: str) -> int | str:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:                      # DNS, TLS, proxy, timeout
        return f"{type(exc).__name__}: {exc}"


def collect() -> dict[str, list[str]]:
    """URL -> where it came from."""
    found: dict[str, list[str]] = {}

    addon = REPO / "repository.cable.skins" / "addon.xml"
    if addon.is_file():
        root = ET.parse(addon).getroot()
        d = root.find("./extension[@point='xbmc.addon.repository']/dir")
        if d is not None:
            for tag in ("info", "checksum", "datadir"):
                el = d.find(tag)
                if el is not None and el.text:
                    found.setdefault(el.text.strip(), []).append(
                        f"repository.cable.skins/addon.xml <{tag}>")

    readme = REPO / "README.md"
    if readme.is_file():
        for m in RAW_RE.finditer(readme.read_text()):
            found.setdefault(m.group(0).rstrip(".,)"), []).append("README.md")

    return found


def main() -> int:
    urls = collect()
    if not urls:
        print("no published URLs found to check")
        return 0

    sentinel = status(SENTINEL)
    public = sentinel == 200
    if not public:
        print(f"repository appears private or main is missing "
              f"(sentinel README.md -> {sentinel}).")
        print("Kodi cannot authenticate to GitHub, so URL-based installs will not "
              "work until the repository is public.")
        print(f"Skipping {len(urls)} URL check(s); the zip-based install path is "
              f"unaffected.")
        return 0

    print(f"repository is public (sentinel -> 200); checking {len(urls)} URL(s)")
    failures = []
    for url in sorted(urls):
        st = status(url)
        ok = st == 200
        # <datadir> is a directory prefix; raw.githubusercontent serves no
        # listing for it, so 404 there is expected and not a fault.
        is_datadir = any("datadir" in src for src in urls[url])
        mark = "OK " if ok else ("dir" if is_datadir else "FAIL")
        print(f"  {mark} {st:>6}  {url}")
        for src in urls[url]:
            print(f"           from {src}")
        if not ok and not is_datadir:
            failures.append((url, st))

    if failures:
        print(f"\n{len(failures)} URL(s) do not resolve:", file=sys.stderr)
        for url, st in failures:
            print(f"  {st}  {url}", file=sys.stderr)
        return 1
    print("all published URLs resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
