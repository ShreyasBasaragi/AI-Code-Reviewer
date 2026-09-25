"""
Test: RAG + Fine-Tuned Qwen 2.5 Coder 3B Connection
Runs: retrieve_context() -> augment_context() -> QwenLLM.analyze_code()
"""
import os
os.environ["USE_TF"] = "0"
os.environ["USE_TORCH"] = "1"
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SAMPLE_CODE = """def get_user(user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return db.execute(query)
"""

def main():
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 60)
    print("  CRITIQUE: RAG + Qwen LLM Connection Test")
    print("=" * 60)

    # ── Step 1: RAG Retrieval ─────────────────────────────────
    print("\n[1] Importing RAG retrieve module...")
    from rag.retrieve import retrieve_context
    print("    OK: rag.retrieve imported.")

    print("\n[2] Running retrieve_context() on sample code...")
    context = retrieve_context(SAMPLE_CODE, k=3)
    print(f"    OK: Retrieved {len(context)} context snippets from ChromaDB.")
    for i, snip in enumerate(context, 1):
        print(f"    [{i}] {snip.splitlines()[0][:80]}...")

    # ── Step 2: Augmentation ─────────────────────────────────
    print("\n[3] Importing RAG augment module...")
    from rag.augment import augment_context
    print("    OK: rag.augment imported.")

    augmented = augment_context(SAMPLE_CODE, context)
    print(f"    OK: Augmented prompt built ({len(augmented)} chars).")

    # ── Step 3: Load Qwen LLM ────────────────────────────────
    print("\n[4] Loading fine-tuned QwenLLM (this may take ~30s on first run)...")
    from llm.qwen_client import QwenLLM
    llm = QwenLLM()
    print("    OK: QwenLLM loaded successfully on", llm.device)

    # ── Step 4: Inference ────────────────────────────────────
    print("\n[5] Running analyze_code() inference...")
    print("    Input Code:")
    print("   ", SAMPLE_CODE.strip())
    print()

    review = llm.analyze_code(code=SAMPLE_CODE, context=context)

    print("\n" + "=" * 60)
    print("  MODEL OUTPUT")
    print("=" * 60)
    print(review)
    print("=" * 60)
    print("\n[SUCCESS] RAG + Qwen LLM pipeline is fully connected!")

if __name__ == "__main__":
    main()
