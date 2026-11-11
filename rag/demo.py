"""Standalone, read-only demonstration of Critique RAG v2 retrieval.

Demonstrates context retrieval across 10 distinct code patterns:
1. Mutable default argument anti-pattern
2. SQL injection via string concatenation
3. Bare exception handling (swallowing errors)
4. Resource leak / unclosed file descriptor
5. Unsafe dynamic eval() execution
6. Hardcoded secret / credentials
7. Unvalidated file path traversal
8. Unsafe subprocess execution (shell=True)
9. Insecure random number generation for crypto
10. Poor Python naming & convention / PEP 8 violation

Usage:
    python -m rag.demo
"""
from __future__ import annotations

import logging
from rag.retrieve import retrieve_context

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DEMO_SNIPPETS = [
    {
        "id": "1",
        "name": "Mutable Default Parameter Anti-Pattern",
        "code": """def process_items(item_id, item_list=[]):
    item_list.append(item_id)
    return item_list""",
    },
    {
        "id": "2",
        "name": "SQL Injection via String Concatenation",
        "code": """def find_user(username):
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    return db.execute(query)""",
    },
    {
        "id": "3",
        "name": "Bare Exception Handling Swallowing Errors",
        "code": """try:
    data = fetch_external_user_data(user_id)
    save_to_database(data)
except:
    pass""",
    },
    {
        "id": "4",
        "name": "Resource Leak (Unclosed File)",
        "code": """def load_config(path):
    f = open(path, 'r')
    cfg = json.load(f)
    return cfg""",
    },
    {
        "id": "5",
        "name": "Unsafe eval() Execution",
        "code": """def evaluate_math_expression(user_expr: str):
    return eval(user_expr)""",
    },
    {
        "id": "6",
        "name": "Hardcoded Credentials / Secret",
        "code": """API_SECRET_KEY = "sk-live-998877665544332211aabbcc"
def connect_service():
    return client.connect(token=API_SECRET_KEY)""",
    },
    {
        "id": "7",
        "name": "Unvalidated Path Traversal",
        "code": """def read_user_file(filename: str):
    path = os.path.join("/var/data", filename)
    with open(path, "r") as f:
        return f.read()""",
    },
    {
        "id": "8",
        "name": "Unsafe Subprocess Invocation with shell=True",
        "code": """import subprocess
def ping_host(host):
    return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)""",
    },
    {
        "id": "9",
        "name": "Insecure Random Number Generation for Security Token",
        "code": """import random
def generate_auth_token():
    return "".join(random.choice("0123456789abcdef") for _ in range(32))""",
    },
    {
        "id": "10",
        "name": "Python Naming & Style Violation",
        "code": """def Process_Data(ITEM_LIST):
    Result = []
    for I in ITEM_LIST:
        if I==None: continue
        Result.append(I*2)
    return Result""",
    },
]


def run_demo() -> None:
    print("=" * 75)
    print("   CRITIQUE RAG v2 MODULE - READ-ONLY RETRIEVAL DEMONSTRATION")
    print("=" * 75)
    print("Executing read-only context retrieval across 10 code patterns...\n")

    for snippet in DEMO_SNIPPETS:
        print("-" * 75)
        print(f"[{snippet['id']}/10] Pattern: {snippet['name']}")
        print("Input Code:")
        print(snippet["code"])
        print("\nRetrieving context from ChromaDB (max 4 results)...")

        contexts = retrieve_context(snippet["code"])

        if not contexts:
            print("[WARN] No context returned. Ensure database has been ingested: python -m rag.ingest")
        else:
            print(f"Retrieved {len(contexts)} relevant context chunk(s):")
            for idx, ctx in enumerate(contexts, start=1):
                preview = "\n".join("  | " + line for line in ctx.strip().splitlines()[:10])
                if len(ctx.strip().splitlines()) > 10:
                    preview += "\n  | ... [truncated for display]"
                print(f"\n[Result #{idx}]\n{preview}")
        print()

    print("=" * 75)
    print("Demo complete. All retrievals executed with zero database mutations.")
    print("=" * 75)


if __name__ == "__main__":
    run_demo()
