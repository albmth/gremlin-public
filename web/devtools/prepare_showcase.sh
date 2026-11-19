#!/usr/bin/env bash
# Prepare the owner-selected example from the preserved original and comparison media.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
source_dir="$root/artifacts/fixed-prompt-validation-20261106/website-media"
reference="$root/artifacts/web-demo-snow-leopard-2026-11-02/reference.png"
out="$root/web/gremlin_web/static/showcase"
mkdir -p "$out"
ffmpeg -v error -y -i "$reference" -frames:v 1 -q:v 3 "$out/reference.jpg"
cp "$source_dir/meimei-original.mp4" "$out/clip.mp4"
cp "$source_dir/meimei-original.jpg" "$out/clip.jpg"
cp "$source_dir/meimei-gremlin.mp4" "$out/result.mp4"
cp "$source_dir/meimei-gremlin.jpg" "$out/result.jpg"
