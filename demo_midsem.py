"""
CRITIQUE — Autonomous AI Code Reviewer
MIDSEM PRESENTATION DEMONSTRATION SCRIPT

Demonstrates:
  1. Target Code Input (Vulnerabilities / Anti-patterns)
  2. RAG Knowledge Retrieval from ChromaDB
  3. Augmentation & Context Assembly
  4. Fine-Tuned Qwen 2.5 Coder 3B LoRA Inference
  5. Windows Desktop Toast Alert Notification
  6. MCP Tool Invocation
"""

import sys
import time
from rag.retrieve import retrieve_context
from rag.augment import augment_context
from llm.qwen_client import QwenLLM
from mcp_server.notifier import notify_user_if_issue, send_windows_toast

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

    print("\n[STEP 4] Running Fine-Tuned Qwen 2.5 Coder 3B Model Inference...")
    t1 = time.time()
    review = llm.analyze_code(code=case["code"], context=retrieved)
    t_llm = time.time() - t1
    print(f"--> Inference complete in {t_llm:.2f}s.")

    print("\n[STEP 5] Model Review Output:")
    print("=" * 60)
    print(review)
    print("=" * 60)

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

    # Initial test toast to verify desktop notifications on presenter's screen
    print("\n[INIT] Testing Windows Desktop Toast notification subsystem...")
    send_windows_toast("Critique AI Initialized", "Autonomous Code Reviewer is online and ready.")
    print("--> Welcome toast sent to Windows Action Center / Desktop.")

    print("\n[INIT] Initializing Fine-Tuned Qwen 2.5 Coder 3B LoRA Model...")
    llm = QwenLLM()

    print("\nSelect an option:")
    print("  1. Run SQL Injection Vulnerability Test")
    print("  2. Run Mutable Default Parameter Test")
    print("  3. Run Clean Code Baseline Test")
    print("  4. Run All 3 Tests Sequentially (Full Demo)")

    choice = input("\nEnter choice [1-4] (default=1): ").strip() or "1"

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
