from abc import ABC, abstractmethod
from typing import List

from review_schema import ReviewFinding


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

    def generate_fix(
        self, code: str, finding: ReviewFinding, context: List[str]
    ) -> str:
        """Optionally generate a structured fix candidate for one validated finding."""
        raise NotImplementedError("This LLM provider does not support fix generation.")

    def describe_issue(self, code: str, context: List[str]) -> str:
        """Describe a hard/complex issue in detail without providing a fix.

        Default implementation falls back to analyze_code.
        Providers may override this to give a more detailed explanation
        tailored for hard issues that require manual intervention.
        """
        return self.analyze_code(code, context)
