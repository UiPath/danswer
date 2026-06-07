# Our own TEI reranker image with the model exported to ONNX and BAKED IN.
#
# Why ONNX: the TEI CPU image (`cpu-*`) runs on ONNX Runtime and expects an
# `onnx/model.onnx` in the model dir. BAAI/bge-reranker-v2-m3 ships only
# PyTorch/safetensors weights (no onnx/), so the stock image 404s trying to
# download `onnx/model.onnx`. We export it to ONNX with HF Optimum at BUILD
# time and lay it out the way TEI expects, so at runtime TEI loads it locally
# (HF_HUB_OFFLINE=1) — no download, no re-download on restart, no HF dependency.
#
# Build + push (linux/amd64):
#   docker build -f k8s/optional/tei-rerank/Dockerfile \
#     -t sfbrdevhelmweacr.azurecr.io/danswer/tei-reranker:bge-v2-m3-2 \
#     --platform linux/amd64 --load k8s/optional/tei-rerank
#   docker push sfbrdevhelmweacr.azurecr.io/danswer/tei-reranker:bge-v2-m3-2

ARG TEI_BASE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.5
ARG RERANK_MODEL=BAAI/bge-reranker-v2-m3

# Stage 1: export the cross-encoder reranker (sequence-classification, 1 logit)
# to ONNX and arrange it the way TEI's CPU runtime wants — config + tokenizer at
# the model root, weights under onnx/ (model.onnx [+ model.onnx_data for >2GB]).
FROM python:3.11-slim AS export
ARG RERANK_MODEL
# Pin a coherent export toolchain:
#  - optimum 1.23.3: `optimum-cli export onnx` is built into [exporters]
#    (≥1.24 split it into a separate optimum-onnx package).
#  - torch 2.2.2: the stable LEGACY torch.onnx exporter, which names the >2GB
#    external-data file the way optimum 1.23.3 expects. Newer torch defaults to
#    the dynamo/onnxscript exporter, whose external-data naming breaks optimum
#    1.23.3's cleanup (FileNotFoundError on model.onnx.data). bge-reranker-v2-m3
#    is ~2.3GB fp32 so it always uses external data.
# numpy<2: torch 2.2.2 was built against numpy 1.x; numpy 2.x breaks its
# tensor<->numpy bridge ("Numpy is not available"), which the export VALIDATION
# step uses (the export itself succeeds, but validation then fails the build).
RUN pip install --no-cache-dir "optimum[exporters]==1.23.3" "torch==2.2.2" "numpy<2" onnxruntime onnx
RUN optimum-cli export onnx --model "${RERANK_MODEL}" --task text-classification /model \
    && mkdir -p /model/onnx \
    && mv /model/model.onnx /model/onnx/model.onnx \
    && (mv /model/model.onnx_data /model/onnx/model.onnx_data 2>/dev/null || true)

# Stage 2: TEI runtime with the exported model baked in. Served from a local
# path so nothing is fetched at runtime.
FROM ${TEI_BASE}
COPY --from=export /model /model
ENV HF_HUB_OFFLINE=1
