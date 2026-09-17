# RAG Module — Critique Autonomous AI Code Reviewer

**Owner:** Teammate 1  
**Folder:** `/rag`  
**Frozen Interface Contract:** `retrieve_context(code: str, k: int = 5) -> list[str]`

---

## Overview

The `rag` module provides Retrieval-Augmented Generation context for the Critique code review pipeline. It indexes Python style guides, common code anti-patterns, and past PR review comments into a local vector database (**ChromaDB**) using `sentence-transformers` (`all-MiniLM-L6-v2`).

When given a code snippet or pull request diff, it retrieves the top-k most relevant guidelines and past feedback chunks, which are passed to the fine-tuned LLM module to analyze the code.

---

## Directory Structure

```text
rag/
├── config.py             # Collection names, persistence paths, model selection
├── data/
│   ├── raw/              # Markdown/JSON source documents
│   │   ├── style_guide.md
│   │   ├── common_patterns.json
│   │   └── past_reviews.json
│   ├── processed/        # Intermediate JSON chunks artifact (chunks.json)
│   └── chroma_db/        # Local ChromaDB persistent database (gitignored)
├── ingest.py             # Idempotent data chunking & embedding upsert script
├── retrieve.py           # Public retrieval interface: retrieve_context()
├── demo.py               # Runnable standalone test/demo CLI script
├── tests/
│   └── test_retrieval.py # Automated unit tests
└── README.md             # Module documentation
```

---

## Setup & Ingestion

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Populate / Ingest Data into ChromaDB
To chunk the raw data files and load embeddings into ChromaDB, run:
```bash
python -m rag.ingest
```
*Ingestion is idempotent — running it multiple times will update existing entries without creating duplicates.*

---

## Usage (Integration for MCP & LLM Teammates)

To retrieve relevant style context for a code snippet, import and call `retrieve_context`:

```python
from rag.retrieve import retrieve_context

code_snippet = """
def append_to_list(val, my_list=[]):
    my_list.append(val)
    return my_list
"""

# Retrieve top 5 relevant context snippets
context_list = retrieve_context(code_snippet, k=5)

for snippet in context_list:
    print(snippet)
```

---

## Standalone Demo

To test retrieval interactively on pre-configured sample code inputs:
```bash
python -m rag.demo
```

---

## Running Unit Tests

Run automated unit tests using `pytest`:
```bash
pytest rag/tests/
```
