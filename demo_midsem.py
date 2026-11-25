"""
CRITIQUE — Autonomous AI Code Reviewer
MIDSEM PRESENTATION DEMONSTRATION SCRIPT

Demonstrates:
  1. Target Code Input (Vulnerabilities / Anti-patterns)
  2. RAG Knowledge Retrieval from ChromaDB
  3. Augmentation & Context Assembly
  4. Groq API — Easy/Hard Classification
  5. Fine-Tuned Qwen 2.5 Coder 3B LoRA Inference
     - If EASY: Qwen generates a code fix
     - If HARD: Qwen describes the issue in detail
  6. Windows Desktop Toast Alert Notification
"""

import os
os.environ["USE_TF"] = "0"
os.environ["USE_TORCH"] = "1"
import sys
import time
from rag.retrieve import retrieve_context
from rag.augment import augment_context
from llm.qwen_client import QwenLLM
from mcp_server.notifier import notify_user_if_issue, send_windows_toast
from mcp_server.orchestrator import classify_review_with_groq

DEMO_CASES = [
    {
        "id": 1,
        "name": "SQL Injection Vulnerability (Security - Critical)",
        "file_path": "services/user_service.py",
        "code": """def get_user_profile(user_id):
    # Fetch user data directly from database
    query = "SELECT * FROM users WHERE id = '" + user_id + "'"
    cursor = db.cursor()
    cursor.execute(query)
    return cursor.fetchone()"""
    },
    {
        "id": 2,
        "name": "Mutable Default Parameter Anti-Pattern (Bug / Style)",
        "file_path": "utils/event_logger.py",
        "code": """def log_event(event_name, metadata={}):
    metadata["timestamp"] = time.time()
    metadata["event"] = event_name
    events_list.append(metadata)
    return metadata"""
    },
    {
        "id": 3,
        "name": "Clean, Well-Formed Code (Baseline - No Issues Expected)",
        "file_path": "math/calculator.py",
        "code": """def calculate_discount(price: float, discount_percent: float) -> float:
    \"\"\"Calculate final price after applying percentage discount.\"\"\"
    if price < 0 or discount_percent < 0 or discount_percent > 100:
        raise ValueError("Price and discount must be valid non-negative values.")
    return round(price * (1 - discount_percent / 100), 2)"""
    }
]


