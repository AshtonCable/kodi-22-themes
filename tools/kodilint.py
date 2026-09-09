#!/usr/bin/env python3
"""Static validator for Kodi 22 skins.

Kodi cannot be run in CI here, and more importantly a whole class of skinning
mistakes produces *no* runtime diagnostic at all:

  * an unknown colour name falls through CGUIColorManager::GetColor to
    sscanf("%x") and renders fully transparent -- silently,
  * duplicate <include>/<constant>/<variable>/<default> names resolve
    first-wins via try_emplace -- silently,
  * <include file=".." condition=".."> ignores the condition inside a window
    file (ResolveIncludes) while honouring it inside Includes.xml
    (LoadIncludes) -- silently,
  * $VAR[] in <font> or in a texture tag other than <texture>/<imagepath>
    resolves to nothing -- silently.

So this linter is the primary verification for these skins, not a nicety. It is
calibrated against vendored Estuary 4.1.0: `kodilint.py vendor/skin.estuary`
must report exactly the known-real findings and nothing else. That calibration
is a CI job, and it is what stops the linter from rotting into noise.

stdlib only, apart from an optional Pillow pass for asset checks.

Exit: 0 clean, 1 findings at or above the fail threshold, 2 tool failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REF = Path(__file__).resolve().parent / "reference"

# ---------------------------------------------------------------------------
# Kodi engine constants. Every one of these was read out of the Kodi 22 source
# rather than assumed; changing them without re-reading the source is a bug.
# ---------------------------------------------------------------------------

# GUIFontManager.cpp: the complete set. Anything else is silently ignored.
VALID_FONT_STYLES = {
    "bold", "italics", "bolditalics", "uppercase", "lowercase", "capitalize", "lighten",
}

# GUIControlFactory.cpp:1139/1181 -- only these two *control* texture tags go
# through GetInfoTexture. Every other control texture tag is resolved once,
# statically, at parse time, so $VAR/$INFO in one silently yields nothing.
INFO_DRIVEN_TEXTURE_TAGS = {"texture", "imagepath"}

# <icon>/<thumb> on a static <item> are NOT control textures: GUIStaticItem.cpp:31
# reads them with GetInfoLabel, so they are infolabels and $VAR/$INFO is correct
# and idiomatic there (shipping Estuary relies on it). Never flag these.
INFOLABEL_ART_TAGS = {"icon", "thumb"}

# Tags naming a texture file. Used both for existence checks and to detect
# $VAR misuse in the ones that are not info-driven.
TEXTURE_TAGS = {
    "texture", "imagepath", "bordertexture", "texturefocus", "texturenofocus",
    "texturefocusdisabled", "texturenofocusdisabled", "texturebg", "midtexture",
    "lefttexture", "middletexture", "righttexture", "overlaytexture", "alttexture",
    "textureradioonfocus", "textureradioonnofocus", "textureradioofffocus",
    "textureradiooffnofocus", "textureradioon", "textureradiooff",
    "texturesliderbackground", "texturesliderbar", "texturesliderbarfocus",
    "textureslidernib", "textureslidernibfocus", "texturecheckmark",
    "texturecheckmarknofocus", "textureup", "texturedown", "textureupfocus",
    "texturedownfocus", "textureupdisabled", "texturedowndisabled",
    "textureleft", "textureright", "textureleftfocus", "texturerightfocus",
    "texturepagecontrol", "tickmarkstexture", "thumb", "icon",
}

# GUIControlFactory.cpp: fields parsed as CGUIInfoColor.
COLOR_TAGS = {
    "textcolor", "focusedcolor", "disabledcolor", "shadowcolor", "selectedcolor",
    "invalidcolor", "headlinecolor", "titlecolor", "colordiffuse", "colorbox",
    "hitrectcolor", "controllerdiffuse", "backgroundcolor", "textcolor2",
    "midcolor", "bordercolor", "glyphcolor",
}

# Root element per file, by name; anything else in xml/ may be window or includes.
ROOT_BY_NAME = {
    "Font.xml": {"fonts"},
    "Timers.xml": {"timers"},
}
GENERIC_ROOTS = {"window", "includes"}

# Geometry-ish tags whose value must be a number, percentage, constant name or
# one of Kodi's positioning keywords.
GEOMETRY_TAGS = {
    "left", "right", "top", "bottom", "width", "height", "centerleft",
    "centerright", "centertop", "centerbottom", "offsetx", "offsety",
    "itemgap", "textoffsetx", "textoffsety", "bordersize", "depth",
    "radioposx", "radioposy", "radiowidth", "radioheight",
    "spinposx", "spinposy", "spinwidth", "spinheight",
    "sliderwidth", "sliderheight", "textwidth", "itemwidth", "itemheight",
    "texturewidth", "textureheight", "movement", "focusposition",
}
# Control types that can never take focus, so a duplicate id on them is inert:
# nothing can SetFocus() them and Control.HasFocus() is meaningless. Estuary puts
# id="1" on five such controls in VideoFullScreen.xml. Anything NOT listed here is
# treated as focusable, so the check errs toward reporting.
NON_FOCUSABLE_TYPES = {
    "image", "largeimage", "multiimage", "label", "fadelabel", "group",
    "progress", "visualisation", "videowindow", "gamewindow", "gamecontroller",
    "rss", "ranges", "border",
}

GEOMETRY_KEYWORDS = {
    "auto", "r", "p", "keep", "stretch", "scale", "center", "left", "right",
    "top", "bottom", "true", "false", "middle", "justify", "no", "yes",
}

HEXCOLOR_RE = re.compile(r"^[0-9a-fA-F]{8}$")
NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?%?$")
VAR_RE = re.compile(r"\$VAR\[([^\],]+)")
EXP_RE = re.compile(r"\$EXP\[([^\]]+)\]")
LOCALIZE_RE = re.compile(r"\$LOCALIZE\[(\d+)\]")
PARAM_RE = re.compile(r"\$PARAM\[([^\]]+)\]")
SKIN_STR_RE = re.compile(r"\$LOCALIZE\[(\d+)\]")

SEVERITIES = {"ERROR": 0, "WARN": 1, "INFO": 2}


@dataclass
class Finding:
    severity: str
    code: str
    path: Path
    line: int
    message: str

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root.parent)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}: {self.severity} {self.code} {self.message}"


@dataclass
class Skin:
    """Everything the linter needs to know about one skin directory."""
    root: Path
    res_dirs: list[Path] = field(default_factory=list)
    addon_id: str = ""
    gui_version: str = ""
    debugging: str = ""
    # definitions
    includes: dict[str, Path] = field(default_factory=dict)
    variables: dict[str, Path] = field(default_factory=dict)
    expressions: dict[str, Path] = field(default_factory=dict)
    constants: dict[str, Path] = field(default_factory=dict)
    defaults: dict[str, Path] = field(default_factory=dict)
    fonts: dict[str, Path] = field(default_factory=dict)
    colors: set[str] = field(default_factory=set)
    theme_colors: dict[str, set[str]] = field(default_factory=dict)
    skin_strings: set[int] = field(default_factory=set)
    include_params: dict[str, set[str]] = field(default_factory=dict)
    # raw text cache for line lookup
    text: dict[Path, str] = field(default_factory=dict)
    trees: dict[Path, ET.Element] = field(default_factory=dict)


_LINE_CURSOR: dict[tuple[Path, str], int] = {}


def line_of(skin: Skin, path: Path, needle: str, fallback: int = 1) -> int:
    """Recover a line number for a token.

    ElementTree does not expose source positions, and subclassing the parser to
    get them is more fragile than just finding the literal in the raw text --
    the output only needs to point a human at the right line.

    Repeated tokens advance a per-(file, token) cursor, so N findings on the same
    literal report N distinct lines instead of the first line N times.
    """
    text = skin.text.get(path)
    if not text or not needle:
        return fallback
    key = (path, needle)
    start = _LINE_CURSOR.get(key, 0)
    idx = text.find(needle, start)
    if idx < 0:                      # wrap: more findings than literal occurrences
        idx = text.find(needle)
        if idx < 0:
            return fallback
    _LINE_CURSOR[key] = idx + len(needle)
    return text.count("\n", 0, idx) + 1


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_addon_xml(skin: Skin, findings: list[Finding]) -> None:
    addon = skin.root / "addon.xml"
    if not addon.is_file():
        findings.append(Finding("ERROR", "E16", addon, 1, "addon.xml is missing"))
        return
    skin.text[addon] = addon.read_text(errors="replace")
    try:
        tree = ET.parse(addon).getroot()
    except ET.ParseError as exc:
        findings.append(Finding("ERROR", "E01", addon, getattr(exc, "position", (1, 0))[0],
                                f"addon.xml is not well-formed: {exc}"))
        return

    skin.addon_id = tree.get("id", "")
    if skin.addon_id != skin.root.name:
        findings.append(Finding(
            "ERROR", "E16", addon, line_of(skin, addon, 'id='),
            f"addon id {skin.addon_id!r} must equal the directory name "
            f"{skin.root.name!r} -- Kodi will refuse to load it otherwise"))

    for imp in tree.iter("import"):
        if imp.get("addon") == "xbmc.gui":
            skin.gui_version = imp.get("version", "")

    ext = tree.find("./extension[@point='xbmc.gui.skin']")
    if ext is None:
        findings.append(Finding("ERROR", "E16", addon, 1,
                                "no <extension point=\"xbmc.gui.skin\"> -- not a skin"))
        return
    skin.debugging = ext.get("debugging", "false")

    seen: set[str] = set()
    for res in ext.iter("res"):
        folder = res.get("folder", "")
        if not folder or folder in seen:
            continue
        seen.add(folder)
        d = skin.root / folder
        if not d.is_dir():
            findings.append(Finding(
                "ERROR", "E15", addon, line_of(skin, addon, f'folder="{folder}"'),
                f"<res folder=\"{folder}\"> names a directory that does not exist"))
        else:
            skin.res_dirs.append(d)


def parse_xml_files(skin: Skin, findings: list[Finding]) -> None:
    for res in skin.res_dirs:
        for path in sorted(res.glob("*.xml")):
            skin.text[path] = path.read_text(errors="replace")
            try:
                skin.trees[path] = ET.parse(path).getroot()
            except ET.ParseError as exc:
                row = getattr(exc, "position", (1, 0))[0]
                findings.append(Finding("ERROR", "E01", path, row,
                                        f"not well-formed XML: {exc}"))
                continue
            root_tag = skin.trees[path].tag
            expected = ROOT_BY_NAME.get(path.name, GENERIC_ROOTS)
            if root_tag not in expected:
                findings.append(Finding(
                    "ERROR", "E02", path, line_of(skin, path, f"<{root_tag}"),
                    f"root element <{root_tag}> is not valid here "
                    f"(expected one of {sorted(expected)}) -- CGUIWindow rejects it"))


def load_colors(skin: Skin, findings: list[Finding]) -> None:
    cdir = skin.root / "colors"
    defaults = cdir / "defaults.xml"
    if not defaults.is_file():
        findings.append(Finding("ERROR", "E12", cdir / "defaults.xml", 1,
                                "colors/defaults.xml is missing -- every colour "
                                "name in the skin will render transparent"))
        return
    for path in sorted(cdir.glob("*.xml")):
        skin.text[path] = path.read_text(errors="replace")
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            findings.append(Finding("ERROR", "E01", path, 1, f"not well-formed XML: {exc}"))
            continue
        if root.tag != "colors":
            findings.append(Finding("ERROR", "E02", path, 1,
                                    f"colour file root is <{root.tag}>, expected <colors>"))
            continue
        names = {c.get("name", "") for c in root.iter("color") if c.get("name")}
        if path.name == "defaults.xml":
            skin.colors = names
        else:
            skin.theme_colors[path.stem] = names


def load_strings(skin: Skin, findings: list[Finding]) -> None:
    po = skin.root / "language" / "resource.language.en_gb" / "strings.po"
    if not po.is_file():
        findings.append(Finding("ERROR", "E07", po, 1,
                                "language/resource.language.en_gb/strings.po is missing"))
        return
    skin.text[po] = po.read_text(errors="replace")
    skin.skin_strings = {
        int(m) for m in re.findall(r'^msgctxt\s+"#(\d+)"', skin.text[po], flags=re.MULTILINE)
    }


def conditional_include_branches(skin: Skin) -> dict[Path, str]:
    """Map file -> condition for <include file=".." condition=".."/> in an includes file.

    GUIIncludes.cpp:221-236 honours the condition in LoadIncludes, so only one
    branch is ever loaded. Estuary uses this for Constants_720 / Constants_1080,
    which legitimately define the same 18 constant names.
    """
    branches: dict[Path, str] = {}
    for path, root in skin.trees.items():
        if root.tag != "includes":
            continue
        for el in root.iter("include"):
            fileref, cond = el.get("file"), el.get("condition")
            if not (fileref and cond):
                continue
            for res in skin.res_dirs:
                target = res / fileref
                if target.is_file():
                    branches[target] = cond
                    break
    return branches


def collect_definitions(skin: Skin, findings: list[Finding]) -> None:
    """Pass 1. Kodi's include namespace is global, so collect across all files.

    try_emplace means a duplicate name is silently ignored and the FIRST
    definition wins, which is one of the nastiest bugs to chase by hand.
    """
    branches = conditional_include_branches(skin)

    def note(kind: str, table: dict[str, Path], name: str, path: Path, code: str) -> None:
        if name in table:
            first = table[name]
            # Two different mutually-exclusive conditional includes: only one is
            # ever loaded, so this is not a collision.
            if (path in branches and first in branches
                    and branches[path] != branches[first]):
                return
            first = table[name]
            try:
                first_rel = first.relative_to(skin.root)
            except ValueError:
                first_rel = first
            findings.append(Finding(
                "WARN", code, path, line_of(skin, path, f'name="{name}"'),
                f"duplicate {kind} {name!r}; first definition in {first_rel} wins "
                f"silently (try_emplace) -- this one has no effect"))
        else:
            table[name] = path

    for path, root in skin.trees.items():
        for el in root.iter("include"):
            name = el.get("name")
            if name:
                note("<include>", skin.includes, name, path, "W03")
                params = {p.get("name") for p in el.findall("param") if p.get("name")}
                skin.include_params[name] = {p for p in params if p}
        for el in root.iter("variable"):
            if el.get("name"):
                note("<variable>", skin.variables, el.get("name", ""), path, "W03")
        for el in root.iter("expression"):
            if el.get("name"):
                note("<expression>", skin.expressions, el.get("name", ""), path, "W03")
        for el in root.iter("constant"):
            if el.get("name"):
                note("<constant>", skin.constants, el.get("name", ""), path, "W03")
        for el in root.iter("default"):
            if el.get("type"):
                note("<default type>", skin.defaults, el.get("type", ""), path, "W03")

    font_xml = None
    for path, root in skin.trees.items():
        if root.tag == "fonts":
            font_xml = path
            for fs in root.iter("fontset"):
                for f in fs.iter("font"):
                    nm = f.findtext("name", "").strip()
                    if nm and nm not in skin.fonts:
                        skin.fonts[nm] = path
    if font_xml is None:
        findings.append(Finding("ERROR", "E09", skin.root / "xml" / "Font.xml", 1,
                                "no <fonts> file found -- every <font> reference will fail"))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _skippable_value(value: str) -> bool:
    """Values the linter cannot statically resolve, and must not flag."""
    if not value or value in {"-", "0"}:
        return True
    if "$" in value:            # $INFO / $VAR / $PARAM / $LOCALIZE / $EXP
        return True
    if "://" in value:          # full VFS path or URL
        return True
    return False


def check_texture_paths(skin: Skin, findings: list[Finding]) -> None:
    bundled = set()
    bt = REF / "bundled_textures.txt"
    if bt.is_file():
        bundled = {l.strip() for l in bt.read_text().splitlines() if l.strip()}

    media = skin.root / "media"

    def resolve(value: str) -> bool:
        if value.startswith("special://skin/"):
            return (skin.root / value[len("special://skin/"):]).exists()
        if value.startswith("special://xbmc/media/"):
            return value[len("special://xbmc/media/"):] in bundled
        if value.startswith("special://"):
            return True   # profile/home/temp paths are runtime-only
        return (media / value).exists()

    for path, root in skin.trees.items():
        for el in root.iter():
            tag = el.tag.lower()
            # tag values
            if tag in TEXTURE_TAGS:
                value = (el.text or "").strip()
                if not _skippable_value(value) and not HEXCOLOR_RE.match(value):
                    if not resolve(value):
                        findings.append(Finding(
                            "ERROR", "E06", path, line_of(skin, path, value),
                            f"<{el.tag}> references missing texture {value!r} "
                            f"(looked in media/)"))
                # $VAR in a tag that is not info-driven silently resolves to nothing
                if "$VAR[" in value or "$INFO[" in value:
                    if (tag not in INFO_DRIVEN_TEXTURE_TAGS
                            and tag not in INFOLABEL_ART_TAGS):
                        findings.append(Finding(
                            "WARN", "W05", path, line_of(skin, path, value),
                            f"<{el.tag}> is not info-driven; only "
                            f"{sorted(INFO_DRIVEN_TEXTURE_TAGS)} accept $VAR/$INFO, "
                            f"so {value!r} will resolve to nothing"))
            # diffuse= is always a texture. fallback= is a texture ONLY on a
            # texture tag -- on <label> it is a localised string id.
            attrs = ["diffuse"] + (["fallback"] if tag in TEXTURE_TAGS else [])
            for attr in attrs:
                value = (el.get(attr) or "").strip()
                if value and not _skippable_value(value) and not resolve(value):
                    findings.append(Finding(
                        "ERROR", "E06", path, line_of(skin, path, value),
                        f"{attr}=\"{value}\" references a missing texture "
                        f"(looked in media/)"))


def check_colors(skin: Skin, findings: list[Finding]) -> None:
    """The highest-value check in the tool.

    CGUIColorManager::GetColor falls through an unknown name to sscanf("%x"),
    yielding 0 -- fully transparent -- and logs nothing at all.
    """
    for path, root in skin.trees.items():
        for el in root.iter():
            values = []
            if el.tag.lower() in COLOR_TAGS:
                values.append((el.tag, (el.text or "").strip()))
            for attr in ("colordiffuse", "textcolor", "focusedcolor", "colorbox"):
                v = (el.get(attr) or "").strip()
                if v:
                    values.append((attr, v))
            for where, value in values:
                if _skippable_value(value) or HEXCOLOR_RE.match(value):
                    continue
                if value in skin.colors:
                    continue
                # defined only in a theme overlay -> transparent when that theme is off
                owners = [t for t, names in skin.theme_colors.items() if value in names]
                if owners:
                    findings.append(Finding(
                        "WARN", "W10", path, line_of(skin, path, value),
                        f"colour {value!r} (used in {where}) is defined only in "
                        f"colors/{owners[0]}.xml, not defaults.xml -- renders "
                        f"transparent whenever that theme is not selected"))
                else:
                    findings.append(Finding(
                        "ERROR", "E12", path, line_of(skin, path, value),
                        f"unknown colour {value!r} in <{where}> -- "
                        f"GetColor() will render it fully transparent with no log line"))


def check_include_refs(skin: Skin, findings: list[Finding]) -> None:
    for path, root in skin.trees.items():
        is_window = root.tag == "window"
        for el in root.iter("include"):
            if el.get("name"):
                continue  # a definition
            # <include file="X" condition="C"/> -- condition honoured only in LoadIncludes
            fileref = el.get("file")
            if fileref:
                if el.get("condition") and is_window:
                    findings.append(Finding(
                        "WARN", "W04", path, line_of(skin, path, f'file="{fileref}"'),
                        f"<include file=\"{fileref}\" condition=...> inside a window "
                        f"file: ResolveIncludes loads the file unconditionally and "
                        f"ignores the condition (it is only honoured in Includes.xml)"))
                for res in skin.res_dirs:
                    if (res / fileref).is_file():
                        break
                else:
                    findings.append(Finding(
                        "ERROR", "E03", path, line_of(skin, path, f'file="{fileref}"'),
                        f"<include file=\"{fileref}\"> does not exist"))
                continue
            name = el.get("content") or (el.text or "").strip()
            if not name or "$" in name:
                continue
            if name not in skin.includes:
                findings.append(Finding(
                    "ERROR", "E03", path, line_of(skin, path, name),
                    f"<include> references undefined include {name!r}"))


def check_var_exp_refs(skin: Skin, findings: list[Finding]) -> None:
    for path, text in skin.text.items():
        if path.suffix != ".xml" or path not in skin.trees:
            continue
        for name in set(VAR_RE.findall(text)):
            n = name.strip()
            if n and n not in skin.variables:
                findings.append(Finding("ERROR", "E04", path, line_of(skin, path, f"$VAR[{name}"),
                                        f"$VAR[{n}] is not defined by any <variable>"))
        for name in set(EXP_RE.findall(text)):
            n = name.strip()
            if n and n not in skin.expressions:
                findings.append(Finding("ERROR", "E05", path, line_of(skin, path, f"$EXP[{name}]"),
                                        f"$EXP[{n}] is not defined by any <expression>"))


def check_localize(skin: Skin, findings: list[Finding]) -> None:
    core: set[int] = set()
    cs = REF / "core_string_ids.txt"
    if cs.is_file():
        core = {int(l) for l in cs.read_text().split() if l.isdigit()}
    lo, hi = 31000, 31999
    pin = REF / "PIN.json"
    if pin.is_file():
        lo, hi = json.loads(pin.read_text()).get("skin_string_range", [lo, hi])

    used_skin: set[int] = set()
    for path, text in skin.text.items():
        if path not in skin.trees:
            continue
        for raw in set(LOCALIZE_RE.findall(text)):
            sid = int(raw)
            if lo <= sid <= hi:
                used_skin.add(sid)
                if sid not in skin.skin_strings:
                    findings.append(Finding(
                        "ERROR", "E07", path, line_of(skin, path, f"$LOCALIZE[{raw}]"),
                        f"$LOCALIZE[{sid}] is in the skin range {lo}-{hi} but is not "
                        f"defined in strings.po"))
            elif core and sid not in core:
                findings.append(Finding(
                    "ERROR", "E08", path, line_of(skin, path, f"$LOCALIZE[{raw}]"),
                    f"$LOCALIZE[{sid}] is not a Kodi core string id"))

    for sid in sorted(skin.skin_strings - used_skin):
        po = skin.root / "language" / "resource.language.en_gb" / "strings.po"
        findings.append(Finding("INFO", "W08", po, line_of(skin, po, f'"#{sid}"'),
                                f"skin string {sid} is defined but never used"))


def check_fonts(skin: Skin, findings: list[Finding]) -> None:
    bundled = set()
    bf = REF / "bundled_fonts.txt"
    if bf.is_file():
        bundled = {l.strip() for l in bf.read_text().splitlines() if l.strip()}
    fonts_dir = skin.root / "fonts"

    for path, root in skin.trees.items():
        if root.tag != "fonts":
            continue
        for f in root.iter("font"):
            filename = (f.findtext("filename", "") or "").strip()
            name = (f.findtext("name", "") or "").strip()
            if not filename:
                continue
            if not filename.lower().endswith(".ttf"):
                findings.append(Finding(
                    "WARN", "W06", path, line_of(skin, path, filename),
                    f"font {name!r} uses {filename!r}; GUIFontManager accepts only "
                    f".ttf and silently rejects anything else"))
            if not (fonts_dir / filename).is_file() and filename not in bundled:
                findings.append(Finding(
                    "ERROR", "E10", path, line_of(skin, path, filename),
                    f"font file {filename!r} is neither in fonts/ nor bundled with "
                    f"Kodi (bundled: {sorted(bundled)})"))
            for style_el in f.iter("style"):
                for token in (style_el.text or "").split():
                    if token.lower() not in VALID_FONT_STYLES:
                        findings.append(Finding(
                            "ERROR", "E11", path, line_of(skin, path, token),
                            f"<style>{token}</style> is not a valid Kodi font style "
                            f"(valid: {sorted(VALID_FONT_STYLES)}) and is ignored"))

    for path, root in skin.trees.items():
        for el in root.iter("font"):
            if el.find("filename") is not None or el.find("name") is not None:
                continue  # a definition inside Font.xml
            value = (el.text or "").strip()
            if not value:
                continue
            if "$VAR[" in value or "$INFO[" in value:
                findings.append(Finding(
                    "WARN", "W05", path, line_of(skin, path, value),
                    f"<font>{value}</font> -- <font> is resolved once at parse time and "
                    f"is not info-driven, so this will resolve to no font"))
            elif "$" in value:
                continue  # $PARAM in a parameterised include: resolved by the caller
            elif value not in skin.fonts:
                findings.append(Finding(
                    "ERROR", "E09", path, line_of(skin, path, value),
                    f"<font> references {value!r} which is not defined in Font.xml"))


def check_required_files(skin: Skin, findings: list[Finding]) -> None:
    rf = REF / "required_files.json"
    if not rf.is_file():
        return
    required = json.loads(rf.read_text())
    present = {p.name for res in skin.res_dirs for p in res.glob("*.xml")}
    for name in required:
        if name not in present:
            findings.append(Finding(
                "ERROR", "E13", skin.root, 1,
                f"required window file {name} is missing -- Kodi 22 expects all "
                f"{len(required)} of them"))


def check_gui_version(skin: Skin, findings: list[Finding], expect: str) -> None:
    addon = skin.root / "addon.xml"
    if skin.gui_version != expect:
        findings.append(Finding(
            "ERROR", "E14", addon, line_of(skin, addon, "xbmc.gui"),
            f"<import addon=\"xbmc.gui\" version=\"{skin.gui_version}\"> should be "
            f"{expect} for this Kodi target"))


def check_release_flags(skin: Skin, findings: list[Finding], release: bool) -> None:
    if release and skin.debugging.lower() == "true":
        addon = skin.root / "addon.xml"
        findings.append(Finding(
            "ERROR", "E17", addon, line_of(skin, addon, 'debugging='),
            'extension has debugging="true"; must be "false" in a release'))


def _visible_ancestors(parents: dict, el) -> list[str]:
    """<visible> conditions on an element's ancestor chain."""
    conds, cur = [], parents.get(el)
    while cur is not None:
        cond = cur.findtext("visible")
        if cond and cond.strip():
            conds.append(" ".join(cond.split()))
        cur = parents.get(cur)
    return conds


