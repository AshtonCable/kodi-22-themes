#!/usr/bin/env python3
"""Build the Kodi repository tree served from this GitHub repo.

Produces, under repo/:

    addons.xml            the concatenated <addon> blocks of every shipped add-on
    addons.xml.sha256     hex digest of exactly those bytes
    <id>/<id>-<ver>.zip   one installable zip per add-on

Kodi's CRepository fetches addons.xml, verifies it against the digest named by
<checksum verify="sha256">, and then resolves each add-on's zip beneath
<datadir>. So the digest must be of the byte-for-byte content served, and the
directory layout must match what addons.xml advertises -- both of which this
script and tools/check_repo.py together guarantee.

Also generates repository.cable.skins/icon.png if it is missing, so a fresh
checkout can build the whole thing.

Usage:
    python3 tools/build_repo.py
    python3 tools/build_repo.py --check    # verify committed output is current
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "repo"
DIST = REPO / "dist"

# Every add-on directory that should be published. A directory qualifies by
# having an addon.xml whose id matches the directory name (kodilint enforces
# that agreement, and Kodi requires it).
def addon_dirs() -> list[Path]:
    found = []
    for d in sorted(REPO.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in {"vendor", "tools", "docs", "repo", "dist"}:
            continue
        if (d / "addon.xml").is_file():
            found.append(d)
    return found


def addon_meta(d: Path) -> tuple[str, str]:
    root = ET.parse(d / "addon.xml").getroot()
    addon_id, version = root.get("id"), root.get("version")
    if not addon_id or not version:
        raise SystemExit(f"error: {d.name}/addon.xml lacks an id or version")
    if addon_id != d.name:
        raise SystemExit(
            f"error: {d.name}/addon.xml declares id {addon_id!r}; Kodi requires the "
            f"add-on id to equal the directory name")
    return addon_id, version


def build_addons_xml(dirs: list[Path]) -> str:
    """Concatenate each add-on.xml's root element under a single <addons>.

    Comments are dropped deliberately: the per-add-on files carry long
    maintenance notes that have no business in an index Kodi parses on every
    refresh, and dropping them keeps the digest stable against comment edits.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<addons>"]
    for d in dirs:
        root = ET.parse(d / "addon.xml").getroot()
        body = ET.tostring(root, encoding="unicode")
        # normalise indentation so the index reads sensibly when inspected
        for line in body.strip().splitlines():
            parts.append("\t" + line.rstrip())
    parts.append("</addons>")
    return "\n".join(parts) + "\n"


