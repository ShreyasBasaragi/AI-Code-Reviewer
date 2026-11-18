# AI Code Reviewer Dataset Pipeline

Portable, source-aware, non-destructive dataset tools. The implementation supports bounded sample mode and SQLite-backed compact-corpus selection over canonical JSONL. It does not train or run models. Full raw-source canonicalization is still a separate readiness gate.

## Install

From this directory:

```bash
python -m pip install -e '.[test,tokenizer,parquet]'
```

Core adapters need PyYAML. Token budgets require the optional `tokenizers` package and an existing local `tokenizer.json`; no tokenizer/model files are downloaded. Parquet is optional and requires PyArrow.

## Bounded sample workflow

From the project root:

```bash
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli inspect --config dataset_pipeline/configs/sources.yaml
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli build --config dataset_pipeline/configs/sources.yaml --sample-per-source 100 --seed phase2a-sample-v1 --output-dir /tmp/aicr-sample-a
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli validate --input-dir /tmp/aicr-sample-a
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli verify --input-dir /tmp/aicr-sample-a
```

Each configured file read is capped at `sample_max_bytes_per_file` (4 MiB by default). The reader opens deterministic byte windows spread across the file, then hash-ranks parsed records within those bounded windows. This is a reproducible bounded sample, not a statistically representative sample of each entire corpus. `msg-train-selected.jsonl` is excluded because Phase 1.5 verified it is a subset of full `msg-train.jsonl`.

Outputs are canonical JSONL, quarantine JSONL, quality report, stable artifact manifest, and a separate runtime report. Source files remain read-only. `inspect` reports configured paths and sizes without hashing or scanning full files.

Create a source integrity manifest without reading source contents:

```bash
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli source-manifest \
  --config dataset_pipeline/configs/sources.yaml \
  --output dataset_inventory_reports/source_integrity_manifest.json
```

Add `--hash` only when a complete sequential read of every configured source file is acceptable. A manifest created with checksums can be verified before reconstruction:

```bash
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli verify-source-manifest \
  --config dataset_pipeline/configs/sources.yaml \
  --manifest dataset_inventory_reports/source_integrity_manifest.json
```

## Compact corpus selection

Selection streams canonical JSONL and stores record metadata, token counts, group relations, and checkpoint state in SQLite. Code text is not copied into the working database. Selection requires an explicit objective eligibility flag, source-supported supervision, and an allowed source split (default `train`). Unverified Devign candidates and Microsoft comment-presence labels are not admitted as generic defect/severity examples.

Example with an already available local tokenizer file:

```bash
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli select \
  --input /tmp/aicr-sample-a/canonical.jsonl --output-dir /tmp/aicr-selected \
  --objective review_comment_generation --max-examples 50000 --max-tokens 2000000 \
  --tokenizer-json /path/to/local/tokenizer.json \
  --selection-config dataset_pipeline/configs/selection.yaml
```

`--max-examples`, `--max-tokens`, or both may be supplied. Token budgets require a tokenizer JSON path and are counted separately for objective input and target with that tokenizer. Optional per-record limits are `--max-input-tokens`, `--max-target-tokens`, and `--max-record-tokens`; these exclude whole records instead of truncating code. A pre-tokenization safety cap defaults to 250,000 characters for each input and target (`--max-input-chars` and `--max-target-chars`) to prevent a single pathological record from exceeding bounded-memory goals. Over-cap records are excluded and counted, never shortened. Set a higher cap only after checking memory headroom. `--max-temp-bytes` caps the SQLite working database and WAL at checkpoint boundaries. The manifest records tokenizer SHA-256, token distributions before and after selection, per-source/task/language counts, exclusions, group IDs, source locators, and reconstruction readiness. Missing pinned source revisions or full-source checksums make `reconstruction_ready` false. The compact export is written under `compact_training_corpus/`, separately from the full canonical export.

Checkpoint state is committed at safe input byte offsets. Resume only with the same input bytes, tokenizer, objective, budgets, quotas, and seed:

```bash
PYTHONPATH=dataset_pipeline/src python -m dataset_pipeline.cli select \
  --input /portable/project/sample/canonical.jsonl --output-dir /portable/project/output \
  --objective review_comment_generation --max-examples 50000 \
  --tokenizer-json /portable/project/tokenizer.json --resume
```

For another machine, clone/copy the project and source datasets to the configured relative layout (or edit `configs/sources.yaml`), install the package from this directory, and use the same pinned source revisions, tokenizer file, config, and seed. `manifest.json` pins the canonical input SHA-256 and selection configuration SHA-256; `selection_manifest.jsonl` records source IDs/locators and selected token counts. Compare those artifacts and the exported JSONL hashes to reproduce the result. Local data marked `local-unpinned` is not yet reconstruction-ready.

Coverage quotas use `min_quotas` and `max_quotas` in selection YAML, keyed by canonical dimensions such as `source`, `source_task`, or `language`, then by value. Selection is deterministic for fixed inputs, tokenizer, configuration and seed. Related pair/PR/commit groups are indivisible; detected groups crossing source splits are excluded. No labels are synthesized or inferred.

## Current safety limits

- Full raw-source canonical builds are not exposed by the bounded sample CLI; a source SHA-256 pass necessarily reads that source file end to end.
- No final Devign split is created because its source split is unresolved.
- No RAG-derived records are admitted.
- No near-duplicate search, archive extraction, external download, LLM, training, fine-tuning or inference is performed.
- Parquet writing is available through the optional exporter API; JSONL is the default.

Run software tests with `PYTHONPATH=dataset_pipeline/src .venv/bin/python -m pytest dataset_pipeline/tests`. Fixtures are synthetic software fixtures only, not training examples.

Phase 2B audit artifacts are in `dataset_inventory_reports/`: [eligibility audit](../dataset_inventory_reports/phase2b_eligibility_audit.md), [token distribution and scale metrics](../dataset_inventory_reports/phase2b_token_distribution_report.md), [validation/readiness report](../dataset_inventory_reports/phase2b_validation_report.md), [source integrity manifest](../dataset_inventory_reports/phase2b_source_integrity_manifest.json), and [portable reconstruction guide](../dataset_inventory_reports/phase2b_reconstruction_guide.md).