def _looks_complementary(a: list[str], b: list[str]) -> bool:
    """True if two ancestor condition sets look mutually exclusive.

    Estuary's DialogMusicInfo defines control 130 twice, in sibling groups whose
    <visible> conditions are negations of one another, so only one ever exists to
    be focused. That is a legitimate idiom, not a collision. Detecting genuine
    logical negation is not tractable here, so we look for the shape Kodi skins
    actually use: one condition containing the other's terms with '!' flipped.
    """
    for ca in a:
        for cb in b:
            if ca == cb:
                continue
            na, nb = ca.replace("!", ""), cb.replace("!", "")
            if na == nb and ca != cb:
                return True
            # "X + Y" vs "!X | !Y" -- De Morgan, the common hand-written form
            if (set(na.replace("|", "+").split("+")) ==
                    set(nb.replace("|", "+").split("+")) and ca != cb):
                return True
    return False


def check_control_ids(skin: Skin, findings: list[Finding]) -> None:
    for path, root in skin.trees.items():
        if root.tag != "window":
            continue
        parents = {child: parent for parent in root.iter() for child in parent}
        by_id: dict[str, list] = defaultdict(list)
        for el in root.iter("control"):
            cid = el.get("id")
            if not cid or cid == "0":
                continue
            if (el.get("type") or "").lower() in NON_FOCUSABLE_TYPES:
                continue   # inert: cannot be focused or addressed
            by_id[cid].append(el)

        for cid, els in sorted(by_id.items()):
            if len(els) < 2:
                continue
            conds = [_visible_ancestors(parents, el) for el in els]
            exclusive = all(
                _looks_complementary(conds[i], conds[j])
                for i in range(len(els)) for j in range(i + 1, len(els))
            ) and all(conds)
            if exclusive:
                findings.append(Finding(
                    "INFO", "I02", path, line_of(skin, path, f'id="{cid}"'),
                    f"control id {cid} is defined {len(els)} times but the copies sit "
                    f"under mutually exclusive <visible> conditions, so only one "
                    f"exists at a time"))
            else:
                findings.append(Finding(
                    "WARN", "W01", path, line_of(skin, path, f'id="{cid}"'),
                    f"focusable control id {cid} is used {len(els)} times in one "
                    f"window; SetFocus/Control.HasFocus will address only the first"))


