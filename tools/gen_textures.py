#!/usr/bin/env python3
"""Generate a skin's texture set with Pillow.

Design rules, which are Estuary's rules and the reason it stays fast:

  * Every texture is WHITE (or plain alpha) and is tinted at use time with
    <texture colordiffuse="...">. One file therefore serves every colour and
    every state, which is why this set is ~30 files instead of ~200.
  * Loose PNGs are always uploaded as BGRA8 (Texture.cpp:189), so VRAM is
    width * height * 4 regardless of PNG bit depth. Bit-depth tricks only pay
    off inside an XBT, which we do not build. The lever is PIXEL COUNT, so
    gradients are authored a few pixels wide and stretched by Kodi.
  * Rounded shapes are drawn at 4x and downsampled with LANCZOS, because
    ImageDraw.rounded_rectangle aliases badly at these radii.
  * Straight (non-premultiplied) alpha: that is what Kodi expects.

Everything lands under media/cable/ so the delta from vendored Estuary stays
obvious and nothing collides with an inherited path.

Usage:
    python3 tools/gen_textures.py --skin skin.cable.tv
    python3 tools/gen_textures.py --skin skin.cable.tv --check   # reproducibility
"""
from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
SS = 4  # supersample factor
WHITE = (255, 255, 255, 255)


def _save(im: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True, compress_level=9)


def solid(size, rgba, out: Path) -> None:
    _save(Image.new("RGBA", size, rgba), out)


def rounded_rect(size, radius, out: Path, stroke: int = 0) -> None:
    """Filled or outlined rounded rectangle, for 9-slice stretching.

    The source must be at least 2*radius + a couple of px so the 9-slice corners
    are never sampled across the midpoint.
    """
    w, h = size
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    box = [0, 0, w * SS - 1, h * SS - 1]
    if stroke:
        d.rounded_rectangle(box, radius=radius * SS, outline=WHITE, width=stroke * SS)
    else:
        d.rounded_rectangle(box, radius=radius * SS, fill=WHITE)
    _save(im.resize(size, Image.LANCZOS), out)


def circle(size, out: Path, stroke: int = 0) -> None:
    w, h = size
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    box = [0, 0, w * SS - 1, h * SS - 1]
    if stroke:
        d.ellipse(box, outline=WHITE, width=stroke * SS)
    else:
        d.ellipse(box, fill=WHITE)
    _save(im.resize(size, Image.LANCZOS), out)


def elevation_ring(size, radius, blur, offset_y, alpha, out: Path) -> None:
    """A pre-blurred shadow, used as a 9-slice <bordertexture> with infill=false.

    This is how the skin gets Material elevation without any per-card blur:
    Kodi 22 exposes no blur to skins at all. Because CGUIBorderedImage refits the
    ring to the image's render rect, one file tracks every card size, and it
    inherits the card's focus zoom for free.
    """
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pad = blur * 2
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([pad, pad + offset_y, w - 1 - pad, h - 1 - pad + offset_y],
                        radius=radius, fill=(0, 0, 0, alpha))
    _save(im.filter(ImageFilter.GaussianBlur(blur)), out)


def vgrad(size, stops, out: Path, gamma: float = 2.2) -> None:
    """Vertical alpha ramp, authored narrow and stretched horizontally by Kodi.

    Gamma-corrected rather than linear: a linear alpha ramp visibly bands on
    8-bit TV panels, which is exactly where this skin runs.
    """
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = im.load()
    for y in range(h):
        t = y / (h - 1)
        a = 0.0
        for i in range(len(stops) - 1):
            t0, a0 = stops[i]
            t1, a1 = stops[i + 1]
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                a = a0 + (a1 - a0) * u
                break
        else:
            a = stops[-1][1]
        alpha = int(round(255 * (a ** gamma)))
        for x in range(w):
            px[x, y] = (255, 255, 255, alpha)
    _save(im, out)


