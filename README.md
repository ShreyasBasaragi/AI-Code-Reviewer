# Critique — Autonomous AI Code Reviewer for GitHub Pull Requests

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.6.0%2Bcu124-EE4C2C.svg)](https://pytorch.org/)
[![Model](https://img.shields.io/badge/Base%20Model-Qwen%202.5%20Coder%203B-purple.svg)](https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct)
[![PEFT LoRA](https://img.shields.io/badge/Fine--Tuning-PEFT%20LoRA-orange.svg)](https://github.com/huggingface/peft)
[![Vector DB](https://img.shields.io/badge/RAG-ChromaDB-brightgreen.svg)](https://www.trychroma.com/)
[![MCP](https://img.shields.io/badge/Protocol-Model%20Context%20Protocol%20(FastMCP)-black.svg)](https://github.com/jlowin/fastmcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Critique** is an autonomous AI-powered code review system designed to evaluate GitHub Pull Requests in real time. It combines **Retrieval-Augmented Generation (RAG)** over engineering guidelines, a **domain fine-tuned Qwen 2.5 Coder 3B LoRA model** optimized for 4-bit consumer GPU execution, an **automated Easy/Hard difficulty classifier**, and an **MCP integration layer** with native desktop toast notifications and automated GitHub comment dispatching.

---

## Architecture Overview

```
                      GitHub Pull Request Opened / Updated
                                       │
                                       ▼
                       MCP / GitHub Webhook Listener
                        (Fetches PR Diffs & Metadata)
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │       RAG Retrieval (ChromaDB)        │
                   │  Pulls Style Rules, Security Policies  │
                   │    & Historical Anti-Pattern Context   │
                   └───────────────────┬───────────────────┘
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │    Fine-Tuned LLM (Qwen 2.5 Coder 3B) │
                   │       LoRA Adapter + 4-bit NF4 Quant  │
                   │        Inference on RTX 3050 GPU      │
                   └───────────────────┬───────────────────┘
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │     Automated Heuristic Classifier     │
                   │   [Hard]: Security / Bugs / Leaks     │
                   │   [Easy]: Style / Naming / Docstrings │
                   └───────────────────┬───────────────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
           GitHub PR Review Comment        Windows Desktop Alert
          (Posted via GitHub REST API)    (Native Toast Notification)
```

---

## Key Features

1. **RAG-Augmented Code Analysis:**
   - Vector database powered by **ChromaDB** using `all-MiniLM-L6-v2` embeddings.
   - Automatically retrieves relevant Python engineering standards, security anti-patterns (e.g., SQL injections, resource leaks, mutable default parameters), and historical review precedents.
   - Fully deterministic context augmentation ensuring reliable model conditioning.

2. **Fine-Tuned Domain LLM (Qwen 2.5 Coder 3B):**
   - Base model: `Qwen/Qwen2.5-Coder-3B-Instruct`.
   - Low-Rank Adaptation (**LoRA**) targeting attention projection layers (`q_proj`, `k_proj`, `v_proj`, `o_proj`, rank=8, alpha=16).
   - Dynamic **4-bit NF4 Quantization** via `bitsandbytes`, enabling local inference inside **~1.8 GB VRAM** on an **NVIDIA RTX 3050 Laptop GPU** (average latency: **17.9s** per review).

3. **Autonomous Easy vs. Hard Classification:**
   - Heuristically categorizes issues into:
     - **`[Hard]`**: SQL injection, memory leaks, unclosed descriptors, logic flaws, authentication/security vulnerabilities.
     - **`[Easy]`**: Variable naming, PEP 8 style, docstrings, formatting, readability.
   - Defaults to `Hard` when ambiguous to guarantee developer attention on edge cases.

4. **Model Context Protocol (MCP) & Real-Time Notifications:**
   - Implements the open **FastMCP** server standard (`review_code`, `review_file`, `review_diff` tools).
   - Triggers native **Windows Desktop Toast notifications** immediately upon detecting high-severity flaws.
   - Posts structured review comments with code recommendations (`# BAD` vs. `# GOOD`) directly onto GitHub PRs via the REST API.

---

## Quantitative Evaluation Benchmark

The pilot fine-tuned model and RAG pipeline were quantitatively benchmarked across a curated test suite of vulnerable and clean code samples:

### Classification & Detection Metrics

| Metric | Score | Description |
|---|:---:|---|
| **Recall (Bug Detection Rate)** | **66.7%** | Successfully detected 2 out of 3 actual bugs (caught SQL injections, credential leaks, mutable defaults, and bare except clauses). |
| **Precision** | **57.1%** | Percentage of flagged issues that were true functional/security defects. |
| **F1-Score** | **0.615** | Balanced harmonic mean of precision and recall. |
| **Category Match Rate** | **50.0%** | Accuracy in categorizing the specific vulnerability type (`Security` vs. `Bug`). |
| **Inference Latency** | **17.90s** | Average review generation time on local RTX 3050 GPU (4 GB VRAM). |

### Linguistic & Code Generation Quality (ROUGE & BLEU)

Evaluated against reference human expert code reviews:

| Metric | Overall Score | Peak Score (SQL Injection) | What It Measures |
|---|:---:|:---:|---|
| **ROUGE-L (L-Score)** | **16.91%** | **27.6%** | Longest Common Subsequence (sentence structure alignment with expert reviewers). |
| **ROUGE-1** | **22.91%** | **35.3%** | Unigram lexical overlap (accurate domain terminology). |
| **ROUGE-2** | **8.15%** | **17.9%** | Bigram phrase and recommendation continuity. |
| **BLEU-4** | **4.21%** | **15.2%** | N-gram exact match precision against human reference code fixes. |

---

## Repository Structure

```
C:\Major-Project\
├── llm/                            # Fine-Tuned LLM Module
│   ├── model/                      # LoRA adapter weights & tokenizer
│   │   ├── adapter_config.json     # LoRA rank=8, alpha=16 configuration
│   │   ├── adapter_model.safetensors # Fine-tuned adapter delta weights
│   │   ├── tokenizer.json          # Tokenizer vocabulary
│   │   └── tokenizer_config.json   # Tokenizer parameters
│   ├── base.py                     # Provider-independent BaseLLM abstract class
│   ├── qwen_client.py              # QwenLLM client with 4-bit NF4 GPU quantization
│   └── groq_client.py              # Cloud API fallback client
├── rag/                            # Retrieval-Augmented Generation Module
│   ├── data/
│   │   ├── chroma_db/              # Persisted ChromaDB vector embeddings
│   │   └── raw/                    # Baseline knowledge source files
│   │       ├── style_guide.md      # Python style and engineering rules
│   │       └── common_patterns.json# Anti-patterns (SQLi, leaks, defaults)
│   ├── augment.py                  # Deterministic prompt augmentation builder
│   ├── retrieve.py                 # Public retrieve_context(code, k=5) interface
│   ├── config.py                   # Vector store configuration & protobuf guards
│   ├── ingest.py                   # ChromaDB ingestion pipeline
│   └── evaluate.py                 # RAG retrieval quality evaluation
├── mcp_server/                     # MCP & Integration Module
│   ├── orchestrator.py             # Single pipeline orchestrator & Easy/Hard classifier
│   ├── server.py                   # FastMCP stdio server (review_code, review_file, review_diff)
│   └── notifier.py                 # Windows desktop toast notification alert engine
├── demo_midsem.py                  # Interactive CLI presentation demonstration
├── test_github_pr.py               # Live GitHub PR review runner (API integration)
├── test_pipeline.py                # Standalone end-to-end integration test (4 scenarios)
├── evaluate_llm.py                 # Quantitative benchmark scoring script
├── calculate_nlp_scores.py         # ROUGE-L, ROUGE-1, ROUGE-2, and BLEU-4 calculator
├── setup_demo_prs.py               # Automation script to initialize demo repositories
└── requirements.txt                # Python project dependencies
```

---

## Installation & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.12)
- NVIDIA GPU with 4 GB+ VRAM (CUDA 12.4 supported) or CPU fallback
- Git configured on Windows

### 1. Clone & Install Dependencies
```powershell
git clone https://github.com/ShreyasBasaragi/AI-Code-Reviewer.git
cd AI-Code-Reviewer
pip install -r requirements.txt
```

### 2. Populate RAG Vector Database
```powershell
python -c "from rag.ingest import load_raw_baseline_chunks, deduplicate_and_filter_records, ingest_to_chromadb; recs = load_raw_baseline_chunks(); acc, _ = deduplicate_and_filter_records(recs); ingest_to_chromadb(acc)"
```

---

## How to Run & Demonstrate

### 1. Live GitHub Pull Request Review (Evaluation Demo)
Executes against a real pull request, retrieves context, performs GPU inference, and posts the critique directly onto GitHub:

```powershell
python test_github_pr.py --owner 4EdmunPeyton21 --repo critique-demo-midsem --pr 1 --yes
```

> **Live PR Demonstration:** [4EdmunPeyton21/critique-demo-midsem PR #1](https://github.com/4EdmunPeyton21/critique-demo-midsem/pull/1)

### 2. Standalone Pipeline Test (Offline / No GitHub Required)
Tests 4 bug patterns through RAG, Qwen LoRA inference, and Easy/Hard classification:

```powershell
python test_pipeline.py
```

### 3. Interactive CLI Presentation Mode
```powershell
python demo_midsem.py
```
Provides an interactive menu (`1`-`4`) demonstrating SQL Injection, Mutable Defaults, and Clean Code validation.

### 4. Run Quantitative Metrics Evaluation
```powershell
# Compute Accuracy, Precision, Recall, F1, and Latency
python evaluate_llm.py

# Compute ROUGE-L, ROUGE-1, ROUGE-2, and BLEU-4 scores
python calculate_nlp_scores.py
```

### 5. Run FastMCP Server for IDEs (Cursor / Claude Desktop)
```powershell
python -m mcp_server.server
```

Configure your IDE MCP settings:
```json
{
  "mcpServers": {
    "critique": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\Major-Project"
    }
  }
}
```

---

## Project Status & Roadmap

| Milestone | Target | Status | Deliverables |
|---|---|:---:|---|
| **Midsem Milestone** | **Proof of Concept (PoC)** | **100% COMPLETE** | • Working RAG Vector DB in ChromaDB<br>• Pilot LoRA Qwen 2.5 Coder 3B on local GPU<br>• End-to-end Orchestrator with Easy/Hard classification<br>• Real GitHub PR automated review posting<br>• Windows native desktop toast notifications |
| **Endsem Milestone** | **Production Ready** | *In Progress (~65% Overall)* | • Full-scale training on multi-thousand Microsoft CodeReviewer dataset<br>• Target F1 > 0.85 and ROUGE-L > 35%<br>• 24/7 Cloud webhook deployment (Docker / FastAPI)<br>• Multi-language support (JavaScript, TypeScript, Go, Java)<br>• Web analytics dashboard for review metrics |

---

## Contributors
- **Shreyas Basaragi** — [GitHub](https://github.com/ShreyasBasaragi)
- **Atharv Dalvi** — [GitHub](https://github.com/4EdmunPeyton21)