def check_geometry(skin: Skin, findings: list[Finding]) -> None:
    for path, root in skin.trees.items():
        for el in root.iter():
            if el.tag.lower() not in GEOMETRY_TAGS:
                continue
            value = (el.text or "").strip()
            if not value or "$" in value or NUMERIC_RE.match(value):
                continue
            if value.lower() in GEOMETRY_KEYWORDS or value in skin.constants:
                continue
            if all(part.strip() in skin.constants or NUMERIC_RE.match(part.strip())
                   for part in value.split(",") if part.strip()):
                continue
            findings.append(Finding(
                "WARN", "W02", path, line_of(skin, path, f">{value}<"),
                f"<{el.tag}>{value}</{el.tag}> is neither a number, a keyword, nor a "
                f"<constant> -- it will parse as 0"))


def check_animations(skin: Skin, findings: list[Finding]) -> None:
    """loop/pulse only work on type="Conditional" (VisibleEffect.cpp)."""
    for path, root in skin.trees.items():
        for el in root.iter("animation"):
            atype = (el.get("type") or (el.text or "").strip()).lower()
            for attr in ("loop", "pulse"):
                if (el.get(attr) or "").lower() == "true" and atype != "conditional":
                    findings.append(Finding(
                        "WARN", "W07", path, line_of(skin, path, f'{attr}="true"'),
                        f'{attr}="true" only has an effect on '
                        f'<animation type="Conditional">, not {atype!r}'))
            # a looping slide with a tween cannot wrap seamlessly
            if (el.get("loop") or "").lower() == "true":
                for eff in list(el.iter("effect")) or [el]:
                    if (eff.get("type") or "").lower() == "slide" and eff.get("tween"):
                        findings.append(Finding(
                            "WARN", "W07", path, line_of(skin, path, 'loop="true"'),
                            "looping slide carries a tween; loop resets with no "
                            "interpolation, so the wrap will be visible -- use linear"))


