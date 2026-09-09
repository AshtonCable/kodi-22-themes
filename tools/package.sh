#!/usr/bin/env bash
# Package each skin into an installable zip.
#
# Kodi's "Install from zip file" requires the add-on-id directory at the ZIP
# ROOT, so we zip from the repository root with the directory name included --
# not from inside the skin directory.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p dist

version_of() {
	python3 - "$1" <<'PY'
import sys, xml.etree.ElementTree as ET
print(ET.parse(f"{sys.argv[1]}/addon.xml").getroot().get("version"))
PY
}

shopt -s nullglob
for skin in skin.cable.* repository.cable.*; do
	[ -d "$skin" ] || continue
	[ -f "$skin/addon.xml" ] || continue
	version="$(version_of "$skin")"
	out="dist/${skin}-${version}.zip"
	rm -f "$out"
	zip -qr "$out" "$skin" \
		-x '*.git*' -x '*/.DS_Store' -x '*/__pycache__/*' -x '*.pyc'
	printf '%-42s %8s bytes\n' "$out" "$(stat -c%s "$out")"
done

echo
echo "Install on a device with: Settings > Add-ons > Install from zip file"
