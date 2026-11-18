# Retrieval evaluation set

`retrieval_queries_v1.jsonl` contains distinct provisional development and validation queries. It is a development aid, not a human-judged relevance set. `expected_issue_category` describes query intent only; it does not label any retrieved evidence as relevant or prove that code is defective.

Use `retrieval_annotation_template.jsonl` to record independent judgments. Suggested relevance values are 0 (irrelevant), 1 (topically related, not applicable), 2 (relevant supporting context), and 3 (directly applicable evidence). Applicability is a separate judgment. Do not publish precision, recall, MRR, or nDCG until judgments are independently reviewed and marked as human annotations.

The current retrieval runner reports unjudged diagnostics only: source and parent diversity, language match status, context-budget utilization, empty retrieval rate, distance ordering, and latency. It never persists retrieved document text.
