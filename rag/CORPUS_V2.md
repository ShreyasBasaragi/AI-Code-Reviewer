# Critique RAG v2 Corpus Build & Knowledge Base

This branch implements a reliable, reproducible, and lightweight ChromaDB knowledge base containing historical code reviews, security patterns, and Python coding standards.

All local raw source archives, processed canonical chunks, evaluation artifacts, and the Chroma database directory are gitignored.

---

## 1. Corpus Composition and Measured Build Counts

The v2 static knowledge base contains strictly the three approved categories:

| Category | Source Material | Final Indexed Chunks | Quota Specification | Status |
|---|---|---:|---:|:---:|
| **Historical Code Reviews** | Microsoft CodeReviewer `Comment_Generation` (training split only) | **5,200** | 5,000–10,000 | PASS |
| **Security Patterns** | MITRE CWE XML + OWASP Cheat Sheet Series Markdown | **550** | 500–1,000 | PASS |
| **Style & Static Rules** | PEP 8 + Google Python Style Guide + Bandit Plugin Rules | **350** | 300–500 | PASS |
| **Total Corpus** | | **6,100** | **6,000–12,000** | **PASS** |

### Verified Storage Metrics
- **Collection Name:** `critique_code_context_v2`
- **Embedding Model:** `sentence-transformers/all-MiniLM-L6-v2` (384-dimensional cosine space)
- **Max Input Length:** 256 tokens (chunking target: 200 tokens with 20-token overlap)
- **Persisted Database Path:** `rag/data/chroma_db_v2`
- **Measured Database Directory Size:** **35.38 MB** (Target: < 50 MB, PASS)
- **Ingestion Report:** `rag/data/processed/ingestion_report.json`
- **Canonical Chunks Artifact:** `rag/data/processed/canonical_chunks.jsonl`

---

## 2. Holdout & Contamination Protection

- The Hugging Face `ronantakizawa/github-codereview` dataset is **strictly excluded** from the v2 retrieval corpus.
- The `github_codereview.py` adapter is never called by the static v2 ingestion pipeline.
- The teammate will supply the official 100 held-out evaluation examples from `ronantakizawa/github-codereview` (`test` split).
- **Current Status:** Official teammate holdout list is pending receipt. A standardized exclusion manifest schema (`rag/data/evaluation/heldout_manifest.json`) is deployed and active during ingestion. Candidate records are checked against stable fingerprints, IDs, and normalized text overlap. The build report explicitly records `holdout_verified: false` and `status: "pending_official_list"`.

---

## 3. Retrieval Interface Contract

The RAG v2 module exposes a single public retrieval function:

```python
from rag.retrieve import retrieve_context

code = """
def calculate_total(items, cache={}):
    cache["items"] = items
    return sum(items)
"""

contexts = retrieve_context(code)
for context in contexts:
    print(context)
```

### Retrieval Guarantees
1. **Signature:** Exactly `retrieve_context(code: str) -> list[str]`. Does not require or accept `k`.
2. **Cardinality:** Returns up to **four** (`DEFAULT_K = 4`) context strings, ordered from most relevant to least relevant.
3. **Empty Input Handling:** Returns `[]` for empty strings or whitespace without querying the vector store.
4. **Missing Database Handling:** Returns `[]` with a clear warning if the database or collection has not been ingested; never triggers automatic ingestion or collection creation during retrieval.
5. **Read-Only:** Zero mutations to the database, filesystem, or evaluation files.
6. **Window-Aware:** For large code snippets, retrieves across overlapping windows and ranks unique candidate documents by cosine distance.

---

## 4. Local Build and Verification Workflow

### 1. Fetch Reference Rules
```bash
python scripts/fetch_reference_sources.py
```
Downloads MITRE CWE XML, OWASP Cheat Sheets, PEP 8, Google Python Style Guide, and Bandit plugin rules into `rag/data/raw/`.

### 2. Fetch Microsoft CodeReviewer Training Records
```bash
python scripts/fetch_microsoft_codereviewer.py
```
Verifies MD5 checksum and extracts training records from `Comment_Generation`. Test and validation splits are never ingested.

### 3. Build & Validate the Vector Database
```bash
python -m rag.ingest
```
Performs validation, holdout exclusion checks, semantic chunking, deduplication, quota enforcement, ChromaDB batch insertion, and generates `rag/data/processed/ingestion_report.json`.

### 4. Run Retrieval Demonstration & Evaluation
```bash
# Interactive demo across 10 distinct code patterns:
python -m rag.demo

# Quantitative quality & latency benchmark:
python -m rag.evaluate
```

### 5. Run Test Suite
```bash
pytest -v tests/test_rag_v2.py rag/tests/
```
Runs 32 unit and regression tests covering canonical models, chunking, holdout exclusions, quotas, retrieval contracts, side-effect freedom, and pipeline integration.
