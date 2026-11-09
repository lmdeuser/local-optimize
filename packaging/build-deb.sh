#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-"$ROOT/artifacts/debian"}
cd "$ROOT"
mkdir -p "$ROOT/artifacts" "$OUTPUT"
WORK=$(mktemp -d "$ROOT/artifacts/deb-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/source"

while IFS= read -r -d '' path; do
    cp --parents -- "$path" "$WORK/source/"
done < <(git ls-files -co --exclude-standard -z)

PACKAGE=$(dpkg-parsechangelog -l"$WORK/source/debian/changelog" -S Source)
VERSION=$(dpkg-parsechangelog -l"$WORK/source/debian/changelog" -S Version)
UPSTREAM_VERSION=${VERSION%-*}
mkdir -p "$WORK/original/$PACKAGE-$UPSTREAM_VERSION"
cp -a "$WORK/source/." "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/"
rm -rf "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/debian" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/artifacts" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/models"
tar -C "$WORK/original" -czf "$WORK/${PACKAGE}_${UPSTREAM_VERSION}.orig.tar.gz" \
    "$PACKAGE-$UPSTREAM_VERSION"

cd "$WORK/source"
dpkg-buildpackage --no-sign

for artifact in "$WORK"/local-optimize_*; do
    if [[ -f "$artifact" ]]; then
        cp -- "$artifact" "$OUTPUT/"
    fi
done
printf 'Debian artifacts written to %s\n' "$OUTPUT"
