#!/usr/bin/env python3
"""Fetch openly-licensed fonts and bake static instances for Kodi.

Two Kodi facts drive this tool:

  * CGUIFontTTF calls FT_New_Face and never sets variation coordinates, so a
    variable TTF renders ONLY its default instance. Weights other than the
    default would silently fall back or be synthesised.
  * GUIFontManager accepts .ttf only; .otf is silently rejected.

google/fonts now ships most families as variable-only (no static/ subdirectory),
so we download the variable font and instance it with fontTools. The output is a
plain static TTF with fvar stripped -- exactly what Kodi wants.

Also writes a FONTS.md attribution block and ships the upstream licence next to
the fonts, as OFL section 1 requires the licence to travel with the font.

Usage:
    python3 tools/fetch_fonts.py --skin skin.cable.tv
    python3 tools/fetch_fonts.py --skin skin.cable.tv --verify   # checksums only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GF = "https://raw.githubusercontent.com/google/fonts/main"

# family -> upstream paths and the static instances to bake.
# `axes` must pin EVERY axis the variable font declares, or instancer leaves a
# partial variable font behind.
FAMILIES: dict[str, dict] = {
    "roboto": {
        "variable": "ofl/roboto/Roboto%5Bwdth,wght%5D.ttf",
        "licence": ("ofl/roboto/OFL.txt", "roboto_OFL.txt"),
        "licence_name": "SIL Open Font License 1.1",
        "credit": "Roboto by Christian Robertson (Google Fonts)",
        "instances": {
            "Roboto-Regular.ttf": {"wght": 400, "wdth": 100},
            "Roboto-Medium.ttf": {"wght": 500, "wdth": 100},
            "Roboto-Bold.ttf": {"wght": 700, "wdth": 100},
            # Kodi has no letter-spacing/tracking control at all: the font system
            # exposes size, aspect (a horizontal scale, which distorts glyphs and
            # is the wrong tool), linespacing and style. Leanback's row headings
            # are tracked caps, so bake the tracking into the advances.
            "Roboto-Medium-Tracked.ttf": {"wght": 500, "wdth": 100, "_track": 0.07},
        },
    },
    "inter": {
        "variable": "ofl/inter/Inter%5Bopsz,wght%5D.ttf",
        "licence": ("ofl/inter/OFL.txt", "inter_OFL.txt"),
        "licence_name": "SIL Open Font License 1.1",
        "credit": "Inter by Rasmus Andersson",
        "instances": {
            "Inter-Regular.ttf": {"wght": 400, "opsz": 20},
            "Inter-SemiBold.ttf": {"wght": 600, "opsz": 20},
        },
    },
    "jost": {
        "variable": "ofl/jost/Jost%5Bwght%5D.ttf",
        "licence": ("ofl/jost/OFL.txt", "jost_OFL.txt"),
        "licence_name": "SIL Open Font License 1.1",
        "credit": "Jost* by Owen Earl (indestructible type*)",
        "instances": {
            "Jost-Light.ttf": {"wght": 300},
            "Jost-Regular.ttf": {"wght": 400},
            "Jost-Medium.ttf": {"wght": 500},
        },
    },
    "opensans": {
        "variable": "ofl/opensans/OpenSans%5Bwdth,wght%5D.ttf",
        "licence": ("ofl/opensans/OFL.txt", "opensans_OFL.txt"),
        "licence_name": "SIL Open Font License 1.1",
        "credit": "Open Sans by Steve Matteson (Google Fonts)",
        "instances": {
            "OpenSans-Regular.ttf": {"wght": 400, "wdth": 100},
            "OpenSans-SemiBold.ttf": {"wght": 600, "wdth": 100},
            "OpenSans-Bold.ttf": {"wght": 700, "wdth": 100},
        },
    },
}

# Which families each skin ships. Kept here so the tool is the single source of
# truth for what lands in a skin's fonts/ directory.
SKIN_FONTS: dict[str, list[str]] = {
    "skin.cable.tv": ["roboto"],
    "skin.cable.retro": ["jost", "opensans"],
    "skin.cable.stream": ["inter"],
}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def apply_tracking(font, amount: float) -> None:
    """Widen every glyph advance by `amount` em, emulating letter-spacing.

    Also widens the left side bearing by half the amount so the extra space is
    split either side of the glyph rather than all trailing it, which keeps the
    optical centring of a centred label correct.
    """
    upem = font["head"].unitsPerEm
    delta = int(round(amount * upem))
    font["hmtx"].metrics = {
        name: (adv + delta, lsb + delta // 2)
        for name, (adv, lsb) in font["hmtx"].metrics.items()
    }


def instance_font(raw: bytes, axes: dict[str, float], out: Path) -> None:
    from io import BytesIO
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    axes = dict(axes)
    track = axes.pop("_track", None)

    font = TTFont(BytesIO(raw))
    declared = {a.axisTag for a in font["fvar"].axes}
    missing = declared - set(axes)
    if missing:
        raise SystemExit(
            f"error: {out.name} does not pin axes {sorted(missing)}; "
            f"instancer would leave a partial variable font behind")
    static = instancer.instantiateVariableFont(font, axes, inplace=False,
                                               updateFontNames=True)
    if track:
        apply_tracking(static, float(track))
    out.parent.mkdir(parents=True, exist_ok=True)
    static.save(out)
    # Sanity: a real static TTF has no fvar table left.
    check = TTFont(out)
    if "fvar" in check:
        raise SystemExit(f"error: {out.name} still contains fvar after instancing")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skin", required=True, help="skin directory name")
    ap.add_argument("--verify", action="store_true",
                    help="only report checksums of existing fonts")
    args = ap.parse_args()

    skin_dir = REPO / args.skin
    if not skin_dir.is_dir():
        raise SystemExit(f"error: {skin_dir} does not exist")
    fonts_dir = skin_dir / "fonts"
    families = SKIN_FONTS.get(args.skin)
    if not families:
        raise SystemExit(f"error: no font set declared for {args.skin}")

    if args.verify:
        for ttf in sorted(fonts_dir.glob("*.ttf")):
            digest = hashlib.sha256(ttf.read_bytes()).hexdigest()
            print(f"{digest}  {ttf.name}  ({ttf.stat().st_size} bytes)")
        return 0

    manifest: list[dict] = []
    for family in families:
        spec = FAMILIES[family]
        url = f"{GF}/{spec['variable']}"
        print(f"fetching {url}")
        raw = fetch(url)
        print(f"  {len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()[:16]}...")

        for filename, axes in spec["instances"].items():
            out = fonts_dir / filename
            instance_font(raw, axes, out)
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            print(f"  baked {filename}  {out.stat().st_size} bytes  "
                  f"({', '.join(f'{k}={v}' for k, v in axes.items())})")
            manifest.append({
                "file": filename, "family": family, "axes": axes,
                "sha256": digest, "bytes": out.stat().st_size,
                "licence": spec["licence_name"], "credit": spec["credit"],
                "upstream": f"google/fonts:{spec['variable']}",
            })

        lic_src, lic_dst = spec["licence"]
        (fonts_dir / lic_dst).write_bytes(fetch(f"{GF}/{lic_src}"))
        print(f"  licence -> fonts/{lic_dst}")

    (skin_dir / "fonts" / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n")

    lines = ["# Fonts shipped with this skin", "",
             "Generated by `tools/fetch_fonts.py`. Static instances are baked from",
             "google/fonts variable sources because Kodi's FreeType integration never",
             "sets variation coordinates, so a variable TTF would render only its",
             "default instance.", ""]
    for entry in manifest:
        lines.append(f"## {entry['file']}")
        lines.append("")
        lines.append(f"- {entry['credit']}")
        lines.append(f"- Licence: {entry['licence']} (see `fonts/`)")
        lines.append(f"- Upstream: `{entry['upstream']}`")
        lines.append(f"- Instanced at: "
                     f"{', '.join(f'{k}={v}' for k, v in entry['axes'].items())}")
        lines.append(f"- SHA-256: `{entry['sha256']}`")
        lines.append("")
    (skin_dir / "FONTS.md").write_text("\n".join(lines))
    print(f"wrote {args.skin}/FONTS.md and fonts/MANIFEST.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
