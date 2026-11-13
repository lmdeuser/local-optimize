#!/bin/bash
set -euo pipefail

: "${TMPDIR:?Set TMPDIR to a writable disk-backed task directory}"
[[ -d "$TMPDIR" && -w "$TMPDIR" ]] || { echo "TMPDIR is not a writable directory" >&2; exit 1; }
case "$(findmnt -n -o FSTYPE --target "$TMPDIR")" in
    tmpfs|ramfs|'') echo "TMPDIR must be disk-backed" >&2; exit 1 ;;
esac
export TMPDIR

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Commit tracked changes before publishing source assets" >&2
    exit 1
fi
target=$(git rev-parse HEAD)
tag="obs-source-$(python3 -c 'import re; print(re.match(r"^\S+ \((\S+)\)", open("debian/changelog").readline()).group(1))')"
if gh release view "$tag" >/dev/null 2>&1; then
    echo "Release $tag already exists; refusing to overwrite published source assets" >&2
    exit 1
fi
WORK=$(mktemp -d "$ROOT/artifacts/obs-source.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

python3 packaging/build_source_assets.py --output-dir "$WORK/dist"
gh release create "$tag" "$WORK"/dist/* --target "$target" \
        --title "OBS source ${tag#obs-source-}" \
        --notes "Debian source assets from the project-local runner. Runtime and models are provisioned on target setup."
gh release view "$tag" --json url --jq .url
