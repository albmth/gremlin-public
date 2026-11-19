# Immutable Python base. CUDA 13.0 runtime libraries come from the pinned PyTorch wheels.
# The wheels are installed in five parallel-pullable layers: one 3.1 GB layer streamed and
# extracted alone for 177 to 318 s on a fresh RunPod host, the same bytes in five layers for
# 79 to 88 s (candidate A, 30 October 2026); every installed file is byte-identical either way.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS engine
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg libgl1 libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/worker
COPY requirements.lock ./
# The final pip line installs the remainder and proves every pin of the lock is satisfied.
RUN pip install --no-cache-dir --no-deps --extra-index-url https://download.pytorch.org/whl/cu130 \
    nvidia-cudnn-cu13==9.13.0.50 nvidia-cufft==12.0.0.15
RUN pip install --no-cache-dir --no-deps --extra-index-url https://download.pytorch.org/whl/cu130 \
    nvidia-cublas==13.0.0.19 nvidia-cusparse==12.6.2.49 nvidia-cusolver==12.0.3.29 nvidia-curand==10.4.0.35
RUN pip install --no-cache-dir --no-deps --extra-index-url https://download.pytorch.org/whl/cu130 \
    nvidia-nccl-cu13==2.27.7 nvidia-cusparselt-cu13==0.8.0 nvidia-nvshmem-cu13==3.3.24 \
    nvidia-cuda-nvrtc==13.0.48 nvidia-cuda-runtime==13.0.48 nvidia-cuda-cupti==13.0.48 \
    nvidia-nvjitlink==13.0.39 nvidia-nvtx==13.0.39 nvidia-cufile==1.15.0.42
RUN pip install --no-cache-dir --no-deps --extra-index-url https://download.pytorch.org/whl/cu130 \
    torch==2.9.1+cu130
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu130 -r requirements.lock
RUN git init -q /opt/ComfyUI && git -C /opt/ComfyUI remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/ComfyUI fetch -q --depth 1 origin 8f04fd6ecf4a548937971e840af2a985529fab76 \
    && git -C /opt/ComfyUI checkout -q 8f04fd6ecf4a548937971e840af2a985529fab76
ENV PYTHONPATH=/opt/worker PYTHONUNBUFFERED=1
RUN mkdir -p /work/jobs /opt/ComfyUI/user /opt/ComfyUI/temp /opt/ComfyUI/input /opt/ComfyUI/output /opt/ComfyUI/models && chown -R 10001:10001 /work /opt/ComfyUI/user /opt/ComfyUI/temp /opt/ComfyUI/input /opt/ComfyUI/output /opt/ComfyUI/models

FROM engine AS runtime
COPY imprint_worker ./imprint_worker
COPY recipe.json model-release.json ./
COPY tiers ./tiers
COPY notices ./notices
ENV IMPRINT_WEIGHT_SOURCE=runtime-hf HF_HUB_DISABLE_TELEMETRY=1
# Deployment supplies the matching explicit model release and engine settings.
# Native cache startup never downloads or copies weights into this model-free image.
ENTRYPOINT ["python", "-m", "imprint_worker.handler"]