def check_params(skin: Skin, findings: list[Finding]) -> None:
    """INFO-only: Kodi resolves an unsupplied $PARAM to empty string, which is
    the intended behaviour for optional params, so this is noisy by nature."""
    supplied: dict[str, set[str]] = defaultdict(set)
    for path, root in skin.trees.items():
        for el in root.iter("include"):
            content = el.get("content")
            if not content:
                continue
            for p in el.findall("param"):
                if p.get("name"):
                    supplied[content].add(p.get("name", ""))

    for name, defn_path in skin.includes.items():
        root = skin.trees.get(defn_path)
        if root is None:
            continue
        for el in root.iter("include"):
            if el.get("name") != name:
                continue
            body = ET.tostring(el, encoding="unicode")
            declared = skin.include_params.get(name, set())
            for used in set(PARAM_RE.findall(body)):
                if used not in declared and used not in supplied.get(name, set()):
                    findings.append(Finding(
                        "INFO", "I01", defn_path,
                        line_of(skin, defn_path, f"$PARAM[{used}]"),
                        f"include {name!r} uses $PARAM[{used}] with no <param> default "
                        f"and no caller supplying it"))


def check_assets(skin: Skin, findings: list[Finding], max_px: int, max_bytes: int) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    for png in (skin.root / "media").rglob("*.png"):
        size = png.stat().st_size
        try:
            with Image.open(png) as im:
                w, h = im.size
        except Exception as exc:
            findings.append(Finding("WARN", "W09", png, 1, f"PNG fails to decode: {exc}"))
            continue
        if max(w, h) > max_px:
            findings.append(Finding(
                "WARN", "W09", png, 1,
                f"{w}x{h} exceeds the {max_px}px budget; loose PNGs upload as BGRA8 "
                f"so VRAM is w*h*4 = {w * h * 4 // 1024} KiB"))
        if size > max_bytes:
            findings.append(Finding("WARN", "W09", png, 1,
                                    f"{size} bytes exceeds the {max_bytes}-byte budget"))


