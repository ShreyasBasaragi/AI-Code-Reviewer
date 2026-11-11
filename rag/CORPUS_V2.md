# RAG v2 corpus build

This branch implements a reproducible, versioned RAG corpus build. The local
source archives, processed chunks, evaluation examples, and Chroma database
are ignored by Git.

## Intended corpus composition

| Teammate category | Source adapter | Target |
|---|---|---:|
| Historical PR reviews | Microsoft CodeReviewer Comment_Generation training split | 5,000–10,000 chunks |
| Security patterns | MITRE CWE + OWASP Cheat Sheet Series | 500–1,000 chunks |
| Style/static rules | PEP 8 + Google Python Style Guide + Bandit plugin descriptions | 300–500 chunks |
| **Total** | | **6,000–12,000 chunks** |

The implementation caps the security adapter at 700 CWE weaknesses plus 250
OWASP sections and the style adapter at 400 extracted records. All records are
token-chunked with the all-MiniLM-L6-v2 tokenizer at 220 tokens with 24-token
overlap, below the model's 256-token input limit.

The Hugging Face `ronantakizawa/github-codereview` dataset is not an ingestion
source. A deterministic 100-row holdout is selected from its `test` split and
written to `rag/data/evaluation/` with a manifest containing row indices and
SHA-256 fingerprints. This is a project-defined holdout, not a teammate-
provided holdout list. Cross-dataset semantic overlap with Microsoft data is
not yet proven absent.

## Local setup

Activate the project virtual environment, then install the project requirements:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Create the held-out evaluation set:

```bash
python scripts/select_heldout_examples.py
```

Download the official reference material:

```bash
python scripts/fetch_reference_sources.py
```

Download Microsoft CodeReviewer Comment_Generation data. This archive is
approximately 847 MB, so ensure sufficient disk space and a stable connection:

```bash
python scripts/fetch_microsoft_codereviewer.py
```

Build the v2 index:

```bash
python -m rag.ingest
```

The v2 database is stored in `rag/data/chroma_db_v2/`; the previous
`rag/data/chroma_db/` directory and `critique_code_context` collection are not
modified. The ingestion report is written to
`rag/data/processed/ingestion_report.json`.

Run read-only retrieval smoke checks:

```bash
python -m rag.demo
python -m rag.evaluate
```

## Interface contract

The pipeline integration API is exactly:

```python
from rag.retrieve import retrieve_context

contexts: list[str] = retrieve_context(code)
```

It returns up to four context strings, most relevant first. Retrieval does not
create a collection, ingest data, or invoke the demo.

## Validation caveats

- The corpus build must be run locally because the source archives and Chroma
  database are intentionally not stored in Git.
- The ingestion report must be checked to confirm each category count and the
  50 MB database target on the actual machine.
- The 100-row holdout is deterministic for the same Hugging Face dataset
  revision and split. Its manifest records the dataset fingerprint.
- The current keyword smoke evaluation is only a sanity check. It is not a
  substitute for a labeled retrieval relevance evaluation or the teammate's
  exact 100 held-out examples.
- Verify the license and attribution requirements of each downloaded source
  before redistributing derived corpus files.
