import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
from pathlib import Path

# Base directory for the rag module
RAG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RAG_DIR.parent

# Data directory paths
DATA_DIR = RAG_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
CHROMA_PERSIST_DIR = DATA_DIR / "chroma_db"

# ChromaDB collection configuration
COLLECTION_NAME = "critique_code_context"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_K = 5

# Ensure necessary directories exist
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
