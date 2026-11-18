# Phase 2A implementation — bounded sample validation

## Scope

The package implements a versioned canonical envelope, Microsoft CodeReviewer and Devign-candidate adapters, task-specific validation/quarantine, exact duplicate/conflict audits, split/leakage checks, JSONL and optional Parquet exports, deterministic manifests, and a coverage-aware compact selector. The compact selector supports example and exact-token budgets, configurable minimum/maximum quotas by source/task/language, deterministic seeds, source-label coverage, exclusions and group-atomic selection. Manifests preserve source locators and original IDs and record reconstruction readiness.

No RAG source is configured. The Devign candidate's target is kept in `source_labels.target`; `defect_status` remains null, eligibility is false, and its source split remains null. Microsoft `y` is preserved as a namespaced source label and is never promoted to defect or severity. No severity or explanation annotations were generated. No source file, RAG database or existing application code was changed. No archive was extracted or external dataset downloaded. No model weights, inference, training or fine-tuning were used.

## Bounded real-data sample

Two sample builds used the same config and seed. Each of 13 configured files was read through at most 4 MiB of deterministic byte windows spread across the file. Parsed rows were hash-ranked within those windows. The aggregate read budget was 54,525,952 bytes (52 MiB). This is a bounded, reproducible sample, not a statistically representative estimate of the full corpora.

| Source/task | Sampled | Valid canonical | Excluded/quarantined |
|---|---:|---:|---:|
| Microsoft Comment Generation | 30 | 28 | 2 |
| Microsoft Code Refinement | 30 | 30 | 0 |
| Microsoft Diff Quality Estimation | 60 | 56 | 4 |
| Devign candidate | 10 | 10 | 0 |
| **Total** | **130** | **124** | **6** |

All six exclusions had missing required `before_code`. The canonical sample passed validation. Source split values were preserved. Devign remains split-unknown. Each sample artifact contained unique record IDs.

## Compact selector check

Selection ran on the 124-record sample for `review_comment_generation`, with example cap 30 and token cap 20,000. Tokenization used only the locally cached Qwen2.5-Coder-3B-Instruct tokenizer JSON (snapshot `488639f1ff808d1d3d0ba301aef8c11461451ec5`, SHA-256 `c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539`). No model weights were accessed.

| Measure | Result |
|---|---:|
| Eligible train comment-generation pool tokenized | 10 records / 48,918 tokens |
| Eligible pool tokens (min / median / p90 / p95 / max) | 446 / 2,541 / 14,511 / 19,089 / 19,089 |
| Selected | 4 records / 19,703 tokens |
| Selected tokens (min / median / p90 / p95 / max) | 446 / 2,373 / 14,511 / 14,511 / 14,511 |
| Excluded by selection | 120: 95 unsupported objective, 15 non-train split, 4 cross-split groups, 6 budget |
| Selected source/task/language | Microsoft CodeReviewer / comment generation / language missing in these sample rows |
| Source-label coverage | `y=1` on 4 rows, preserved only as the original task-specific label |
| Compact corpus storage | 126,237 bytes (records JSONL plus selection manifest) |
| Reconstruction ready | No: local source revision and full-source checksums are unpinned |

Token counts are exact for the concatenated canonical code/diff/comment fields with this tokenizer. They do not include a future prompt or chat template. Ten eligible examples are too few to finalize size targets; 50,000–100,000 remains provisional. The selected language is missing in this sample, so it does not demonstrate language coverage.

## Resource and reproducibility results

- Each build's maximum aggregate source read was 54,525,952 bytes (52 MiB); no source file was scanned fully and no full-source checksum was computed.
- Sample build A: 0.283 s; process peak RSS 47,857,664 bytes. Sample build B: 0.288 s; process peak RSS 47,775,744 bytes. Peak RSS includes Python runtime and sample pools.
- Canonical sample JSONL size: 3,439,541 bytes. Compact corpus size including manifest: 126,237 bytes.
- Two builds produced byte-identical canonical, quarantine, quality-report and manifest artifacts. Verification confirmed artifact hashes and 124 unique IDs. `validate` reported 124 valid and zero invalid canonical rows.
- Automated software tests: **17 passed**. A separate optional Parquet exporter check wrote and read one fixture row successfully.

## Readiness limits

This is **not ready for a full-corpus run**. The v1 selector keeps canonical input and selected rows in memory and refuses canonical inputs above 256 MiB; disk-backed full-corpus selection is not implemented. Full-source checksums, checkpoint/resume for full processing, Parquet input, and external GitHub/HaPy adapters remain future work. The selection manifest contains source locators and checksum fields, but correctly reports `reconstruction_ready=false` for these unpinned samples. Final size targets require larger, source-stratified token measurements with the intended task prompt format.
