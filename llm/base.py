from abc import ABC, abstractmethod
from typing import List


class BaseLLM(ABC):
    """
    Provider-independent interface for Critique's LLM.

    The RAG layer supplies:
        code: source code / PR diff
        context: retrieved code-review knowledge

    The LLM returns:
        natural-language review analysis
    """

    @abstractmethod
    def analyze_code(self, code: str, context: List[str]) -> str:
        """Analyze code using retrieved RAG context."""
        raise NotImplementedError