#!/usr/bin/env python3
"""Derive Kodi-version-specific reference data from a pinned xbmc source tree.

The linter needs to know things that only Kodi's own source can tell us: which
window XMLs a skin must provide, which string ids core already defines, and which
fonts/textures ship with Kodi rather than with the skin. Hard-coding those lists
would rot silently as Kodi 22 moves from beta to final, so we generate them and
commit the result (CI then runs offline).

Usage:
    python3 tools/gen_reference_data.py --xbmc-src /path/to/xbmc [--check]

--check regenerates into memory and diffs against the committed files, exiting
non-zero on drift. That is what the weekly CI job runs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REF = HERE / "reference"

# Files a skin must provide that are not discoverable from the WINDOW_* table:
# the first three are looked up by name elsewhere in the source, the last two are
# structural files the skin loader always reads.
EXTRA_REQUIRED = [
    "DialogButtonMenu.xml",
    "DialogVideoManager.xml",
    "Timers.xml",
    "Font.xml",
    "Includes.xml",
]

# Present in the source's window table but legacy: no shipping skin provides it
# and Kodi does not fault without it.
EXCLUDE_REQUIRED = {"DialogSubMenu.xml"}

WINDOW_RE = re.compile(r'\bWINDOW_[A-Z_0-9]+\s*,\s*"([A-Za-z0-9_]+\.xml)"')


def _git_sha(src: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def required_files(src: Path) -> list[str]:
    """Window XMLs a Kodi 22 skin must provide."""
    found: set[str] = set()
    for path in (src / "xbmc").rglob("*"):
        if path.suffix not in {".h", ".cpp"} or not path.is_file():
            continue
        try:
            found.update(WINDOW_RE.findall(path.read_text(errors="replace")))
        except OSError:
            continue
    if not found:
        raise SystemExit("error: found no WINDOW_* -> xml mappings; is --xbmc-src correct?")
    return sorted((found | set(EXTRA_REQUIRED)) - EXCLUDE_REQUIRED)


def core_string_ids(src: Path) -> list[int]:
    """String ids defined by Kodi core (so $LOCALIZE[<31000] can be validated)."""
    po = src / "addons" / "resource.language.en_gb" / "resources" / "strings.po"
    if not po.is_file():
        raise SystemExit(f"error: core strings.po not found at {po}")
    ids = {int(m) for m in re.findall(r'^msgctxt\s+"#(\d+)"', po.read_text(errors="replace"),
                                     flags=re.MULTILINE)}
    return sorted(ids)


def bundled_fonts(src: Path) -> list[str]:
    """Fonts shipped with Kodi; a skin may reference these without shipping them."""
    return sorted(p.name for p in (src / "media").rglob("*")
                  if p.is_file() and p.suffix.lower() in {".ttf", ".otf"})


def bundled_textures(src: Path) -> list[str]:
    """Textures reachable as special://xbmc/media/..."""
    media = src / "media"
    return sorted(str(p.relative_to(media)).replace("\\", "/")
                  for p in media.rglob("*")
                  if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"})


def skin_string_range(src: Path) -> tuple[int, int]:
    """Read SKIN_STRING_RANGE_{START,END} rather than assuming 31000-31999."""
    header = (src / "xbmc" / "addons" / "Skin.h").read_text(errors="replace")
    start = re.search(r"SKIN_STRING_RANGE_START\s*=\s*(\d+)", header)
    end = re.search(r"SKIN_STRING_RANGE_END\s*=\s*(\d+)", header)
    if not (start and end):
        raise SystemExit("error: could not read SKIN_STRING_RANGE_* from Skin.h")
    return int(start.group(1)), int(end.group(1))


def gui_version(src: Path) -> tuple[str, str]:
    """The xbmc.gui version a skin must import, plus its backwards-compatible abi."""
    text = (src / "addons" / "xbmc.gui" / "addon.xml").read_text(errors="replace")
    ver = re.search(r'<addon\s+id="xbmc\.gui"\s+version="([^"]+)"', text)
    abi = re.search(r'<backwards-compatibility\s+abi="([^"]+)"', text)
    return (ver.group(1) if ver else "unknown", abi.group(1) if abi else "")


def build(src: Path) -> dict[str, str]:
    """Return {relative filename: file content} for every reference artefact."""
    kodi_ver = "unknown"
    version_txt = src / "version.txt"
    if version_txt.is_file():
        fields = dict(
            line.split(None, 1) for line in version_txt.read_text().splitlines()
            if line.strip() and len(line.split(None, 1)) == 2
        )
        kodi_ver = (f"{fields.get('VERSION_MAJOR', '?')}.{fields.get('VERSION_MINOR', '?')}"
                    f" {fields.get('VERSION_TAG', '')}").strip()

    gui_ver, gui_abi = gui_version(src)
    lo, hi = skin_string_range(src)
    req = required_files(src)
    ids = core_string_ids(src)

    meta = {
        "generated_from": "https://github.com/xbmc/xbmc",
        "commit": _git_sha(src),
        "kodi_version": kodi_ver,
        "xbmc_gui_version": gui_ver,
        "xbmc_gui_backwards_abi": gui_abi,
        "skin_string_range": [lo, hi],
        "required_file_count": len(req),
        "core_string_count": len(ids),
    }

    return {
        "PIN.json": json.dumps(meta, indent=2) + "\n",
        "required_files.json": json.dumps(req, indent=2) + "\n",
        "core_string_ids.txt": "".join(f"{i}\n" for i in ids),
        "bundled_fonts.txt": "".join(f"{f}\n" for f in bundled_fonts(src)),
        "bundled_textures.txt": "".join(f"{t}\n" for t in bundled_textures(src)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xbmc-src", required=True, type=Path,
                    help="path to a checkout of github.com/xbmc/xbmc")
    ap.add_argument("--check", action="store_true",
                    help="diff against committed files instead of writing; non-zero on drift")
    args = ap.parse_args()

    if not args.xbmc_src.is_dir():
        raise SystemExit(f"error: {args.xbmc_src} is not a directory")

    artefacts = build(args.xbmc_src)
    REF.mkdir(parents=True, exist_ok=True)

    drift = []
    for name, content in artefacts.items():
        target = REF / name
        if args.check:
            existing = target.read_text() if target.is_file() else None
            if existing != content:
                drift.append(name)
        else:
            target.write_text(content)
            print(f"wrote {target.relative_to(HERE.parent)} "
                  f"({len(content.splitlines())} lines)")

    if args.check:
        if drift:
            print("reference data is stale (Kodi source moved): " + ", ".join(drift),
                  file=sys.stderr)
            print("regenerate with: python3 tools/gen_reference_data.py --xbmc-src <path>",
                  file=sys.stderr)
            return 1
        print("reference data is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