def run_demo_case(case: dict, llm: QwenLLM):
    print("\n" + "=" * 75)
    print(f"  RUNNING TEST CASE #{case['id']}: {case['name']}")
    print(f"  Target File: {case['file_path']}")
    print("=" * 75)

    print("\n[STEP 1] Target Code:")
    print("-" * 50)
    print(case["code"])
    print("-" * 50)

    print("\n[STEP 2] Querying RAG Vector Store (ChromaDB)...")
    t0 = time.time()
    retrieved = retrieve_context(case["code"], k=3)
    t_rag = time.time() - t0
    print(f"--> Retrieved {len(retrieved)} relevant knowledge chunks in {t_rag:.2f}s:")
    for idx, snippet in enumerate(retrieved, start=1):
        first_line = snippet.strip().splitlines()[0] if snippet.strip() else ""
        print(f"    ({idx}) {first_line[:90]}...")

    print("\n[STEP 3] Generating Augmented Context Prompt...")
    augmented = augment_context(case["code"], retrieved)
    print("--> Context prompt assembled successfully.")

    # -----------------------------------------------------------------
    # STEP 4: Groq classifies the issue as Easy or Hard
    # -----------------------------------------------------------------
    print("\n[STEP 4] Groq API — Classifying issue (Easy / Hard)...")
    t_cls = time.time()
    classification, reason, source = classify_review_with_groq(case["code"], retrieved)
    t_cls = time.time() - t_cls
    badge = "✅ Easy" if classification == "Easy" else "⚠️  Hard"
    print(f"--> Classification: {badge}  |  Source: {source}  ({t_cls:.2f}s)")
    print(f"    Reason: {reason}")

    # -----------------------------------------------------------------
    # STEP 5: Local Qwen LLM — analyze code, then fix or describe
    # -----------------------------------------------------------------
    print("\n[STEP 5] Fine-Tuned Qwen 2.5 Coder 3B — Code Analysis...")
    t1 = time.time()
    review = llm.analyze_code(code=case["code"], context=retrieved)
    t_llm = time.time() - t1
    print(f"--> Inference complete in {t_llm:.2f}s.")

    print("\n[STEP 5a] Qwen Review Output:")
    print("=" * 60)
    print(review)
    print("=" * 60)

    if classification == "Easy":
        print("\n[STEP 5b] ✨ EASY ISSUE — Qwen generating code fix...")
        try:
            from review_schema import ReviewFinding
            from mcp_server.orchestrator import parse_review_response
            parsed = parse_review_response(review, [])
            if parsed.valid and parsed.findings:
                finding = parsed.findings[0]
                t_fix = time.time()
                fix_output = llm.generate_fix(case["code"], finding, retrieved)
                t_fix = time.time() - t_fix
                print(f"--> Fix generated in {t_fix:.2f}s:")
                print("-" * 60)
                print(fix_output)
                print("-" * 60)
            else:
                print("--> No actionable findings to fix (code may already be correct).")
        except Exception as exc:
            print(f"--> Fix generation failed: {exc}")

    else:
        print("\n[STEP 5b] 🔍 HARD ISSUE — Qwen describing issue in detail (no auto-fix)...")
        try:
            describe_fn = getattr(llm, "describe_issue", None)
            if callable(describe_fn):
                t_desc = time.time()
                description = describe_fn(case["code"], retrieved)
                t_desc = time.time() - t_desc
                print(f"--> Description generated in {t_desc:.2f}s:")
            else:
                description = review
            print("-" * 60)
            print(description)
            print("-" * 60)
        except Exception as exc:
            print(f"--> Issue description failed: {exc}")
            print("    (Using review output above as the issue description.)")

    print("\n[STEP 6] MCP Desktop Notification Trigger:")
    notified = notify_user_if_issue(review, file_path=case["file_path"])
    if notified:
        print("--> [ALERT DISPATCHED] Windows Desktop Toast notification displayed!")
    else:
        print("--> [NO ALERT] Code passed quality checks without critical findings.")


def main():
    print("=" * 75)
    print("       CRITIQUE — AUTONOMOUS AI CODE REVIEWER")
    print("           MIDSEM PRESENTATION DEMONSTRATION")
    print("=" * 75)
    print()
    print("Pipeline flow:")
    print("  1. RAG context retrieval (ChromaDB)")
    print("  2. Groq API  → Easy / Hard classification")
    print("  3. Local Qwen LLM → Code analysis")
    print("     Easy issue → Qwen generates a fix")
    print("     Hard issue → Qwen describes the issue (no auto-fix)")

    # Initial test toast to verify desktop notifications on presenter's screen
    print("\n[INIT] Testing Windows Desktop Toast notification subsystem...")
    send_windows_toast("Critique AI Initialized", "Autonomous Code Reviewer is online and ready.")
    print("--> Welcome toast sent to Windows Action Center / Desktop.")

    print("\n[INIT] Initializing Fine-Tuned Qwen 2.5 Coder 3B LoRA Model...")
    llm = QwenLLM()

    print("\nSelect an option:")
    print("  1. Run SQL Injection Vulnerability Test     [expects: Hard]")
    print("  2. Run Mutable Default Parameter Test       [expects: Hard or Easy]")
    print("  3. Run Clean Code Baseline Test             [expects: Easy]")
    print("  4. Run All 3 Tests Sequentially (Full Demo)")

    choice = input("\nEnter choice [1-4] (default=4): ").strip() or "4"

    if choice in ["1", "2", "3"]:
        idx = int(choice) - 1
        run_demo_case(DEMO_CASES[idx], llm)
    else:
        for case in DEMO_CASES:
            run_demo_case(case, llm)
            time.sleep(2)

    print("\n" + "=" * 75)
    print("  [SUCCESS] Midsem Presentation Demo Completed Successfully!")
    print("=" * 75)


if __name__ == "__main__":
    main()