def zip_addon(d: Path, dest_dir: Path, addon_id: str, version: str) -> Path:
    """Zip an add-on with its id directory at the ZIP ROOT.

    That root layout is what Kodi's "Install from zip file" requires, so it is
    asserted by tools/check_repo.py rather than left to trust.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{addon_id}-{version}.zip"
    if out.exists():
        out.unlink()
    subprocess.run(
        ["zip", "-qr", "-X", str(out), d.name,
         "-x", "*.git*", "-x", "*/.DS_Store", "-x", "*/__pycache__/*", "-x", "*.pyc"],
        cwd=REPO, check=True,
    )
    return out


def make_icon(path: Path) -> None:
    if path.is_file():
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print(f"warning: Pillow missing, cannot generate {path}", file=sys.stderr)
        return
    im = Image.new("RGBA", (512, 512), (14, 14, 16, 255))
    d = ImageDraw.Draw(im)
    # a stack of three plates: "several skins", in the Cable TV accent
    for i, (y, col) in enumerate(((150, (42, 44, 48)), (222, (42, 44, 48)),
                                  (294, (0, 176, 255)))):
        d.rounded_rectangle([116, y, 396, y + 56], radius=10, fill=col + (255,))
    im.save(path, optimize=True)
    print(f"generated {path.relative_to(REPO)}")


def build() -> dict[str, str]:
    dirs = addon_dirs()
    if not dirs:
        raise SystemExit("error: found no add-on directories to publish")
    make_icon(REPO / "repository.cable.skins" / "icon.png")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    DIST.mkdir(parents=True, exist_ok=True)

    index = build_addons_xml(dirs)
    (OUT / "addons.xml").write_text(index)
    digest = hashlib.sha256(index.encode()).hexdigest()
    (OUT / "addons.xml.sha256").write_text(digest + "\n")

    published = {}
    for d in dirs:
        addon_id, version = addon_meta(d)
        z = zip_addon(d, OUT / addon_id, addon_id, version)
        # dist/ carries the same zips at a flat, human-linkable path for the
        # README's direct-download instructions.
        shutil.copy2(z, DIST / z.name)
        published[addon_id] = version
        print(f"  {addon_id:26} {version:8} {z.stat().st_size:>9} bytes")

    print(f"addons.xml: {len(index)} bytes, sha256 {digest}")
    return published


def digest_tree() -> dict[str, str]:
    out = {}
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(OUT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def verify() -> int:
    """Structural checks on the published tree.

    Everything here fails silently in Kodi if wrong: a digest mismatch makes the
    repository refuse to refresh with only a log line, and a zip whose add-on
    directory is not at the root installs into a broken path.
    """
    import zipfile

    errors: list[str] = []
    index_path = OUT / "addons.xml"
    digest_path = OUT / "addons.xml.sha256"
    if not index_path.is_file():
        print("error: repo/addons.xml missing; run tools/build_repo.py", file=sys.stderr)
        return 1

    raw = index_path.read_bytes()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"error: repo/addons.xml does not parse: {exc}", file=sys.stderr)
        return 1
    if root.tag != "addons":
        errors.append(f"repo/addons.xml root is <{root.tag}>, expected <addons>")

    # the digest must be of exactly the bytes served
    if not digest_path.is_file():
        errors.append("repo/addons.xml.sha256 missing")
    else:
        want = digest_path.read_text().split()[0].lower()
        got = hashlib.sha256(raw).hexdigest()
        if want != got:
            errors.append(f"digest mismatch: addons.xml.sha256 says {want}, "
                          f"actual sha256 is {got}. Kodi verifies this and will "
                          f"refuse to refresh the repository.")
        else:
            print(f"  digest matches addons.xml ({len(raw)} bytes)")

    listed = {}
    for addon in root.findall("addon"):
        aid, ver = addon.get("id"), addon.get("version")
        if not aid or not ver:
            errors.append("an <addon> in addons.xml lacks id or version")
            continue
        listed[aid] = ver

        z = OUT / aid / f"{aid}-{ver}.zip"
        if not z.is_file():
            errors.append(f"addons.xml advertises {aid} {ver} but {z.relative_to(REPO)} "
                          f"does not exist; Kodi resolves zips beneath <datadir>")
            continue

        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            roots = {n.split("/", 1)[0] for n in names if n.strip()}
            if roots != {aid}:
                errors.append(f"{z.name} has {sorted(roots)} at the zip root; Kodi's "
                              f"'Install from zip file' needs exactly {aid!r} there")
            inner = f"{aid}/addon.xml"
            if inner not in names:
                errors.append(f"{z.name} has no {inner}")
            else:
                ir = ET.fromstring(zf.read(inner))
                if (ir.get("id"), ir.get("version")) != (aid, ver):
                    errors.append(
                        f"{z.name} contains addon.xml declaring "
                        f"{ir.get('id')} {ir.get('version')}, but addons.xml says "
                        f"{aid} {ver}")
                else:
                    print(f"  {aid:26} {ver:8} zip root OK, addon.xml agrees "
                          f"({len(names)} entries)")

    on_disk = {d.name for d in OUT.iterdir() if d.is_dir()}
    if on_disk != set(listed):
        stray = sorted(on_disk - set(listed))
        if stray:
            errors.append(f"repo/ has directories not listed in addons.xml: {stray}")

    if errors:
        print(f"\n{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"repository tree valid: {len(listed)} add-on(s) published")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed repo/ is not what a fresh build produces")
    ap.add_argument("--verify", action="store_true",
                    help="structural checks: digest, zip root layout, addons.xml agreement")
    args = ap.parse_args()

    if args.verify:
        return verify()

    if not args.check:
        build()
        return 0

    # A zip's bytes depend on file mtimes, so comparing zips is meaningless.
    # addons.xml and its digest are the parts that must not drift, plus the set
    # of published filenames.
    before_index = (OUT / "addons.xml").read_text() if (OUT / "addons.xml").is_file() else None
    before_files = set(digest_tree())
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "repo"
        if OUT.exists():
            shutil.copytree(OUT, backup)
        build()
        after_index = (OUT / "addons.xml").read_text()
        after_files = set(digest_tree())
        shutil.rmtree(OUT)
        if backup.exists():
            shutil.copytree(backup, OUT)

    problems = []
    if before_index != after_index:
        problems.append("repo/addons.xml is stale; re-run tools/build_repo.py")
    if before_files != after_files:
        missing = sorted(after_files - before_files)
        extra = sorted(before_files - after_files)
        if missing:
            problems.append(f"not committed: {missing}")
        if extra:
            problems.append(f"committed but no longer built: {extra}")
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1
    print("repo/ is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
