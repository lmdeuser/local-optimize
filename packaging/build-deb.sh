#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-"$ROOT/artifacts/debian"}
cd "$ROOT"
mkdir -p "$ROOT/artifacts" "$OUTPUT"
WORK=$(mktemp -d "$ROOT/artifacts/deb-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/source" "$WORK/tmp" "$WORK/pip-cache"
export TMPDIR=${TMPDIR:-"$WORK/tmp"}
export PIP_CACHE_DIR=${PIP_CACHE_DIR:-"$WORK/pip-cache"}

while IFS= read -r -d '' path; do
    cp --parents -- "$path" "$WORK/source/"
done < <(git ls-files -z)

python3 -c 'import sys; assert sys.version_info[:2] == (3, 14), "Python 3.14 is required"'
if [[ -n ${RUNTIME_SOURCE:-} ]]; then
    test -f "$RUNTIME_SOURCE/manifest.json"
    test -x "$RUNTIME_SOURCE/venv/bin/python"
    cp -a "$RUNTIME_SOURCE" "$WORK/source/runtime"
else
    runtime_args=()
    if [[ -n ${OLLAMA_MODELS_SOURCE:-} ]]; then
        runtime_args+=(--ollama-source "$OLLAMA_MODELS_SOURCE")
    elif [[ -r /usr/share/ollama/.ollama/models/manifests/registry.ollama.ai/library/qwen2.5-coder/7b ]]; then
        runtime_args+=(--ollama-source /usr/share/ollama/.ollama/models)
    fi
    python3 "$ROOT/packaging/stage_runtime.py" \
        --source-root "$ROOT" \
        --output-root "$WORK/source/runtime" \
        --python "$(command -v python3)" \
        "${runtime_args[@]}"
fi

PACKAGE=$(dpkg-parsechangelog -l"$WORK/source/debian/changelog" -S Source)
VERSION=$(dpkg-parsechangelog -l"$WORK/source/debian/changelog" -S Version)
UPSTREAM_VERSION=${VERSION%-*}
mkdir -p "$WORK/original/$PACKAGE-$UPSTREAM_VERSION"
cp -a "$WORK/source/." "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/"
rm -rf "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/debian" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/artifacts" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/models" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/runtime/venv" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/runtime/models" \
       "$WORK/original/$PACKAGE-$UPSTREAM_VERSION/runtime/ollama-models"
python3 "$ROOT/packaging/payload_components.py" pack \
    --source-root "$WORK/source" \
    --main-root "$WORK/original/$PACKAGE-$UPSTREAM_VERSION" \
    --package "$PACKAGE" \
    --version "$UPSTREAM_VERSION" \
    --output-dir "$WORK"
rm -rf "$WORK/source/runtime/venv" \
       "$WORK/source/runtime/models" \
       "$WORK/source/runtime/ollama-models"
for archive in "$WORK"/"${PACKAGE}_${UPSTREAM_VERSION}".orig-runtime-*.tar.gz; do
    tar -xzf "$archive" -C "$WORK/source" --strip-components=1
done

cd "$WORK/source"
if [[ ${SOURCE_ONLY:-0} == 1 ]]; then
    dpkg-buildpackage --no-sign -S
else
    dpkg-buildpackage --no-sign
fi

for artifact in "$WORK"/local-optimize_*; do
    if [[ -f "$artifact" ]]; then
        cp -- "$artifact" "$OUTPUT/"
    fi
done
printf 'Debian artifacts written to %s\n' "$OUTPUT"
