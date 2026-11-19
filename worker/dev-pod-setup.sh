#!/usr/bin/env bash
# Prepare a stock RunPod PyTorch CUDA 13 pod like worker/Dockerfile for SSH quality iteration,
# then start the private ComfyUI the way the serverless handler does. The production path is the image.
# Expects this repository synced to /opt/gremlin; see docs/fixtures.md.
#
# Usage: dev-pod-setup.sh [RECIPE ...]
#   Downloads and verifies the assets of every listed recipe (default: worker/recipe.json), so a
#   tier run passes worker/tiers/*.json here first.
#   COMFY_ARGS="--use-sage-attention"  extra engine flags; the engine is (re)started on every run.
#   INSTALL_SAGE=1                      adds the Triton SageAttention package to the environment.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
recipes=("$@")
[ ${#recipes[@]} -gt 0 ] || recipes=(/opt/gremlin/worker/recipe.json)
apt-get update -qq
apt-get install -y -qq --no-install-recommends git ffmpeg libgl1 libglib2.0-0 ca-certificates
mkdir -p /opt/worker
cp -r /opt/gremlin/worker/{imprint_worker,recipe.json,tiers,requirements.lock,notices} /opt/worker/
if [ ! -x /opt/venv/bin/python ]; then
  uv venv -q --python /usr/bin/python3.12 /opt/venv
  uv pip install -q --python /opt/venv/bin/python --index-strategy unsafe-best-match \
    --extra-index-url https://download.pytorch.org/whl/cu130 -r /opt/worker/requirements.lock
fi
if [ "${INSTALL_SAGE:-0}" = 1 ]; then
  uv pip install -q --python /opt/venv/bin/python sageattention==1.0.6
fi
revision=$(python3 -c "import json; print(json.load(open('/opt/worker/recipe.json'))['comfy_revision'])")
if [ ! -d /opt/ComfyUI ]; then
  git clone -q https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI
  git -C /opt/ComfyUI checkout -q "$revision"
fi
mkdir -p /work/jobs /work/temp /work/runs /opt/ComfyUI/{user,temp,input,output,models}
id -u 10001 >/dev/null 2>&1 || useradd -u 10001 -M -d /work -s /usr/sbin/nologin imprint
chown -R 10001:10001 /work /opt/ComfyUI/{user,temp,input,output,models}
as_worker() { HOME=/work setpriv --reuid=10001 --regid=10001 --clear-groups "$@"; }
cd /opt/worker
for recipe in "${recipes[@]}"; do
  as_worker /opt/venv/bin/python -m imprint_worker.assets --root /opt/ComfyUI/models \
    --recipe "$recipe" --download
done
pkill -u 10001 -f "main.py --listen" || true
sleep 2
cd /opt/ComfyUI
# shellcheck disable=SC2086
HF_HUB_OFFLINE=1 as_worker setsid -f /opt/venv/bin/python main.py --listen 127.0.0.1 \
  --port 8188 --disable-all-custom-nodes --offline --temp-directory /work/temp \
  --preview-method none ${COMFY_ARGS:-} >> /work/comfy.log 2>&1 < /dev/null
for _ in $(seq 1 90); do
  curl -sf -o /dev/null http://127.0.0.1:8188/system_stats && exit 0
  sleep 2
done
echo "ComfyUI did not become ready; see /work/comfy.log" >&2
exit 1
