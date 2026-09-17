import os
from typing import List, Optional

from groq import Groq

from llm.base import BaseLLM


DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqLLM(BaseLLM):
    """
    Temporary Groq-backed implementation of the Critique LLM interface.

    This class is intentionally independent of the RAG implementation.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.1,
    ) -> None:
        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. "
                "Export your Groq API key before running this test."
            )

        self.client = Groq(api_key=api_key)
        self.model = model or os.getenv("GROQ_MODEL", DEFAULT_MODEL)
        self.temperature = temperature

    @staticmethod
    def _build_prompt(code: str, context: List[str]) -> str:
        if not code or not code.strip():
            raise ValueError("Code input cannot be empty.")

        context_text = "\n\n".join(
            f"--- Retrieved Context #{index} ---\n{item}"
            for index, item in enumerate(context, start=1)
            if item and item.strip()
        )

        if not context_text:
            context_text = "No relevant RAG context was retrieved."

        return f"""
You are Critique, an AI code-review assistant.

Your job is to analyze the supplied code or pull-request diff and identify
meaningful software-quality issues.

Use the retrieved code-review context as supporting knowledge.
Do not blindly repeat retrieved context.
Do not invent an issue merely because the context mentions one.

================ CODE / PR DIFF ================
{code}

================ RETRIEVED RAG CONTEXT ================
{context_text}

================ REQUIRED ANALYSIS ================
Provide your response using exactly these sections:

Issue Found:
Yes or No

Issue Type:
Bug / Security / Performance / Style / Refactoring / Readability / Other

Severity:
Easy / Hard / Uncertain

Explanation:
Explain what is wrong or why the code is acceptable.

Evidence:
Reference the relevant part of the supplied code.

Recommendation:
Suggest how the issue should be addressed, if an issue exists.
""".strip()

    def analyze_code(self, code: str, context: List[str]) -> str:
        prompt = self._build_prompt(code, context)

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise software code reviewer. "
                        "Use supplied retrieval context as evidence, "
                        "but make your own judgment from the code."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError("Groq returned an empty response.")

        return content.strip()