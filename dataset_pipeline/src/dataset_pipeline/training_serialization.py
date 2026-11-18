"""Qwen chat serialization for source-grounded code-review objectives.

This module formats training examples and counts tokens with a caller-supplied
local tokenizer. It never loads model weights or performs inference.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


SERIALIZATION_VERSION = "qwen2.5-code-review-chat-v1"

_SYSTEM = {
    "defect_detection": (
        "Classify whether the supplied function is vulnerable. Return only a JSON object "
        'with one field, "defect_status", whose value is "defective" or "non_defective".'
    ),
    "fix_recommendation": (
        "Apply the review feedback to the provided code change. Return the complete corrected "
        "code only. Do not add explanation or markdown fences."
    ),
    "review_comment_generation": (
        "Review the supplied code change. Return the source review comment only. Do not add "
        "a severity, defect label, or explanation that is absent from the source comment."
    ),
}


def _code(value: Any) -> str:
    return value if isinstance(value, str) else ""


def training_messages(record: Mapping[str, Any], objective: str) -> list[dict[str, str]]:
    """Build one user prompt and one source-supported assistant target."""
    if objective not in _SYSTEM:
        raise ValueError(f"unsupported training objective: {objective}")
    before = _code(record.get("before_code"))
    diff = _code(record.get("diff"))
    comment = _code(record.get("review_comment"))

    if objective == "defect_detection":
        status = record.get("defect_status")
        if status not in ("defective", "non_defective") or not before.strip():
            raise ValueError("defect detection requires a function and a source-supported binary label")
        user = f"Task: defect_detection\n\nFunction:\n```\n{before}\n```"
        target = json.dumps({"defect_status": status}, separators=(",", ":"))
    elif objective == "fix_recommendation":
        after = _code(record.get("after_code"))
        if not before.strip() or not after.strip() or not comment.strip():
            raise ValueError("fix recommendation requires before code, source comment, and fixed code")
        user = (
            "Task: fix_recommendation\n\n"
            f"Before code:\n```\n{before}\n```\n\n"
            f"Diff context:\n```diff\n{diff}\n```\n\n"
            f"Review comment:\n{comment}"
        )
        target = after
    else:
        if not before.strip() or not comment.strip():
            raise ValueError("review comment generation requires before code and source comment")
        user = (
            "Task: review_comment_generation\n\n"
            f"Code context:\n```\n{before}\n```\n\n"
            f"Diff context:\n```diff\n{diff}\n```"
        )
        target = comment

    return [
        {"role": "system", "content": _SYSTEM[objective]},
        {"role": "user", "content": user},
        {"role": "assistant", "content": target},
    ]


def qwen_chat_token_counts(tokenizer: Any, messages: Sequence[Mapping[str, str]]) -> dict[str, int]:
    """Count prompt, assistant target (including end marker), and full chat template."""
    if len(messages) != 3 or messages[-1].get("role") != "assistant":
        raise ValueError("expected system, user, and assistant messages")
    prompt_result = tokenizer.apply_chat_template(
        list(messages[:2]), tokenize=True, add_generation_prompt=True
    )
    full_result = tokenizer.apply_chat_template(
        list(messages), tokenize=True, add_generation_prompt=False
    )
    def token_ids(result: Any) -> list[int]:
        if isinstance(result, Mapping):
            result = result["input_ids"]
        elif hasattr(result, "input_ids"):
            result = result.input_ids
        if hasattr(result, "tolist"):
            result = result.tolist()
        if result and isinstance(result[0], list):
            result = result[0]
        return list(result)

    prompt = token_ids(prompt_result)
    full = token_ids(full_result)
    if full[: len(prompt)] != prompt:
        raise ValueError("Qwen chat template does not preserve prompt as a full-sequence prefix")
    return {
        "input_tokens": len(prompt),
        "target_tokens": len(full) - len(prompt),
        "total_tokens": len(full),
    }
