import os
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

# Base directory for the RAG module.
RAG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RAG_DIR.parent

# Data directory paths.
DATA_DIR = RAG_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
CHROMA_PERSIST_DIR = DATA_DIR / "chroma_db"

# Versioned corpus: the previous critique_code_context collection is left
# untouched so existing local data can be retained or compared.
COLLECTION_NAME = "critique_code_context_v2"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_K = 4

# Directory creation belongs to explicit ingestion/setup commands, not import
# time or retrieval. Keep paths declarative here.
