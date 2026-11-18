# Groq review structured-output configuration

The review contract is defined by `review_schema.review_json_schema()` and versioned as `review-v1`. `GroqLLM.analyze_code` sends it as `response_format.type = json_schema` with `strict = true` for `openai/gpt-oss-20b`. The response object includes the existing `summary` and `findings` structure and all existing parser-supported finding fields. Nullable/defaultable application fields are represented as nullable but still required JSON properties, as required by strict mode. Extra properties are disallowed.

The provider does not fall back to plain JSON or an unconstrained prompt request. A configured model outside the explicitly supported strict-output allowlist raises `UnsupportedStructuredOutputModelError` before making a request. To use another Groq model, verify that it supports strict JSON Schema output and explicitly update that allowlist and its tests.

Strict structured output enforces response shape and field types. `parse_review_response` remains authoritative for application semantics: evidence IDs must exist in retrieved evidence, the model cannot make up citations, uncited findings must be uncertain or abstained, line bounds must be valid, and risk-priority claims must be evidence-supported. Strict JSON mode does not replace those checks.

Each evaluation result now records `model_id` and:

```json
{
  "mode": "json_schema",
  "strict": true,
  "schema_name": "code_review",
  "schema_version": "review-v1"
}
```

The five-case pre-change result at `evaluation/results/phase_e2_pilot.json` is preserved. New runs should use a distinct output path so that pilot remains the baseline.

Groq documents strict Structured Outputs for `openai/gpt-oss-20b`; its strict mode requires all object fields and `additionalProperties: false`. The installed Groq SDK's chat completion method accepts the `response_format` parameter. Sources: [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs), [Groq API reference](https://console.groq.com/docs/api-reference).
