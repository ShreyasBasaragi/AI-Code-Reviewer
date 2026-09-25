import os
import sys
import logging
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from rag.retrieve import retrieve_context, retrieve_context_for_repository
from mcp_server.notifier import notify_user_if_issue, parse_review_verdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("critique-mcp")

# Initialize FastMCP Server
mcp = FastMCP(
    name="Critique AI Code Reviewer",
    instructions="Autonomous AI Code Review Server with RAG Knowledge Retrieval and Desktop Alerts"
)

# Global LLM instance (lazy initialized)
_llm_instance = None


def get_llm():
    """Lazy initialize the fine-tuned Qwen LLM instance."""
    global _llm_instance
    if _llm_instance is None:
        try:
            from llm.qwen_client import QwenLLM
            logger.info("Initializing fine-tuned Qwen 2.5 Coder LLM...")
            _llm_instance = QwenLLM()
        except Exception as e:
            logger.warning(f"Could not load local Qwen model: {e}. Falling back to GroqLLM if configured.")
            try:
                from llm.groq_client import GroqLLM
                _llm_instance = GroqLLM()
            except Exception as ge:
                raise RuntimeError(f"Failed to load any LLM backend (Qwen: {e}, Groq: {ge})") from e
    return _llm_instance


@mcp.tool()
def review_code(code: str, file_path: str = "", repository: str = "") -> str:
    """
    Review a code snippet or function using the RAG knowledge base and fine-tuned Qwen model.
    Pops up a Windows desktop notification alert if an issue is detected.

    Args:
        code: Source code string to review.
        file_path: Optional file path or module name for context.
        repository: Optional repository name (e.g. 'psf/requests') for tailored RAG retrieval.

    Returns:
        Structured code review findings including severity, issue type, evidence, and fix.
    """
    if not code or not code.strip():
        return "Error: Code snippet is empty."

    logger.info(f"Received review request for: {file_path or 'unnamed snippet'}")

    # 1. Retrieve RAG context from ChromaDB
    repo_param = repository.strip() if repository else None
    context = retrieve_context_for_repository(code=code, repository=repo_param, k=5)
    logger.info(f"Retrieved {len(context)} RAG context snippets.")

    # 2. Run LLM Inference
    llm = get_llm()
    review = llm.analyze_code(code=code, context=context)

    # 3. Trigger Desktop Toast Alert if an issue was found
    notify_user_if_issue(review, file_path=file_path)

    return review


@mcp.tool()
def review_file(file_path: str) -> str:
    """
    Read a local source code file from disk, review it, and notify the developer of issues.

    Args:
        file_path: Absolute or relative path to the local file to review.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found at path '{file_path}'"

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"Error reading file '{file_path}': {e}"

    return review_code(code=content, file_path=str(path.name))


@mcp.tool()
def review_diff(diff: str, repository: str = "") -> str:
    """
    Review a git patch or pull request diff using RAG historical reviews and fine-tuned Qwen.
    Pops up a desktop alert if issues are identified.

    Args:
        diff: The git unified diff string.
        repository: Optional repository name for historical context matching.
    """
    return review_code(code=diff, file_path="PR Diff", repository=repository)


@mcp.resource("critique://status")
def get_system_status() -> str:
    """Check Critique server status, active model, and RAG vector store breakdown."""
    from rag.ingest_github import get_chromadb_source_breakdown
    breakdown = get_chromadb_source_breakdown()

    status_lines = [
        "Critique Autonomous AI Code Reviewer Status:",
        f"Active Model: Qwen 2.5 Coder 3B (LoRA Adapter)",
        f"Desktop Notifier: Windows Native Toast (Active)",
        "ChromaDB Indexed Collections:"
    ]
    if breakdown:
        for src, count in breakdown.items():
            status_lines.append(f"  - {src}: {count} records")
    else:
        status_lines.append("  (ChromaDB is empty or uninitialized)")

    return "\n".join(status_lines)


if __name__ == "__main__":
    # Run standalone stdio server for MCP clients (Cursor, Claude Desktop, Antigravity)
    mcp.run()
