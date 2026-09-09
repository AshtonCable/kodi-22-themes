#!/usr/bin/env python3
"""Render a static mock of the home screen from the skin's own assets.

Kodi 22 cannot be run in this environment, so this is the closest thing to
seeing the skin: it reads the real geometry constants out of Constants_1080.xml,
the real colours out of colors/defaults.xml, and composites the real generated
textures and the real baked Roboto faces.

It is NOT a Kodi emulator and proves nothing about Kodi's behaviour. What it does
prove is that the arithmetic adds up - card rects, the shadow inflation, the
focus zoom centre, the row pitch, the info band - which is exactly the class of
mistake that is invisible in XML and obvious in a picture.

Usage: python3 tools/preview_home.py skin.cable.tv [-o preview.png]
"""
from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080


def load_constants(xml_dir: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in ET.parse(xml_dir / "Constants_1080.xml").getroot().iter("constant"):
        name, val = c.get("name"), (c.text or "").strip()
        if name and re_int(val) is not None:
            out[name] = re_int(val)
    return out


def re_int(v: str):
    try:
        return int(v)
    except ValueError:
        return None


def load_colors(skin: Path) -> dict[str, tuple[int, int, int, int]]:
    out = {}
    for c in ET.parse(skin / "colors" / "defaults.xml").getroot().iter("color"):
        name, hexv = c.get("name"), (c.text or "").strip()
        if name and len(hexv) == 8:
            a, r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4, 6))
            out[name] = (r, g, b, a)
    return out


def load_card_geometry(xml_dir: Path) -> dict:
    """Read the card's focus zoom and metadata-band offsets out of the skin.

    The preview must not carry its own copy of these numbers, or it stops being
    a check and becomes a parallel implementation that can agree with itself
    while disagreeing with the skin.
    """
    geo = {}
    anims = ET.parse(xml_dir / "Includes_Animations.xml").getroot()
    for inc in anims.iter("include"):
        if inc.get("name") == "Anim_CardFocus":
            for prm in inc.findall("param"):
                if prm.get("name") == "zoom":
                    geo["zoom"] = int((prm.text or "108").strip())

    home = ET.parse(xml_dir / "Includes_CableHome.xml").getroot()
    for inc in home.iter("include"):
        if inc.get("name") != "LeanCard16x9":
            continue
        defn = inc.find("definition")
        # the art control is the one carrying <bordersize>
        for ctrl in defn.iter("control"):
            if ctrl.find("bordersize") is not None:
                geo["art_shadow_top"] = int(ctrl.findtext("top"))
            fnt = (ctrl.findtext("font") or "").strip()
            if fnt == "lb_cardtitle":
                geo["title_top"] = int(ctrl.findtext("top"))
            elif fnt == "lb_cardsub":
                geo["sub_top"] = int(ctrl.findtext("top"))
            # the placeholder plate carries the true art rect
            tex = ctrl.find("texture")
            if (tex is not None and (tex.text or "").strip() == "colors/white.png"
                    and ctrl.findtext("top") and "art_top" not in geo):
                geo["art_top"] = int(ctrl.findtext("top"))
    geo.setdefault("zoom", 108)
    geo.setdefault("art_top", 40)
    geo.setdefault("title_top", 222)
    geo.setdefault("sub_top", 252)
    return geo


def load_fontset(xml_dir: Path, fonts_dir: Path) -> dict[str, tuple[Path, int, str]]:
    out = {}
    for f in ET.parse(xml_dir / "Font.xml").getroot().iter("font"):
        name = (f.findtext("name") or "").strip()
        fn = (f.findtext("filename") or "").strip()
        size = re_int((f.findtext("size") or "0").strip()) or 0
        style = (f.findtext("style") or "").strip()
        if name and fn:
            out[name] = (fonts_dir / fn, size, style)
    return out