# ---------------------------------------------------------------------------

def lint(skin_dir: Path, args) -> list[Finding]:
    findings: list[Finding] = []
    skin = Skin(root=skin_dir)

    load_addon_xml(skin, findings)
    if not skin.res_dirs:
        skin.res_dirs = [d for d in (skin_dir / "xml",) if d.is_dir()]
    parse_xml_files(skin, findings)
    load_colors(skin, findings)
    load_strings(skin, findings)
    collect_definitions(skin, findings)

    check_required_files(skin, findings)
    check_gui_version(skin, findings, args.gui_version)
    check_release_flags(skin, findings, args.release)
    check_include_refs(skin, findings)
    check_var_exp_refs(skin, findings)
    check_texture_paths(skin, findings)
    check_colors(skin, findings)
    check_localize(skin, findings)
    check_fonts(skin, findings)
    check_control_ids(skin, findings)
    check_geometry(skin, findings)
    check_animations(skin, findings)
    check_assets(skin, findings, args.max_texture_px, args.max_texture_bytes)
    if args.pedantic:
        check_params(skin, findings)

    baseline = set()
    if args.baseline and Path(args.baseline).is_file():
        for entry in json.loads(Path(args.baseline).read_text()):
            baseline.add((entry["code"], entry["file"], entry["token"]))
    if baseline:
        kept = []
        for f in findings:
            try:
                rel = str(f.path.relative_to(skin_dir))
            except ValueError:
                rel = f.path.name
            if (f.code, rel, f.message) in baseline:
                continue
            kept.append(f)
        findings = kept

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skins", nargs="+", type=Path, help="skin directories to lint")
    ap.add_argument("--gui-version", default="5.18.0", help="expected xbmc.gui version")
    ap.add_argument("--strict", action="store_true", help="fail on WARN as well as ERROR")
    ap.add_argument("--pedantic", action="store_true", help="also report INFO findings")
    ap.add_argument("--release", action="store_true", help="apply release-only checks")
    ap.add_argument("--baseline", default="", help="JSON file of accepted findings")
    ap.add_argument("--max-texture-px", type=int, default=1024)
    ap.add_argument("--max-texture-bytes", type=int, default=131072)
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--expect", default="",
                    help="JSON of expected {code: count}; fail on any mismatch. "
                         "Used to calibrate the linter against vendored Estuary so "
                         "it cannot silently rot into noise.")
    args = ap.parse_args()

    total = defaultdict(int)
    rc = 0
    for skin_dir in args.skins:
        if not skin_dir.is_dir():
            print(f"error: {skin_dir} is not a directory", file=sys.stderr)
            return 2
        findings = lint(skin_dir, args)
        show = [f for f in findings if args.pedantic or f.severity != "INFO"]
        show.sort(key=lambda f: (SEVERITIES[f.severity], str(f.path), f.line))

        print(f"=== {skin_dir} ===")
        if not args.summary_only:
            for f in show:
                print("  " + f.render(skin_dir))

        counts = defaultdict(int)
        for f in show:
            counts[f.severity] += 1
            total[f.code] += 1
        print(f"  {counts['ERROR']} error(s), {counts['WARN']} warning(s), "
              f"{counts['INFO']} info")
        if counts["ERROR"] or (args.strict and counts["WARN"]):
            rc = 1

    if total:
        print("by check: " + ", ".join(f"{k}={v}" for k, v in sorted(total.items())))

    if args.expect:
        expect_path = Path(args.expect)
        if not expect_path.is_file():
            print(f"error: --expect file {expect_path} not found", file=sys.stderr)
            return 2
        expected = {k: v for k, v in json.loads(expect_path.read_text()).items()
                    if not k.startswith("_")}
        actual = dict(total)
        if expected == actual:
            print(f"calibration OK: {expect_path.name} matches exactly")
            return 0
        print("\ncalibration MISMATCH -- the linter is wrong, not the fixture.",
              file=sys.stderr)
        for code in sorted(set(expected) | set(actual)):
            e, a = expected.get(code, 0), actual.get(code, 0)
            if e != a:
                print(f"  {code}: expected {e}, got {a}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
