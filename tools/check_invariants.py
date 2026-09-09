#!/usr/bin/env python3
"""Project-specific invariant checks that a generic linter cannot express.

Three things in this skin are load-bearing and fail SILENTLY if wrong, so they
get their own assertions:

  1. The pin-to-top arithmetic. If lb_rowgap is not exactly
     (lb_rowpitch - lb_rowsviewport), rows drift instead of pinning, and there
     is no error anywhere - it just looks subtly wrong on a TV.
  2. Row navigation wiring. Each row passes prev_row/next_row as a NEIGHBOURING
     WRAPPER id. A typo produces a dead end at one edge of the home screen, with
     nothing in the log.
  3. Include names passed through a $PARAM. kodilint cannot resolve
     <include content="$PARAM[card]"> statically, so a bad card name would slip
     through; here we check every value any caller actually passes.

Usage: python3 tools/check_invariants.py skin.cable.tv
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

PARAM_INCLUDE_RE = re.compile(r"\$PARAM\[([^\]]+)\]")


def load_constants(xml_dir: Path) -> dict[str, str]:
    consts: dict[str, str] = {}
    for path in sorted(xml_dir.glob("Constants_*.xml")):
        for c in ET.parse(path).getroot().iter("constant"):
            name = c.get("name")
            if name and name not in consts:
                consts[name] = (c.text or "").strip()
    return consts


def check_pin_to_top(xml_dir: Path, errors: list[str]) -> None:
    for path in sorted(xml_dir.glob("Constants_*.xml")):
        consts = {c.get("name"): (c.text or "").strip()
                  for c in ET.parse(path).getroot().iter("constant") if c.get("name")}
        need = ["lb_rowpitch", "lb_rowsviewport", "lb_rowgap", "lb_rowtop"]
        missing = [n for n in need if n not in consts]
        if missing:
            errors.append(f"{path.name}: missing constants {missing}")
            continue
        pitch = int(consts["lb_rowpitch"])
        viewport = int(consts["lb_rowsviewport"])
        gap = int(consts["lb_rowgap"])
        top = int(consts["lb_rowtop"])
        if gap != pitch - viewport:
            errors.append(
                f"{path.name}: pin-to-top broken. lb_rowgap is {gap} but must be "
                f"lb_rowpitch - lb_rowsviewport = {pitch} - {viewport} = {pitch - viewport}. "
                f"Rows will drift instead of pinning, with no runtime error.")
        if top + viewport != 1080:
            errors.append(
                f"{path.name}: lb_rowtop ({top}) + lb_rowsviewport ({viewport}) = "
                f"{top + viewport}, not 1080. The grouplist's declared height must equal "
                f"lb_rowsviewport or every offset is wrong.")
        print(f"  {path.name}: pitch={pitch} viewport={viewport} gap={gap} top={top}  OK")


def check_row_geometry(xml_dir: Path, errors: list[str]) -> None:
    """Every row wrapper must declare height == lb_rowsviewport."""
    consts = load_constants(xml_dir)
    for path in sorted(xml_dir.glob("*.xml")):
        root = ET.parse(path).getroot()
        for inc in root.iter("include"):
            if not (inc.get("name") or "").startswith("LeanRow"):
                continue
            defn = inc.find("definition") or inc
            for grp in defn.iter("control"):
                if grp.get("type") != "group":
                    continue
                h = (grp.findtext("height") or "").strip()
                if h and h != "lb_rowsviewport":
                    errors.append(
                        f"{path.name}: include {inc.get('name')!r} row wrapper declares "
                        f"height {h!r}; it must be lb_rowsviewport or pin-to-top breaks.")
                break
            else:
                errors.append(f"{path.name}: include {inc.get('name')!r} has no wrapper group")
            print(f"  {inc.get('name')}: wrapper height = lb_rowsviewport  OK")


def check_row_wiring(xml_dir: Path, errors: list[str]) -> None:
    """prev_row/next_row must name a wrapper id that exists in the same window."""
    for path in sorted(xml_dir.glob("*.xml")):
        root = ET.parse(path).getroot()
        if root.tag != "window":
            continue
        groups: dict[str, dict[str, str]] = {}
        for inc in root.iter("include"):
            if not (inc.get("content") or "").startswith("LeanRow"):
                continue
            params = {p.get("name"): p.get("value") for p in inc.findall("param")}
            gid = params.get("group_id")
            if gid:
                groups[gid] = params
        if not groups:
            continue
        known = set(groups) | {"8000"}   # 8000 is the top bar
        for gid, params in sorted(groups.items()):
            for key in ("prev_row", "next_row"):
                target = params.get(key)
                if target is None:
                    errors.append(f"{path.name}: row {gid} does not declare {key}")
                elif target not in known:
                    errors.append(
                        f"{path.name}: row {gid} {key}={target} names no known wrapper "
                        f"(known: {sorted(known)}). This is a silent dead end.")
            if params.get("list_id") != str(int(gid) + 1):
                errors.append(
                    f"{path.name}: row {gid} has list_id {params.get('list_id')}; the "
                    f"convention is wrapper+1 so the pairs stay readable.")
        # the chain must be traversable from top to bottom and back
        order = sorted(groups, key=int)
        for a, b in zip(order, order[1:]):
            if groups[a].get("next_row") != b:
                errors.append(f"{path.name}: row {a} next_row is "
                              f"{groups[a].get('next_row')}, expected {b}")
            if groups[b].get("prev_row") != a:
                errors.append(f"{path.name}: row {b} prev_row is "
                              f"{groups[b].get('prev_row')}, expected {a}")
        print(f"  {path.name}: {len(groups)} rows, navigation chain "
              f"{' -> '.join(order)}  OK")


def check_param_includes(xml_dir: Path, errors: list[str]) -> None:
    """Resolve include names that are passed in through a $PARAM."""
    defined: set[str] = set()
    for path in sorted(xml_dir.glob("*.xml")):
        for inc in ET.parse(path).getroot().iter("include"):
            if inc.get("name"):
                defined.add(inc.get("name"))

    # which params are used as an include name anywhere
    dynamic: set[str] = set()
    for path in sorted(xml_dir.glob("*.xml")):
        for inc in ET.parse(path).getroot().iter("include"):
            for attr in (inc.get("content"), (inc.text or "").strip()):
                if attr and "$PARAM[" in attr:
                    dynamic.update(PARAM_INCLUDE_RE.findall(attr))
    if not dynamic:
        return

    # every value any caller passes for those params must be a defined include
    passed: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in sorted(xml_dir.glob("*.xml")):
        for inc in ET.parse(path).getroot().iter("include"):
            for p in inc.findall("param"):
                if p.get("name") in dynamic and p.get("value"):
                    passed[p.get("name")].add((p.get("value"), path.name))
    for param in sorted(dynamic):
        values = passed.get(param, set())
        for value, where in sorted(values):
            if "$" in value:
                continue
            if value not in defined:
                errors.append(
                    f"{where}: param {param}={value!r} is used as an include name but no "
                    f"<include name=\"{value}\"> exists. kodilint cannot see this.")
        print(f"  $PARAM[{param}] as include name: "
              f"{sorted(v for v, _ in values) or 'no callers'}  OK")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    skin = Path(sys.argv[1])
    xml_dir = skin / "xml"
    if not xml_dir.is_dir():
        print(f"error: {xml_dir} not found", file=sys.stderr)
        return 2

    errors: list[str] = []
    print("pin-to-top arithmetic:")
    check_pin_to_top(xml_dir, errors)
    print("row wrapper geometry:")
    check_row_geometry(xml_dir, errors)
    print("row navigation wiring:")
    check_row_wiring(xml_dir, errors)
    print("include names passed via $PARAM:")
    check_param_includes(xml_dir, errors)

    if errors:
        print(f"\n{len(errors)} invariant violation(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("\nall invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