def tinted(path: Path, size, rgba, mask_from_alpha=True) -> Image.Image:
    im = Image.open(path).convert("RGBA").resize(size, Image.LANCZOS)
    if not mask_from_alpha:
        return im
    solid = Image.new("RGBA", im.size, rgba[:3] + (255,))
    a = im.split()[3].point(lambda v: v * rgba[3] // 255)
    solid.putalpha(a)
    return solid


def nine_slice(src: Image.Image, border: int, w: int, h: int, infill=True) -> Image.Image:
    sw, sh = src.size
    b = min(border, sw // 2, sh // 2)
    out = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    src_boxes = {
        "tl": (0, 0, b, b), "tr": (sw - b, 0, sw, b),
        "bl": (0, sh - b, b, sh), "br": (sw - b, sh - b, sw, sh),
        "t": (b, 0, sw - b, b), "b": (b, sh - b, sw - b, sh),
        "l": (0, b, b, sh - b), "r": (sw - b, b, sw, sh - b),
        "c": (b, b, sw - b, sh - b),
    }
    dst_boxes = {
        "tl": (0, 0, b, b), "tr": (w - b, 0, w, b),
        "bl": (0, h - b, b, h), "br": (w - b, h - b, w, h),
        "t": (b, 0, w - b, b), "b": (b, h - b, w - b, h),
        "l": (0, b, b, h - b), "r": (w - b, b, w, h - b),
        "c": (b, b, w - b, h - b),
    }
    for k, sbox in src_boxes.items():
        if k == "c" and not infill:
            continue
        dx0, dy0, dx1, dy1 = dst_boxes[k]
        if dx1 <= dx0 or dy1 <= dy0:
            continue
        piece = src.crop(sbox)
        if piece.size[0] == 0 or piece.size[1] == 0:
            continue
        out.alpha_composite(piece.resize((dx1 - dx0, dy1 - dy0), Image.BILINEAR), (dx0, dy0))
    return out


def fake_art(w: int, h: int, seed: int) -> Image.Image:
    """Stand-in artwork: a deterministic two-tone gradient per card."""
    rnd = (seed * 2654435761) % (2 ** 32)
    h1 = (rnd % 360) / 360.0
    h2 = ((rnd >> 8) % 360) / 360.0

    def hsv(hh, s, v):
        i = int(hh * 6) % 6
        f = hh * 6 - int(hh * 6)
        p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
        r, g, b = [(v, t, p), (q, v, p), (p, v, t),
                   (p, q, v), (t, p, v), (v, p, q)][i]
        return int(r * 255), int(g * 255), int(b * 255)

    c1, c2 = hsv(h1, 0.55, 0.62), hsv(h2, 0.65, 0.30)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / max(w - 1, 1) * 0.6 + y / max(h - 1, 1) * 0.4)
            px[x, y] = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
    return im.convert("RGBA")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skin", type=Path)
    ap.add_argument("-o", "--out", default="preview-home.png")
    ap.add_argument("--focus-row", type=int, default=0,
                    help="which row index holds focus (0 = first)")
    args = ap.parse_args()

    skin = args.skin
    xml_dir, media, fonts_dir = skin / "xml", skin / "media", skin / "fonts"
    K = load_constants(xml_dir)
    C = load_colors(skin)
    F = load_fontset(xml_dir, fonts_dir)
    G = load_card_geometry(xml_dir)

    def font(name: str) -> ImageFont.FreeTypeFont:
        path, size, _ = F[name]
        return ImageFont.truetype(str(path), size)

    def upper(name: str, text: str) -> str:
        return text.upper() if "uppercase" in F[name][2] else text

    gutter = K["lb_gutter"]
    pitch = K["lb_rowpitch"]
    rowtop = K["lb_rowtop"]
    cardw, cardh = K["lb_cardw"], K["lb_cardh"]
    cardpitch, sqpitch = K["lb_cardpitch"], K["lb_sqpitch"]
    elev = K["lb_elev"]

    img = Image.new("RGBA", (W, H), C["background"])

    # backdrop of the focused item, dimmed (there is no blur in Kodi 22)
    backdrop = fake_art(W, H, 999).resize((W, H))
    dim = Image.new("RGBA", (W, H), (255, 255, 255, 0x33))
    backdrop.putalpha(dim.split()[3])
    img.alpha_composite(backdrop)

    # scrims: the real 8x512 gradient, stretched, top and bottom (flipped)
    vg = Image.open(media / "cable" / "scrims" / "vgrad.png").convert("RGBA")
    scrim = C["scrim_strong"]
    img.alpha_composite(tinted_stretch(vg, (W, 420), scrim))
    img.alpha_composite(tinted_stretch(vg.transpose(Image.FLIP_TOP_BOTTOM), (W, 720), scrim),
                        (0, H - 720))

    d = ImageDraw.Draw(img)

    # ---- top bar ----
    pill = Image.open(media / "cable" / "frames" / "pill.png").convert("RGBA")
    bar_y = 40
    # search pill (focused, to show the accent state)
    sp = nine_slice(recolour(pill, C["accent"]), 30, 230, 62)
    img.alpha_composite(sp, (gutter, bar_y))
    img.alpha_composite(tinted(media / "cable" / "icons" / "search.png", (28, 28),
                               C["accent_ink"]), (gutter + 26, bar_y + 17))
    d.text((gutter + 66, bar_y + 31), "Search", font=font("lb_button"),
           fill=C["accent_ink"], anchor="lm")
    # clock
    d.text((W - 350, bar_y + 31), "9:41", font=font("lb_clock"),
           fill=C["text_secondary"], anchor="rm")
    # settings + power
    for right, icon in ((190, "settings.png"), (gutter, "power.png")):
        x = W - right - 62
        img.alpha_composite(nine_slice(recolour(pill, C["surface_raised"]), 30, 62, 62), (x, bar_y))
        img.alpha_composite(tinted(media / "cable" / "icons" / icon, (28, 28),
                                   C["text_secondary"]), (x + 17, bar_y + 17))

    # ---- rows ----
    rows = [
        ("Continue watching", cardpitch, cardw, cardh, "card16x9", "landscape"),
        ("Recently added movies", cardpitch, cardw, cardh, "card16x9", "landscape"),
        ("Recently added episodes", cardpitch, cardw, cardh, "card16x9", "landscape"),
        ("Shows to continue", cardpitch, cardw, cardh, "card16x9", "landscape"),
        ("Movies", cardpitch, cardw, cardh, "card16x9", "landscape"),
        ("Recently added albums", sqpitch, cardh, cardh, "card1x1", "square"),
    ]
    shadow_src = Image.open(media / "cable" / "shadows" / "elev.png").convert("RGBA")
    ring_src = Image.open(media / "cable" / "frames" / "rounded8-outline.png").convert("RGBA")

    # The grouplist scrolls so the focused row sits at lb_rowtop. That is the
    # whole point of the pin-to-top arithmetic, so the preview applies it:
    # offset(row i) = i * lb_rowpitch, and ScrollTo(focus * pitch).
    scroll = args.focus_row * pitch

    seed = 1
    for ri, (label, cpitch, cw, ch, maskname, _plate) in enumerate(rows):
        ytop = rowtop + ri * pitch - scroll
        if ytop + 288 < rowtop - pitch:
            seed += 8
            continue
        if ytop > H:
            break
        focused_row = ri == args.focus_row
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)

        ld.text((gutter, ytop + 2 + 17), upper("lb_rowlabel", label),
                font=font("lb_rowlabel"), fill=C["text_secondary"], anchor="lm")

        mask = Image.open(media / "cable" / "masks" / f"{maskname}.png").convert("RGBA")
        list_left = 40
        n = math.ceil((W - list_left) / cpitch) + 1
        for ci in range(n):
            slot_x = list_left + ci * cpitch
            local_art_x = 56
            ax, ay = slot_x + local_art_x, ytop + 12 + G["art_top"]
            focused = focused_row and ci == 0
            art = fake_art(cw, ch, seed); seed += 1
            art.putalpha(mask.resize((cw, ch), Image.LANCZOS).split()[3])
            if not focused:
                art = mul_alpha(art, C["card_dim"][3])

            if focused:
                # zoom about the card centre, exactly as Anim_CardFocus does
                z = G["zoom"] / 100.0
                cx, cy = ax + cw / 2, ay + ch / 2
                zw, zh = int(cw * z), int(ch * z)
                zx, zy = int(cx - zw / 2), int(cy - zh / 2)
                # shadow rect is the card rect inflated by bordersize, then zoomed
                sw_, sh_ = int((cw + elev * 2) * z), int((ch + elev * 2) * z)
                sx, sy = int(cx - sw_ / 2), int(cy - sh_ / 2)
                layer.alpha_composite(
                    recolour(nine_slice(shadow_src, 40, sw_, sh_, infill=False),
                             C["shadow_tint"]), (sx, sy))
                zart = art.resize((zw, zh), Image.LANCZOS)
                layer.alpha_composite(zart, (zx, zy))
                layer.alpha_composite(
                    recolour(nine_slice(ring_src, 16, zw + 8, zh + 8), C["focus_ring"]),
                    (zx - 4, zy - 4))
                # info band: sibling of the zoom group, so unscaled
                ld.text((ax, ytop + 12 + G["title_top"] + 15), "The Quiet Season",
                        font=font("lb_cardtitle"), fill=C["text_primary"], anchor="lm")
                ld.text((ax, ytop + 12 + G["sub_top"] + 13), "2024",
                        font=font("lb_cardsub"), fill=C["text_hint"], anchor="lm")

                # Assert the geometry actually clears, so the preview fails loudly
                # rather than just looking slightly wrong.
                card_bottom = ay + ch / 2 + (ch / 2) * z
                band_top = ytop + 12 + G["title_top"]
                if card_bottom > band_top:
                    print(f"  GEOMETRY ERROR: zoomed card bottom {card_bottom:.1f} "
                          f"overlaps metadata band top {band_top}", file=sys.stderr)
                    globals()["_GEOM_FAIL"] = True
            else:
                layer.alpha_composite(art, (ax, ay))

        if not focused_row:
            layer = mul_alpha(layer, 140)   # Anim_RowDim: unfocused rows recede
        # CGUIControlGroupList::Render calls SetClipRegion, so anything scrolled
        # above the grouplist's top edge is not drawn. Clip, or the preview shows
        # rows bleeding behind the top bar that Kodi would never render.
        clip = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        clip.paste(layer.crop((0, rowtop, W, H)), (0, rowtop))
        img.alpha_composite(clip)

    out = Path(args.out)
    img.convert("RGB").save(out, quality=92)
    print(f"wrote {out} ({W}x{H})")
    print(f"  constants: gutter={gutter} pitch={pitch} rowtop={rowtop} "
          f"card={cardw}x{cardh} cardpitch={cardpitch} elev={elev}")
    print(f"  card: zoom={G['zoom']}% art_top={G['art_top']} "
          f"title_top={G['title_top']} sub_top={G['sub_top']}")
    if globals().get("_GEOM_FAIL"):
        return 1
    return 0


def tinted_stretch(src: Image.Image, size, rgba) -> Image.Image:
    im = src.resize(size, Image.BILINEAR)
    return recolour(im, rgba)


def recolour(im: Image.Image, rgba) -> Image.Image:
    solid = Image.new("RGBA", im.size, rgba[:3] + (255,))
    a = im.split()[3].point(lambda v: v * rgba[3] // 255)
    solid.putalpha(a)
    return solid


def mul_alpha(im: Image.Image, factor: int) -> Image.Image:
    out = im.copy()
    out.putalpha(im.split()[3].point(lambda v: v * factor // 255))
    return out


if __name__ == "__main__":
    sys.exit(main())