def radial(size, out: Path, gamma: float = 2.0, peak: int = 255) -> None:
    w, h = size
    im = Image.new("RGBA", (w * 1, h * 1), (0, 0, 0, 0))
    px = im.load()
    cx, cy = (w - 1) / 2, (h - 1) / 2
    rmax = math.hypot(cx, cy)
    for y in range(h):
        for x in range(w):
            r = math.hypot(x - cx, y - cy) / rmax
            a = max(0.0, 1.0 - r) ** gamma
            px[x, y] = (255, 255, 255, int(round(peak * a)))
    _save(im, out)


def vignette(size, out: Path, strength: float = 0.55, gamma: float = 1.6) -> None:
    """Corner darkening, one quad over the backdrop. Black with rising alpha."""
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = im.load()
    cx, cy = (w - 1) / 2, (h - 1) / 2
    rmax = math.hypot(cx, cy)
    for y in range(h):
        for x in range(w):
            r = math.hypot(x - cx, y - cy) / rmax
            px[x, y] = (0, 0, 0, int(round(255 * strength * (r ** gamma))))
    _save(im, out)


def arc_spinner(size, out: Path, sweep: int = 270, width: int = 8) -> None:
    w, h = size
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    inset = width * SS // 2 + SS
    d.arc([inset, inset, w * SS - 1 - inset, h * SS - 1 - inset],
          start=-90, end=-90 + sweep, fill=WHITE, width=width * SS)
    _save(im.resize(size, Image.LANCZOS), out)


# --- icons ------------------------------------------------------------------
# Simple geometry only: these are tinted at the use site, so one file per glyph
# covers focused, unfocused and disabled.

def _icon_canvas(px: int):
    im = Image.new("RGBA", (px * SS, px * SS), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im), px * SS


