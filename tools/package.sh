#!/usr/bin/env bash
# Build every installable artefact.
#
# Thin wrapper over tools/build_repo.py so there is exactly one implementation of
# "how an add-on becomes a zip". It produces:
#
#   dist/<id>-<version>.zip   flat, for the README's direct-download links
#   repo/                     the Kodi repository tree (addons.xml + per-add-on zips)
#
# Then verifies the result, because both the index digest and the zip root layout
# fail silently in Kodi when wrong.
set -euo pipefail

cd "$(dirname "$0")/.."

python3 tools/build_repo.py
echo
python3 tools/build_repo.py --verify
echo
echo "dist/:"
ls -1sh dist/*.zip 2>/dev/null | sed 's/^/  /'
echo
echo "Install on a device with: Settings > Add-ons > Install from zip file"
echo "See the Install section of README.md for the full walkthrough."
