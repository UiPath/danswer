import os


MODEL_SERVER_HOST = os.environ.get("MODEL_SERVER_HOST") or "localhost"
MODEL_SERVER_ALLOWED_HOST = os.environ.get("MODEL_SERVER_HOST") or "0.0.0.0"
MODEL_SERVER_PORT = int(os.environ.get("MODEL_SERVER_PORT") or "9000")
# Model server for indexing should use a separate one to not allow indexing to introduce delay
# for inference
INDEXING_MODEL_SERVER_HOST = (
    os.environ.get("INDEXING_MODEL_SERVER_HOST") or MODEL_SERVER_HOST
)
INDEXING_MODEL_SERVER_PORT = int(
    os.environ.get("INDEXING_MODEL_SERVER_PORT") or MODEL_SERVER_PORT
)

# Danswer custom Deep Learning Models
INTENT_MODEL_VERSION = "danswer/intent-model"
INTENT_MODEL_CONTEXT_SIZE = 256

# Bi-Encoder, other details
DOC_EMBEDDING_CONTEXT_SIZE = 512

# Cross Encoder Settings
# Global master switch for cross-encoder reranking. When true, the reranker is
# available and the app will rerank for assistants that opt in
# (Persona.rerank_enabled). When false (default) reranking is never attempted
# regardless of per-assistant flags.
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "").lower() == "true"
# If set, reranking is served by a Hugging Face Text-Embeddings-Inference (TEI)
# container at this base URL (its /rerank endpoint) — a CPU-optimized,
# full-precision way to host the cross-encoder WITHOUT a GPU. When set, our own
# model server does NOT load the cross-encoder (TEI owns it). Empty => use the
# legacy in-model-server sentence-transformers path.
RERANK_SERVER_URL = (os.environ.get("RERANK_SERVER_URL") or "").rstrip("/")
ENABLE_RERANKING_ASYNC_FLOW = (
    os.environ.get("ENABLE_RERANKING_ASYNC_FLOW", "").lower() == "true"
)
ENABLE_RERANKING_REAL_TIME_FLOW = (
    os.environ.get("ENABLE_RERANKING_REAL_TIME_FLOW", "").lower() == "true"
)
# Only using one cross-encoder for now. Env-overridable so a GPU-backed prod
# deployment can select a stronger reranker (e.g. BAAI/bge-reranker-v2-m3)
# without a code change; local/dev keeps the small default.
CROSS_ENCODER_MODEL_ENSEMBLE = [
    os.environ.get("RERANK_MODEL_NAME") or "mixedbread-ai/mxbai-rerank-xsmall-v1"
]
CROSS_EMBED_CONTEXT_SIZE = 512

# This controls the minimum number of pytorch "threads" to allocate to the embedding
# model. If torch finds more threads on its own, this value is not used.
MIN_THREADS_ML_MODELS = int(os.environ.get("MIN_THREADS_ML_MODELS") or 1)

# Model server that has indexing only set will throw exception if used for reranking
# or intent classification
INDEXING_ONLY = os.environ.get("INDEXING_ONLY", "").lower() == "true"

# notset, debug, info, warning, error, or critical
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
