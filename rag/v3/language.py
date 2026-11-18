"""Conservative deterministic language identification for v3 source records."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageResult:
    language: str
    origin: str
    confidence: float
    evidence: str | None = None


_ALIASES = {
    "py": "python", "python": "python", "pyi": "python",
    "java": "java", "js": "javascript", "jsx": "javascript",
    "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "tsx": "typescript",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "h": "cpp",
    "c": "c", "cs": "csharp", "csharp": "csharp", "go": "go",
    "rb": "ruby", "ruby": "ruby", "php": "php", "rs": "rust",
}

_EXTENSION_CONFIDENCE = 0.90
_SYNTAX_CONFIDENCE = 0.70


def classify_language(
    text: str,
    *,
    explicit_language: str | None = None,
    file_path: str | None = None,
) -> LanguageResult:
    """Classify only on direct evidence; confidence is ordinal, not probability.

    Scores are evidence levels (explicit=1.0, extension=.9, syntax=.7, unknown=0),
    not calibrated probabilities. Conflicting high-confidence syntax remains unknown.
    """
    if explicit_language:
        language = _ALIASES.get(explicit_language.strip().lower().lstrip("."))
        if language:
            return LanguageResult(language, "upstream_explicit", 1.0, "source language field")
    if file_path:
        suffix = file_path.rsplit("/", 1)[-1].rsplit(".", 1)
        if len(suffix) == 2:
            language = _ALIASES.get(suffix[-1].lower())
            if language:
                return LanguageResult(language, "file_extension", _EXTENSION_CONFIDENCE, suffix[-1])

    sample = text or ""
    signature_patterns: list[tuple[str, re.Pattern[str], str]] = [
        ("java", re.compile(r"(?m)^\s*(?:package\s+[\w.]+\s*;|import\s+java\.[\w.*]+\s*;)|@Override\b"), "Java declaration/import/annotation"),
        ("csharp", re.compile(r"(?m)^\s*using\s+System(?:\.[\w.]+)?\s*;|\b\w+\s*\{\s*get\s*;\s*set\s*;"), "C# namespace or property signature"),
        ("cpp", re.compile(r"#\s*include\s*[<\"]|\bstd::|\btemplate\s*<|\bnullptr\b|\bKRATOS_[A-Z_]+\b"), "C/C++ syntax"),
        ("go", re.compile(r"(?m)^\s*package\s+[A-Za-z_]\w*\s*$|\bfunc\s*\([^)]*\)\s*[A-Za-z_]\w*\s*\(|\b:=|<-"), "Go package/receiver/short declaration"),
        ("ruby", re.compile(r"\battr_accessor\b|\brequire\s+['\"]|\b(?:do\s*\|[^|]*\||def\s+[A-Za-z_]\w*[!?=])|@[A-Za-z_]\w*\s*=|\belsif\b"), "Ruby/Rails syntax"),
        ("php", re.compile(r"<\?php|\$this\s*->|\bnamespace\s+[A-Za-z_\\]+\s*;"), "PHP syntax"),
        ("typescript", re.compile(r"(?m)^\s*(?:interface|type)\s+[A-Za-z_]\w*\s*(?:=|\{)|:\s*(?:string|number|boolean|Promise<)"), "TypeScript type declaration"),
        ("javascript", re.compile(r"\bmodule\.exports\b|\b(?:const|let)\s+[A-Za-z_$][\w$]*\s*=|\bexport\s+(?:default|function|class)\b|\bfunction\s+[A-Za-z_$][\w$]*\s*\("), "JavaScript declaration"),
        ("python", re.compile(r"(?m)^\s*(?:async\s+)?def\s+[A-Za-z_]\w*\s*\([^\n]*\)\s*(?:->\s*[^:]+)?\s*:|^\s*from\s+[\w.]+\s+import\s+|^\s*if\s+__name__\s*==\s*['\"]__main__['\"]\s*:|^\s*class\s+[A-Za-z_]\w*(?:\([^\n]*\))?\s*:"), "Python declaration/import"),
    ]
    matches = [(name, evidence) for name, pattern, evidence in signature_patterns if pattern.search(sample)]
    distinct = {name for name, _ in matches}
    if len(distinct) == 1:
        language = next(iter(distinct))
        return LanguageResult(language, "syntax_heuristic", _SYNTAX_CONFIDENCE,
                              next(evidence for name, evidence in matches if name == language))
    return LanguageResult("unknown", "unknown", 0.0,
                          "ambiguous or insufficient language-specific syntax" if matches else None)
