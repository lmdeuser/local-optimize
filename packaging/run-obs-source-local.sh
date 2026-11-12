#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
WORK=$(mktemp -d "$ROOT/artifacts/obs-source.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

python3 packaging/build_source_assets.py --output-dir "$WORK/dist"
tag="obs-source-$(python3 -c 'import re; print(re.match(r"^\S+ \((\S+)\)", open("debian/changelog").readline()).group(1))')"
if gh release view "$tag" >/dev/null 2>&1; then
    gh release upload "$tag" "$WORK"/dist/* --clobber
else
    gh release create "$tag" "$WORK"/dist/* \
        --title "OBS source ${tag#obs-source-}" \
        --notes "Debian source assets from the project-local runner. Runtime components remain pinned in OBS."
fi
gh release view "$tag" --json url --jq .url