def icon_search(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    lw = int(s * 0.085)
    d.ellipse([s * 0.13, s * 0.13, s * 0.66, s * 0.66], outline=WHITE, width=lw)
    d.line([s * 0.62, s * 0.62, s * 0.87, s * 0.87], fill=WHITE, width=lw)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_settings(px, out: Path) -> None:
    """Gear: an annulus plus eight radial teeth."""
    im, d, s = _icon_canvas(px)
    cx = cy = s / 2
    r_out, r_in, teeth = s * 0.34, s * 0.19, 8
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=WHITE)
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        tw = s * 0.085
        pts = []
        for sign in (-1, 1):
            pa = a + sign * tw / (2 * r_out)
            pts.append((cx + r_out * 0.92 * math.cos(pa), cy + r_out * 0.92 * math.sin(pa)))
        for sign in (1, -1):
            pa = a + sign * tw / (2 * s * 0.47)
            pts.append((cx + s * 0.47 * math.cos(pa), cy + s * 0.47 * math.sin(pa)))
        d.polygon(pts, fill=WHITE)
    d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=(0, 0, 0, 0))
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_power(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    lw = int(s * 0.085)
    d.arc([s * 0.17, s * 0.17, s * 0.83, s * 0.83], start=-60, end=240,
          fill=WHITE, width=lw)
    d.line([s * 0.5, s * 0.10, s * 0.5, s * 0.42], fill=WHITE, width=lw)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_chevron(px, out: Path, flip: bool = False) -> None:
    im, d, s = _icon_canvas(px)
    lw = int(s * 0.095)
    pts = [(s * 0.36, s * 0.22), (s * 0.66, s * 0.5), (s * 0.36, s * 0.78)]
    if flip:
        pts = [(s - x, y) for x, y in pts]
    d.line(pts, fill=WHITE, width=lw, joint="curve")
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_arrow(px, out: Path, up: bool = True) -> None:
    im, d, s = _icon_canvas(px)
    pts = [(s * 0.5, s * 0.22), (s * 0.80, s * 0.66), (s * 0.20, s * 0.66)]
    if not up:
        pts = [(x, s - y) for x, y in pts]
    d.polygon(pts, fill=WHITE)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_play(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    d.polygon([(s * 0.28, s * 0.18), (s * 0.84, s * 0.5), (s * 0.28, s * 0.82)], fill=WHITE)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_pause(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    d.rectangle([s * 0.26, s * 0.18, s * 0.42, s * 0.82], fill=WHITE)
    d.rectangle([s * 0.58, s * 0.18, s * 0.74, s * 0.82], fill=WHITE)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_check(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    d.line([(s * 0.18, s * 0.53), (s * 0.41, s * 0.75), (s * 0.83, s * 0.27)],
           fill=WHITE, width=int(s * 0.11), joint="curve")
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_close(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    lw = int(s * 0.10)
    d.line([(s * 0.23, s * 0.23), (s * 0.77, s * 0.77)], fill=WHITE, width=lw)
    d.line([(s * 0.77, s * 0.23), (s * 0.23, s * 0.77)], fill=WHITE, width=lw)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_star(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    cx = cy = s / 2
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        r = s * 0.42 if i % 2 == 0 else s * 0.17
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=WHITE)
    _save(im.resize((px, px), Image.LANCZOS), out)


def icon_folder(px, out: Path) -> None:
    im, d, s = _icon_canvas(px)
    d.rounded_rectangle([s * 0.10, s * 0.28, s * 0.90, s * 0.78],
                        radius=int(s * 0.06), fill=WHITE)
    d.rounded_rectangle([s * 0.10, s * 0.20, s * 0.44, s * 0.34],
                        radius=int(s * 0.04), fill=WHITE)
    _save(im.resize((px, px), Image.LANCZOS), out)


def switch(size, out: Path, on: bool) -> None:
    """Material-style toggle, drawn as ONE image because Kodi's radiobutton takes
    a single texture per state rather than composing track and knob itself.

    Track and knob are both white but at different alphas, so a single
    colordiffuse at the use site tints the whole control coherently: accent when
    on, muted when off.
    """
    w, h = size
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    trh = int(h * 0.52) * SS                     # track height
    trt = (h * SS - trh) // 2
    d.rounded_rectangle([0, trt, w * SS - 1, trt + trh], radius=trh // 2,
                        fill=(255, 255, 255, 90))
    kr = int(h * 0.44) * SS                      # knob radius
    kx = (w * SS - kr) if on else kr
    d.ellipse([kx - kr, h * SS // 2 - kr, kx + kr, h * SS // 2 + kr],
              fill=(255, 255, 255, 255))
    _save(im.resize(size, Image.LANCZOS), out)


def placeholder(size, radius, out: Path) -> None:
    """Fallback art: a flat rounded plate on the card surface colour.

    Not white-and-tinted like the rest, because Kodi's fallback= path draws it
    without our colordiffuse, so it must carry its own colour.
    """
    w, h = size
    im = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w * 2 - 1, h * 2 - 1], radius=radius * 2,
                        fill=(42, 44, 48, 255))
    # a soft inner glyph so an empty card does not read as a loading failure
    cx, cy, r = w, h, min(w, h) * 0.28
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 38),
              width=max(2, int(min(w, h) * 0.05)))
    _save(im.resize(size, Image.LANCZOS), out)


def build(media: Path) -> None:
    c = media / "cable"

    # --- 9-slice masks for rounded artwork. Sized at or above the largest
    # rendered card (a 300px card at 110% zoom is 330) so corner arcs are never
    # upsampled. Used as diffuse=, which is a second texture unit in the SAME
    # draw call, not an extra quad.
    rounded_rect((384, 216), 12, c / "masks" / "card16x9.png")
    rounded_rect((256, 384), 12, c / "masks" / "card2x3.png")
    rounded_rect((256, 256), 12, c / "masks" / "card1x1.png")
    circle((128, 128), c / "masks" / "circle.png")

    # --- surfaces. rounded8 replaces seven separate Estuary button/panel
    # textures; state comes from colordiffuse, not from separate files.
    rounded_rect((48, 48), 8, c / "frames" / "rounded8.png")
    rounded_rect((48, 48), 8, c / "frames" / "rounded8-outline.png", stroke=3)
    rounded_rect((96, 96), 40, c / "frames" / "pill.png")
    rounded_rect((96, 96), 40, c / "frames" / "pill-outline.png", stroke=3)

    # --- elevation. One ring for every raised thing in the skin.
    elevation_ring((128, 128), 14, blur=10, offset_y=4, alpha=200,
                   out=c / "shadows" / "elev.png")

    # --- scrims. 8px wide, stretched horizontally by Kodi; 512 tall to avoid
    # banding. Used with flipy="true" for the reverse direction, so one file
    # serves top and bottom.
    vgrad((8, 512), [(0.0, 1.0), (0.55, 0.35), (1.0, 0.0)],
          c / "scrims" / "vgrad.png")
    vignette((320, 180), c / "scrims" / "vignette.png")
    radial((512, 512), c / "scrims" / "glow.png", gamma=2.2, peak=230)

    # --- controls
    switch((92, 48), c / "controls" / "switch-on.png", on=True)
    switch((92, 48), c / "controls" / "switch-off.png", on=False)

    # --- busy spinner
    arc_spinner((96, 96), c / "spinner.png")

    # --- icons, 64px, white, tinted at the use site
    icons = media / "cable" / "icons"
    icon_search(64, icons / "search.png")
    icon_settings(64, icons / "settings.png")
    icon_power(64, icons / "power.png")
    icon_chevron(64, icons / "chevron-right.png")
    icon_chevron(64, icons / "chevron-left.png", flip=True)
    icon_arrow(64, icons / "arrow-up.png", up=True)
    icon_arrow(64, icons / "arrow-down.png", up=False)
    icon_play(64, icons / "play.png")
    icon_pause(64, icons / "pause.png")
    icon_check(64, icons / "check.png")
    icon_close(64, icons / "close.png")
    icon_star(64, icons / "star.png")
    icon_folder(64, icons / "folder.png")

    # --- fallback plates, matching the aspect of each card family
    placeholder((512, 288), 12, c / "placeholders" / "landscape.png")
    placeholder((342, 513), 12, c / "placeholders" / "poster.png")
    placeholder((512, 512), 12, c / "placeholders" / "square.png")


def brand_assets(skin_dir: Path) -> None:
    """Add-on icon and fanart.

    Kodi shows these in the add-on browser and the skin picker, so they must
    exist -- addon.xml declares them. Estuary's own icon/fanart/screenshots were
    deleted on fork: they depict Estuary, and shipping them would be both
    misleading and 1.2 MB of dead weight.

    The mark is the skin's own idea in miniature: a row of cards with the middle
    one focused, lifted and accented.
    """
    res = skin_dir / "resources"
    res.mkdir(parents=True, exist_ok=True)
    BG, SURFACE, ACCENT = (14, 14, 16), (42, 44, 48), (0, 176, 255)

    def card_row(canvas, cx, cy, cw, ch, gap, n, focus_scale, radius,
                 focused_index=None, dim=1.0):
        """One row of cards. focused_index=None draws an all-unfocused row."""
        d = ImageDraw.Draw(canvas, "RGBA")
        total = n * cw + (n - 1) * gap
        x = cx - total / 2
        surface = tuple(int(c * dim) for c in SURFACE)
        for i in range(n):
            focused = focused_index is not None and i == focused_index
            w, h = (cw * focus_scale, ch * focus_scale) if focused else (cw, ch)
            left, top = x + (cw - w) / 2, cy - h / 2
            if focused:
                # a cheap stand-in for elevation: a soft dark plate behind
                glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                ImageDraw.Draw(glow).rounded_rectangle(
                    [left - 6, top - 2, left + w + 6, top + h + 10],
                    radius=radius, fill=(0, 0, 0, 150))
                canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
                d = ImageDraw.Draw(canvas, "RGBA")
            d.rounded_rectangle([left, top, left + w, top + h], radius=radius,
                                fill=ACCENT + (255,) if focused else surface + (255,))
            x += cw + gap

    icon = Image.new("RGBA", (512, 512), BG + (255,))
    card_row(icon, 256, 250, 116, 65, 22, 3, 1.34, 10, focused_index=1)
    d = ImageDraw.Draw(icon)
    d.rounded_rectangle([176, 372, 336, 384], radius=6, fill=(255, 255, 255, 46))
    d.rounded_rectangle([176, 402, 272, 414], radius=6, fill=(255, 255, 255, 28))
    _save(icon, res / "icon.png")

    fan = Image.new("RGBA", (1920, 1080), BG + (255,))
    # Three rows. Only the focused row carries an accent card and the focus
    # scale, and the rows either side are dimmed -- which is exactly what the
    # skin does at runtime, so the fanart is an honest preview.
    card_row(fan, 960, 300, 300, 169, 28, 5, 1.0, 16, dim=0.55)
    card_row(fan, 960, 570, 300, 169, 28, 5, 1.10, 16, focused_index=2)
    card_row(fan, 960, 850, 300, 169, 28, 5, 1.0, 16, dim=0.55)
    # Bottom scrim: darkest at the bottom edge, fading upward.
    vig = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    band = 4
    steps = 150
    for i in range(steps):
        y = 1080 - (i + 1) * band
        a = int(165 * (1 - i / steps) ** 1.4)
        vd.rectangle([0, y, 1920, y + band], fill=(0, 0, 0, a))
    fan.alpha_composite(vig)
    fan.convert("RGB").save(res / "fanart.jpg", quality=88, optimize=True,
                            progressive=True)


# Estuary textures we regenerate IN PLACE, at byte-identical dimensions and with
# the same 9-slice semantics, so that ~170 inherited call sites pick up Cable TV's
# corner radius and palette without editing a single one of them. This is the
# texture-side equivalent of the colour retarget in colors/defaults.xml.
#
# Semantics matter here and are not guessable, so they were read off Estuary's
# actual pixels: the dark surfaces bake their own colour because many call sites
# use them bare with no colordiffuse, while the focus fill is near-white
# precisely so call sites CAN tint it. Keep Estuary's alpha values - inherited
# XML composites these against each other and expects them.
ESTUARY_OVERRIDES = [
    # (path,                          size,     radius, rgb,          alpha)
    ("buttons/button-fo.png",         (80, 80), 12, (255, 255, 255), 255),
    ("buttons/button-nofo.png",       (80, 80), 12, (26, 27, 30),    204),
    ("buttons/dialogbutton-fo.png",   (80, 80), 12, (255, 255, 255), 255),
    ("buttons/dialogbutton-nofo.png", (80, 80), 12, (37, 39, 43),    128),
    ("dialogs/dialog-bg.png",         (80, 80), 12, (26, 27, 30),    204),
]
# 1x1 flat fills: stretched by Kodi and tinted at most call sites. Retargeted to
# the Cable TV palette; dimensions must stay 1x1 or the stretch changes.
ESTUARY_FLAT_OVERRIDES = [
    ("dialogs/dialog-bg-nobo.png", (26, 27, 30), 204),
    ("lists/panel.png",            (26, 27, 30), 230),
    ("lists/focus.png",            (255, 255, 255), 255),
]


def estuary_overrides(media: Path) -> None:
    for rel, size, radius, rgb, alpha in ESTUARY_OVERRIDES:
        w, h = size
        im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle(
            [0, 0, w * SS - 1, h * SS - 1], radius=radius * SS, fill=rgb + (alpha,))
        _save(im.resize(size, Image.LANCZOS), media / rel)

    for rel, rgb, alpha in ESTUARY_FLAT_OVERRIDES:
        _save(Image.new("RGBA", (1, 1), rgb + (alpha,)), media / rel)

    # overlays/shadow.png is used 53 times as
    #   <bordertexture border="21" infill="false">overlays/shadow.png
    # so the gradient must live INSIDE the outer 21px band and be uniform along
    # each edge, because Kodi stretches the edge strips. A blurred rounded rect
    # whose edge sits ~18px in satisfies both.
    w = h = 80
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([18, 18, w - 19, h - 19], radius=12,
                                         fill=(0, 0, 0, 195))
    _save(im.filter(ImageFilter.GaussianBlur(7)), media / "overlays" / "shadow.png")

    # frame/InfoBar.png (16x512) is the top/bottom bar gradient in TopBar and
    # BottomBar. Estuary's is a teal-tinted bar; ours is a neutral scrim so the
    # bars read as darkening over content rather than as a coloured chrome strip.
    bar = Image.new("RGBA", (16, 512), (0, 0, 0, 0))
    px = bar.load()
    for y in range(512):
        t = y / 511
        a = int(round(214 * ((1 - t) ** 1.35)))
        for x in range(16):
            px[x, y] = (10, 10, 12, a)
    _save(bar, media / "frame" / "InfoBar.png")


OVERRIDE_PATHS = ([rel for rel, *_ in ESTUARY_OVERRIDES]
                  + [rel for rel, *_ in ESTUARY_FLAT_OVERRIDES]
                  + ["overlays/shadow.png", "frame/InfoBar.png"])


def digest_tree(root: Path) -> dict[str, str]:
    """Digest of everything gen_textures owns: media/cable plus the in-place
    Estuary overrides."""
    out = {
        "cable/" + str(p.relative_to(root / "cable")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "cable").rglob("*.png"))
    } if (root / "cable").is_dir() else {}
    for rel in OVERRIDE_PATHS:
        f = root / rel
        if f.is_file():
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skin", required=True)
    ap.add_argument("--check", action="store_true",
                    help="regenerate into a temp dir and verify byte-identical output")
    args = ap.parse_args()

    skin_dir = REPO / args.skin
    if not skin_dir.is_dir():
        raise SystemExit(f"error: {skin_dir} does not exist")

    if args.check:
        existing = digest_tree(skin_dir / "media")
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp))
            estuary_overrides(Path(tmp))
            fresh = digest_tree(Path(tmp))
        if existing != fresh:
            only_committed = sorted(set(existing) - set(fresh))
            only_fresh = sorted(set(fresh) - set(existing))
            changed = sorted(k for k in set(existing) & set(fresh)
                             if existing[k] != fresh[k])
            print("texture set is NOT reproducible from tools/gen_textures.py:",
                  file=sys.stderr)
            for k in only_committed:
                print(f"  committed but not generated: {k}", file=sys.stderr)
            for k in only_fresh:
                print(f"  generated but not committed: {k}", file=sys.stderr)
            for k in changed:
                print(f"  content differs: {k}", file=sys.stderr)
            return 1
        total = sum((skin_dir / "media" / k).stat().st_size for k in existing)
        print(f"reproducible: {len(existing)} textures, {total} bytes total")
        return 0

    build(skin_dir / "media")
    estuary_overrides(skin_dir / "media")
    brand_assets(skin_dir)
    tree = digest_tree(skin_dir / "media")
    total = sum((skin_dir / "media" / k).stat().st_size for k in tree)
    for name in sorted(tree):
        f = skin_dir / "media" / name
        with Image.open(f) as im:
            print(f"  {name:44} {im.size[0]:>4}x{im.size[1]:<4} {f.stat().st_size:>6} B")
    print(f"{len(tree)} textures, {total} bytes ({total/1024:.1f} KiB) total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
